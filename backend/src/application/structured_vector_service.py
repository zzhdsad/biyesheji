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
from collections.abc import AsyncIterator, Awaitable, Callable
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


class VectorizeCancelled(Exception):
    """用户主动停止向量化；携带已写入条数，供断点续跑使用。"""

    def __init__(self, written: int = 0) -> None:
        super().__init__("vectorize cancelled")
        self.written = max(0, int(written))


VECTOR_CANCEL_MARKER = "已停止向量化，可继续"


def resume_checkpoint(status: str | None, count: int, error: str | None) -> int:
    """判断是否可以从上一次停止处续跑。

    只有「pending + 有已写入条数 + 停止标记」才算断点；完成后的再次向量化
    不应把旧计数当作断点，否则会漏写。"""
    if status != VECTOR_STATUS_PENDING:
        return 0
    if int(count or 0) <= 0:
        return 0
    if not str(error or "").startswith("已停止向量化"):
        return 0
    return int(count)


def _progress_chunk() -> int:
    """每批记录数：一次编码 + 一次进度回写（Milvus 内部再按 64 分片发送）。

    取 VECTORIZE_ENCODE_BATCH_SIZE（默认 256）：一次 Embedding 调用传入 256 条，
    FlagEmbedding 能在更大集合内按长度排序再切微批，动态 padding 的填充更少；
    Milvus 侧 insert_batch 仍按 VECTORIZE_BATCH_SIZE（64）分片，单条 RPC 大小
    不变。模型内部前向微批仍由 EMBEDDING_BATCH_SIZE 控制，所以合并批次不会抬高
    单次前向的 token 上限。
    """
    return max(int(settings.VECTORIZE_ENCODE_BATCH_SIZE or 0), 1)


