"""文档用例服务：上传校验、文件存储、入库。"""

import uuid
from pathlib import Path

from fastapi import UploadFile
from loguru import logger
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.config import settings
from src.core.exceptions import AppException, NotFoundError, PermissionDeniedError
from src.core.source_meta import (
    credibility_for,
    is_valid_era,
    is_valid_source_type,
)
from src.domain.models import Document, KnowledgeBase, User
from src.infrastructure.storage import BaseStorage, get_storage

# BUSINESS_RULES §4 支持格式：PDF/DOCX/PPTX/XLSX/TXT/MD/HTML/CSV
ALLOWED_EXTENSIONS = {"pdf", "docx", "pptx", "xlsx", "txt", "md", "html", "csv"}


class DocumentService:
    """文档上传用例。"""

    def __init__(self, db: AsyncSession, storage: BaseStorage | None = None) -> None:
        self.db = db
        self.storage = storage or get_storage()

    async def upload(
        self,
        kb_id: uuid.UUID,
        file: UploadFile,
        user: User | None = None,
        source_type: str | None = None,
        era: str | None = None,
    ) -> Document:
        """上传文档：校验 → 存储 → 入库，返回 documents 记录。

        校验顺序（从前到后，依次短路返回）：
        0. 来源类型/年代受控枚举 → 400
        1. 文件类型白名单 → 400
        2. 文件大小 → 400
        3. KB 存在性 → 404
        4. KB 访问权限（需 user 参数）→ 403
        5. 存储 + 入库

        credibility_level 不接受手工输入，由 source_type 按固定映射自动推导
        （AGENTS.md 中医约束第 6 条）。

        Raises:
            AppException: 来源枚举非法 / 文件类型不支持 / 为空 / 超过大小限制
            NotFoundError: 知识库不存在
            PermissionDeniedError: 用户无权访问该知识库
        """
        # 0. 来源可信度枚举校验（枚举外取值拒绝入库）
        if not is_valid_source_type(source_type):
            raise AppException(400, f"非法来源类型：{source_type}")
        if not is_valid_era(era):
            raise AppException(400, f"非法年代：{era}")

        # 1. 文件类型白名单校验
        filename = file.filename or ""
        suffix = Path(filename).suffix.lstrip(".").lower()
        if suffix not in ALLOWED_EXTENSIONS:
            raise AppException(400, f"不支持的文件类型 .{suffix}，仅支持 PDF/DOCX/PPTX/XLSX/TXT/MD/HTML/CSV")

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

        # 4. KB 访问权限校验（RBAC：admin 全通；否则 owner/member/public）
        if user is not None and user.role != "admin":
            ok = await self._check_kb_access(kb_id, user.id)
            if not ok:
                raise PermissionDeniedError(f"无权访问知识库 {kb_id}")

        # 5. 存储原始文件（本地 / MinIO，由 STORAGE_BACKEND 决定）
        doc_id = uuid.uuid4()
        file_key = f"{kb_id}/{doc_id}.{suffix}"
        self.storage.save(file_key, content)

        # 6. 写入 documents 表（parse_status=pending，待 Celery 异步解析）
        # 可信度等级由来源类型固定映射推导，禁止手工传入
        doc = Document(
            id=doc_id,
            kb_id=kb_id,
            file_name=filename,
            file_type=suffix,
            file_size=len(content),
            storage_path=file_key,
            parse_status="pending",
            source_type=source_type,
            era=era,
            credibility_level=credibility_for(source_type),
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

    async def backfill_source(
        self,
        source_type: str | None,
        era: str | None,
        doc_ids: list[uuid.UUID] | None = None,
        file_names: list[str] | None = None,
        kb_id: uuid.UUID | None = None,
        accessible_kb_ids: list[uuid.UUID] | None = None,
    ) -> dict:
        """历史文档来源可信度批量补标（AGENTS.md 中医约束第 6 条）。

        按文档 id / 文档名（精确匹配，多值）/ 知识库（视为同批上传）筛选，
        至少提供一个筛选条件；权限范围外的文档不可见、不可改。

        更新内容：
        1. documents.source_type / era / credibility_level（等级仍由类型固定推导）
        2. chunks.source_type / credibility_level 冗余同步
        Milvus 向量行的来源字段需重新向量化才会更新：completed 文档的 id 随返回值
        reindex_doc_ids 给出，由路由层派发 reindex（幂等，先清旧向量）。

        Raises:
            AppException: 400 枚举非法 / 未提供筛选条件
            NotFoundError: kb_id 不存在
        """
        if not is_valid_source_type(source_type):
            raise AppException(400, f"非法来源类型：{source_type}")
        if not is_valid_era(era):
            raise AppException(400, f"非法年代：{era}")
        if not doc_ids and not file_names and kb_id is None:
            raise AppException(400, "至少提供一个筛选条件：doc_ids / file_names / kb_id")
        if kb_id is not None:
            if await self.db.get(KnowledgeBase, kb_id) is None:
                raise NotFoundError("知识库不存在")

        stmt = select(Document).where(Document.deleted_at.is_(None))
        # accessible_kb_ids=None 表示管理员（不限范围）；否则强制限定可访问 KB
        if accessible_kb_ids is not None:
            if not accessible_kb_ids:
                return {"updated": 0, "doc_ids": [], "reindex_doc_ids": []}
            stmt = stmt.where(Document.kb_id.in_(accessible_kb_ids))
        if doc_ids:
            stmt = stmt.where(Document.id.in_(doc_ids))
        if file_names:
            stmt = stmt.where(Document.file_name.in_(file_names))
        if kb_id is not None:
            stmt = stmt.where(Document.kb_id == kb_id)

        docs = (await self.db.scalars(stmt)).all()
        if not docs:
            return {"updated": 0, "doc_ids": [], "reindex_doc_ids": []}

        level = credibility_for(source_type)
        target_ids = [d.id for d in docs]
        await self.db.execute(
            update(Document)
            .where(Document.id.in_(target_ids))
            .values(source_type=source_type, era=era, credibility_level=level)
        )
        # chunks 只冗余 source_type / credibility_level（era 不冗余）
        from src.domain.models import Chunk

        await self.db.execute(
            update(Chunk)
            .where(Chunk.doc_id.in_(target_ids))
            .values(source_type=source_type, credibility_level=level)
        )
        await self.db.commit()

        reindex_ids = [d.id for d in docs if d.parse_status == "completed"]
        logger.info(
            f"来源补标完成 docs={len(target_ids)} source_type={source_type} "
            f"era={era} level={level} 待重向量化={len(reindex_ids)}"
        )
        return {
            "updated": len(target_ids),
            "doc_ids": [str(i) for i in target_ids],
            "reindex_doc_ids": [str(i) for i in reindex_ids],
        }

    async def _check_kb_access(self, kb_id: uuid.UUID, user_id: uuid.UUID) -> bool:
        """轻量权限校验：owner_id == user_id OR visibility == public OR 在 kb_members 中。"""
        # 直接用一条 SQL 判断，避免额外的 ORM 对象构造
        from src.domain.models import KBMember

        from sqlalchemy import or_

        stmt = select(KnowledgeBase.id).where(
            KnowledgeBase.id == kb_id,
            or_(
                KnowledgeBase.owner_id == user_id,
                KnowledgeBase.visibility == "public",
                KnowledgeBase.id.in_(
                    select(KBMember.kb_id).where(KBMember.user_id == user_id)
                ),
            ),
        )
        result = await self.db.scalar(stmt)
        return result is not None
