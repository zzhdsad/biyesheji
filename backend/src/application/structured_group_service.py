"""关联结构化数据服务（第四阶段：文件组 → 关系确认 → 关联知识记录）。

与第三阶段的分工：
- 第三阶段（StructuredParseService）负责「单个文件的字段识别 + 逐行独立记录」，
  本服务**只读取**它的结果，不重新解析文件、不改那份逻辑；
- 因此关联处理天然复用了格式识别、类型推断、行号/JSON 路径追溯。

状态机（structured_groups.status）：
    draft（关系未确认）→ validated（已校验）→ confirmed（已生成正式关联记录）
    组内文件被替换/删除 → stale（既有结果不再可信，必须重新校验并再次确认）

失败保护（对齐任务要求「关联解析失败时保留已有的有效关联结果」）：
- 关联记录先在内存里全部算完，再在**同一个事务**里删旧插新；
- 任何一步失败都 rollback，旧的一份正式记录原样保留，只写 error_message；
- 未经用户确认的关系一律不参与生成（confirmed 是硬开关）。

RAG 边界：本服务不碰 documents.parse_status / chunks，普通文档的检索链路完全
不受影响。第六阶段起，**仅在状态提交之后**调用 ``sync_structured_group`` 同步
向量：confirmed 写入选一索引，其它状态（draft/validated/stale）清掉旧向量；
同步失败只记录 group.vector_status，不影响已生成的关联记录。
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timezone
from typing import Any

from loguru import logger
from sqlalchemy import delete, func, insert, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.config import settings
from src.core.exceptions import AppException, NotFoundError
from src.core.file_spaces import STRUCTURED_SPACE
from src.domain.models import (
    STRUCTURED_GROUP_CONFIRMED,
    STRUCTURED_GROUP_DRAFT,
    STRUCTURED_GROUP_STALE,
    STRUCTURED_GROUP_VALIDATED,
    Document,
    StructuredGroup,
    StructuredGroupFile,
    StructuredGroupRecord,
    StructuredParse,
    StructuredRecord,
)
from src.utils.structured_relation import (
    FieldProfileProbe,
    GroupFileSchema,
    LinkBuilder,
    RELATION_KINDS,
    RELATION_ONE_TO_MANY,
    key_candidates_from_probe,
    validate_group,
)
from src.utils.structured_relation import relation_id as build_relation_id

_MAX_ERROR_CHARS = 2000
GROUP_NAME_MAX = 200


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _left_only_key_fields(relations: list[dict[str, Any]]) -> dict[str, set[str]]:
    """在校验里「只作为关联左侧」出现的文件需要哪些字段。

    校验左侧只用关联键（判空、查重、找不到的示例值），右侧要按键取出完整记录。
    因此左侧独有的文件可以做键投影：几十万行的关联表不必整份驻留内存。
    既当左又当右的文件不投影，避免右侧查找丢字段。
    """
    right_docs = {str(r.get("to_doc_id") or "") for r in relations} - {""}
    out: dict[str, set[str]] = {}
    for rel in relations:
        from_doc = str(rel.get("from_doc_id") or "")
        field = str(rel.get("from_field") or "")
        if not from_doc or not field or from_doc in right_docs:
            continue
        out.setdefault(from_doc, set()).add(field)
    return out


def _clean_name(raw: str) -> str:
    name = (raw or "").strip()
    if len(name) > GROUP_NAME_MAX:
        raise AppException(400, f"文件组名称最长 {GROUP_NAME_MAX} 个字符")
    return name


class StructuredGroupService:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    # ── 读取 ────────────────────────────────────────────────────────────────

    async def get(self, group_id: uuid.UUID) -> StructuredGroup:
        group = await self.db.get(StructuredGroup, group_id)
        if group is None:
            raise NotFoundError("关联文件组不存在")
        return group

    async def list_files(self, group_id: uuid.UUID) -> list[StructuredGroupFile]:
        rows = await self.db.scalars(
            select(StructuredGroupFile)
            .where(StructuredGroupFile.group_id == group_id)
            .order_by(StructuredGroupFile.order_no.asc())
        )
        return list(rows)

    async def file_statuses(self, group_id: uuid.UUID) -> list[dict[str, Any]]:
        """成员解析状态快照：处理概览只关心状态，不加载任何结构化记录。

        与 detail() 分开是为了让 /processing 轮询保持轻量——大组的记录量可达数万行，
        每次轮询都读一遍只会让页面比任务本身还慢。
        """
        files = await self.list_files(group_id)
        if not files:
            return []
        doc_ids = [item.doc_id for item in files]
        parses = {
            str(parse.doc_id): parse
            for parse in (
                await self.db.scalars(
                    select(StructuredParse).where(StructuredParse.doc_id.in_(doc_ids))
                )
            ).all()
        }
        docs = {
            str(doc.id): doc
            for doc in (
                await self.db.scalars(select(Document).where(Document.id.in_(doc_ids)))
            ).all()
        }
        statuses: list[dict[str, Any]] = []
        for item in files:
            parse = parses.get(str(item.doc_id))
            doc = docs.get(str(item.doc_id))
            statuses.append(
                {
                    "doc_id": str(item.doc_id),
                    "file_name": item.file_name,
                    "current_file_name": doc.file_name if doc else item.file_name,
                    "alias": item.alias,
                    "parse_status": parse.status if parse else "pending",
                    "record_count": parse.record_count if parse else 0,
                    "error_message": parse.error_message if parse else "",
                    "removed": doc is None or doc.deleted_at is not None,
                }
            )
        return statuses

    async def _record_count(self, doc_id: uuid.UUID) -> int:
        return int(
            await self.db.scalar(
                select(func.count())
                .select_from(StructuredRecord)
                .where(StructuredRecord.doc_id == doc_id)
            )
            or 0
        )

    async def _iter_record_batches(
        self, doc_id: uuid.UUID, *, batch_size: int | None = None
    ):
        """按 ``seq_no`` 升序分批读取解析记录（键集分页，每次只在内存里留一批）。

        用 ``seq_no > 上一批末位`` 而不是 OFFSET：偏移分页在大表上会重复扫描前缀，
        且写入与读取并发时可能漏行；(doc_id, seq_no) 有索引，键集分页既能走完
        全部记录又不会重复或遗漏，最后一批不足 batch_size 也照常返回。
        """
        size = max(1, batch_size or settings.STRUCTURED_GROUP_READ_BATCH)
        last_seq: int | None = None
        while True:
            stmt = (
                select(
                    StructuredRecord.id,
                    StructuredRecord.seq_no,
                    StructuredRecord.row_no,
                    StructuredRecord.sheet_name,
                    StructuredRecord.json_path,
                    StructuredRecord.payload,
                )
                .where(StructuredRecord.doc_id == doc_id)
                .order_by(StructuredRecord.seq_no.asc())
                .limit(size)
            )
            if last_seq is not None:
                stmt = stmt.where(StructuredRecord.seq_no > last_seq)
            rows = (await self.db.execute(stmt)).all()
            if not rows:
                return
            last_seq = int(rows[-1][1])
            yield [
                {
                    "record_id": str(row[0]),
                    "row_no": row[2],
                    "sheet_name": row[3],
                    "json_path": row[4],
                    "payload": dict(row[5] or {}),
                }
                for row in rows
            ]
            if len(rows) < size:
                return

    async def _load_records(
        self, doc_id: uuid.UUID, *, keep_fields: set[str] | None = None
    ) -> tuple[list[dict[str, Any]], int]:
        """分批读出记录；``keep_fields`` 只保留参与关联的字段（键投影，行数不变）。"""
        out: list[dict[str, Any]] = []
        total = 0
        async for batch in self._iter_record_batches(doc_id):
            total += len(batch)
            if keep_fields is None:
                out.extend(batch)
            else:
                for rec in batch:
                    payload = rec["payload"]
                    rec["payload"] = {name: payload.get(name) for name in keep_fields}
                    out.append(rec)
        return out, total

    async def load_schema_meta(self, group_id: uuid.UUID) -> list[GroupFileSchema]:
        """组内文件的结构与记录数，**不读任何记录**（选锚点、判断有没有数据用）。"""
        files = await self.list_files(group_id)
        schemas: list[GroupFileSchema] = []
        for item in files:
            doc = await self.db.get(Document, item.doc_id)
            if doc is None:
                continue
            parse = await self.db.scalar(
                select(StructuredParse).where(StructuredParse.doc_id == doc.id)
            )
            schemas.append(
                GroupFileSchema(
                    doc_id=str(doc.id),
                    file_name=doc.file_name,
                    alias=item.alias or "",
                    order_no=item.order_no,
                    fields=list(parse.fields) if parse and parse.fields else [],
                    total_rows=await self._record_count(doc.id),
                )
            )
        return schemas

    async def load_schemas(
        self,
        group_id: uuid.UUID,
        *,
        exclude_doc_ids: set[uuid.UUID] | None = None,
        key_fields_by_doc: dict[str, set[str]] | None = None,
    ) -> list[GroupFileSchema]:
        """把组内文件的**第三阶段解析结果**读成内存结构（字段 + 记录）。

        ``exclude_doc_ids`` 仅供详情页使用：已删除 / 在回收站的文件不再加载整份
        记录（那些记录不能参与关联，加载只会让详情变慢）；校验、推测、生成等
        需要完整事实的调用方不传该参数，行为不变。

        ``key_fields_by_doc`` 给指定文件（通常是锚点文件）做**键投影**：只保留
        参与关联的字段值。这类文件只作为关联的「左」侧出现，校验阶段用不到其它
        字段，正式生成阶段又由 ``_iter_record_batches`` 逐批读取，所以把整份
        payload 留在内存里纯属浪费。记录条数与顺序不受投影影响。
        """
        files = await self.list_files(group_id)
        schemas: list[GroupFileSchema] = []
        for item in files:
            if exclude_doc_ids and item.doc_id in exclude_doc_ids:
                continue
            doc = await self.db.get(Document, item.doc_id)
            if doc is None:
                continue
            parse = await self.db.scalar(
                select(StructuredParse).where(StructuredParse.doc_id == doc.id)
            )
            records, total = await self._load_records(
                doc.id, keep_fields=(key_fields_by_doc or {}).get(str(doc.id))
            )
            schemas.append(
                GroupFileSchema(
                    doc_id=str(doc.id),
                    file_name=doc.file_name,
                    alias=item.alias or "",
                    order_no=item.order_no,
                    fields=list(parse.fields) if parse and parse.fields else [],
                    records=records,
                    total_rows=total,
                )
            )
        return schemas

    async def detail(self, group_id: uuid.UUID) -> dict[str, Any]:
        """文件组详情：成员文件 + 各文件结构识别（字段/类型/记录数/主键候选）+ 关系与校验报告。

        主键候选需要「整个字段的取值分布」，但只需要统计量，不需要记录本身：
        这里按批扫过记录并把结果喂给 ``FieldProfileProbe``，扫完即释放，
        因此几十万行的组也不会把整份记录压进内存（旧实现受 20000 条上限限制）。
        """
        group = await self.get(group_id)
        files = await self.list_files(group_id)
        parses = {}
        docs = {}
        if files:
            rows = (
                await self.db.scalars(
                    select(StructuredParse).where(
                        StructuredParse.doc_id.in_([f.doc_id for f in files])
                    )
                )
            ).all()
            parses = {str(p.doc_id): p for p in rows}
            rows = (
                await self.db.scalars(select(Document).where(Document.id.in_([f.doc_id for f in files])))
            ).all()
            docs = {str(d.id): d for d in rows}

        file_out = []
        for item in files:
            parse = parses.get(str(item.doc_id))
            doc = docs.get(str(item.doc_id))
            # 已删除 / 在回收站的文件不能参与关联：只展示解析快照，不扫记录
            removed = doc is None or doc.deleted_at is not None
            field_specs = list(parse.fields) if parse and parse.fields else []
            names = [str(spec.get("name", "")) for spec in field_specs if spec.get("name")]
            total_rows = 0 if removed else await self._record_count(item.doc_id)
            candidates: list[dict[str, Any]] = []
            if total_rows and names:
                schema = GroupFileSchema(
                    doc_id=str(item.doc_id),
                    file_name=doc.file_name,
                    alias=item.alias or "",
                    order_no=item.order_no,
                    fields=field_specs,
                    total_rows=total_rows,
                )
                probe = FieldProfileProbe(names)
                async for batch in self._iter_record_batches(item.doc_id):
                    await asyncio.to_thread(probe.add, batch)
                candidates = key_candidates_from_probe(schema, probe)
            file_out.append(
                {
                    "doc_id": str(item.doc_id),
                    "file_name": item.file_name,
                    "current_file_name": doc.file_name if doc else item.file_name,
                    "alias": item.alias,
                    "order_no": item.order_no,
                    "parse_status": parse.status if parse else "pending",
                    "detected_format": parse.detected_format if parse else "",
                    "record_count": parse.record_count if parse else 0,
                    "anomaly_count": parse.anomaly_count if parse else 0,
                    "error_message": parse.error_message if parse else "",
                    "fields": names,
                    "field_specs": field_specs,
                    "key_candidates": candidates,
                    "removed": removed,
                }
            )
        return {
            "group": group,
            "files": file_out,
            "relations": list(group.relations or []),
            "validation": dict(group.validation or {}),
        }

    # ── 建组与成员维护 ──────────────────────────────────────────────────────

    async def create_group(
        self, kb_id: uuid.UUID, name: str, doc_ids: list[uuid.UUID]
    ) -> StructuredGroup:
        if len(doc_ids) < 2:
            raise AppException(400, "关联数据至少需要 2 个文件；单个文件请用第三阶段的独立解析")
        unique_ids = list(dict.fromkeys(doc_ids))
        docs = (
            await self.db.scalars(select(Document).where(Document.id.in_(unique_ids)))
        ).all()
        found = {str(d.id) for d in docs}
        missing = [str(i) for i in unique_ids if str(i) not in found]
        if missing:
            raise AppException(400, f"以下文件不存在或无权访问：{'、'.join(missing)}")
        wrong_space = [d.file_name for d in docs if d.space != STRUCTURED_SPACE]
        if wrong_space:
            raise AppException(
                400, f"普通文档不能作为关联文件组的成员，请先上传到结构化数据空间：{'、'.join(wrong_space)}"
            )
        trashed = [d.file_name for d in docs if d.deleted_at is not None]
        if trashed:
            raise AppException(400, f"回收站中的文件不能加入文件组：{'、'.join(trashed)}")

        group = StructuredGroup(
            kb_id=kb_id,
            name=_clean_name(name),
            status=STRUCTURED_GROUP_DRAFT,
            relations=[],
            validation={},
        )
        self.db.add(group)
        await self.db.flush()

        for index, doc_id in enumerate(unique_ids):
            doc = next(d for d in docs if str(d.id) == str(doc_id))
            self.db.add(
                StructuredGroupFile(
                    group_id=group.id,
                    doc_id=doc.id,
                    kb_id=kb_id,
                    order_no=index,
                    file_name=doc.file_name,
                )
            )
        await self.db.commit()
        await self.db.refresh(group)
        logger.info(f"关联文件组已创建 id={group.id} 文件数={len(unique_ids)}")
        return group

    async def add_files(self, group_id: uuid.UUID, doc_ids: list[uuid.UUID]) -> StructuredGroup:
        group = await self.get(group_id)
        existing = {
            str(f.doc_id) for f in await self.db.scalars(
                select(StructuredGroupFile).where(StructuredGroupFile.group_id == group.id)
            )
        }
        docs = (
            await self.db.scalars(select(Document).where(Document.id.in_(doc_ids)))
        ).all()
        order = len(existing)
        added = 0
        for doc in docs:
            if str(doc.id) in existing:
                continue
            if doc.space != STRUCTURED_SPACE or doc.deleted_at is not None:
                raise AppException(400, f"「{doc.file_name}」不是结构化数据空间的可用地文件，无法加入文件组")
            self.db.add(
                StructuredGroupFile(
                    group_id=group.id,
                    doc_id=doc.id,
                    kb_id=group.kb_id,
                    order_no=order,
                    file_name=doc.file_name,
                )
            )
            order += 1
            added += 1
        if added:
            self._invalidate_relation_config(group, "文件组构成已变化，请重新校验关系")
            await self.db.commit()
            await self.db.refresh(group)
            await self._sync_vectors(group.id)
        return group

    async def set_file_alias(
        self, group_id: uuid.UUID, doc_id: uuid.UUID, alias: str
    ) -> StructuredGroupFile:
        row = await self.db.scalar(
            select(StructuredGroupFile).where(
                StructuredGroupFile.group_id == group_id,
                StructuredGroupFile.doc_id == doc_id,
            )
        )
        if row is None:
            raise NotFoundError("该文件不在文件组内")
        row.alias = (alias or "").strip()[:64]
        await self.db.commit()
        await self.db.refresh(row)
        return row

    async def remove_file(self, group_id: uuid.UUID, doc_id: uuid.UUID) -> StructuredGroup:
        """从文件组移出成员：同步删掉引用它的关系，避免留下无效关系。"""
        group = await self.get(group_id)
        row = await self.db.scalar(
            select(StructuredGroupFile).where(
                StructuredGroupFile.group_id == group.id,
                StructuredGroupFile.doc_id == doc_id,
            )
        )
        if row is None:
            raise NotFoundError("该文件不在文件组内")
        await self.db.delete(row)
        remaining = int(
            await self.db.scalar(
                select(func.count())
                .select_from(StructuredGroupFile)
                .where(StructuredGroupFile.group_id == group.id)
            )
            or 0
        )
        relations = [
            r
            for r in (group.relations or [])
            if str(r.get("from_doc_id")) != str(doc_id) and str(r.get("to_doc_id")) != str(doc_id)
        ]
        group.relations = relations
        if remaining < 2:
            # 组内已不足以构成关系：正式记录全部作废，不保留指向缺失文件的关联结果
            await self.db.execute(
                delete(StructuredGroupRecord).where(StructuredGroupRecord.group_id == group.id)
            )
            group.record_count = 0
            group.status = STRUCTURED_GROUP_STALE
            group.validation = {}
            group.error_message = "文件组只剩不到 2 个文件，关联记录已作废，请补充文件后重新校验"
        else:
            self._invalidate_relation_config(
                group, "文件组构成已变化，请重新校验并确认后再生成"
            )
        await self.db.commit()
        await self.db.refresh(group)
        # 组已不再 confirmed：旧向量不得继续参与检索（同步入口自行吞异常）
        await self._sync_vectors(group.id)
        return group

    # ── 关系配置 / 校验 / 确认 ───────────────────────────────────────────────

    def _invalidate_relation_config(self, group: StructuredGroup, reason: str) -> None:
        """关系配置一变，校验报告立即作废；已经生成过正式记录则整组转 stale，
        绝不允许「旧记录 + 新关系」混在一起（未重新确认前不得当作有效结果）。"""
        group.validation = {}
        group.validated_at = None
        if group.record_count:
            group.status = STRUCTURED_GROUP_STALE
            group.error_message = reason
        else:
            group.status = STRUCTURED_GROUP_DRAFT
            group.error_message = ""

    async def save_relations(
        self, group_id: uuid.UUID, relations: list[dict[str, Any]]
    ) -> StructuredGroup:
        """保存用户选择/确认的关系列表（整份替换）。"""
        group = await self.get(group_id)
        files = await self.list_files(group_id)
        allowed = {str(f.doc_id) for f in files}

        normalized: list[dict[str, Any]] = []
        seen: set[str] = set()
        for raw in relations:
            from_doc = str(raw.get("from_doc_id") or "")
            to_doc = str(raw.get("to_doc_id") or "")
            from_field = str(raw.get("from_field") or "").strip()
            to_field = str(raw.get("to_field") or "").strip()
            if from_doc not in allowed or to_doc not in allowed:
                raise AppException(400, "关系里包含不属于该文件组的文件")
            if from_doc == to_doc:
                raise AppException(400, "关联关系必须连接两个不同文件")
            if not from_field or not to_field:
                raise AppException(400, "请为每条关系指定两侧的关联字段")
            kind = str(raw.get("kind") or RELATION_ONE_TO_MANY)
            if kind not in RELATION_KINDS:
                raise AppException(400, f"关联类型只能是 {'、'.join(RELATION_KINDS)}")
            rid = build_relation_id(from_doc, from_field, to_doc, to_field)
            if rid in seen:
                continue
            seen.add(rid)
            origin = str(raw.get("origin") or "manual")
            if origin not in ("manual", "suggested"):
                origin = "manual"
            item: dict[str, Any] = {
                "id": rid,
                "from_doc_id": from_doc,
                "from_field": from_field,
                "to_doc_id": to_doc,
                "to_field": to_field,
                "kind": kind,
                "origin": origin,
                # 推测/预填关系缺省仍是「未确认」；只有用户手动选字段才默认已确认
                "confirmed": (
                    bool(raw["confirmed"])
                    if raw.get("confirmed") is not None
                    else origin == "manual"
                ),
                "confidence": raw.get("confidence"),
                "reason": str(
                    raw.get("reason") or ("用户手动配置" if origin == "manual" else "系统推测")
                ),
            }
            # 保留推测阶段写下的评分与依据，保存/确认其它关系时不会丢
            for extra in ("score", "coverage", "row_coverage", "evidence"):
                value = raw.get(extra)
                if value is not None:
                    item[extra] = value
            if item.get("score") is not None:
                try:
                    item["score"] = max(0, min(100, int(item["score"])))
                except (TypeError, ValueError):
                    item.pop("score", None)
            if raw.get("tier") in ("high", "medium", "low"):
                item["tier"] = raw["tier"]
            normalized.append(item)
        group.relations = normalized
        self._invalidate_relation_config(group, "关联关系配置已修改，请重新校验并确认后再生成")
        await self.db.commit()
        await self.db.refresh(group)
        await self._sync_vectors(group.id)
        return group

    async def set_relation_confirmation(
        self, group_id: uuid.UUID, target_relation_id: str, confirmed: bool
    ) -> StructuredGroup:
        """勾选/取消勾选某条关系（兼容历史遗留的未确认关系）。"""
        group = await self.get(group_id)
        relations = [dict(r) for r in (group.relations or [])]
        hit = False
        for rel in relations:
            if str(rel.get("id")) == target_relation_id:
                rel["confirmed"] = bool(confirmed)
                hit = True
        if not hit:
            raise NotFoundError("该关系不在文件组的关系配置里")
        group.relations = relations
        self._invalidate_relation_config(group, "关联关系的确认状态已变化，请重新校验并确认后再生成")
        await self.db.commit()
        await self.db.refresh(group)
        await self._sync_vectors(group.id)
        return group

    async def validate(self, group_id: uuid.UUID) -> dict[str, Any]:
        group = await self.get(group_id)
        if not group.relations:
            raise AppException(
                400,
                "还没有任何关联关系：请手动选择两侧文件和关联字段后再校验",
            )
        relations = [dict(r) for r in group.relations]
        # 只作为关联左侧出现的文件按关联键投影读取：大表不会整份进内存，
        # 但记录条数与示例值都是全部数据的真实事实，不受行数上限影响
        schemas = await self.load_schemas(group_id, key_fields_by_doc=_left_only_key_fields(relations))
        report = validate_group(schemas, relations)
        # 校验报告里额外记录锚点候选，供确认接口使用
        report["checked_at"] = _now().isoformat()
        await self.db.execute(
            update(StructuredGroup)
            .where(StructuredGroup.id == group.id)
            .values(
                validation=report,
                # 关系配置本身可能已被推测更新过，这里同步回去（校验会补 kind/id）
                relations=[dict(r) for r in group.relations],
                status=STRUCTURED_GROUP_VALIDATED,
                validated_at=_now(),
                error_message="",
            )
        )
        await self.db.commit()
        # 校验不等于确认：状态已不是 confirmed，旧向量必须先退出检索
        await self._sync_vectors(group.id)
        logger.info(
            f"关联文件组校验完成 id={group.id} 问题={len(report['issues'])} "
            f"可确认={report['can_confirm']}"
        )
        return report

    async def confirm(
        self,
        group_id: uuid.UUID,
        anchor_doc_id: str | None = None,
        force: bool = False,
        *,
        sync_vectors: bool = True,
    ) -> dict[str, Any]:
        """按已确认关系整份生成关联记录（失败保留旧结果）。

        ``sync_vectors=False`` 供 HTTP 路由使用：关联事实照常提交，向量化由后台任务
        按 ``run_group_vectorize_safely`` 执行，避免大组确认请求被 Embedding 阻塞。

        生成过程是流式的：锚点记录按 ``STRUCTURED_GROUP_READ_BATCH`` 分批从数据库读出，
        展开结果按 ``STRUCTURED_GROUP_WRITE_BATCH`` 分批写入，全程在同一个事务里，
        因此锚点有多少行都能生成完整记录（旧实现受 20000 条上限截断），
        而任何一批失败都会整体回滚、保留上一次的正式关联记录。
        """
        group = await self.get(group_id)
        relations = [dict(r) for r in (group.relations or [])]

        # 校验只需要左侧的关联键、右侧的完整记录；锚点侧的记录随后按批流式读取。
        # 所以这里对「只作为左侧出现」的文件做键投影，几十万行不会整份驻留内存。
        schemas = await self.load_schemas(
            group_id, key_fields_by_doc=_left_only_key_fields(relations)
        )
        report = validate_group(schemas, relations)

        if not relations:
            raise AppException(400, "还没有任何关联关系，无法生成关联记录")
        if report["structural_error_count"]:
            raise AppException(
                400,
                f"存在 {report['structural_error_count']} 条不成立的关系（文件或字段已不存在），请先修正后重新校验",
            )
        if report["unconfirmed_count"]:
            raise AppException(
                400,
                f"还有 {report['unconfirmed_count']} 条关系未经你确认：推测的关系不能直接入库",
            )
        if report["force_required"] and not force:
            raise AppException(
                409,
                f"存在 {report['unmatched_error_count']} 条外键无法匹配的记录。确认生成会保留这些原始行并标注提示，"
                "不会丢弃或修改任何数据；如确认请再次提交（force）",
            )

        builder = LinkBuilder(schemas, relations, anchor_doc_id)
        if builder.skipped:
            raise AppException(400, f"没有可生成的关联记录：{builder.skipped}")
        anchor_uuid = uuid.UUID(builder.anchor.doc_id)
        if builder.anchor.record_count == 0:
            raise AppException(400, "没有可生成的关联记录：锚点文件没有数据行")

        read_size = max(1, settings.STRUCTURED_GROUP_READ_BATCH)
        write_size = max(1, settings.STRUCTURED_GROUP_WRITE_BATCH)
        read_count = 0
        written_count = 0
        batch_count = 0
        try:
            await self.db.execute(
                delete(StructuredGroupRecord).where(
                    StructuredGroupRecord.group_id == group.id
                )
            )
            pending: list[dict[str, Any]] = []
            async for batch in self._iter_record_batches(anchor_uuid, batch_size=read_size):
                batch_count += 1
                read_count += len(batch)
                # 展开是纯 CPU 工作，放进线程执行，避免几十万行把事件循环卡住
                pending.extend(await asyncio.to_thread(builder.build_many, batch))
                while len(pending) >= write_size:
                    chunk = pending[:write_size]
                    del pending[:write_size]
                    await self._insert_group_records(group, written_count, chunk)
                    written_count += len(chunk)
            # 最后一批不足 write_size 的余量同样要写完，否则末尾记录会丢
            if pending:
                await self._insert_group_records(group, written_count, pending)
                written_count += len(pending)
            if written_count != read_count:
                raise AppException(
                    500,
                    f"关联记录生成条数不一致（读取 {read_count} 行，写入 {written_count} 条），已回滚",
                )
            stats = builder.stats()
            stats.update(
                {
                    "read_count": read_count,
                    "record_count": written_count,
                    "failed_count": 0,
                    "batch_count": batch_count,
                }
            )
            report["anchor_doc_id"] = stats.get("anchor_doc_id")
            await self.db.execute(
                update(StructuredGroup)
                .where(StructuredGroup.id == group.id)
                .values(
                    status=STRUCTURED_GROUP_CONFIRMED,
                    record_count=written_count,
                    validation={**report, "generation": stats},
                    confirmed_at=_now(),
                    error_message="",
                )
            )
            await self.db.commit()
        except AppException:
            await self.db.rollback()
            raise
        except Exception as exc:  # noqa: BLE001 - 写库失败整份回滚，旧结果保留
            await self.db.rollback()
            logger.error(f"关联记录落库失败 group_id={group_id}: {exc}")
            raise AppException(
                500, f"关联记录写入失败，原有结果已保留：{exc}"
            ) from exc

        # 关联事实已提交：把每条关联记录接入统一向量索引（replace_doc 先写新再清旧，
        # 写入失败时旧向量仍在，只把 vector_status 置 failed 供重新向量化）
        if sync_vectors:
            await self._sync_vectors(group.id)
        logger.info(
            f"关联记录生成完成 group_id={group_id} 读取={read_count} 条数={written_count} "
            f"锚点={stats.get('anchor_file')} 批次={batch_count} 提示={stats.get('anomaly_count')}"
        )
        return {"record_count": written_count, "stats": stats}

    async def _insert_group_records(
        self, group: StructuredGroup, start_seq: int, rows: list[dict[str, Any]]
    ) -> None:
        """写入一批正式关联记录。

        ``seq_no`` 从 ``start_seq`` 连续推进，配合调用方的单次事务，
        既保证批次之间不重号，也让「已写入条数」就是「应有的最大序号」。
        """
        if not rows:
            return
        await self.db.execute(
            insert(StructuredGroupRecord),
            [
                {
                    "group_id": group.id,
                    "kb_id": group.kb_id,
                    "seq_no": start_seq + index,
                    "anchor_doc_id": uuid.UUID(str(r["anchor_doc_id"])),
                    "anchor_record_id": (
                        uuid.UUID(str(r["anchor_record_id"])) if r.get("anchor_record_id") else None
                    ),
                    "knowledge_text": r["knowledge_text"],
                    "parts": r["parts"],
                    "sources": r["sources"],
                    "relation_ids": r["relation_ids"],
                    "anomalies": r["anomalies"],
                }
                for index, r in enumerate(rows)
            ],
        )

    # ── 记录读取 ────────────────────────────────────────────────────────────

    async def list_records(
        self,
        group_id: uuid.UUID,
        limit: int,
        offset: int,
        keyword: str | None,
        only_anomaly: bool,
    ) -> tuple[list[StructuredGroupRecord], int]:
        group = await self.get(group_id)
        filters = [StructuredGroupRecord.group_id == group.id]
        if keyword:
            filters.append(StructuredGroupRecord.knowledge_text.ilike(f"%{keyword.strip()}%"))
        if only_anomaly:
            filters.append(func.jsonb_array_length(StructuredGroupRecord.anomalies) > 0)
        total = int(
            await self.db.scalar(
                select(func.count()).select_from(StructuredGroupRecord).where(*filters)
            )
            or 0
        )
        rows = (
            await self.db.scalars(
                select(StructuredGroupRecord)
                .where(*filters)
                .order_by(StructuredGroupRecord.seq_no.asc())
                .offset(offset)
                .limit(limit)
            )
        ).all()
        return list(rows), total

    # ── 文件替换 / 删除联动 ─────────────────────────────────────────────────

    async def mark_stale_by_docs(
        self, doc_ids: list[uuid.UUID], reason: str, drop_records: bool = False
    ) -> list[uuid.UUID]:
        """组内文件被替换或删除后调用：既有正式结果不再可信，必须重新校验。

        ``drop_records=True`` 用于文件被彻底删除的场景——关系已经不成立，
        不能留下指向不存在文件的关联记录（「不能留下无效关系」）。
        """
        if not doc_ids:
            return []
        group_ids = list(
            await self.db.scalars(
                select(StructuredGroupFile.group_id).where(
                    StructuredGroupFile.doc_id.in_(doc_ids)
                )
            )
        )
        if not group_ids:
            return []
        if drop_records:
            await self.db.execute(
                delete(StructuredGroupRecord).where(
                    StructuredGroupRecord.group_id.in_(group_ids)
                )
            )
        await self.db.execute(
            update(StructuredGroup)
            .where(StructuredGroup.id.in_(group_ids))
            .values(
                status=STRUCTURED_GROUP_STALE,
                validation={},
                error_message=reason[:_MAX_ERROR_CHARS],
                **({"record_count": 0} if drop_records else {}),
            )
        )
        logger.info(f"关联文件组已标记为需重新校验 ids={[str(g) for g in group_ids]} 原因={reason}")
        return group_ids

    async def delete_group(self, group_id: uuid.UUID) -> None:
        group = await self.get(group_id)
        await self.db.delete(group)
        await self.db.commit()
        # 组已不存在：关联记录随外键级联删除，向量也必须一并清掉
        from src.application.structured_vector_service import clear_structured_group_vectors

        await clear_structured_group_vectors(group_id)
        logger.info(f"关联文件组已删除 id={group_id}")

    async def _sync_vectors(self, group_id: uuid.UUID) -> None:
        """状态提交后的向量同步：confirmed 写入，其余状态清空（不抛异常）。"""
        from src.application.structured_vector_service import sync_structured_group

        await sync_structured_group(self.db, group_id)
        # 同步内部会 Core UPDATE structured_groups，ORM 会把 onupdate 的
        # updated_at 标记为过期；路由随后在同步上下文里序列化 group 会触发
        # 懒加载（MissingGreenlet → 500）。这里在异步上下文里补一次刷新，
        # 让响应拿到同步后的真实时间与向量状态。
        group = await self.db.get(StructuredGroup, group_id)
        if group is not None:
            await self.db.refresh(group)
