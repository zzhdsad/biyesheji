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
from src.core.deps import (
    KB_WRITE_ROLES,
    get_accessible_kb_ids,
    get_kb_role,
    require_kb_write,
)
from src.core.exceptions import AppException, NotFoundError, PermissionDeniedError
from src.domain.models import Chunk, Document, KnowledgeBase, User
from src.infrastructure.database import get_db
from src.utils.timeutil import utcnow

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
    source_type: str | None = None
    era: str | None = None
    credibility_level: int | None = None
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
    source_type: str | None = None
    credibility_level: int | None = None


class BackfillSourceRequest(BaseModel):
    """历史文档来源可信度批量补标请求（三选一筛选，至少给一个）。"""

    source_type: str
    era: str | None = None
    doc_ids: list[uuid.UUID] | None = None
    file_names: list[str] | None = None
    kb_id: uuid.UUID | None = None


class BackfillSourceResponse(BaseModel):
    updated: int
    doc_ids: list[str]
    reindex_doc_ids: list[str]


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


async def _require_kb_write_access(
    db: AsyncSession, user: User, kb_id: uuid.UUID
) -> None:
    """写操作权限校验（BUG-004）：owner / admin / editor 才可写。

    viewer 成员与"仅因 public 可见"的用户一律 403，不能因为前端隐藏了按钮
    就认为服务端可以不校验。

    知识库不存在时不在此处抛错：既有校验顺序是「参数/文件 400 → KB 存在性 404
    → 权限 403」（见 DocumentService.upload），保留该顺序以免破坏既有契约；
    不存在的 KB 后续必然走到各自的 404 分支，不存在绕过风险。
    """
    kb = await db.get(KnowledgeBase, kb_id)
    if kb is None:
        return
    await require_kb_write(db, user, kb_id)


async def _writable_kb_ids(db: AsyncSession, user: User) -> set[uuid.UUID] | None:
    """当前用户可写的知识库集合；admin 返回 None（由调用方按"不限"处理）。"""
    if user.role == "admin":
        return None
    accessible = await get_accessible_kb_ids(db, user)
    writable: set[uuid.UUID] = set()
    for kb_id in accessible:
        role = await get_kb_role(db, user, kb_id)
        if role in KB_WRITE_ROLES:
            writable.add(kb_id)
    return writable


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

    stmt = select(Document).where(
        Document.kb_id.in_(accessible), Document.deleted_at.is_(None)
    )
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
    source_type: str | None = Form(None),
    era: str | None = Form(None),
    db: AsyncSession = Depends(get_db),
) -> Document:
    """上传文档（PDF/DOCX/TXT/MD）。

    可选来源标注：source_type（受控枚举）+ era；credibility_level 由后端按
    固定映射自动推导，不接受表单传入。

    安全：权限校验已集成到 DocumentService.upload 内部，
    校验顺序为：来源枚举 (400) → 文件类型 (400) → 文件大小 (400)
    → KB 存在性 (404) → KB 权限 (403)。

    BUG-004：上传属写操作，额外校验 KB 写角色（viewer 与公开库只读用户 403）。
    """
    user: User = request.state.user
    await _require_kb_write_access(db, user, kb_id)
    service = DocumentService(db)
    doc = await service.upload(
        kb_id=kb_id, file=file, user=user, source_type=source_type, era=era
    )
    _dispatch_parse(background_tasks, doc.id)
    return doc


