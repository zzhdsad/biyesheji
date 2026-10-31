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
from typing import Any, Iterable, Sequence

from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

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
_DISPLAY_FIELD_HINTS: tuple[str, ...] = ("name", "名称", "标题", "题目", "title", "label")
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
    ) -> tuple[int, list[RowError]]:
        """保存原始记录（幂等：同一 kb + record_key 会被覆盖更新）。

        保留每行完整字段到 payload，不拼接文本、不丢字段、不翻译。
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
    ) -> tuple[dict[str, uuid.UUID], list[RowError]]:
        """按 source_pk 幂等写入实体，返回 (source_pk → entity_id)。"""
        index: dict[str, uuid.UUID] = {}
        errors: list[RowError] = []
        try:
            async with self.db.begin_nested():
                for start in range(0, len(rows), BATCH_SIZE):
                    batch = rows[start : start + BATCH_SIZE]
                    pks = {_pk_value(row.data, pk_field) for row in batch}
                    pks.discard("")
                    existing = await self._existing_entities(kb_id, entity_type, pks)
                    for row in batch:
                        pk = _pk_value(row.data, pk_field)
                        if not pk:
                            errors.append(
                                RowError(file_name, row.row_no, "缺少主键值，跳过该行实体")
                            )
                            continue
                        record_key = record_key_for(file_hash, row.row_no) if file_hash else None
                        current = existing.get(pk)
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
                            await self.db.flush()
                            index[pk] = entity.id
                        else:
                            current.name = _display_name(row.data, pk)
                            current.payload = dict(row.data)
                            current.source_row_no = row.row_no
                            current.import_batch_id = batch_id
                            if record_key:
                                current.record_key = record_key
                            await self.db.flush()
                            index[pk] = current.id
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
        created = 0
        for offset, (source_id, target_id, rel_type) in enumerate(triples):
            exists = await self.db.scalar(
                select(GenericRelation.id).where(
                    GenericRelation.kb_id == kb_id,
                    GenericRelation.source_entity_id == source_id,
                    GenericRelation.target_entity_id == target_id,
                    GenericRelation.relation_type == rel_type,
                )
            )
            if exists is not None:
                continue
            self.db.add(
                GenericRelation(
                    kb_id=kb_id,
                    source_entity_id=source_id,
                    target_entity_id=target_id,
                    relation_type=rel_type,
                    payload=(payloads[offset] if payloads and offset < len(payloads) else {}),
                    confidence=confidence,
                    status=status,
                    provenance=provenance[:255],
                    import_batch_id=batch_id,
                    created_by=created_by,
                )
            )
            created += 1
            if created % BATCH_SIZE == 0:
                await self.db.flush()
        await self.db.flush()
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
        query = select(GenericRelation).where(
            GenericRelation.kb_id == kb_id,
            GenericRelation.status == KG_STATUS_CONFIRMED,
            GenericRelation.deleted_at.is_(None),
        )
        if batch_id:
            query = query.where(GenericRelation.import_batch_id == batch_id)
        relations = (await self.db.execute(query)).scalars().all()
        if not relations:
            return 0, 0

        entity_ids: set[uuid.UUID] = set()
        for relation in relations:
            entity_ids.add(relation.source_entity_id)
            entity_ids.add(relation.target_entity_id)
        node_ids = await self._ensure_kg_nodes(kb_id, entity_ids)

        created_edges = 0
        for relation in relations:
            source_node = node_ids.get(relation.source_entity_id)
            target_node = node_ids.get(relation.target_entity_id)
            if source_node is None or target_node is None:
                continue
            exists = await self.db.scalar(
                select(KgEdge.id).where(
                    KgEdge.source_node_id == source_node,
                    KgEdge.target_node_id == target_node,
                    KgEdge.relation_type == relation.relation_type,
                )
            )
            if exists is not None:
                continue
            self.db.add(
                KgEdge(
                    source_node_id=source_node,
                    target_node_id=target_node,
                    relation_type=relation.relation_type,
                    provenance=relation.provenance[:64],
                    kb_id=kb_id,
                    status=KG_STATUS_CONFIRMED,
                    confidence=relation.confidence,
                )
            )
            created_edges += 1
            if created_edges % BATCH_SIZE == 0:
                await self.db.flush()
        await self.db.flush()
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

        missing = [i for i in ids if i not in mapping]
        if missing:
            entities = (
                await self.db.execute(
                    select(GenericEntity).where(GenericEntity.id.in_(missing))
                )
            ).scalars().all()
            for entity in entities:
                node = KgNode(
                    node_type=KG_NODE_TYPE_ENTITY,
                    kb_id=kb_id,
                    entity_id=entity.id,
                    name=entity.name[:128],
                    status=KG_STATUS_CONFIRMED,
                )
                self.db.add(node)
                await self.db.flush()
                mapping[entity.id] = node.id
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
    ) -> ImportStats:
        """关联导入全流程：原始记录 → 实体 → 关系 →（确认后）KG。

        每个逻辑单元独立 SAVEPOINT：某张表或某组关系失败只回滚该单元，
        其他单元的数据与错误明细都会返回，便于前端逐条展示与重试。
        """
        stats = ImportStats()
        await self.require_write(user, kb_id)
        status = KG_STATUS_CONFIRMED if auto_confirm else KG_STATUS_PENDING

        # 1) 原始记录 + 实体
        for table_name, rows in rows_by_table.items():
            file = files_by_table.get(table_name)
            file_hash = getattr(file, "file_hash", "") or ""
            file_id = getattr(file, "id", None)
            file_name = getattr(file, "file_name", table_name) or table_name

            written, errors = await self.save_records(
                job_id=job_id,
                file_id=file_id,
                kb_id=kb_id,
                file_hash=file_hash,
                entity_type=table_name,
                rows=rows,
                file_name=file_name,
            )
            stats.records += written
            stats.errors.extend(errors)

            # 无主键的表（如关联表）不建实体：它的行只作为原始记录与关系来源保留。
            # 若照样调用 upsert_entities，会把每一行都报成「缺少主键」的假失败。
            pk_field = primary_keys.get(table_name)
            if pk_field:
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
                )
                stats.errors.extend(errors)

        # 2) 直接外键（1:1 / 1:N）
        # 关联表自身没有主键，它的外键由下面的关联表逻辑统一处理；
        # 若再按「直接外键」走一遍，会因拿不到子表主键而报错，产生两条假失败。
        junction_tables = {j.table for j in junctions}
        for fk in foreign_keys:
            if fk.child_table in junction_tables:
                continue
            try:
                async with self.db.begin_nested():
                    created = await self._link_direct(kb_id, fk, rows_by_table, primary_keys, status, batch_id, user)
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

        total_entities = await self._count_entities(kb_id, batch_id)
        stats.entities = total_entities

        # 4) 只有已确认的关系才进 KG
        if auto_confirm:
            nodes, edges = await self.sync_confirmed_to_kg(kb_id, batch_id=batch_id)
            stats.kg_nodes = nodes
            stats.kg_edges = edges

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
