"""文档管理路由：上传（自动异步解析）、列表、详情、手动解析/重试、切片查询。"""

import uuid
from datetime import datetime

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, UploadFile
from loguru import logger
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.application.document_service import DocumentService
from src.core.config import settings
from src.core.exceptions import NotFoundError
from src.domain.models import Chunk, Document
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
    error_message: str
    created_at: datetime


class ChunkOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    doc_id: uuid.UUID
    chunk_index: int
    content: str
    token_count: int
    title_path: str | None
    page_num: int | None


def _run_parse_safely(doc_id: str) -> None:
    """后台解析包装：执行解析→向量化流水线。失败已由 Service 回写 failed 状态与
    error_message，此处仅记录日志，避免后台任务异常冒泡导致响应中断。"""
    from src.application.parse_runner import run_parse_pipeline

    try:
        run_parse_pipeline(doc_id)
    except Exception:
        logger.error(f"后台解析任务执行失败 doc_id={doc_id}")


def _dispatch_parse(background_tasks: BackgroundTasks, doc_id: uuid.UUID) -> None:
    """按 PARSE_BACKEND 派发解析：celery（Redis 队列）/ background（进程内线程池）。"""
    if settings.PARSE_BACKEND == "celery":
        from src.application.tasks import parse_document_task

        parse_document_task.delay(str(doc_id))
    else:
        background_tasks.add_task(_run_parse_safely, str(doc_id))


def _dispatch_vectorize(background_tasks: BackgroundTasks, doc_id: uuid.UUID) -> None:
    """按 PARSE_BACKEND 派发向量化：celery / background。"""
    if settings.PARSE_BACKEND == "celery":
        from src.application.tasks import vectorize_document_task

        vectorize_document_task.delay(str(doc_id))
    else:
        background_tasks.add_task(_run_vectorize_safely, str(doc_id))


def _run_vectorize_safely(doc_id: str) -> None:
    """后台向量化包装：失败已由 IndexingService 回写 failed 状态与 error_message。"""
    from src.application.index_runner import run_vectorize

    try:
        run_vectorize(doc_id)
    except Exception:
        logger.error(f"后台向量化任务执行失败 doc_id={doc_id}")


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
    background_tasks: BackgroundTasks,
    kb_id: uuid.UUID = Form(...),
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
) -> Document:
    """上传文档（PDF/DOCX/TXT）。

    流程：校验 → 存储 → 写入 documents 表 → 异步派发解析任务
    （TECH_DESIGN：上传后异步解析，不阻塞 API；parse_status: pending → parsing → success/failed）。
    """
    service = DocumentService(db)
    doc = await service.upload(kb_id=kb_id, file=file)
    _dispatch_parse(background_tasks, doc.id)
    return doc


@router.get("/{doc_id}", response_model=DocumentOut)
async def get_document(doc_id: uuid.UUID, db: AsyncSession = Depends(get_db)) -> Document:
    """文档详情（轮询解析状态与错误信息）。"""
    doc = await db.get(Document, doc_id)
    if doc is None:
        raise NotFoundError("文档不存在")
    return doc


@router.post("/{doc_id}/parse", response_model=DocumentOut, status_code=202)
async def parse_document(
    doc_id: uuid.UUID,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
) -> Document:
    """手动触发解析/重新解析（失败重试入口，202 Accepted）。

    状态通过 GET /documents/{doc_id} 轮询；后续可升级为 SSE 推送进度。
    """
    doc = await db.get(Document, doc_id)
    if doc is None:
        raise NotFoundError("文档不存在")
    _dispatch_parse(background_tasks, doc_id)
    return doc


@router.get("/{doc_id}/chunks", response_model=list[ChunkOut])
async def list_chunks(doc_id: uuid.UUID, db: AsyncSession = Depends(get_db)) -> list[Chunk]:
    """文档切片列表（按 chunk_index 升序），用于核对解析结果与溯源。"""
    doc = await db.get(Document, doc_id)
    if doc is None:
        raise NotFoundError("文档不存在")
    stmt = select(Chunk).where(Chunk.doc_id == doc_id).order_by(Chunk.chunk_index)
    rows = (await db.scalars(stmt)).all()
    return list(rows)


@router.post("/{doc_id}/reindex", response_model=DocumentOut, status_code=202)
async def reindex_document(
    doc_id: uuid.UUID,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
) -> Document:
    """重新向量化：将已解析（success）文档的切片向量化写入 Milvus（202 Accepted）。

    - completed：已完成向量化，幂等重写（先清旧向量）
    - success：切片就绪待向量化
    - 其他状态（pending/parsing/failed）拒绝，需先调用 /parse 完成解析
    """
    doc = await db.get(Document, doc_id)
    if doc is None:
        raise NotFoundError("文档不存在")
    if doc.parse_status not in ("success", "completed"):
        from src.core.exceptions import AppException

        raise AppException(
            409, f"文档切片未就绪（parse_status={doc.parse_status}），请先调用 /parse 解析"
        )
    _dispatch_vectorize(background_tasks, doc_id)
    return doc


@router.delete("/{doc_id}")
async def delete_document(doc_id: uuid.UUID) -> dict:
    """删除文档及其向量数据。

    TODO: 删除 documents 记录（chunks 外键级联）+ Milvus 向量 + 原始文件。
    """
    return {"id": str(doc_id), "deleted": True}
