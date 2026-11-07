"""第六阶段：结构化知识（非关联记录 / 关联记录）接入统一向量索引。

设计边界（阶段六要求 1–4 / 7–8）：
- **复用**第二阶段的 BGE-M3 Embedding 与 Milvus collection（``document_chunks``），
  不新建向量库、不新增加密/切片流程；普通文档的向量化链路完全不动。
- 一条有效记录一个向量：非关联数据用 ``StructuredRecord.knowledge_text``，
  关联数据用 ``StructuredGroupRecord.knowledge_text``；不重新解析原始文件，
  也不把整份文件拼成一个向量。
- ID 隔离：结构化记录用 ``sr:<record_id>`` 作为向量主键、``sr:<doc_id>`` 作为
  doc_id；关联记录用 ``sgr:<record_id>`` / ``sgr:<group_id>``。前缀保证与
  文档切片（UUID）、资源向量（sha256）不会冲突，且 ``delete_by_doc`` 与分批替换
  天然按文件 / 文件组幂等。
- 来源标识：Milvus 动态字段写 ``source_kind``（structured / structured_group）、
  ``source_id``（原始记录 ID）、``source_name``（文件名 / 文件组名）与
  ``embedding_model``；老集合（无动态字段）自动降级，检索期由文档 / 组 ID
  回表 PostgreSQL 补齐（doc_id 前缀本身即来源类型标识）。
- 状态：结构化解析概览 / 文件组各自维护 pending → indexing → completed / failed，
  失败原因如实落库；重新向量化走分批 staged 替换（键集分页读一批 → 编码一批 →
  写一批，全部新向量写入成功后才清理旧向量，BUG-046 语义），
  清理旧向量之前任何一批失败，旧向量都保持不动。
- 关系约束：只有 ``status == confirmed`` 的文件组才允许写入向量；未确认 /
  stale 的组在同步时直接清掉旧向量，绝不把推测定论当作已确认知识。

本模块只被服务层调用（解析成功、组确认/失效、彻底删除、手动重新向量化），
失败一律不向业务链路抛出：图谱/排序/解析是业务事实，向量索引是可重建的派生物。
"""

from __future__ import annotations

import asyncio
import threading
import uuid
from collections.abc import AsyncIterator, Callable
from datetime import datetime, timezone

from loguru import logger
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.config import settings
from src.core.file_spaces import STRUCTURED_SPACE
from src.domain.models import (
    STRUCTURED_GROUP_CONFIRMED,
    STRUCTURED_PARSE_SUCCESS,
    VECTOR_STATUS_COMPLETED,
    VECTOR_STATUS_FAILED,
    VECTOR_STATUS_INDEXING,
    VECTOR_STATUS_PENDING,
    Document,
    StructuredGroup,
    StructuredGroupRecord,
    StructuredParse,
    StructuredRecord,
)
from src.infrastructure.embedding import BaseEmbedding, get_embedding
from src.infrastructure.milvus_store import (
    _BULK_ID_CHUNK,
    BaseVectorStore,
    VectorRow,
    VectorStoreError,
    get_vector_store,
)

# 向量 ID / doc_id 前缀（来源类型标识；同时保证与其他来源不撞主键）
STRUCTURED_DOC_PREFIX = "sr:"
STRUCTURED_GROUP_DOC_PREFIX = "sgr:"

# Milvus 动态字段中的来源类别
SOURCE_KIND_STRUCTURED = "structured"
SOURCE_KIND_STRUCTURED_GROUP = "structured_group"

_MAX_ERROR_CHARS = 2000

# 结构化向量化全局唯一：队列本身已是单消费者，这里再兜一层，保证任何直接调用
# （脚本、测试、sync_vectors=True）也不会与后台任务同时跑 BGE-M3。
_VECTOR_WRITE_SEM = threading.Semaphore(1)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def structured_doc_id(doc_id: uuid.UUID | str) -> str:
    """结构化文件的向量分组键（非 UUID，避免被当成普通文档行）。"""
    return f"{STRUCTURED_DOC_PREFIX}{doc_id}"


