"""关联导入 → 业务资源表（herbs / prescriptions / theories / literatures）。

关联导入原先只写通用实体 / 关系 / 图谱，四张业务表始终为空，前端
「中药 / 方剂 / 中医理论 / 文献」看不到任何导入数据。本服务按
dataset_scanner 的字段规则（类型判定的唯一来源）识别每张表，把可识别的
资源行同时写入对应业务表，并处理两类关联表：

- 属性表（CHP_ID + Medicinal_properties + Class）：按来源标识回溯 herbs，
  补全 properties / channels（只填空字段，不覆盖已有值）；
- 组成表（CPM_ID + CHP_ID + Dosage_ratio）：解析方剂与中药后写入
  prescription_ingredients，用量取原始比例并将 unit 标注为「比例」。

约束：
- 只写语义明确的字段；数据集缺失的内容留空，**不伪造**；
- 幂等：业务表按 name 唯一约束 ON CONFLICT DO NOTHING，重复导入不加行；
- 不挂载知识库、不做逐条向量化（BGE-M3 CPU 下十万级资源不可行），
  四类前端模块读取的正是这四张全局业务表。
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any, Awaitable, Callable, Sequence

from loguru import logger
from sqlalchemy import bindparam, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from src.application import dataset_scanner
from src.application.import_service import _EMPTY_TOKENS, _build_values
from src.domain.models import (
    Herb,
    Literature,
    Prescription,
    PrescriptionIngredient,
    Theory,
)

_BATCH_SIZE = 500

# 资源类型 → 业务表模型（与 dataset_scanner 的 detected_type 对齐）
_RESOURCE_MODELS = {
    "herb": Herb,
    "prescription": Prescription,
    "theory": Theory,
    "literature": Literature,
}

# D7 属性表：Class 决定落点（原始值为英文，系统不翻译）
_CLASS_TO_TARGET = {
    "medicinal flavor": "flavor",
    "therapeutic nature": "nature",
    "meridian tropism": "channel",
}

_STAT_FIELDS = {
    "herb": "herbs",
    "prescription": "prescriptions",
    "theory": "theories",
    "literature": "literatures",
}

_TARGET_LABELS = {
    "herb": "中药",
    "prescription": "方剂",
    "theory": "中医理论",
    "literature": "文献",
    "properties": "中药属性补全",
    "composition": "方剂组成",
}


@dataclass
class BusinessError:
    """单张表写入失败（不阻断其它表）。"""

    table: str
    message: str


@dataclass
class BusinessStats:
    """业务表写入统计。"""

    herbs: int = 0
    prescriptions: int = 0
    theories: int = 0
    literatures: int = 0
    ingredients: int = 0
    existing: int = 0
    enriched: int = 0
    errors: list[BusinessError] = field(default_factory=list)


def classify_tables(rows_by_table: dict[str, Sequence[Any]]) -> dict[str, str]:
    """表名 → 业务写入类别；无法识别的表返回空字符串（只保留通用导入结果）。"""
    kinds: dict[str, str] = {}
    for table, rows in rows_by_table.items():
        if not rows:
            continue
        fields = list(rows[0].data.keys())
        detected, _reason, _mappings, _pending = dataset_scanner.detect_dataset(fields)
        if detected in _RESOURCE_MODELS:
            kinds[table] = detected
        elif {"CHP_ID", "Medicinal_properties", "Class"} <= set(fields):
            kinds[table] = "properties"
        elif {"CPM_ID", "CHP_ID", "Dosage_ratio"} <= set(fields):
            kinds[table] = "composition"
        else:
            kinds[table] = ""
    return kinds


class BusinessResourceService:
    """把关联导入的行写入四张业务表（按 kb 无关的全局资源表，name 幂等）。"""

    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def write_tables(
        self,
        *,
        rows_by_table: dict[str, Sequence[Any]],
        files_by_table: dict[str, Any],
        batch_id: str,
        on_table: Callable[[int, int, str], Awaitable[None]] | None = None,
    ) -> BusinessStats:
        """写入全部可识别的业务表；每张表独立 SAVEPOINT，失败只丢该表。"""
        stats = BusinessStats()
        kinds = classify_tables(rows_by_table)
        resource_tables = [(t, k) for t, k in kinds.items() if k in _RESOURCE_MODELS]
        relation_tables = [(t, k) for t, k in kinds.items() if k in ("properties", "composition")]
        units = resource_tables + relation_tables
        herb_tables = [t for t, k in resource_tables if k == "herb"]
        prescription_tables = [t for t, k in resource_tables if k == "prescription"]

        for index, (table, kind) in enumerate(units, start=1):
            rows = rows_by_table.get(table) or []
            file = files_by_table.get(table)
            file_name = getattr(file, "file_name", table) or table
            try:
                async with self.db.begin_nested():
                    if kind in _RESOURCE_MODELS:
                        created, existing = await self._write_resource(
                            kind=kind,
                            table=table,
                            rows=rows,
                            file_name=file_name,
                            batch_id=batch_id,
                        )
                        field_name = _STAT_FIELDS[kind]
                        setattr(stats, field_name, getattr(stats, field_name) + created)
                        stats.existing += existing
                    elif kind == "properties":
                        stats.enriched += await self._enrich_properties(
                            rows=rows, herb_tables=herb_tables
                        )
                    else:
                        stats.ingredients += await self._write_ingredients(
                            rows=rows,
                            prescription_tables=prescription_tables,
                            herb_tables=herb_tables,
                        )
            except Exception as exc:  # noqa: BLE001 - 单表失败不影响其它表
                stats.errors.append(BusinessError(table, f"{_TARGET_LABELS[kind]}写入失败：{exc}"))
                logger.warning(f"[business_resource] {table}（{kind}）写入失败，已回滚该表: {exc}")
            if on_table is not None:
                await on_table(index, len(units), f"{table}（{_TARGET_LABELS[kind]}）")

        return stats

    # ── 资源表 ──────────────────────────────────────────────────────────────

    async def _write_resource(
        self,
        *,
        kind: str,
        table: str,
        rows: Sequence[Any],
        file_name: str,
        batch_id: str,
    ) -> tuple[int, int]:
        """按 name 幂等写入一张资源表，返回 (新增, 已存在)。"""
        model = _RESOURCE_MODELS[kind]
        values_list: list[dict[str, Any]] = []
        seen: set[str] = set()
        for row in rows:
            values = _build_values(
                kind,
                dict(row.data),
                dataset_name=table,
                source_file=file_name,
            )
            name = (values.get("name") or "").strip()[:128]
            if not name or name in _EMPTY_TOKENS or name in seen:
                continue
            seen.add(name)
            values_list.append(_resource_values(kind, name, values, table, batch_id))

        created = existing = 0
        for start in range(0, len(values_list), _BATCH_SIZE):
            batch = values_list[start : start + _BATCH_SIZE]
            names = [item["name"] for item in batch]
            found = set(
                (await self.db.execute(select(model.name).where(model.name.in_(names))))
                .scalars()
                .all()
            )
            existing += len(found)
            await self.db.execute(
                pg_insert(model.__table__)
                .values(batch)
                .on_conflict_do_nothing(index_elements=[model.__table__.c.name])
            )
            created += len(batch) - len(found)
        return created, existing

    # ── D7 属性表：补全中药性味 / 归经 ──────────────────────────────────────

    async def _enrich_properties(self, *, rows: Sequence[Any], herb_tables: list[str]) -> int:
        grouped: dict[str, dict[str, list[str]]] = defaultdict(
            lambda: {"flavor": [], "nature": [], "channel": []}
        )
        for row in rows:
            chp = str(row.data.get("CHP_ID") or "").strip()
            target = _CLASS_TO_TARGET.get(str(row.data.get("Class") or "").strip().lower())
            value = str(row.data.get("Medicinal_properties") or "").strip()
            if not chp or target is None or not value:
                continue
            bucket = grouped[chp][target]
            if value[:32] not in bucket:
                bucket.append(value[:32])
        if not grouped:
            return 0

        prefixes = await self._source_prefixes(Herb, herb_tables)
        if not prefixes:
            return 0
        herbs = await self._herbs_by_source(list(grouped), prefixes)
        params: list[dict[str, Any]] = []
        for chp, groups in grouped.items():
            for prefix in prefixes:
                herb = herbs.get(f"{prefix}:{chp}")
                if herb is None:
                    continue
                properties, channels = _merge_properties(herb, groups)
                if properties == (herb.properties or "").strip() and channels == list(
                    herb.channels or []
                ):
                    continue
                params.append(
                    {"_id": herb.id, "properties": properties, "channels": channels}
                )

        if not params:
            return 0
        stmt = (
            update(Herb.__table__)
            .where(Herb.__table__.c.id == bindparam("_id"))
            .values(properties=bindparam("properties"), channels=bindparam("channels"))
        )
        for start in range(0, len(params), _BATCH_SIZE):
            # Core 逐行参数化 UPDATE：一处方剂可能有上千味药，逐条往返太慢
            await self.db.execute(stmt, params[start : start + _BATCH_SIZE])
        return len(params)

    async def _source_prefixes(self, model: Any, explicit: Sequence[str]) -> set[str]:
        """可用的溯源前缀：本次导入识别出的表名 + 库中已有资源的 source_dataset。"""
        result = await self.db.execute(
            select(model.source_dataset).where(model.source_dataset.is_not(None)).distinct()
        )
        return {name for name in [*explicit, *result.scalars().all()] if name}

    async def _herbs_by_source(
        self, chp_ids: Sequence[str], prefixes: Sequence[str]
    ) -> dict[str, Herb]:
        sources = [f"{prefix}:{chp}" for prefix in prefixes for chp in chp_ids]
        herbs: dict[str, Herb] = {}
        for start in range(0, len(sources), _BATCH_SIZE):
            chunk = sources[start : start + _BATCH_SIZE]
            result = await self.db.execute(select(Herb).where(Herb.source.in_(chunk)))
            for herb in result.scalars().all():
                herbs[herb.source] = herb
        return herbs

    # ── D4 组成表：方剂 ↔ 中药 ──────────────────────────────────────────────

    async def _write_ingredients(
        self,
        *,
        rows: Sequence[Any],
        prescription_tables: list[str],
        herb_tables: list[str],
    ) -> int:
        if not rows:
            return 0
        pairs = [
            (
                str(row.data.get("CPM_ID") or "").strip(),
                str(row.data.get("CHP_ID") or "").strip(),
                str(row.data.get("Dosage_ratio") or "").strip(),
            )
            for row in rows
        ]
        cpm_ids = sorted({cpm for cpm, _chp, _ratio in pairs if cpm})
        chp_ids = sorted({chp for _cpm, chp, _ratio in pairs if chp})
        prescription_prefixes = await self._source_prefixes(Prescription, prescription_tables)
        herb_prefixes = await self._source_prefixes(Herb, herb_tables)
        if not prescription_prefixes or not herb_prefixes:
            return 0
        prescriptions = await self._ids_by_source(Prescription, cpm_ids, prescription_prefixes)
        herbs = await self._ids_by_source(Herb, chp_ids, herb_prefixes)
        if not prescriptions or not herbs:
            return 0

        values: list[dict[str, Any]] = []
        seen: set[tuple[uuid.UUID, uuid.UUID]] = set()
        order: dict[uuid.UUID, int] = defaultdict(int)
        for cpm, chp, raw_ratio in pairs:
            for prescription_table in prescription_prefixes:
                prescription_id = prescriptions.get(f"{prescription_table}:{cpm}")
                if prescription_id is None:
                    continue
                for herb_table in herb_prefixes:
                    herb_id = herbs.get(f"{herb_table}:{chp}")
                    if herb_id is None:
                        continue
                    key = (prescription_id, herb_id)
                    if key in seen:
                        continue
                    seen.add(key)
                    values.append(
                        {
                            "id": uuid.uuid4(),
                            "prescription_id": prescription_id,
                            "herb_id": herb_id,
                            "amount": _parse_ratio(raw_ratio),
                            # 原始字段是「占比」而非克/两，注明单位，避免被当作剂量
                            "unit": "比例" if raw_ratio else "",
                            "processing": "",
                            "role": "",
                            "sort_order": order[prescription_id],
                        }
                    )
                    order[prescription_id] += 1

        created = 0
        for start in range(0, len(values), _BATCH_SIZE):
            batch = values[start : start + _BATCH_SIZE]
            result = await self.db.execute(
                pg_insert(PrescriptionIngredient.__table__)
                .values(batch)
                .on_conflict_do_nothing(
                    constraint="uq_prescription_ingredients_prescription_herb"
                )
                .returning(PrescriptionIngredient.__table__.c.id)
            )
            created += len(result.all())
        return created

    async def _ids_by_source(
        self, model: Any, ids: Sequence[str], prefixes: Sequence[str]
    ) -> dict[str, uuid.UUID]:
        sources = [f"{prefix}:{value}" for prefix in prefixes for value in ids]
        mapping: dict[str, uuid.UUID] = {}
        for start in range(0, len(sources), _BATCH_SIZE):
            chunk = sources[start : start + _BATCH_SIZE]
            result = await self.db.execute(
                select(model.id, model.source).where(model.source.in_(chunk))
            )
            for row in result.all():
                mapping[row.source] = row.id
        return mapping


def _resource_values(
    kind: str,
    name: str,
    values: dict[str, Any],
    table: str,
    batch_id: str,
) -> dict[str, Any]:
    row: dict[str, Any] = {
        "name": name,
        "aliases": values.get("aliases") or [],
        "source": (values.get("source") or "")[:255],
        "source_dataset": table[:128],
        "import_batch_id": (batch_id or "")[:64] or None,
    }
    if kind == "herb":
        row.update(
            properties=(values.get("properties") or "")[:255],
            channels=values.get("channels") or [],
            effects=values.get("effects") or "",
            description=values.get("description") or "",
        )
    elif kind == "prescription":
        row["usage_method"] = (values.get("usage_method") or "")[:255]
    elif kind == "theory":
        row["content"] = values.get("content") or ""
    elif kind == "literature":
        row.update(
            author=(values.get("author") or "")[:255],
            dynasty=(values.get("dynasty") or "")[:32],
            content=values.get("content") or "",
        )
    return row


def _merge_properties(herb: Herb, groups: dict[str, list[str]]) -> tuple[str, list[str]]:
    """只填空字段：已有性味/归经不覆盖，避免重复导入时反复覆盖人工整理内容。"""
    properties = (herb.properties or "").strip()
    if not properties:
        parts = []
        if groups["flavor"]:
            parts.append("性味：" + "、".join(groups["flavor"]))
        if groups["nature"]:
            parts.append("四气：" + "、".join(groups["nature"]))
        properties = "；".join(parts)[:255]
    channels = list(herb.channels or [])
    if not channels and groups["channel"]:
        channels = groups["channel"][:32]
    return properties, channels


def _parse_ratio(raw: str) -> Decimal | None:
    """原始占比 → Numeric(10,2)；非数值不猜测（留 NULL）。"""
    if not raw:
        return None
    try:
        return Decimal(raw)
    except (InvalidOperation, ValueError):
        return None
