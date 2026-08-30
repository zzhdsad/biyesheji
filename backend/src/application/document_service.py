"""文档用例服务：上传校验、文件存储、入库。"""

import uuid
from pathlib import Path

from fastapi import UploadFile
from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.config import settings
from src.core.exceptions import AppException, NotFoundError
from src.domain.models import Document, KnowledgeBase
from src.infrastructure.storage import BaseStorage, get_storage

# 当前阶段支持格式（PRD 3.1 的 Must-have 子集：PDF/DOCX/TXT）
ALLOWED_EXTENSIONS = {"pdf", "docx", "txt"}


class DocumentService:
    """文档上传用例。"""

    def __init__(self, db: AsyncSession, storage: BaseStorage | None = None) -> None:
        self.db = db
        self.storage = storage or get_storage()

    async def upload(self, kb_id: uuid.UUID, file: UploadFile) -> Document:
        """上传文档：校验 → 存储 → 入库，返回 documents 记录。

        Raises:
            AppException: 文件类型不支持 / 为空 / 超过大小限制
            NotFoundError: 知识库不存在
        """
        # 1. 文件类型白名单校验
        filename = file.filename or ""
        suffix = Path(filename).suffix.lstrip(".").lower()
        if suffix not in ALLOWED_EXTENSIONS:
            raise AppException(400, f"不支持的文件类型 .{suffix}，仅支持 PDF/DOCX/TXT")

        # 2. 大小校验（先落盘到临时文件，read 进内存判断；超大文件分片上传见 PRD 8）
        content = await file.read()
        max_bytes = settings.MAX_FILE_SIZE_MB * 1024 * 1024
        if len(content) == 0:
            raise AppException(400, "文件为空")
        if len(content) > max_bytes:
            raise AppException(400, f"文件超过 {settings.MAX_FILE_SIZE_MB}MB 限制")

        # 3. 知识库存在性校验：kb_id 是检索隔离与越权防护的关键字段
        kb = await self.db.get(KnowledgeBase, kb_id)
        if kb is None:
            raise NotFoundError("知识库不存在")

        # 4. 存储原始文件（本地 / MinIO，由 STORAGE_BACKEND 决定）
        doc_id = uuid.uuid4()
        file_key = f"{kb_id}/{doc_id}.{suffix}"
        self.storage.save(file_key, content)

        # 5. 写入 documents 表（parse_status=pending，待 Celery 异步解析）
        doc = Document(
            id=doc_id,
            kb_id=kb_id,
            file_name=filename,
            file_type=suffix,
            file_size=len(content),
            storage_path=file_key,
            parse_status="pending",
        )
        try:
            self.db.add(doc)
            await self.db.commit()
        except Exception:
            await self.db.rollback()
            self.storage.delete(file_key)  # 入库失败清理文件，避免孤儿
            raise
        await self.db.refresh(doc)

        logger.info(
            f"文档上传成功 doc_id={doc.id} kb_id={kb_id} "
            f"file={filename!r} size={len(content)} backend={settings.STORAGE_BACKEND}"
        )
        # TODO: 派发 Celery 解析任务（pending → parsing → success/failed）
        return doc