def structured_group_doc_id(group_id: uuid.UUID | str) -> str:
    """关联文件组的向量分组键。"""
    return f"{STRUCTURED_GROUP_DOC_PREFIX}{group_id}"


def structured_vector_id(record_id: uuid.UUID | str) -> str:
    """一条结构化记录对应的向量主键（≤ 64 字符，满足 Milvus VARCHAR(64)）。"""
    return f"{STRUCTURED_DOC_PREFIX}{record_id}"


def structured_group_vector_id(record_id: uuid.UUID | str) -> str:
    """一条关联记录对应的向量主键。"""
    return f"{STRUCTURED_GROUP_DOC_PREFIX}{record_id}"


def is_structured_vector_id(value: object) -> bool:
    return str(value or "").startswith(STRUCTURED_DOC_PREFIX)


def is_structured_group_vector_id(value: object) -> bool:
    return str(value or "").startswith(STRUCTURED_GROUP_DOC_PREFIX)


def is_structured_doc_key(value: object) -> bool:
    """doc_id 是否为结构化文件键（``sr:<doc_uuid>``）。"""
    raw = str(value or "")
    return raw.startswith(STRUCTURED_DOC_PREFIX)


def is_structured_group_doc_key(value: object) -> bool:
    raw = str(value or "")
    return raw.startswith(STRUCTURED_GROUP_DOC_PREFIX)


def _uuid_after_prefix(value: object, prefix: str) -> uuid.UUID | None:
    raw = str(value or "")
    if not raw.startswith(prefix):
        return None
    try:
        return uuid.UUID(raw[len(prefix) :])
    except (ValueError, TypeError):
        return None


def record_uuid_of_vector_id(vector_id: object) -> uuid.UUID | None:
    """``sr:<record_uuid>`` → record_uuid（其它来源返回 None）。"""
    return _uuid_after_prefix(vector_id, STRUCTURED_DOC_PREFIX)


def group_record_uuid_of_vector_id(vector_id: object) -> uuid.UUID | None:
    return _uuid_after_prefix(vector_id, STRUCTURED_GROUP_DOC_PREFIX)


def doc_uuid_of_structured_key(doc_id: object) -> uuid.UUID | None:
    return _uuid_after_prefix(doc_id, STRUCTURED_DOC_PREFIX)


def group_uuid_of_doc_key(doc_id: object) -> uuid.UUID | None:
    return _uuid_after_prefix(doc_id, STRUCTURED_GROUP_DOC_PREFIX)


def locate_text(row: StructuredRecord) -> str:
    """记录定位文案：JSON 给路径，表格给「工作表 · 第 N 行」。"""
    if row.json_path:
        return str(row.json_path)
    sheet = f"{row.sheet_name} · " if row.sheet_name else ""
    return f"{sheet}第 {row.row_no} 行"


