"""文档解析用例：加载 → 解析 → 切片 → 入库（chunks 表）→ 状态回写。"""

import asyncio
import uuid

from loguru import logger
from sqlalchemy import delete, update
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.exceptions import AppException, NotFoundError
from src.domain.models import Chunk, Document
from src.infrastructure.parser import BaseParser, ParseError, get_parser
from src.infrastructure.storage import BaseStorage, get_storage
from src.utils.chunking import chunk_document, estimate_tokens


class ParseService:
    """文档解析编排。

    状态机：pending/success/failed → parsing → success | failed
    幂等：重复触发会清理旧切片后重写（用于"重新解析/重试"场景）。
    """

    def __init__(
        self,
        db: AsyncSession,
        storage: BaseStorage | None = None,
        parser: BaseParser | None = None,
    ) -> None:
        self.db = db
        self.storage = storage or get_storage()
        self.parser = parser or get_parser()

    async def run(self, doc_id: uuid.UUID) -> Document:
        """解析指定文档并落库切片。

        Raises:
            NotFoundError: 文档不存在
            AppException: 409 正在解析中 / 422 解析失败（状态已置 failed）
        """
        doc = await self.db.get(Document, doc_id)
        if doc is None:
            raise NotFoundError("文档不存在")
        if doc.parse_status == "parsing":
            raise AppException(409, "文档正在解析中，请勿重复触发")

        doc.parse_status = "parsing"
        doc.error_message = ""
        await self.db.commit()
        started_at = asyncio.get_event_loop().time()

        # CPU/IO 密集部分放线程池，避免阻塞事件循环
        try:
            content = await asyncio.to_thread(self.storage.load, doc.storage_path)
            md = await asyncio.to_thread(self.parser.parse, content, doc.file_type)
            raw_chunks = await asyncio.to_thread(chunk_document, md)
        except ParseError as exc:
            await self._mark_failed(doc.id, str(exc))
            raise AppException(422, str(exc)) from exc
        except Exception as exc:
            logger.exception(f"文档解析异常 doc_id={doc_id}")
            await self._mark_failed(doc.id, f"解析失败：{exc}")
            raise

        # 幂等重写：先清旧切片
        await self.db.execute(delete(Chunk).where(Chunk.doc_id == doc.id))
        for rc in raw_chunks:
            self.db.add(
                Chunk(
                    doc_id=doc.id,
                    kb_id=doc.kb_id,
                    chunk_index=rc.chunk_index,
                    content=rc.content,
                    token_count=estimate_tokens(rc.content),
                    title_path=rc.title_path,
                    # page_num 待 Docling 页码映射（TODO: 下个迭代）
                )
            )
        doc.parse_status = "success"
        doc.chunk_count = len(raw_chunks)
        doc.error_message = ""
        await self.db.commit()
        await self.db.refresh(doc)

        elapsed_ms = int((asyncio.get_event_loop().time() - started_at) * 1000)
        logger.info(
            f"文档解析完成 doc_id={doc.id} chunks={doc.chunk_count} "
            f"parser={type(self.parser).__name__} 耗时={elapsed_ms}ms"
        )
        # TODO: 切片向量化入库 Milvus（下一个迭代）
        return doc

    async def _mark_failed(self, doc_id: uuid.UUID, message: str) -> None:
        await self.db.execute(
            update(Document)
            .where(Document.id == doc_id)
            .values(parse_status="failed", error_message=message[:2000])
        )
        await self.db.commit()
