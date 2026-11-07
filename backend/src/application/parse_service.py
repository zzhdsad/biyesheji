"""文档解析用例：加载 → 解析 → 切片 → 入库（chunks 表）→ 状态回写。"""

import asyncio
import uuid

from loguru import logger
from sqlalchemy import delete, update
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.document_formats import delimiter_for, is_image
from src.core.exceptions import AppException, NotFoundError
from src.core.file_spaces import is_parse_enabled
from src.domain.models import Chunk, Document
from src.infrastructure.parser import BaseParser, ParseError, get_parser
from src.infrastructure.storage import BaseStorage, get_storage
from src.utils.chunking import RawChunk, chunk_delimited, chunk_document, estimate_tokens
from src.utils.processing_status import is_stale_processing


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

        空间守卫：只有 ``space=document``（普通文档空间）的文档进入解析，
        结构化数据空间的文件一律拒绝——即使被误投递到解析队列，也不会
        产生切片或向量（服务层兜底，不依赖调用方的入口校验）。
        """
        doc = await self.db.get(Document, doc_id)
        if doc is None:
            raise NotFoundError("文档不存在")
        if not is_parse_enabled(doc.space):
            raise AppException(
                400, f"文档所在空间（space={doc.space}）不参与解析与向量化"
            )
        # 重试守卫（回收站 / 原始文件）：服务层兜底，不依赖路由入口校验。
        # 已删除或文件本体丢失时若继续解析，会产生与磁盘不一致的切片，
        # 后续向量化又会把这些切片写进 Milvus，因此这里直接拒绝。
        if doc.deleted_at is not None:
            raise AppException(400, "文件已在回收站中，无法解析")
        if not doc.storage_path:
            raise AppException(400, "该文档没有可解析的原始文件记录，无法重新处理")
        # 正在解析时拒绝重复触发；但超过 _STALE_PARSING_MINUTES 仍停在
        # parsing 视为后台进程已崩溃的残留状态，放行重试（口径与结构化解析一致）。
        if doc.parse_status == "parsing" and not is_stale_processing(doc.updated_at):
            raise AppException(409, "文档正在解析中，请勿重复触发")
        # 图片没有可检索文本：直接标记成功（0 切片），仅支持站内预览
        if is_image(doc.file_type):
            await self._complete_image(doc)
            return doc

        doc.parse_status = "parsing"
        doc.error_message = ""
        await self.db.commit()
        started_at = asyncio.get_event_loop().time()

        # CPU/IO 密集部分放线程池，避免阻塞事件循环
        try:
            content = await asyncio.to_thread(self.storage.load, doc.storage_path)
            raw_chunks = await asyncio.to_thread(
                self._build_chunks, doc.file_type, content
            )
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
                    page_num=rc.page_num,
                    # 来源可信度冗余自文档，保证重新解析后与 documents 一致
                    source_type=doc.source_type,
                    credibility_level=doc.credibility_level,
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
        # 切片向量化由 run_parse_pipeline → IndexingService 接续完成
        return doc

    def _build_chunks(self, file_type: str, content: bytes) -> list[RawChunk]:
        """解析 + 切片：表格类按行组块（保留表头），其余走结构感知切片。"""
        pages = self.parser.parse_pages(content, file_type)
        delimiter = delimiter_for(file_type)
        raw_chunks: list[RawChunk] = []
        for page_num, page_text in pages:
            pieces = (
                chunk_delimited(page_text, delimiter)
                if delimiter
                else chunk_document(page_text)
            )
            for rc in pieces:
                rc.page_num = page_num
                raw_chunks.append(rc)
        # 页码内切片序号从 0 开始，合并后必须重排为全局 chunk_index
        for idx, rc in enumerate(raw_chunks):
            rc.chunk_index = idx
        return raw_chunks

    async def _complete_image(self, doc: Document) -> None:
        """图片无可检索文本：清空切片并标记 success（0 切片，不进入向量化）。"""
        await self.db.execute(delete(Chunk).where(Chunk.doc_id == doc.id))
        doc.parse_status = "success"
        doc.chunk_count = 0
        doc.error_message = ""
        await self.db.commit()
        await self.db.refresh(doc)
        logger.info(f"图片文档已登记（仅预览，不参与检索） doc_id={doc.id}")

    async def _mark_failed(self, doc_id: uuid.UUID, message: str) -> None:
        await self.db.execute(
            update(Document)
            .where(Document.id == doc_id)
            .values(parse_status="failed", error_message=message[:2000])
        )
        await self.db.commit()