@router.post("/backfill-source", response_model=BackfillSourceResponse)
async def backfill_document_source(
    body: BackfillSourceRequest,
    request: Request,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """历史文档来源可信度批量补标（按文档 id / 文档名 / 知识库批次）。

    - 枚举外取值 400；credibility_level 后端固定映射推导
    - documents 与 chunks 同步更新；completed 文档自动派发重新向量化以更新 Milvus
    - 安全：非管理员仅能补标自己可**写**知识库内的文档（viewer 不得改写，BUG-004）
    """
    user: User = request.state.user
    if body.kb_id is not None:
        await _require_kb_write_access(db, user, body.kb_id)
    accessible = await _writable_kb_ids(db, user)
    result = await DocumentService(db).backfill_source(
        source_type=body.source_type,
        era=body.era,
        doc_ids=body.doc_ids,
        file_names=body.file_names,
        kb_id=body.kb_id,
        accessible_kb_ids=accessible,
    )
    # 已向量化文档需重新写入 Milvus 才能让来源标量生效（幂等：先清旧向量）
    for doc_id in result["reindex_doc_ids"]:
        _dispatch_vectorize(background_tasks, uuid.UUID(doc_id))
    return result


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

    安全：校验文档所属知识库的访问权限 + 写角色（viewer 不得触发，BUG-004）。
    """
    user: User = request.state.user
    doc = await _get_doc_with_access_check(db, user, doc_id)
    await _require_kb_write_access(db, user, doc.kb_id)
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
    await _require_kb_write_access(db, user, doc.kb_id)

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
    """删除文档 → 移入回收站（软删除，默认7天可恢复）。

    安全：校验文档所属知识库的访问权限，且必须由服务端校验写角色
    （需 Editor 以上或 owner/admin；viewer 403，BUG-004）。
    """
    user: User = request.state.user
    doc = await _get_doc_with_access_check(db, user, doc_id)
    await _require_kb_write_access(db, user, doc.kb_id)

    doc.deleted_at = utcnow()
    await db.commit()
    return {"id": str(doc_id), "deleted": True, "message": "已移入回收站，7天内可恢复"}


@router.get("/trash/list", response_model=list[DocumentOut])
async def list_trash_documents(
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> list[Document]:
    """回收站文档列表（仅 admin，自动清理过期项）。"""
    from datetime import timedelta
    from src.core.config import settings as cfg

    user: User = request.state.user
    if user.role != "admin":
        from src.core.exceptions import PermissionDeniedError
        raise PermissionDeniedError("仅管理员可查看回收站")

    # 清理过期项
    cutoff = utcnow() - timedelta(days=cfg.TRASH_RETENTION_DAYS)
    expired = (await db.scalars(select(Document).where(Document.deleted_at < cutoff))).all()
    for d in expired:
        await db.delete(d)
    if expired:
        await db.commit()

    rows = (
        await db.scalars(
            select(Document)
            .where(Document.deleted_at.is_not(None))
            .order_by(Document.deleted_at.desc())
        )
    ).all()
    return list(rows)


@router.post("/{doc_id}/restore", response_model=DocumentOut)
async def restore_document(
    doc_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> Document:
    """从回收站恢复文档（仅 admin）。"""
    user: User = request.state.user
    if user.role != "admin":
        from src.core.exceptions import PermissionDeniedError
        raise PermissionDeniedError("仅管理员可恢复")

    doc = await db.get(Document, doc_id)
    if doc is None or doc.deleted_at is None:
        from src.core.exceptions import NotFoundError
        raise NotFoundError("文档不在回收站中")

    doc.deleted_at = None
    await db.commit()
    await db.refresh(doc)
    return doc


@router.delete("/{doc_id}/purge")
async def purge_document(
    doc_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """彻底删除文档（仅 admin，不可恢复，级联删除 chunks + 清理向量/文件）。"""
    user: User = request.state.user
    if user.role != "admin":
        from src.core.exceptions import PermissionDeniedError
        raise PermissionDeniedError("仅管理员可彻底删除")

    doc = await db.get(Document, doc_id)
    if doc is None:
        from src.core.exceptions import NotFoundError
        raise NotFoundError("文档不存在")

    # 清理 Milvus 向量（BUG-006：方法名错误 + 异常被吞 → 向量永久残留）
    # 清理失败必须中断 purge：PG 记录删除不可逆，留下孤儿向量会让问答引用
    # 到"未知文档"（AGENTS.md §8：禁止伪造来源），且再无清理入口。
    try:
        from src.infrastructure.milvus_store import get_vector_store
        store = get_vector_store()
        store.delete_by_doc(str(doc_id))
    except Exception as exc:
        logger.error(f"文档向量清理失败 doc_id={doc_id}: {exc}")
        raise AppException(422, f"文档向量清理失败，无法彻底删除：{exc}") from exc

    # 清理原始文件（best-effort）
    try:
        from src.infrastructure.storage import get_storage
        storage = get_storage()
        if doc.storage_path:
            storage.delete(doc.storage_path)
    except Exception as e:
        logger.warning(f"清理文件失败（不影响删除）: {e}")

    await db.delete(doc)
    await db.commit()
    return {"id": str(doc_id), "purged": True}
