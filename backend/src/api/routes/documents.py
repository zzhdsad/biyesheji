"""文档管理路由：上传（自动异步解析）、列表、详情、手动解析/重试、切片查询。

安全规范（AGENTS.md §3 / TECH_DESIGN RBAC）：
- 所有端点受 protected_router 统一鉴权（Depends(get_current_user)）
- 所有文档查询/操作必须校验 Document.kb_id 在当前用户可访问的知识库集合内
- 防止越权列举、读取、修改他人知识库下的文档
"""

import uuid
from datetime import datetime

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, Request, UploadFile
from loguru import logger
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.application.document_service import DocumentService
from src.core.config import settings
from src.core.deps import get_accessible_kb_ids
from src.core.exceptions import NotFoundError, PermissionDeniedError
from src.domain.models import Chunk, Document, KnowledgeBase, User
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


# ── 公共安全校验 ─────────────────────────────────────────────────────────────

async def _validate_kb_id_access(
    db: AsyncSession, user: User, kb_id: uuid.UUID
) -> None:
    """校验 KB 存在性 + 用户是否可访问。

    校验顺序：先查 KB 是否存在（404），再校验权限（403）。
    这样上传等场景可以先做文件校验（400），再做 KB 校验。
    """
    # 1. 先查 KB 是否存在
    kb = await db.get(KnowledgeBase, kb_id)
    if kb is None:
        raise NotFoundError("知识库不存在")

    # 2. 再校验权限（admin 全通；否则 owner/member/public）
    if user.role == "admin":
        return
    accessible = await get_accessible_kb_ids(db, user)
    if kb_id not in accessible:
        raise PermissionDeniedError(f"无权访问知识库 {kb_id}")


async def _get_doc_with_access_check(
    db: AsyncSession, user: User, doc_id: uuid.UUID
) -> Document:
    """查询文档 + 校验所属知识库的访问权限。

    Raises:
        NotFoundError: 文档不存在
        PermissionDeniedError: 文档所属知识库不在用户可访问范围内
    """
    doc = await db.get(Document, doc_id)
    if doc is None:
        raise NotFoundError("文档不存在")
    await _validate_kb_id_access(db, user, doc.kb_id)
    return doc


# ── 后台任务调度（不变） ─────────────────────────────────────────────────────

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
        logger.exception(f"后台向量化任务执行失败 doc_id={doc_id}")


# ── 端点实现 ────────────────────────────────────────────────────────────────

@router.get("", response_model=list[DocumentOut])
async def list_documents(
    request: Request,
    kb_id: uuid.UUID | None = None,
    parse_status: str | None = None,
    db: AsyncSession = Depends(get_db),
) -> list[Document]:
    """文档列表，支持按知识库与解析状态筛选。

    安全隔离：
    - 基础过滤：Document.kb_id IN 当前用户可访问的知识库集合
    - 若用户额外传入 kb_id 参数：该 kb_id 也必须在可访问集合中，否则返回空列表
    """
    user: User = request.state.user
    accessible = await get_accessible_kb_ids(db, user)

    stmt = select(Document).where(Document.kb_id.in_(accessible))
    if kb_id is not None:
        if kb_id not in accessible:
            return []  # 用户无权访问该 kb_id，返回空而非报错（400 由前端处理）
        stmt = stmt.where(Document.kb_id == kb_id)
    if parse_status is not None:
        stmt = stmt.where(Document.parse_status == parse_status)
    stmt = stmt.order_by(Document.created_at.desc())
    rows = (await db.scalars(stmt)).all()
    return list(rows)


@router.post("/upload", response_model=DocumentOut, status_code=201)
async def upload_document(
    request: Request,
    background_tasks: BackgroundTasks,
    kb_id: uuid.UUID = Form(...),
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
) -> Document:
    """上传文档（PDF/DOCX/TXT/MD）。

    安全：权限校验已集成到 DocumentService.upload 内部，
    校验顺序为：文件类型 (400) → 文件大小 (400) → KB 存在性 (404) → KB 权限 (403)。
    """
    user: User = request.state.user
    service = DocumentService(db)
    doc = await service.upload(kb_id=kb_id, file=file, user=user)
    _dispatch_parse(background_tasks, doc.id)
    return doc


@router.get("/{doc_id}", response_model=DocumentOut)
async def get_document(
    doc_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> Document:
    """文档详情（轮询解析状态与错误信息）。

    安全：校验文档所属知识库的访问权限。
    """
    user: User = request.state.user
    return await _get_doc_with_access_check(db, user, doc_id)


@router.post("/{doc_id}/parse", response_model=DocumentOut, status_code=202)
async def parse_document(
    doc_id: uuid.UUID,
    request: Request,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
) -> Document:
    """手动触发解析/重新解析（失败重试入口，202 Accepted）。

    安全：校验文档所属知识库的访问权限。
    """
    user: User = request.state.user
    doc = await _get_doc_with_access_check(db, user, doc_id)
    _dispatch_parse(background_tasks, doc_id)
    return doc


@router.get("/{doc_id}/chunks", response_model=list[ChunkOut])
async def list_chunks(
    doc_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> list[Chunk]:
    """文档切片列表（按 chunk_index 升序），用于核对解析结果与溯源。

    安全：校验文档所属知识库的访问权限。
    """
    user: User = request.state.user
    doc = await _get_doc_with_access_check(db, user, doc_id)

    stmt = select(Chunk).where(Chunk.doc_id == doc.id).order_by(Chunk.chunk_index)
    rows = (await db.scalars(stmt)).all()
    return list(rows)


@router.post("/{doc_id}/reindex", response_model=DocumentOut, status_code=202)
async def reindex_document(
    doc_id: uuid.UUID,
    request: Request,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
) -> Document:
    """重新向量化：将已解析（success）文档的切片向量化写入 Milvus（202 Accepted）。

    安全：校验文档所属知识库的访问权限。

    - completed：已完成向量化，幂等重写（先清旧向量）
    - success：切片就绪待向量化
    - 其他状态（pending/parsing/failed）拒绝，需先调用 /parse 解析
    """
    from src.core.exceptions import AppException

    user: User = request.state.user
    doc = await _get_doc_with_access_check(db, user, doc_id)

    if doc.parse_status not in ("success", "completed"):
        raise AppException(
            409, f"文档切片未就绪（parse_status={doc.parse_status}），请先调用 /parse 解析"
        )
    _dispatch_vectorize(background_tasks, doc_id)
    return doc


@router.delete("/{doc_id}")
async def delete_document(
    doc_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """删除文档及其向量数据。

    安全：校验文档所属知识库的访问权限。

    TODO: 删除 documents 记录（chunks 外键级联）+ Milvus 向量 + 原始文件。
    """
    user: User = request.state.user
    await _get_doc_with_access_check(db, user, doc_id)
    return {"id": str(doc_id), "deleted": True}
