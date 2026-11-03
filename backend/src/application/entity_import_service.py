"""通用导入：实体与关系持久化（关联导入的核心写入层）。

职责边界：
- 只做**写入与状态管理**，不做文件解析（tabular_parser）与关系发现（relation_discovery）；
- 所有写入强制带 kb_id，关系解析前后都校验「实体属于当前知识库」，杜绝跨库引用；
- 候选关系默认 pending，**只有 confirmed 才会同步进 kg_nodes / kg_edges**；
- 幂等基于稳定标识：原始记录用 record_key（sha256(文件哈希:行号)），
  实体用 source_pk（数据自带主键值），关系用 (源, 目标, 类型) 唯一约束；
- 部分失败可恢复：每个逻辑单元（一张表 / 一组关系）独立 SAVEPOINT，
  单元失败只回滚该单元并记录错误，已成功的单元不丢失。

事务边界：本服务内部用 SAVEPOINT 分段提交，最终由调用方 `commit()`；
任一步骤抛错时调用方 `rollback()` 即可整体回滚（单元测试依赖该语义）。
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Iterable, Sequence

from loguru import logger
from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from src.application.business_resource_service import BusinessResourceService
from src.application.import_progress import ImportProgress
from src.application.relation_discovery import ForeignKeyCandidate, JunctionCandidate
from src.application.tabular_parser import ParsedRow
from src.core.deps import require_kb_write
from src.domain.models import (
    KG_NODE_TYPE_ENTITY,
    KG_STATUS_CONFIRMED,
    KG_STATUS_PENDING,
    KG_STATUS_REJECTED,
    GenericEntity,
    GenericRelation,
    ImportRecord,
    KgEdge,
    KgNode,
    User,
)

# 单批处理的行数上限（避免单条 INSERT 过大）
BATCH_SIZE = 500
# 实体显示名候选字段（找不到就用主键值）
# 真实中医数据集的名称列多是英文语义列名（Chinese_term / Chinese_herbal_pieces…），
# 只认「名称/标题」会让 4 万实体的名字退化成 CHP00001 / CPM00065 这类主键值。
_DISPLAY_FIELD_HINTS: tuple[str, ...] = (
    "name",
    "名称",
    "标题",
    "题目",
    "title",
    "label",
    "中文名",
    "Chinese_term",
    "Chinese_herbal_pieces",
    "Chinese_patent_medicine",
    "Chinese_Character",
    "book_name",
    "书名",
)
# 允许的关系统一状态（与领域常量一致，避免各处写字符串）
_VALID_STATUSES = frozenset({KG_STATUS_PENDING, KG_STATUS_CONFIRMED, KG_STATUS_REJECTED})


def record_key_for(file_hash: str, row_no: int) -> str:
    """稳定的源记录标识：同一份文件的同一行无论导入多少次都不变。"""
    return hashlib.sha256(f"{file_hash}:{row_no}".encode("utf-8")).hexdigest()


def _display_name(payload: dict[str, Any], fallback: str) -> str:
    """挑一个可读显示名：先精确匹配，再包含匹配。

    包含匹配是必要的：真实数据集里常见「术语名称」「药品名称」这类带前缀的列名，
    只做精确匹配会全部落空、退化成显示主键值，图谱节点将失去可读性。
    """
    items = [(str(k), v) for k, v in payload.items() if v not in (None, "")]
    for hint in _DISPLAY_FIELD_HINTS:
        for key, value in items:
            if key.lower() == hint.lower():
                return str(value)[:255]
    for hint in _DISPLAY_FIELD_HINTS:
        for key, value in items:
            if hint.lower() in key.lower():
                return str(value)[:255]
    return str(fallback)[:255]


def _pk_value(payload: dict[str, Any], pk_field: str | None) -> str:
    if pk_field:
        value = payload.get(pk_field)
        if value not in (None, ""):
            return str(value)
    return ""


@dataclass
class RowError:
    """单行失败信息（前端按文件+行号展示）。"""

    file_name: str
    row_no: int
    message: str


@dataclass
class ImportStats:
    """一次导入的统计与失败明细。"""

    records: int = 0
    entities: int = 0
    relations: int = 0
    kg_nodes: int = 0
    kg_edges: int = 0
    # 同时写入业务表（中药/方剂/理论/文献）的新增条数
    business_herbs: int = 0
    business_prescriptions: int = 0
    business_theories: int = 0
    business_literatures: int = 0
    business_ingredients: int = 0
    # 数据集中已存在同名业务资源（按 name 幂等跳过）
    business_existing: int = 0
    errors: list[RowError] = field(default_factory=list)

    @property
    def failed_rows(self) -> int:
        return len(self.errors)

    def to_dict(self) -> dict[str, Any]:
        return {
            "records": self.records,
            "entities": self.entities,
            "relations": self.relations,
            "kg_nodes": self.kg_nodes,
            "kg_edges": self.kg_edges,
            "business_herbs": self.business_herbs,
            "business_prescriptions": self.business_prescriptions,
            "business_theories": self.business_theories,
            "business_literatures": self.business_literatures,
            "business_ingredients": self.business_ingredients,
            "business_existing": self.business_existing,
            "failed_rows": self.failed_rows,
            "errors": [
                {"file_name": e.file_name, "row_no": e.row_no, "message": e.message}
                for e in self.errors
            ],
        }


class EntityImportService:
    """实体 / 关系持久化服务（按知识库隔离）。"""

    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    # ── 权限 ────────────────────────────────────────────────────────────────

    async def require_write(self, user: User, kb_id: uuid.UUID) -> str:
        """校验当前用户对知识库有写权限（owner/admin/editor）。"""
        return await require_kb_write(self.db, user, kb_id)

    # ── 原始记录 ────────────────────────────────────────────────────────────

    async def save_records(
        self,
        *,
        job_id: uuid.UUID,
        file_id: uuid.UUID | None,
        kb_id: uuid.UUID | None,
        file_hash: str,
        entity_type: str | None,
        rows: Sequence[ParsedRow],
        file_name: str = "",
        on_batch: Callable[[int], Awaitable[None]] | None = None,
    ) -> tuple[int, list[RowError]]:
        """保存原始记录（幂等：同一 kb + record_key 会被覆盖更新）。

        保留每行完整字段到 payload，不拼接文本、不丢字段、不翻译。
        on_batch 每批回调一次（参数为本批行数），用于长任务进度上报。
        """
        errors: list[RowError] = []
        written = 0
        try:
            async with self.db.begin_nested():
                for start in range(0, len(rows), BATCH_SIZE):
                    batch = rows[start : start + BATCH_SIZE]
                    keys = {record_key_for(file_hash, row.row_no) for row in batch}
                    # 幂等键优先取 kb_id：同一份文件换个任务重新导入同一知识库时，
                    # 必须命中既有记录（否则会撞 uq_import_records_kb_record_key）
                    existing = await self._existing_records(job_id, kb_id, keys)
                    for row in batch:
                        key = record_key_for(file_hash, row.row_no)
                        current = existing.get(key)
                        if current is None:
                            self.db.add(
                                ImportRecord(
                                    job_id=job_id,
                                    file_id=file_id,
                                    kb_id=kb_id,
                                    row_no=row.row_no,
                                    record_key=key,
                                    payload=dict(row.data),
                                    entity_type=entity_type,
                                )
                            )
                        else:
                            current.payload = dict(row.data)
                            current.entity_type = entity_type
                            current.kb_id = kb_id
                        written += 1
                    await self.db.flush()
                    if on_batch is not None:
                        await on_batch(len(batch))
        except Exception as exc:  # noqa: BLE001 - 单元失败要转成可展示错误
            errors.append(RowError(file_name, 0, f"原始记录保存失败：{exc}"))
            logger.warning(f"[entity_import] 原始记录保存失败，已回滚该单元: {exc}")
        return written, errors

    async def _existing_records(
        self, job_id: uuid.UUID, kb_id: uuid.UUID | None, keys: Iterable[str]
    ) -> dict[str, ImportRecord]:
        key_list = list(keys)
        if not key_list:
            return {}
        query = select(ImportRecord).where(ImportRecord.record_key.in_(key_list))
        if kb_id is not None:
            query = query.where(ImportRecord.kb_id == kb_id)
        else:
            query = query.where(ImportRecord.job_id == job_id)
        rows = await self.db.execute(query)
        return {r.record_key: r for r in rows.scalars().all()}

    # ── 实体 ────────────────────────────────────────────────────────────────

    async def upsert_entities(
        self,
        *,
        kb_id: uuid.UUID,
        entity_type: str,
        rows: Sequence[ParsedRow],
        pk_field: str | None,
        file_id: uuid.UUID | None = None,
        file_hash: str = "",
        batch_id: str | None = None,
        created_by: uuid.UUID | None = None,
        file_name: str = "",
        on_batch: Callable[[int], Awaitable[None]] | None = None,
    ) -> tuple[dict[str, uuid.UUID], list[RowError]]:
        """按 source_pk 幂等写入实体，返回 (source_pk → entity_id)。

        on_batch 每批回调一次（参数为本批行数），用于长任务进度上报。
        """
        index: dict[str, uuid.UUID] = {}
        errors: list[RowError] = []
        try:
            async with self.db.begin_nested():
                for start in range(0, len(rows), BATCH_SIZE):
                    batch = rows[start : start + BATCH_SIZE]
                    pks = {_pk_value(row.data, pk_field) for row in batch}
                    pks.discard("")
                    existing = await self._existing_entities(kb_id, entity_type, pks)
                    # 同批内重复主键会被后面的行覆盖，不能重复 INSERT
                    pending: dict[str, GenericEntity] = {}
                    for row in batch:
                        pk = _pk_value(row.data, pk_field)
                        if not pk:
                            errors.append(
                                RowError(file_name, row.row_no, "缺少主键值，跳过该行实体")
                            )
                            continue
                        record_key = record_key_for(file_hash, row.row_no) if file_hash else None
                        current = existing.get(pk) or pending.get(pk)
                        if current is None:
                            entity = GenericEntity(
                                kb_id=kb_id,
                                entity_type=entity_type,
                                name=_display_name(row.data, pk),
                                payload=dict(row.data),
                                source_file_id=file_id,
                                source_row_no=row.row_no,
                                record_key=record_key,
                                source_pk=pk,
                                import_batch_id=batch_id,
                                created_by=created_by,
                            )
                            self.db.add(entity)
                            pending[pk] = entity
                        else:
                            current.name = _display_name(row.data, pk)
                            current.payload = dict(row.data)
                            current.source_row_no = row.row_no
                            current.import_batch_id = batch_id
                            if record_key:
                                current.record_key = record_key
                    # 每批一次 flush：逐行 flush 会让大文件产生数万次数据库往返
                    if pending:
                        await self.db.flush()
                        for pk, entity in pending.items():
                            index[pk] = entity.id
                    for pk, entity in existing.items():
                        index[pk] = entity.id
                    if on_batch is not None:
                        await on_batch(len(batch))
        except Exception as exc:  # noqa: BLE001
            errors.append(RowError(file_name, 0, f"实体写入失败：{exc}"))
            logger.warning(f"[entity_import] 实体写入失败，已回滚该单元: {exc}")
        return index, errors

    async def _existing_entities(
        self, kb_id: uuid.UUID, entity_type: str, pks: Iterable[str]
    ) -> dict[str, GenericEntity]:
        pk_list = [p for p in pks if p]
        if not pk_list:
            return {}
        rows = await self.db.execute(
            select(GenericEntity).where(
                GenericEntity.kb_id == kb_id,
                GenericEntity.entity_type == entity_type,
                GenericEntity.deleted_at.is_(None),
                GenericEntity.source_pk.in_(pk_list),
            )
        )
        return {e.source_pk: e for e in rows.scalars().all()}

    async def resolve_entities(
        self, kb_id: uuid.UUID, entity_type: str, pks: Iterable[str]
    ) -> dict[str, uuid.UUID]:
        """按 (kb, 类型, 原始主键) 查询实体 ID（跨表关系解析用）。"""
        pk_list = [p for p in pks if p]
        if not pk_list:
            return {}
        rows = await self.db.execute(
            select(GenericEntity.id, GenericEntity.source_pk).where(
                GenericEntity.kb_id == kb_id,
                GenericEntity.entity_type == entity_type,
                GenericEntity.deleted_at.is_(None),
                GenericEntity.source_pk.in_(pk_list),
            )
        )
        return {r.source_pk: r.id for r in rows.all()}

    # ── 关系 ────────────────────────────────────────────────────────────────

    async def create_relations(
        self,
        *,
        kb_id: uuid.UUID,
        triples: Sequence[tuple[uuid.UUID, uuid.UUID, str]],
        relation_type: str,
        status: str = KG_STATUS_PENDING,
        confidence: float = 1.0,
        provenance: str = "",
        batch_id: str | None = None,
        created_by: uuid.UUID | None = None,
        payloads: Sequence[dict[str, Any]] | None = None,
    ) -> int:
        """批量创建关系（跳过已存在的 (源, 目标, 类型)，保证幂等）。

        :param triples: (源实体ID, 目标实体ID, 关系类型) 三元组
        :return: 新建关系数
        """
        if status not in _VALID_STATUSES:
            raise ValueError(f"非法关系状态：{status}")
        if not triples:
            return 0

        # 先用唯一约束去重，再按批 INSERT ... ON CONFLICT DO NOTHING：
        # 十万级关系逐条 SELECT 会超时，且并发导入时 SELECT 判重不可靠。
        rows: list[dict[str, Any]] = []
        seen: set[tuple[uuid.UUID, uuid.UUID, str]] = set()
        for offset, (source_id, target_id, rel_type) in enumerate(triples):
            key = (source_id, target_id, rel_type)
            if key in seen:
                continue
            seen.add(key)
            rows.append(
                {
                    "id": uuid.uuid4(),
                    "kb_id": kb_id,
                    "source_entity_id": source_id,
                    "target_entity_id": target_id,
                    "relation_type": rel_type,
                    "payload": payloads[offset] if payloads and offset < len(payloads) else {},
                    "confidence": confidence,
                    "status": status,
                    "provenance": provenance[:255],
                    "import_batch_id": batch_id,
                    "created_by": created_by,
                }
            )

        created = 0
        table = GenericRelation.__table__
        for start in range(0, len(rows), BATCH_SIZE):
            stmt = (
                pg_insert(table)
                .values(rows[start : start + BATCH_SIZE])
                .on_conflict_do_nothing(constraint="uq_generic_relations_kb_src_dst_type")
                .returning(table.c.id)
            )
            created += len((await self.db.execute(stmt)).all())
        return created

    async def set_relation_status(
        self,
        *,
        kb_id: uuid.UUID,
        relation_id: uuid.UUID,
        status: str,
        user: User,
    ) -> GenericRelation:
        """人工确认 / 取消：修改单条关系状态（带知识库隔离与写权限校验）。"""
        if status not in _VALID_STATUSES:
            raise ValueError(f"非法关系状态：{status}")
        await self.require_write(user, kb_id)

        relation = await self.db.scalar(
            select(GenericRelation).where(
                GenericRelation.id == relation_id,
                GenericRelation.kb_id == kb_id,  # 隔离：跨库 id 直接查不到
            )
        )
        if relation is None:
            raise LookupError("关系不存在或不属于当前知识库")
        relation.status = status
        await self._sync_kg_edge_for(relation)
        await self.db.flush()
        return relation

    async def _sync_kg_edge_for(self, relation: GenericRelation) -> None:
        """关系状态变更后同步 KG 边：非 confirmed 必须把已写入的边撤掉。

        否则「先确认入库、再人工取消」会在图谱里留下幽灵边——
        那类边没有对应的已确认关系，检索时无法解释来源。
        """
        if relation.status == KG_STATUS_CONFIRMED:
            return
        rows = await self.db.execute(
            select(KgNode.id, KgNode.entity_id).where(
                KgNode.kb_id == relation.kb_id,
                KgNode.entity_id.in_([relation.source_entity_id, relation.target_entity_id]),
            )
        )
        mapping = {r.entity_id: r.id for r in rows.all()}
        source = mapping.get(relation.source_entity_id)
        target = mapping.get(relation.target_entity_id)
        if source is None or target is None:
            return
        edge = await self.db.scalar(
            select(KgEdge).where(
                KgEdge.source_node_id == source,
                KgEdge.target_node_id == target,
                KgEdge.relation_type == relation.relation_type,
            )
        )
        if edge is not None:
            await self.db.delete(edge)

    async def bulk_set_status(
        self,
        *,
        kb_id: uuid.UUID,
        relation_ids: Sequence[uuid.UUID],
        status: str,
        user: User,
    ) -> int:
        """批量确认 / 拒绝（人工确认界面的主操作）。"""
        await self.require_write(user, kb_id)
        updated = 0
        for relation_id in relation_ids:
            relation = await self.db.scalar(
                select(GenericRelation).where(
                    GenericRelation.id == relation_id,
                    GenericRelation.kb_id == kb_id,
                )
            )
            if relation is None:
                continue
            relation.status = status
            await self._sync_kg_edge_for(relation)
            updated += 1
        await self.db.flush()
        return updated

    # ── KG 同步（仅 confirmed）──────────────────────────────────────────────

    async def sync_confirmed_to_kg(
        self, kb_id: uuid.UUID, batch_id: str | None = None
    ) -> tuple[int, int]:
        """把 **已确认** 的关系同步进知识图谱；pending / rejected 一律不进。

        Returns:
            (新建节点数, 新建边数)
        """
        query = select(
            GenericRelation.source_entity_id,
            GenericRelation.target_entity_id,
            GenericRelation.relation_type,
            GenericRelation.provenance,
            GenericRelation.confidence,
        ).where(
            GenericRelation.kb_id == kb_id,
            GenericRelation.status == KG_STATUS_CONFIRMED,
            GenericRelation.deleted_at.is_(None),
        )
        if batch_id:
            query = query.where(GenericRelation.import_batch_id == batch_id)
        relations = (await self.db.execute(query)).all()
        if not relations:
            return 0, 0

        entity_ids: set[uuid.UUID] = set()
        for relation in relations:
            entity_ids.add(relation.source_entity_id)
            entity_ids.add(relation.target_entity_id)
        node_ids = await self._ensure_kg_nodes(kb_id, entity_ids)

        # 边同样批量写入：先按 (源, 目标, 类型) 去重，再依赖唯一约束忽略已存在的边
        edge_rows: list[dict[str, Any]] = []
        seen: set[tuple[uuid.UUID, uuid.UUID, str]] = set()
        for relation in relations:
            source_node = node_ids.get(relation.source_entity_id)
            target_node = node_ids.get(relation.target_entity_id)
            if source_node is None or target_node is None:
                continue
            key = (source_node, target_node, relation.relation_type)
            if key in seen:
                continue
            seen.add(key)
            edge_rows.append(
                {
                    "id": uuid.uuid4(),
                    "source_node_id": source_node,
                    "target_node_id": target_node,
                    "relation_type": relation.relation_type,
                    "provenance": (relation.provenance or "")[:64],
                    "description": "",
                    "kb_id": kb_id,
                    "status": KG_STATUS_CONFIRMED,
                    "confidence": relation.confidence,
                }
            )

        created_edges = 0
        table = KgEdge.__table__
        for start in range(0, len(edge_rows), BATCH_SIZE):
            stmt = (
                pg_insert(table)
                .values(edge_rows[start : start + BATCH_SIZE])
                .on_conflict_do_nothing(constraint="uq_kg_edges_source_target_relation")
                .returning(table.c.id)
            )
            created_edges += len((await self.db.execute(stmt)).all())
        logger.info(
            f"[entity_import] KG 同步完成：kb={kb_id} 节点 {len(node_ids)} 边 {created_edges}"
        )
        return len(node_ids), created_edges

    async def _ensure_kg_nodes(
        self, kb_id: uuid.UUID, entity_ids: Iterable[uuid.UUID]
    ) -> dict[uuid.UUID, uuid.UUID]:
        """为实体创建（或复用）KG 节点，返回 entity_id → node_id。"""
        ids = list(entity_ids)
        if not ids:
            return {}
        rows = await self.db.execute(
            select(KgNode.id, KgNode.entity_id).where(
                KgNode.kb_id == kb_id,
                KgNode.entity_id.in_(ids),
            )
        )
        mapping = {r.entity_id: r.id for r in rows.all()}
        if mapping:
            # 实体显示名可能在重新导入时被修正（如从主键值改为中文名），
            # 节点名必须跟着同步，否则图谱按 name/aliases 匹配仍会命中旧主键名。
            entity_name = (
                select(GenericEntity.name)
                .where(GenericEntity.id == KgNode.entity_id)
                .scalar_subquery()
            )
            await self.db.execute(
                update(KgNode)
                .where(
                    KgNode.id.in_(list(mapping.values())),
                    KgNode.name.is_distinct_from(func.left(entity_name, 128)),
                )
                .values(name=func.left(entity_name, 128))
            )

        missing = [i for i in ids if i not in mapping]
        if missing:
            entities = (
                await self.db.execute(
                    select(GenericEntity).where(GenericEntity.id.in_(missing))
                )
            ).scalars().all()
            created: list[tuple[uuid.UUID, KgNode]] = []
            for entity in entities:
                node = KgNode(
                    node_type=KG_NODE_TYPE_ENTITY,
                    kb_id=kb_id,
                    entity_id=entity.id,
                    name=entity.name[:128],
                    status=KG_STATUS_CONFIRMED,
                )
                self.db.add(node)
                created.append((entity.id, node))
            if created:
                await self.db.flush()
                for entity_id, node in created:
                    mapping[entity_id] = node.id
        return mapping

    # ── 关联导入编排 ────────────────────────────────────────────────────────

    async def import_relational(
        self,
        *,
        kb_id: uuid.UUID,
        user: User,
        job_id: uuid.UUID,
        rows_by_table: dict[str, list[ParsedRow]],
        files_by_table: dict[str, Any],
        primary_keys: dict[str, str],
        foreign_keys: Sequence[ForeignKeyCandidate],
        junctions: Sequence[JunctionCandidate],
        batch_id: str,
        auto_confirm: bool = False,
        progress: ImportProgress | None = None,
    ) -> ImportStats:
        """关联导入全流程：原始记录 → 实体 → 关系 →（确认后）KG。

        每个逻辑单元独立 SAVEPOINT：某张表或某组关系失败只回滚该单元，
        其他单元的数据与错误明细都会返回，便于前端逐条展示与重试。
        progress 只用于上报展示进度，写失败不影响导入结果。
        """
        stats = ImportStats()
        await self.require_write(user, kb_id)
        status = KG_STATUS_CONFIRMED if auto_confirm else KG_STATUS_PENDING

        # 进度权重：写入阶段按「原始记录行数 + 实体行数」计算，占 5%–55%；
        # 关系阶段按关系单元数占 55%–90%；业务资源表占 90%–95%；图谱同步占 95%–100%。
        total_rows = sum(len(rows) for rows in rows_by_table.values())
        entity_rows = sum(
            len(rows) for name, rows in rows_by_table.items() if primary_keys.get(name)
        )
        work_total = max(total_rows + entity_rows, 1)
        work_done = 0

        async def tick(n: int, label: str) -> None:
            nonlocal work_done
            work_done += n
            if progress is not None:
                await progress.update(
                    phase="write",
                    percent=5 + 50 * min(work_done, work_total) / work_total,
                    message=f"{label}（{min(work_done, work_total)}/{work_total} 行）",
                )

        if progress is not None:
            await progress.update(
                phase="write", percent=5, message="开始写入原始记录与实体", force=True
            )

        # 1) 原始记录 + 实体
        for table_name, rows in rows_by_table.items():
            file = files_by_table.get(table_name)
            file_hash = getattr(file, "file_hash", "") or ""
            file_id = getattr(file, "id", None)
            file_name = getattr(file, "file_name", table_name) or table_name

            async def on_record(n: int, _table: str = table_name) -> None:
                await tick(n, f"原始记录：{_table}")

            written, errors = await self.save_records(
                job_id=job_id,
                file_id=file_id,
                kb_id=kb_id,
                file_hash=file_hash,
                entity_type=table_name,
                rows=rows,
                file_name=file_name,
                on_batch=on_record,
            )
            stats.records += written
            stats.errors.extend(errors)

            # 无主键的表（如关联表）不建实体：它的行只作为原始记录与关系来源保留。
            # 若照样调用 upsert_entities，会把每一行都报成「缺少主键」的假失败。
            pk_field = primary_keys.get(table_name)
            if pk_field:
                async def on_entity(n: int, _table: str = table_name) -> None:
                    await tick(n, f"实体：{_table}")

                _, errors = await self.upsert_entities(
                    kb_id=kb_id,
                    entity_type=table_name,
                    rows=rows,
                    pk_field=pk_field,
                    file_id=file_id,
                    file_hash=file_hash,
                    batch_id=batch_id,
                    created_by=user.id,
                    file_name=file_name,
                    on_batch=on_entity,
                )
                stats.errors.extend(errors)

        # 2) 直接外键（1:1 / 1:N）
        # 关联表自身没有主键，它的外键由下面的关联表逻辑统一处理；
        # 若再按「直接外键」走一遍，会因拿不到子表主键而报错，产生两条假失败。
        junction_tables = {j.table for j in junctions}
        direct_fks = [fk for fk in foreign_keys if fk.child_table not in junction_tables]
        relation_units = len(direct_fks) + len(junctions)
        relation_done = 0

        async def relation_tick(label: str) -> None:
            nonlocal relation_done
            relation_done += 1
            if progress is not None:
                await progress.update(
                    phase="relations",
                    percent=55 + 35 * relation_done / max(relation_units, 1),
                    message=f"写入关系：{label}",
                )

        if progress is not None:
            await progress.update(
                phase="relations", percent=55, message="开始写入关系", force=True
            )

        for fk in direct_fks:
            try:
                async with self.db.begin_nested():
                    created = await self._link_direct(
                        kb_id, fk, rows_by_table, primary_keys, status, batch_id, user
                    )
                    stats.relations += created
            except Exception as exc:  # noqa: BLE001
                # 带上子表/父表上下文：没有表名的错误无法定位到具体关系
                stats.errors.append(
                    RowError(
                        fk.child_table,
                        0,
                        f"关系 {fk.child_table}.{fk.child_field}→"
                        f"{fk.parent_table}.{fk.parent_field} 写入失败：{exc}",
                    )
                )
                logger.warning(f"[entity_import] 直接关系写入失败，已回滚该单元: {exc}")
            await relation_tick(
                f"{fk.child_table}.{fk.child_field}→{fk.parent_table}.{fk.parent_field}"
            )

        # 3) 关联表（多对多）
        for junction in junctions:
            try:
                async with self.db.begin_nested():
                    created = await self._link_junction(
                        kb_id, junction, rows_by_table, status, batch_id, user
                    )
                    stats.relations += created
            except Exception as exc:  # noqa: BLE001
                stats.errors.append(
                    RowError(junction.table, 0, f"关联表 {junction.label} 写入失败：{exc}")
                )
                logger.warning(f"[entity_import] 关联表关系写入失败，已回滚该单元: {exc}")
            await relation_tick(f"关联表 {junction.label}")

        total_entities = await self._count_entities(kb_id, batch_id)
        stats.entities = total_entities

        # 4) 业务资源表：可识别的中药/方剂/理论/文献同时写入四张业务表，
        #    使前端四个模块可见；与关系是否确认无关（确认只影响知识图谱）。
        if progress is not None:
            await progress.update(
                phase="business", percent=90, message="写入业务资源表", force=True
            )

        async def business_tick(index: int, total: int, label: str) -> None:
            if progress is not None:
                await progress.update(
                    phase="business",
                    percent=90 + 5 * index / max(total, 1),
                    message=f"业务资源：{label}",
                )

        business = await BusinessResourceService(self.db).write_tables(
            rows_by_table=rows_by_table,
            files_by_table=files_by_table,
            batch_id=batch_id,
            on_table=business_tick,
        )
        stats.business_herbs = business.herbs
        stats.business_prescriptions = business.prescriptions
        stats.business_theories = business.theories
        stats.business_literatures = business.literatures
        stats.business_ingredients = business.ingredients
        stats.business_existing = business.existing
        stats.errors.extend(RowError(e.table, 0, e.message) for e in business.errors)

        # 5) 只有已确认的关系才进 KG
        if auto_confirm:
            if progress is not None:
                await progress.update(
                    phase="kg", percent=95, message="同步知识图谱（节点与边）", force=True
                )
            nodes, edges = await self.sync_confirmed_to_kg(kb_id, batch_id=batch_id)
            stats.kg_nodes = nodes
            stats.kg_edges = edges
            if progress is not None:
                await progress.update(
                    phase="kg",
                    percent=99,
                    message=f"图谱同步完成：节点 {nodes}、边 {edges}",
                    force=True,
                )

        return stats

    async def _link_direct(
        self,
        kb_id: uuid.UUID,
        fk: ForeignKeyCandidate,
        rows_by_table: dict[str, list[ParsedRow]],
        primary_keys: dict[str, str],
        status: str,
        batch_id: str,
        user: User,
    ) -> int:
        """子表行 → 父表实体：一条外键一行关系。"""
        child_rows = rows_by_table.get(fk.child_table, [])
        if not child_rows:
            return 0
        child_pk = primary_keys.get(fk.child_table)
        if not child_pk:
            raise ValueError(f"子表 {fk.child_table} 缺少主键，无法建立关系")

        # 子表实体按**子表自身主键**解析，父表实体按外键值解析（两者不是同一个字段）
        child_values = {str(r.data.get(child_pk, "")) for r in child_rows}
        child_values.discard("")
        parent_values = {str(r.data.get(fk.child_field, "")) for r in child_rows}
        parent_values.discard("")

        child_index = await self.resolve_entities(kb_id, fk.child_table, child_values)
        parent_index = await self.resolve_entities(kb_id, fk.parent_table, parent_values)

        triples: list[tuple[uuid.UUID, uuid.UUID, str]] = []
        for row in child_rows:
            child_pk_value = str(row.data.get(child_pk, ""))
            parent_value = str(row.data.get(fk.child_field, ""))
            if not child_pk_value or not parent_value:
                continue
            child_id = child_index.get(child_pk_value)
            parent_id = parent_index.get(parent_value)
            if child_id is None or parent_id is None:
                continue
            triples.append((child_id, parent_id, f"ref_{fk.parent_table}"))
        return await self.create_relations(
            kb_id=kb_id,
            triples=triples,
            relation_type=f"ref_{fk.parent_table}",
            status=status,
            confidence=fk.confidence,
            provenance=f"{fk.child_table}.{fk.child_field}→{fk.parent_table}.{fk.parent_field}",
            batch_id=batch_id,
            created_by=user.id,
        )

    async def _link_junction(
        self,
        kb_id: uuid.UUID,
        junction: JunctionCandidate,
        rows_by_table: dict[str, list[ParsedRow]],
        status: str,
        batch_id: str,
        user: User,
    ) -> int:
        """关联表行 → 两条父表实体之间的关系（多对多）。"""
        rows = rows_by_table.get(junction.table, [])
        if not rows:
            return 0

        left_values = {str(r.data.get(junction.left_field, "")) for r in rows}
        right_values = {str(r.data.get(junction.right_field, "")) for r in rows}
        left_values.discard("")
        right_values.discard("")

        left_index = await self.resolve_entities(kb_id, junction.left_table, left_values)
        right_index = await self.resolve_entities(kb_id, junction.right_table, right_values)

        triples: list[tuple[uuid.UUID, uuid.UUID, str]] = []
        payloads: list[dict[str, Any]] = []
        for row in rows:
            left = str(row.data.get(junction.left_field, ""))
            right = str(row.data.get(junction.right_field, ""))
            left_id = left_index.get(left)
            right_id = right_index.get(right)
            if left_id is None or right_id is None:
                continue
            triples.append((left_id, right_id, junction.relation_type))
            payloads.append(dict(row.data))
        return await self.create_relations(
            kb_id=kb_id,
            triples=triples,
            relation_type=junction.relation_type,
            status=status,
            confidence=junction.confidence,
            provenance=f"junction:{junction.table}({junction.left_field},{junction.right_field})",
            batch_id=batch_id,
            created_by=user.id,
            payloads=payloads,
        )

    async def _count_entities(self, kb_id: uuid.UUID, batch_id: str) -> int:
        result = await self.db.execute(
            select(GenericEntity.id).where(
                GenericEntity.kb_id == kb_id,
                GenericEntity.import_batch_id == batch_id,
            )
        )
        return len(result.all())
