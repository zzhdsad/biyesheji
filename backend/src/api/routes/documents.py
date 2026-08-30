"""文档管理路由：上传、列表、删除、重新向量化。"""

import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, File, Form, UploadFile
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.application.document_service import DocumentService
from src.domain.models import Document
from src.infrastructure.database import get_db

router = APIRouter(prefix="/documents", tags=["documents"])


class DocumentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    kb_id: uuid.UUID
    file_name: str
    file_type: str
    file_size: int
    parse_status: str
    chunk_count: int
    created_at: datetime


@router.get("", response_model=list[DocumentOut])
async def list_documents(
    kb_id: uuid.UUID | None = None,
    parse_status: str | None = None,
    db: AsyncSession = Depends(get_db),
) -> list[Document]:
    """文档列表，支持按知识库与解析状态筛选。

    TODO: 接入鉴权后按用户可见的 kb_id 过滤，防止越权列举。
    """
    stmt = select(Document).order_by(Document.created_at.desc())
    if kb_id is not None:
        stmt = stmt.where(Document.kb_id == kb_id)
    if parse_status is not None:
        stmt = stmt.where(Document.parse_status == parse_status)
    rows = (await db.scalars(stmt)).all()
    return list(rows)


@router.post("/upload", response_model=DocumentOut, status_code=201)
async def upload_document(
    kb_id: uuid.UUID = Form(...),
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
) -> Document:
    """上传文档（PDF/DOCX/TXT）。

    流程：类型/大小校验 → 知识库校验 → 存储原始文件（本地/MinIO）→ 写入 documents 表。
    返回 document_id；parse_status 初始为 pending，解析由后续 Celery 任务异步完成。
    """
    service = DocumentService(db)
    return await service.upload(kb_id=kb_id, file=file)


@router.delete("/{doc_id}")
async def delete_document(doc_id: uuid.UUID) -> dict:
    """删除文档及其向量数据。

    TODO: 删除 documents 记录 + Milvus 中对应 chunk + 原始文件（存储层 delete）。
    """
    return {"id": str(doc_id), "deleted": True}


@router.post("/{doc_id}/reindex")
async def reindex_document(doc_id: uuid.UUID) -> dict:
    """重新向量化。

    TODO: 派发 Celery 任务重新解析并向量化该文档。
    """
    return {"id": str(doc_id), "parse_status": "pending"}
