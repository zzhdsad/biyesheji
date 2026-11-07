"""向量化入库用例：加载切片 → BGE-M3 双路向量化 → Milvus 入库 → 状态回写 completed。

状态机（parse_status）：pending/parsing/failed → 解析 → success（切片就绪）
  → 向量化 → completed | failed（向量化失败，切片保留可重试）
"""

import asyncio
import uuid
from datetime import datetime, timezone

from loguru import logger
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.config import settings
from src.core.exceptions import AppException, NotFoundError
from src.core.file_spaces import is_parse_enabled
from src.domain.models import (
    INDEX_STATUS_FAILED,
    INDEX_STATUS_INDEXED,
    Chunk,
    Document,
)
from src.infrastructure.embedding import BaseEmbedding, EmbeddingError, get_embedding
from src.infrastructure.milvus_store import (
    BaseVectorStore,
    VectorRow,
    VectorStoreError,
    get_vector_store,
)
from src.application.model_config_service import get_effective_config_cached
from src.application.vector_model import key_from_config


class IndexingService:
    """文档切片向量化编排（解析成功后执行；重复触发先清旧向量，幂等）。"""

    def __init__(
        self,
        db: AsyncSession,
        embedding: BaseEmbedding | None = None,
        store: BaseVectorStore | None = None,
    ) -> None:
        self.db = db
        self.embedding = embedding
        self.store = store or get_vector_store()
        self._config_loaded = False
        # 本次向量化使用的 embedding 模型标识（`backend:model`），成功后写入
        # documents.vector_model 与 Milvus 动态字段，供"模型切换后识别旧向量"用。
        self.model_key: str = ""

    async def _ensure_embedding(self) -> None:
        if self._config_loaded:
            return
        self._config_loaded = True
        if self.embedding is None:
            cfg = await get_effective_config_cached(self.db)
            self.embedding = get_embedding(cfg)
            self.model_key = key_from_config(cfg)
        else:
            # 注入实现（测试/资源向量化场景）：回退到 env 配置推导标识
            self.model_key = key_from_config(None)

    async def run(self, doc_id: uuid.UUID) -> Document:
        """向量化指定文档的全部切片并写入 Milvus。

        Raises:
            NotFoundError: 文档不存在
            AppException: 409 状态非 success（切片未就绪）/ 422 向量化失败（状态已置 failed）

        空间守卫：只有 ``space=document``（普通文档空间）的文档可被向量化。
        结构化数据空间的文档在此直接拒绝，且**不回写 failed 状态**（保持
        skipped 语义），避免误入向量库或污染解析状态。
        """
        doc = await self.db.get(Document, doc_id)
        if doc is None:
            raise NotFoundError("文档不存在")
        if not is_parse_enabled(doc.space):
            raise AppException(
                400, f"文档所在空间（space={doc.space}）不参与解析与向量化"
            )
        # 回收站中的文件不再写入 Milvus：否则已删除内容仍会被检索命中，
        # 且还原后向量与文件状态对不上（服务层兜底，不依赖路由入口校验）
        if doc.deleted_at is not None:
            raise AppException(400, "文件已在回收站中，无法向量化")
        # success（切片就绪）/ completed（重建向量）/ failed（向量化失败重试，切片保留）
        # 均可向量化；failed 但无切片时由下方"文档无切片"分支兜底报 422
        if doc.parse_status not in ("success", "completed", "failed"):
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

        # 惰性加载运行时配置并初始化 embedding
        await self._ensure_embedding()

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
        # 记录"该文档当前向量由哪个模型生成"（切换模型后可据此识别旧向量）
        doc.vector_model = self.model_key
        # 同步维护可校验的索引状态：写入成功条数由 Milvus 实际返回决定，
        # 使 documents.index_status/vector_count 可被 IndexStateService 独立校验
        doc.index_status = INDEX_STATUS_INDEXED
        doc.vector_count = len(rows)
        doc.indexed_at = datetime.now(timezone.utc)
        doc.index_error = ""
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
                        source_type=chunk.source_type,
                        credibility_level=chunk.credibility_level,
                        embedding_model=self.model_key or None,
                    )
                )
        return rows

    def _write_milvus(self, doc_id: str, rows: list[VectorRow]) -> None:
        """幂等写入：整体替换该文档向量（BUG-046：先写新再清旧，避免空窗）。"""
        self.store.ensure_collection()
        self.store.replace_doc(doc_id, rows)

    async def _mark_failed(self, doc_id: uuid.UUID, message: str) -> None:
        await self.db.execute(
            update(Document)
            .where(Document.id == doc_id)
            .values(
                parse_status="failed",
                error_message=message[:2000],
                # 同步标记索引失败，便于「失败重试」按 index_status 精确定位
                index_status=INDEX_STATUS_FAILED,
                index_error=message[:2000],
            )
        )
        await self.db.commit()
