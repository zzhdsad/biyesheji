"""向量化入库用例：加载切片 → BGE-M3 双路向量化 → Milvus 入库 → 状态回写 completed。

状态机（parse_status）：pending/parsing/failed → 解析 → success（切片就绪）
  → 向量化 → completed | failed（向量化失败，切片保留可重试）
"""

import asyncio
import uuid

from loguru import logger
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.config import settings
from src.core.exceptions import AppException, NotFoundError
from src.domain.models import Chunk, Document
from src.infrastructure.embedding import BaseEmbedding, EmbeddingError, get_embedding
from src.infrastructure.milvus_store import (
    BaseVectorStore,
    VectorRow,
    VectorStoreError,
    get_vector_store,
)


class IndexingService:
    """文档切片向量化编排（解析成功后执行；重复触发先清旧向量，幂等）。"""

    def __init__(
        self,
        db: AsyncSession,
        embedding: BaseEmbedding | None = None,
        store: BaseVectorStore | None = None,
    ) -> None:
        self.db = db
        self.embedding = embedding or get_embedding()
        self.store = store or get_vector_store()

    async def run(self, doc_id: uuid.UUID) -> Document:
        """向量化指定文档的全部切片并写入 Milvus。

        Raises:
            NotFoundError: 文档不存在
            AppException: 409 状态非 success（切片未就绪）/ 422 向量化失败（状态已置 failed）
        """
        doc = await self.db.get(Document, doc_id)
        if doc is None:
            raise NotFoundError("文档不存在")
        if doc.parse_status != "success":
            raise AppException(
                409, f"文档切片未就绪（parse_status={doc.parse_status}），请先完成解析"
            )

        chunks = (
            await self.db.scalars(select(Chunk).where(Chunk.doc_id == doc.id))
        ).all()
        if not chunks:
            msg = "向量化失败：文档无切片，请重新解析"
            await self._mark_failed(doc.id, msg)
            raise AppException(422, msg)

        chunks = sorted(chunks, key=lambda c: c.chunk_index)
        started_at = asyncio.get_event_loop().time()

        # CPU/GPU 密集的向量化放线程池，避免阻塞事件循环
        try:
            rows = await asyncio.to_thread(self._vectorize, chunks)
            await asyncio.to_thread(self._write_milvus, str(doc.id), rows)
        except (EmbeddingError, VectorStoreError) as exc:
            logger.error(f"文档向量化失败 doc_id={doc_id}: {exc}")
            await self._mark_failed(doc.id, f"向量化失败：{exc}")
            raise AppException(422, f"向量化失败：{exc}") from exc
        except Exception as exc:
            logger.exception(f"文档向量化异常 doc_id={doc_id}")
            await self._mark_failed(doc.id, f"向量化失败：{exc}")
            raise

        doc.parse_status = "completed"
        doc.error_message = ""
        await self.db.commit()
        await self.db.refresh(doc)

        elapsed_ms = int((asyncio.get_event_loop().time() - started_at) * 1000)
        logger.info(
            f"文档向量化完成 doc_id={doc.id} vectors={len(rows)} "
            f"embedding={type(self.embedding).__name__} 耗时={elapsed_ms}ms"
        )
        return doc

    def _vectorize(self, chunks: list[Chunk]) -> list[VectorRow]:
        """分批向量化并组装 Milvus 行。"""
        rows: list[VectorRow] = []
        batch_size = settings.VECTORIZE_BATCH_SIZE
        for i in range(0, len(chunks), batch_size):
            batch = chunks[i : i + batch_size]
            dense, sparse = self.embedding.encode([c.content for c in batch])
            for chunk, dv, sv in zip(batch, dense, sparse):
                rows.append(
                    VectorRow(
                        id=str(chunk.id),
                        doc_id=str(chunk.doc_id),
                        kb_id=str(chunk.kb_id),
                        chunk_index=chunk.chunk_index,
                        content=chunk.content,
                        page_num=chunk.page_num,
                        title_path=chunk.title_path,
                        dense_vector=dv,
                        sparse_vector=sv,
                    )
                )
        return rows

    def _write_milvus(self, doc_id: str, rows: list[VectorRow]) -> None:
        """幂等写入：先清该文档旧向量再插入。"""
        self.store.ensure_collection()
        self.store.delete_by_doc(doc_id)
        inserted = self.store.insert(rows)
        if inserted != len(rows):
            raise VectorStoreError(f"向量入库不完整：期望 {len(rows)} 条，实际 {inserted} 条")

    async def _mark_failed(self, doc_id: uuid.UUID, message: str) -> None:
        await self.db.execute(
            update(Document)
            .where(Document.id == doc_id)
            .values(parse_status="failed", error_message=message[:2000])
        )
        await self.db.commit()