class StructuredVectorService:
    """结构化 / 关联知识记录的向量化与清理（复用现有 Embedding + Milvus）。"""

    def __init__(
        self,
        db: AsyncSession,
        embedding: BaseEmbedding | None = None,
        store: BaseVectorStore | None = None,
    ) -> None:
        self.db = db
        self.embedding = embedding
        self.store = store or get_vector_store()
        self.model_key = ""
        self._config_loaded = False

    async def _ensure_embedding(self) -> None:
        if self._config_loaded:
            return
        self._config_loaded = True
        if self.embedding is not None:
            from src.application.vector_model import key_from_config

            self.model_key = key_from_config(None)
            return
        from src.application.model_config_service import get_effective_config_cached
        from src.application.vector_model import key_from_config

        cfg = await get_effective_config_cached(self.db)
        self.embedding = get_embedding(cfg)
        self.model_key = key_from_config(cfg)

    # ── 非关联结构化：按文件整份替换 ──────────────────────────────────────

    async def vectorize_document(self, doc_id: uuid.UUID) -> int:
        """把该文件的全部有效知识记录写入统一向量索引（按文件幂等替换，分批流式）。

        记录按键集分页逐批读取：每批构造 VectorRow、每批单独编码、每批单独写入，
        内存里任一时刻只有一批记录与向量，不随文件总行数增长。
        """
        doc_key = structured_doc_id(doc_id)
        doc = await self.db.get(Document, doc_id)
        if doc is None:
            await self._delete_doc_vectors(doc_key)
            return 0
        if doc.space != STRUCTURED_SPACE or doc.deleted_at is not None:
            # 不属于结构化空间 / 已进回收站：旧向量不得参与检索
            await self._delete_doc_vectors(doc_key)
            await self._set_parse_status(doc.id, VECTOR_STATUS_PENDING, 0, "")
            return 0

        parse = await self.db.scalar(
            select(StructuredParse).where(StructuredParse.doc_id == doc.id)
        )
        if parse is None or parse.status != STRUCTURED_PARSE_SUCCESS:
            # 解析尚未成功：旧记录仍在（失败保留旧结果），旧向量继续有效；
            # 完全没有解析结果时才清理残留向量。
            if parse is None:
                await self._delete_doc_vectors(doc_key)
                await self._set_parse_status(doc.id, VECTOR_STATUS_PENDING, 0, "")
            return 0

        # 排队期间保持 pending，真正开始 Embedding 才置 indexing（前端可区分等待/执行）
        await self._set_parse_status(doc.id, VECTOR_STATUS_PENDING, parse.vector_count, "")
        semaphore_held = False
        try:
            semaphore_held = await asyncio.to_thread(_VECTOR_WRITE_SEM.acquire)
            await self._set_parse_status(
                doc.id, VECTOR_STATUS_INDEXING, parse.vector_count, ""
            )
            try:
                await self._ensure_embedding()
                # 只取标量字段进生成器：批处理期间不保留整份 ORM 对象
                doc_pk, kb_id, file_name = doc.id, str(doc.kb_id), doc.file_name
                total = await self._replace_in_batches(
                    doc_key,
                    lambda: self._document_batches(doc_pk, kb_id, file_name, doc_key),
                )
            except Exception as exc:  # noqa: BLE001 - 记录失败原因；清旧之前的失败不触碰旧索引
                logger.error(f"结构化记录向量化失败 doc_id={doc.id}: {exc}")
                await self._set_parse_status(
                    doc.id, VECTOR_STATUS_FAILED, 0, str(exc), model=self.model_key or None
                )
                return 0

            await self._set_parse_status(
                doc.id,
                VECTOR_STATUS_COMPLETED,
                total,
                "",
                model=self.model_key or None,
                vectorized=True,
            )
            logger.info(
                f"结构化记录向量化完成 doc_id={doc.id} vectors={total} "
                f"model={self.model_key}"
            )
            return total
        finally:
            if semaphore_held:
                _VECTOR_WRITE_SEM.release()

    # ── 关联结构化：仅 confirmed 组允许写入 ───────────────────────────────

    async def vectorize_group(self, group_id: uuid.UUID) -> int:
        """把已确认（confirmed）文件组的关联记录写入统一向量索引（分批流式）。"""
        group_key = structured_group_doc_id(group_id)
        group = await self.db.get(StructuredGroup, group_id)
        if group is None:
            await self._delete_doc_vectors(group_key)
            return 0
        if group.status != STRUCTURED_GROUP_CONFIRMED:
            # 未确认 / stale：关系不成立或已失效，旧向量不得继续参与检索
            await self._delete_doc_vectors(group_key)
            await self._set_group_status(
                group.id, VECTOR_STATUS_PENDING, 0, "关联关系未确认或已失效，不写入向量索引"
            )
            return 0

        # 排队期间保持 pending，真正开始 Embedding 才置 indexing（前端可区分等待/执行）
        await self._set_group_status(group.id, VECTOR_STATUS_PENDING, group.vector_count, "")
        semaphore_held = False
        try:
            semaphore_held = await asyncio.to_thread(_VECTOR_WRITE_SEM.acquire)
            await self._set_group_status(
                group.id, VECTOR_STATUS_INDEXING, group.vector_count, ""
            )
            try:
                await self._ensure_embedding()
                group_pk, kb_id = group.id, str(group.kb_id)
                group_name = group.name
                total = await self._replace_in_batches(
                    group_key,
                    lambda: self._group_batches(group_pk, kb_id, group_name, group_key),
                )
            except Exception as exc:  # noqa: BLE001 - 记录失败原因；清旧之前的失败不触碰旧索引
                logger.error(f"关联记录向量化失败 group_id={group.id}: {exc}")
                await self._set_group_status(
                    group.id, VECTOR_STATUS_FAILED, 0, str(exc), model=self.model_key or None
                )
                return 0

            await self._set_group_status(
                group.id,
                VECTOR_STATUS_COMPLETED,
                total,
                "",
                model=self.model_key or None,
                vectorized=True,
            )
            logger.info(
                f"关联记录向量化完成 group_id={group.id} vectors={total} "
                f"model={self.model_key}"
            )
            return total
        finally:
            if semaphore_held:
                _VECTOR_WRITE_SEM.release()

    # ── 内部工具 ──────────────────────────────────────────────────────────

    async def _document_batches(
        self, doc_id: uuid.UUID, kb_id: str, file_name: str, doc_key: str
    ) -> AsyncIterator[list[VectorRow]]:
        """键集分页读记录 → 每批构造 VectorRow → 每批单独编码后 yield。

        与 StructuredGroupService._iter_record_batches 同一键集机制
        （seq_no > 上一批末位，(doc_id, seq_no) 有索引）：不重复、不遗漏，
        内存里只留当前一批。生成器可重复调用（重复向量回退时重放）。
        """
        size = max(int(settings.STRUCTURED_VECTOR_READ_BATCH or 0), 1)
        last_seq: int | None = None
        while True:
            stmt = (
                select(
                    StructuredRecord.id,
                    StructuredRecord.seq_no,
                    StructuredRecord.knowledge_text,
                    StructuredRecord.row_no,
                    StructuredRecord.sheet_name,
                    StructuredRecord.json_path,
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
            last_seq = int(rows[-1].seq_no)
            batch = [
                VectorRow(
                    id=structured_vector_id(r.id),
                    doc_id=doc_key,
                    kb_id=kb_id,
                    chunk_index=0,
                    content=(r.knowledge_text or "").strip(),
                    page_num=None,
                    title_path=locate_text(r),
                    dense_vector=[],
                    source_kind=SOURCE_KIND_STRUCTURED,
                    source_id=str(r.id),
                    source_name=file_name,
                    embedding_model=self.model_key or None,
                )
                for r in rows
                if (r.knowledge_text or "").strip()
            ]
            if batch:
                yield await self._encode(batch)
            if len(rows) < size:
                return

    async def _group_batches(
        self, group_id: uuid.UUID, kb_id: str, group_name: str | None, group_key: str
    ) -> AsyncIterator[list[VectorRow]]:
        """关联记录的同款分批生成器（整组记录不一次性加载）。"""
        size = max(int(settings.STRUCTURED_VECTOR_READ_BATCH or 0), 1)
        last_seq: int | None = None
        while True:
            stmt = (
                select(
                    StructuredGroupRecord.id,
                    StructuredGroupRecord.seq_no,
                    StructuredGroupRecord.knowledge_text,
                )
                .where(StructuredGroupRecord.group_id == group_id)
                .order_by(StructuredGroupRecord.seq_no.asc())
                .limit(size)
            )
            if last_seq is not None:
                stmt = stmt.where(StructuredGroupRecord.seq_no > last_seq)
            rows = (await self.db.execute(stmt)).all()
            if not rows:
                return
            last_seq = int(rows[-1].seq_no)
            batch = [
                VectorRow(
                    id=structured_group_vector_id(r.id),
                    doc_id=group_key,
                    kb_id=kb_id,
                    chunk_index=0,
                    content=(r.knowledge_text or "").strip(),
                    page_num=None,
                    title_path=group_name or "关联记录",
                    dense_vector=[],
                    source_kind=SOURCE_KIND_STRUCTURED_GROUP,
                    source_id=str(r.id),
                    source_name=group_name or "关联文件组",
                    embedding_model=self.model_key or None,
                )
                for r in rows
                if (r.knowledge_text or "").strip()
            ]
            if batch:
                yield await self._encode(batch)
            if len(rows) < size:
                return

    async def _replace_in_batches(
        self,
        doc_key: str,
        make_batches: Callable[[], AsyncIterator[list[VectorRow]]],
    ) -> int:
        """分批 staged 替换：先写入全部新向量，全部成功后才清理旧向量（BUG-046）。

        与 replace_doc / replace_docs_bulk 的一致性语义，只是行按批流式进出：
        1. 快照旧向量 id；
        2. 逐批读取/编码/写入（flush=False）——任何一批失败立即抛出，
           此时尚未删除任何旧向量，旧索引保持完整；
        3. 全部成功后只删「旧 id 中不在新 id 集合里」的分片，统一 flush；
        4. 校验条数：底层若把同主键行当新行插入（Milvus insert 允许重复主键，
           InMemory 则是覆盖），回退「清旧写新」并重放批生成器（重读 + 重编码）。

        内存中只保留当前批次的记录/向量与全量 id 短字符串（keep / 旧 id 快照）。
        """
        store = self.store
        await asyncio.to_thread(store.ensure_collection)
        old_ids = await asyncio.to_thread(store.ids_by_doc, doc_key)
        keep: set[str] = set()
        total = 0
        async for batch in make_batches():
            keep.update(r.id for r in batch)
            written = await asyncio.to_thread(store.insert_batch, batch, flush=False)
            if written != len(batch):
                raise VectorStoreError(
                    f"向量入库不完整：期望 {len(batch)} 条，实际 {written} 条"
                )
            total += written

        stale = [i for i in old_ids if i not in keep]
        await asyncio.to_thread(self._delete_stale_and_flush, stale)
        actual = await asyncio.to_thread(store.count_by_doc, doc_key)
        if actual == total:
            return total

        # 重复行回退：新向量已全部写入成功，此时清旧重写不会丢失"有效旧知识"
        # （重写内容与旧向量同源）；重放失败则抛错置 failed，等待下次重建。
        logger.warning(
            f"分批替换后条数不一致 doc_id={doc_key} 期望={total} 实际={actual}，"
            "回退为清旧写新（重放批次）"
        )
        await asyncio.to_thread(store.delete_by_doc, doc_key)
        replayed = 0
        async for batch in make_batches():
            written = await asyncio.to_thread(store.insert_batch, batch, flush=False)
            if written != len(batch):
                raise VectorStoreError(
                    f"向量入库不完整：期望 {len(batch)} 条，实际 {written} 条"
                )
            replayed += written
        await asyncio.to_thread(store.flush)
        actual = await asyncio.to_thread(store.count_by_doc, doc_key)
        if actual != replayed:
            raise VectorStoreError(
                f"向量入库不完整：期望 {replayed} 条，实际 {actual} 条"
            )
        return replayed

    def _delete_stale_and_flush(self, stale: list[str]) -> None:
        """删除过期分片并统一落盘（删除在 flush 前对查询不可见，校验必须先 flush）。"""
        for i in range(0, len(stale), _BULK_ID_CHUNK):
            self.store.delete_by_ids(stale[i : i + _BULK_ID_CHUNK])
        self.store.flush()

    async def _encode(self, rows: list[VectorRow]) -> list[VectorRow]:
        """为**当前一批**生成 dense + sparse 向量（CPU/GPU 密集，放线程池）。

        批内再按 VECTORIZE_BATCH_SIZE 细分调用 Embedding；不累积跨批的
        texts 列表或结果列表，处理完即返回原批次。
        """
        batch = max(int(settings.VECTORIZE_BATCH_SIZE or 0), 1)
        for start in range(0, len(rows), batch):
            chunk = rows[start : start + batch]
            dense, sparse = await asyncio.to_thread(
                self.embedding.encode, [r.content for r in chunk]  # type: ignore[union-attr]
            )
            if len(dense) != len(chunk) or len(sparse) != len(chunk):
                raise RuntimeError(
                    f"Embedding 输出数量不一致：期望 {len(chunk)}，"
                    f"dense={len(dense)} sparse={len(sparse)}"
                )
            for row, dv, sv in zip(chunk, dense, sparse):
                row.dense_vector = dv
                row.sparse_vector = sv
        return rows

    async def _delete_doc_vectors(self, doc_key: str) -> None:
        try:
            await asyncio.to_thread(self.store.delete_by_doc, doc_key)
        except Exception as exc:  # noqa: BLE001 - 清理失败不阻断业务操作（检索侧仍有 PG 校验）
            logger.warning(f"结构化向量清理失败 doc_id={doc_key}: {exc}")

    async def _set_parse_status(
        self,
        doc_id: uuid.UUID,
        status: str,
        count: int,
        error: str,
        *,
        model: str | None = None,
        vectorized: bool = False,
    ) -> None:
        values: dict = {
            "vector_status": status,
            "vector_count": count,
            "vector_error": (error or "")[:_MAX_ERROR_CHARS],
        }
        if model is not None:
            values["vector_model"] = model
        if vectorized:
            values["vectorized_at"] = _now()
        await self.db.execute(
            update(StructuredParse).where(StructuredParse.doc_id == doc_id).values(**values)
        )
        await self.db.commit()

    async def _set_group_status(
        self,
        group_id: uuid.UUID,
        status: str,
        count: int,
        error: str,
        *,
        model: str | None = None,
        vectorized: bool = False,
    ) -> None:
        values: dict = {
            "vector_status": status,
            "vector_count": count,
            "vector_error": (error or "")[:_MAX_ERROR_CHARS],
        }
        if model is not None:
            values["vector_model"] = model
        if vectorized:
            values["vectorized_at"] = _now()
        await self.db.execute(
            update(StructuredGroup)
            .where(StructuredGroup.id == group_id)
            .values(**values)
            .execution_options(synchronize_session=False)
        )
        await self.db.commit()


# ── 调用入口（永不向业务链路抛异常）─────────────────────────────────────────


async def sync_structured_document(db: AsyncSession, doc_id: uuid.UUID) -> None:
    """结构化解析成功 / 文件替换后：置 pending 并入队，不在调用线程里跑 Embedding。"""
    try:
        await db.execute(
            update(StructuredParse)
            .where(StructuredParse.doc_id == doc_id)
            .values(vector_status=VECTOR_STATUS_PENDING, vector_count=0, vector_error="")
        )
        await db.commit()
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"结构化向量状态更新失败（不影响解析结果）doc_id={doc_id}: {exc}")
        return

    from src.application.structured_runner import enqueue_structured_document

    enqueue_structured_document(str(doc_id))


async def sync_structured_group(db: AsyncSession, group_id: uuid.UUID) -> None:
    """文件组状态变化后同步向量：confirmed 入队重建，其余状态立即清掉旧向量。

    未确认 / stale 的组必须**立即**退出检索，因此清理仍走同步路径（只是删除，
    没有 Embedding）；只有已确认组的整份重建交给单消费者队列。
    """
    try:
        group = await db.get(StructuredGroup, group_id)
        if group is not None and group.status == STRUCTURED_GROUP_CONFIRMED:
            await db.execute(
                update(StructuredGroup)
                .where(StructuredGroup.id == group_id)
                .values(vector_status=VECTOR_STATUS_PENDING, vector_error="")
            )
            await db.commit()
            from src.application.structured_runner import enqueue_structured_group

            enqueue_structured_group(str(group_id))
            return
        await StructuredVectorService(db).vectorize_group(group_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"关联向量同步失败（不影响已生成记录）group_id={group_id}: {exc}")


async def clear_structured_group_vectors(group_id: uuid.UUID) -> None:
    """删除文件组的全部向量（组被删除时调用；失败只记日志）。"""
    try:
        await asyncio.to_thread(
            get_vector_store().delete_by_doc, structured_group_doc_id(group_id)
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"关联向量清理失败 group_id={group_id}: {exc}")


async def clear_structured_document_vectors(doc_id: uuid.UUID) -> None:
    """删除结构化文件的全部向量（文件被彻底删除时调用；失败只记日志）。"""
    try:
        await asyncio.to_thread(
            get_vector_store().delete_by_doc, structured_doc_id(doc_id)
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"结构化向量清理失败 doc_id={doc_id}: {exc}")
