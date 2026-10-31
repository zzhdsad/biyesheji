"""导入映射模板：保存、识别、复用与版本兼容。

为什么需要它：
同一份数据集（如 D1～D7）会被反复导入，每次都重新选主键、定实体类型、勾关系
既慢又容易前后不一致（这一次叫「含术语」，下一次叫「涉及」）。模板把这套决策
固化下来，按 Schema 指纹自动匹配，下次导入直接复用。

关键设计：
1. **指纹 = 数据集级别的确定性哈希**：由「表名 + 该表字段名集合」排序后计算，
   与列顺序、文件名、导入顺序无关；新增/删除列会改变指纹，从而不会误用旧模板。
2. **版本兼容**：模板 config 带 version 字段。旧版本按迁移链逐级升级后再使用，
   更高版本（未来版本）直接拒绝并给出明确原因——不能猜，否则会静默错配。
3. **复用前做字段差异检查**：即使指纹完全一致（同一批文件的重复导入），
   也返回 exact/compatible 状态；指纹不匹配时给出缺失/新增字段清单，
   由调用方决定是套用还是重新分析。
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping

from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.application.schema_analyzer import schema_fingerprint
from src.domain.models import ImportMappingTemplate, User

# 当前模板配置结构版本
CURRENT_CONFIG_VERSION = 2
# 支持迁移的最低版本（低于此版本无法安全推断语义，只能让用户重建）
MIN_SUPPORTED_VERSION = 1


class TemplateError(Exception):
    """模板不可用（版本不兼容 / 结构非法）。"""


@dataclass
class TableMapping:
    """单表的映射决策。"""

    table: str
    entity_type: str
    pk_field: str | None = None
    display_field: str | None = None


@dataclass
class RelationMapping:
    """一条关系映射。"""

    child_table: str
    child_field: str
    parent_table: str
    parent_field: str
    relation_type: str = "related_to"


@dataclass
class JunctionMapping:
    table: str
    left_table: str
    left_field: str
    right_table: str
    right_field: str
    relation_type: str = "related_to"


@dataclass
class TemplateConfig:
    """模板内容（可序列化为 JSONB）。"""

    version: int = CURRENT_CONFIG_VERSION
    tables: dict[str, TableMapping] = field(default_factory=dict)
    relations: list[RelationMapping] = field(default_factory=list)
    junctions: list[JunctionMapping] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "tables": {
                name: {
                    "table": m.table,
                    "entity_type": m.entity_type,
                    "pk_field": m.pk_field,
                    "display_field": m.display_field,
                }
                for name, m in self.tables.items()
            },
            "relations": [vars(r) for r in self.relations],
            "junctions": [vars(j) for j in self.junctions],
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> TemplateConfig:
        if not isinstance(raw, dict):
            raise TemplateError("模板配置格式非法：应为对象")
        version = int(raw.get("version", 1))
        raw = _migrate(raw, version)
        tables: dict[str, TableMapping] = {}
        for name, item in (raw.get("tables") or {}).items():
            if not isinstance(item, dict):
                raise TemplateError(f"表 {name} 的映射格式非法")
            tables[str(name)] = TableMapping(
                table=str(item.get("table") or name),
                entity_type=str(item.get("entity_type") or name),
                pk_field=item.get("pk_field"),
                display_field=item.get("display_field"),
            )
        return cls(
            version=CURRENT_CONFIG_VERSION,
            tables=tables,
            relations=[
                RelationMapping(
                    child_table=str(r["child_table"]),
                    child_field=str(r["child_field"]),
                    parent_table=str(r["parent_table"]),
                    parent_field=str(r["parent_field"]),
                    relation_type=str(r.get("relation_type") or "related_to"),
                )
                for r in (raw.get("relations") or [])
                if isinstance(r, dict) and {"child_table", "child_field", "parent_table", "parent_field"} <= r.keys()
            ],
            junctions=[
                JunctionMapping(
                    table=str(j["table"]),
                    left_table=str(j["left_table"]),
                    left_field=str(j["left_field"]),
                    right_table=str(j["right_table"]),
                    right_field=str(j["right_field"]),
                    relation_type=str(j.get("relation_type") or "related_to"),
                )
                for j in (raw.get("junctions") or [])
                if isinstance(j, dict) and {"table", "left_table", "left_field", "right_table", "right_field"} <= j.keys()
            ],
        )


def dataset_fingerprint(tables: Mapping[str, Iterable[str]]) -> str:
    """数据集级指纹：由「表名 + 字段名集合」确定，与列顺序/文件名无关。

    注意必须按表名排序后再拼接，否则同一批文件在不同导入顺序下会算出不同指纹，
    模板就永远匹配不上。
    """
    parts = []
    for table in sorted(tables):
        parts.append(f"{table}\x1e{schema_fingerprint(tables[table])}")
    return hashlib.sha256("\x1d".join(parts).encode("utf-8")).hexdigest()


def _migrate(raw: dict[str, Any], version: int) -> dict[str, Any]:
    """按版本链迁移到当前结构。

    只做**语义不变**的结构调整；无法安全推断的一律抛错，不猜。
    """
    if version > CURRENT_CONFIG_VERSION:
        raise TemplateError(
            f"模板版本 {version} 高于当前支持的 {CURRENT_CONFIG_VERSION}，"
            "请先升级服务端再使用（拒绝按猜测解释未知版本）"
        )
    if version < MIN_SUPPORTED_VERSION:
        raise TemplateError(f"模板版本 {version} 过低且不支持迁移，请重建模板")

    if version == 1:
        # v1 → v2：v1 的关系只有 (子表, 子字段, 父表, 父字段)，没有 relation_type；
        # 补默认值 related_to，语义不变。
        for key in ("relations", "junctions"):
            items = raw.get(key) or []
            fixed = []
            for item in items:
                if isinstance(item, dict):
                    item.setdefault("relation_type", "related_to")
                    fixed.append(item)
            raw[key] = fixed
        version = 2

    raw["version"] = CURRENT_CONFIG_VERSION
    return raw


def _field_diff(
    config: TemplateConfig, tables: Mapping[str, Iterable[str]]
) -> tuple[dict[str, list[str]], dict[str, list[str]]]:
    """比对模板与当前数据：(缺失, 未覆盖)。

    只校验模板**真正依赖**的字段（主键 + 关系字段），
    不要求字段集合完全一致——数据集新增列是常态，不该因此判定不可用。
    """
    current = {
        str(table): {str(f).strip().lower() for f in fields if f}
        for table, fields in tables.items()
    }
    missing: dict[str, list[str]] = {}
    covered: set[str] = set()

    def _need(table: str, field: str | None) -> None:
        if not field:
            return
        fields = current.get(table)
        if fields is None:
            missing.setdefault(table, [])
            if "<整张表缺失>" not in missing[table]:
                missing[table].append("<整张表缺失>")
            return
        if str(field).strip().lower() not in fields:
            missing.setdefault(table, []).append(str(field))

    for table, mapping in config.tables.items():
        covered.add(table)
        _need(table, mapping.pk_field)
    for relation in config.relations:
        covered.add(relation.child_table)
        covered.add(relation.parent_table)
        _need(relation.child_table, relation.child_field)
        _need(relation.parent_table, relation.parent_field)
    for junction in config.junctions:
        covered.add(junction.table)
        _need(junction.table, junction.left_field)
        _need(junction.table, junction.right_field)

    extra = {table: sorted(fields) for table, fields in current.items() if table not in covered}
    return missing, extra


@dataclass
class MatchResult:
    """模板与当前数据集的匹配结果。"""

    template: ImportMappingTemplate | None = None
    fingerprint: str = ""
    status: str = "none"  # none | exact | compatible | mismatch | incompatible
    missing_fields: dict[str, list[str]] = field(default_factory=dict)
    extra_fields: dict[str, list[str]] = field(default_factory=dict)
    message: str = ""

    @property
    def usable(self) -> bool:
        return self.status in ("exact", "compatible")

    def to_dict(self) -> dict[str, Any]:
        return {
            "template_id": str(self.template.id) if self.template else None,
            "template_name": self.template.name if self.template else None,
            "fingerprint": self.fingerprint,
            "status": self.status,
            "usable": self.usable,
            "missing_fields": self.missing_fields,
            "extra_fields": self.extra_fields,
            "message": self.message,
            "config": self.template.config if self.template else None,
        }


class MappingTemplateService:
    """模板的读写与匹配。"""

    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def save(
        self,
        *,
        kb_id: uuid.UUID | None,
        name: str,
        fingerprint: str,
        config: TemplateConfig | dict[str, Any],
        user: User | None = None,
    ) -> ImportMappingTemplate:
        """保存（同名同指纹则覆盖）模板。"""
        if not name.strip():
            raise TemplateError("模板名称不能为空")
        if not fingerprint:
            raise TemplateError("缺少 Schema 指纹")
        payload = config.to_dict() if isinstance(config, TemplateConfig) else config
        # 走一遍解析，确保写入的内容一定可被读回（拒绝存进去才发现非法）
        parsed = TemplateConfig.from_dict(payload)

        existing = await self.db.scalar(
            select(ImportMappingTemplate).where(
                ImportMappingTemplate.name == name.strip(),
                ImportMappingTemplate.schema_fingerprint == fingerprint,
            )
        )
        if existing is not None:
            existing.config = parsed.to_dict()
            existing.kb_id = kb_id
            existing.created_by = user.id if user else existing.created_by
            await self.db.flush()
            return existing

        template = ImportMappingTemplate(
            kb_id=kb_id,
            name=name.strip(),
            schema_fingerprint=fingerprint,
            config=parsed.to_dict(),
            created_by=user.id if user else None,
        )
        self.db.add(template)
        await self.db.flush()
        return template

    async def find_by_fingerprint(
        self, kb_id: uuid.UUID | None, fingerprint: str
    ) -> ImportMappingTemplate | None:
        """按指纹查找：知识库内模板优先，其次全局模板。"""
        if not fingerprint:
            return None
        if kb_id is not None:
            scoped = await self.db.scalar(
                select(ImportMappingTemplate)
                .where(
                    ImportMappingTemplate.schema_fingerprint == fingerprint,
                    ImportMappingTemplate.kb_id == kb_id,
                )
                .order_by(ImportMappingTemplate.created_at.desc())
            )
            if scoped is not None:
                return scoped
        return await self.db.scalar(
            select(ImportMappingTemplate)
            .where(
                ImportMappingTemplate.schema_fingerprint == fingerprint,
                ImportMappingTemplate.kb_id.is_(None),
            )
            .order_by(ImportMappingTemplate.created_at.desc())
        )

    async def list_templates(self, kb_id: uuid.UUID | None) -> list[ImportMappingTemplate]:
        query = select(ImportMappingTemplate)
        if kb_id is None:
            query = query.where(ImportMappingTemplate.kb_id.is_(None))
        else:
            query = query.where(
                (ImportMappingTemplate.kb_id == kb_id)
                | (ImportMappingTemplate.kb_id.is_(None))
            )
        rows = await self.db.execute(query.order_by(ImportMappingTemplate.created_at.desc()))
        return list(rows.scalars().all())

    async def _find_nearest(
        self, kb_id: uuid.UUID | None, tables: Mapping[str, Iterable[str]]
    ) -> ImportMappingTemplate | None:
        """指纹匹配不到时的兜底：按**表名重合度**找最接近的模板。

        为什么必须有这一步：只要 Schema 有一列变化，指纹就变了，
        若只按指纹查找，模板永远退化成「未找到」，用户只能每次重新配置；
        而真正需要提示的是「模板还在，但你删掉了它依赖的 术语id 列」。
        """
        current = set(tables)
        if not current:
            return None
        best: ImportMappingTemplate | None = None
        best_score = 0.0
        for template in await self.list_templates(kb_id):
            try:
                config = TemplateConfig.from_dict(template.config or {})
            except TemplateError:
                continue
            known = set(config.tables) | {r.child_table for r in config.relations}
            known |= {r.parent_table for r in config.relations}
            known |= {j.table for j in config.junctions}
            if not known:
                continue
            overlap = len(current & known) / len(known)
            if overlap >= 0.5 and overlap > best_score:
                best, best_score = template, overlap
        return best

    async def match(
        self,
        kb_id: uuid.UUID | None,
        tables: Mapping[str, Iterable[str]],
    ) -> MatchResult:
        """按当前数据集匹配模板，并给出字段差异清单。

        指纹一致 → exact；Schema 有变化但能按表名近似匹配 → 仍返回模板，
        并按缺失字段判定 mismatch（不可用）/ compatible（可用但有告警）。
        模板版本无法解释 → incompatible（绝不能按猜测解释）。
        """
        fingerprint = dataset_fingerprint(tables)
        template = await self.find_by_fingerprint(kb_id, fingerprint)
        by_fingerprint = template is not None
        if template is None:
            template = await self._find_nearest(kb_id, tables)
        if template is None:
            return MatchResult(fingerprint=fingerprint, status="none", message="未找到匹配模板")

        try:
            config = TemplateConfig.from_dict(template.config or {})
        except TemplateError as exc:
            logger.warning(f"[mapping_template] 模板 {template.name} 不可用: {exc}")
            return MatchResult(
                template=template,
                fingerprint=fingerprint,
                status="incompatible",
                message=str(exc),
            )

        missing, extra = _field_diff(config, tables)
        if missing:
            status = "mismatch"
            message = f"模板依赖的字段/表在当前数据中缺失：{missing}"
        elif extra:
            status = "compatible"
            message = f"存在模板未覆盖的表：{sorted(extra)}（可用，但需确认映射）"
        else:
            status = "exact"
            message = f"完全匹配（模板指纹一致，覆盖 {len(config.tables)} 张表）"

        if not by_fingerprint and status != "mismatch":
            message = f"Schema 已变化，按表名近似匹配；{message}"
        return MatchResult(
            template=template,
            fingerprint=fingerprint,
            status=status,
            missing_fields=missing,
            extra_fields=extra,
            message=message,
        )

    @staticmethod
    def to_plan(config: TemplateConfig) -> dict[str, Any]:
        """把模板转成导入可直接使用的计划（主键 / 关系）。"""
        return {
            "primary_keys": {
                name: m.pk_field for name, m in config.tables.items() if m.pk_field
            },
            "entity_types": {name: m.entity_type for name, m in config.tables.items()},
            "relations": [vars(r) for r in config.relations],
            "junctions": [vars(j) for j in config.junctions],
        }