async def _safe_progress(
    callback: Callable[[int], Awaitable[None]], count: int
) -> None:
    """进度回写失败不得影响向量化本身（进度只是展示数据）。"""
    try:
        await callback(count)
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"向量化进度回写失败 count={count}: {exc}")


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

    async def vectorize_document(
        self, doc_id: uuid.UUID, should_cancel: Callable[[], bool] | None = None
    ) -> int:
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

        # 续跑：只有「上次被用户停止」的 pending 才从已写入条数继续；
        # completed 后再次点击仍是整份重建，不能把旧计数当断点。
        resume_count = resume_checkpoint(
            parse.vector_status, parse.vector_count, parse.vector_error
        )
        resume_keep_ids = (
            await self._resume_record_vector_ids(doc.id, resume_count)
            if resume_count
            else set()
        )
        # 排队期间保持 pending，真正开始 Embedding 才置 indexing（前端可区分等待/执行）
        await self._set_parse_status(
            doc.id,
            VECTOR_STATUS_PENDING,
            resume_count,
            VECTOR_CANCEL_MARKER if resume_count else "",
        )
        semaphore_held = False
        try:
            semaphore_held = await asyncio.to_thread(_VECTOR_WRITE_SEM.acquire)
            await self._set_parse_status(
                doc.id, VECTOR_STATUS_INDEXING, resume_count, ""
            )
            try:
                await self._ensure_embedding()
                # 只取标量字段进生成器：批处理期间不保留整份 ORM 对象
                doc_pk, kb_id, file_name = doc.id, str(doc.kb_id), doc.file_name

                async def _on_progress(n: int) -> None:
                    # 每批写入后回写已入库条数，列表页进度条据此推进
                    await self._set_parse_status(
                        doc_pk, VECTOR_STATUS_INDEXING, n, ""
                    )

                total = await self._replace_in_batches(
                    doc_key,
                    lambda: self._document_batches(
                        doc_pk, kb_id, file_name, doc_key, skip=resume_count
                    ),
                    _on_progress,
                    should_cancel,
                    initial_count=resume_count,
                    keep_ids=resume_keep_ids,
                )
            except VectorizeCancelled as exc:
                # 用户主动停止：保留已写入向量和计数，下一次从这里继续。
                logger.info(
                    f"结构化向量化已被用户停止（保留断点）doc_id={doc.id} "
                    f"vectors={exc.written}"
                )
                await self._set_parse_status(
                    doc.id,
                    VECTOR_STATUS_PENDING,
                    exc.written,
                    VECTOR_CANCEL_MARKER,
                )
                return exc.written
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

    async def vectorize_group(
        self, group_id: uuid.UUID, should_cancel: Callable[[], bool] | None = None
    ) -> int:
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

        # 续跑规则同单文件：只有上次停止留下的 pending 计数才作为断点。
        resume_count = resume_checkpoint(
            group.vector_status, group.vector_count, group.vector_error
        )
        resume_keep_ids = (
            await self._resume_group_vector_ids(group.id, resume_count)
            if resume_count
            else set()
        )
        # 排队期间保持 pending，真正开始 Embedding 才置 indexing（前端可区分等待/执行）
        await self._set_group_status(
            group.id,
            VECTOR_STATUS_PENDING,
            resume_count,
            VECTOR_CANCEL_MARKER if resume_count else "",
        )
        semaphore_held = False
        try:
            semaphore_held = await asyncio.to_thread(_VECTOR_WRITE_SEM.acquire)
            await self._set_group_status(
                group.id, VECTOR_STATUS_INDEXING, resume_count, ""
            )
            try:
                await self._ensure_embedding()
                group_pk, kb_id = group.id, str(group.kb_id)
                group_name = group.name

                async def _on_progress(n: int) -> None:
                    # 每批写入后回写已入库条数，列表页进度条据此推进
                    await self._set_group_status(
                        group_pk, VECTOR_STATUS_INDEXING, n, ""
                    )

                total = await self._replace_in_batches(
                    group_key,
                    lambda: self._group_batches(
                        group_pk, kb_id, group_name, group_key, skip=resume_count
                    ),
                    _on_progress,
                    should_cancel,
                    initial_count=resume_count,
                    keep_ids=resume_keep_ids,
                )
            except VectorizeCancelled as exc:
                # 用户主动停止：保留已写入向量和计数，下一次从这里继续。
                logger.info(
                    f"关联记录向量化已被用户停止（保留断点）group_id={group.id} "
                    f"vectors={exc.written}"
                )
                await self._set_group_status(
                    group.id,
                    VECTOR_STATUS_PENDING,
                    exc.written,
                    VECTOR_CANCEL_MARKER,
                )
                return exc.written
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

    async def _resume_record_vector_ids(
        self, doc_id: uuid.UUID, count: int
    ) -> set[str]:
        """已写入断点对应的记录向量主键（用于避免续跑时被当成旧向量清掉）。"""
        if count <= 0:
            return set()
        rows = await self.db.scalars(
            select(StructuredRecord.id)
            .where(StructuredRecord.doc_id == doc_id)
            .order_by(StructuredRecord.seq_no.asc())
            .limit(count)
        )
        return {structured_vector_id(record_id) for record_id in rows}

    async def _resume_group_vector_ids(
        self, group_id: uuid.UUID, count: int
    ) -> set[str]:
        if count <= 0:
            return set()
        rows = await self.db.scalars(
            select(StructuredGroupRecord.id)
            .where(StructuredGroupRecord.group_id == group_id)
            .order_by(StructuredGroupRecord.seq_no.asc())
            .limit(count)
        )
        return {structured_group_vector_id(record_id) for record_id in rows}

    async def _document_batches(
        self,
        doc_id: uuid.UUID,
        kb_id: str,
        file_name: str,
        doc_key: str,
        *,
        skip: int = 0,
    ) -> AsyncIterator[list[VectorRow]]:
        """键集分页读记录 → 每批构造 VectorRow → 每批单独编码后 yield。

        与 StructuredGroupService._iter_record_batches 同一键集机制
        （seq_no > 上一批末位，(doc_id, seq_no) 有索引）：不重复、不遗漏，
        内存里只留当前一批。生成器可重复调用（重复向量回退时重放）。
        ``skip`` 仅用于断点续跑：按 seq_no 跳过已写入的前 N 条记录。
        """
        size = max(int(settings.STRUCTURED_VECTOR_READ_BATCH or 0), 1)
        last_seq: int | None = None
        first_page = True
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
            if first_page and skip > 0:
                stmt = stmt.offset(skip)
            first_page = False
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
                # 按批产出：一次编码 / 一次进度回写（Milvus 内部再按 64 分片）。
                # 本机 CPU 2 线程下 256 条短文本约 80 秒，进度条约每批推进一次；
                # 停止信号同样在批次之间检查，最多再等当前这一批。
                chunk = _progress_chunk()
                for start in range(0, len(batch), chunk):
                    yield await self._encode(batch[start : start + chunk])
            if len(rows) < size:
                return

    async def _group_batches(
        self,
        group_id: uuid.UUID,
        kb_id: str,
        group_name: str | None,
        group_key: str,
        *,
        skip: int = 0,
    ) -> AsyncIterator[list[VectorRow]]:
        """关联记录的同款分批生成器（整组记录不一次性加载）。"""
        size = max(int(settings.STRUCTURED_VECTOR_READ_BATCH or 0), 1)
        last_seq: int | None = None
        first_page = True
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
            if first_page and skip > 0:
                stmt = stmt.offset(skip)
            first_page = False
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
                chunk = _progress_chunk()
                for start in range(0, len(batch), chunk):
                    yield await self._encode(batch[start : start + chunk])
            if len(rows) < size:
                return

    async def _replace_in_batches(
        self,
        doc_key: str,
        make_batches: Callable[[], AsyncIterator[list[VectorRow]]],
        on_progress: Callable[[int], Awaitable[None]] | None = None,
        should_cancel: Callable[[], bool] | None = None,
        *,
        initial_count: int = 0,
        keep_ids: set[str] | None = None,
    ) -> int:
        """分批 staged 替换：先写入全部新向量，全部成功后才清理旧向量（BUG-046）。

        ``initial_count`` / ``keep_ids`` 用于断点续跑：调用方跳过已写入的前 N 条，
        这些向量在清理旧 id 时必须保留，不能被当成 stale 删除。
        """
        store = self.store
        await asyncio.to_thread(store.ensure_collection)
        old_ids = await asyncio.to_thread(store.ids_by_doc, doc_key)
        keep: set[str] = set(keep_ids or ())
        total = max(0, int(initial_count))
        async for batch in make_batches():
            # 批次之间检查停止信号：单批编码不可中断，但用户点击「停止」后
            # 最多再等当前一批完成就会退出；已写入的批次保留为断点。
            if should_cancel is not None and should_cancel():
                raise VectorizeCancelled(total)
            keep.update(r.id for r in batch)
            written = await asyncio.to_thread(store.insert_batch, batch, flush=False)
            if written != len(batch):
                raise VectorStoreError(
                    f"向量入库不完整：期望 {len(batch)} 条，实际 {written} 条"
                )
            total += written
            # 进度回写：每批结束后更新 vector_count，前端进度条据此推进
            if on_progress is not None:
                await _safe_progress(on_progress, total)

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
        if initial_count:
            # 续跑时生成器已跳过断点前缀，直接清旧重放会漏掉前缀；
            # 宁可置 failed 让用户整份重建，也不能悄悄丢数据。
            raise VectorStoreError(
                f"断点续跑条数不一致：期望 {total}，实际 {actual}；"
                "请重新执行整份向量化"
            )
        await asyncio.to_thread(store.delete_by_doc, doc_key)
        replayed = 0
        async for batch in make_batches():
            if should_cancel is not None and should_cancel():
                raise VectorizeCancelled(replayed)
            written = await asyncio.to_thread(store.insert_batch, batch, flush=False)
            if written != len(batch):
                raise VectorStoreError(
                    f"向量入库不完整：期望 {len(batch)} 条，实际 {written} 条"
                )
            replayed += written
            if on_progress is not None:
                await _safe_progress(on_progress, replayed)
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

        批内再按 VECTORIZE_ENCODE_BATCH_SIZE 细分调用 Embedding；不累积跨批的
        texts 列表或结果列表，处理完即返回原批次。
        """
        batch = _progress_chunk()
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
    """结构化解析成功 / 文件替换后：只把状态置 pending，**不自动入队**。

    向量化改为用户在页面点「开始向量化」手动触发：上传完一批文件先看解析结果与
    记录数，确认无误后再决定何时占用 CPU（避免一次上传把几十万条记录排进长队列）。
    """
    try:
        await db.execute(
            update(StructuredParse)
            .where(StructuredParse.doc_id == doc_id)
            .values(vector_status=VECTOR_STATUS_PENDING, vector_count=0, vector_error="")
        )
        await db.commit()
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"结构化向量状态更新失败（不影响解析结果）doc_id={doc_id}: {exc}")


async def sync_structured_group(db: AsyncSession, group_id: uuid.UUID) -> None:
    """文件组状态变化后同步向量：confirmed 只置 pending（等用户手动触发），
    其余状态立即清掉旧向量。

    未确认 / stale 的组必须**立即**退出检索，因此清理仍走同步路径（只是删除，
    没有 Embedding）；已确认组的整份重建不再自动入队——由页面「开始向量化」按钮
    调用 ``POST /structured/groups/{id}/revectorize`` 手动派发。
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
