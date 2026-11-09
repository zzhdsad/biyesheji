"""文档管理路由：上传（自动异步解析）、列表、详情、站内预览、在线编辑保存、
文件替换、手动解析/重试、切片查询。

安全规范（AGENTS.md §3 / TECH_DESIGN RBAC）：
- 所有端点受 protected_router 统一鉴权（Depends(get_current_user)）
- 所有文档查询/操作必须校验 Document.kb_id 在当前用户可访问的知识库集合内
- 防止越权列举、读取、修改他人知识库下的文档
"""

import asyncio
import hashlib
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, Query, Request, Response, UploadFile
from loguru import logger
from pydantic import BaseModel, ConfigDict
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.application.document_service import DocumentService
from src.core.config import settings
from src.core.document_formats import (
    EDITABLE_EXTENSIONS,
    IMAGE_EXTENSIONS,
    TEXT_EXTENSIONS,
)
from src.core.file_spaces import (
    PARSE_STATUS_SKIPPED,
    STRUCTURED_SPACE,
    is_parse_enabled,
    normalize_space,
)
from src.core.runtime_config import get_system_value
from src.core.deps import (
    KB_WRITE_ROLES,
    get_accessible_kb_ids,
    get_kb_role,
    require_kb_write,
)
from src.core.exceptions import AppException, NotFoundError, PermissionDeniedError
from src.domain.models import (
    INDEX_STATUS_STALE,
    Chunk,
    Document,
    FileFolder,
    KnowledgeBase,
    StructuredGroup,
    StructuredGroupFile,
    StructuredParse,
    User,
)
from src.infrastructure.database import get_db
from src.infrastructure.document_preview import build_preview
from src.utils.file_validation import (
    validate_document_content,
    validate_text_document,
)
from src.utils.processing_status import document_processing, structured_processing
from src.utils.structured_data import is_structured_format
from src.utils.text_encoding import (
    TextDecodeError,
    decode_text,
    encode_text,
    normalize_encoding,
)
from src.utils.timeutil import utcnow

router = APIRouter(prefix="/documents", tags=["documents"])

# 二进制格式的 Content-Type；纯文本类型按实际编码动态设置 charset
_DOC_MEDIA_TYPES = {
    "pdf": "application/pdf",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "gif": "image/gif",
    "webp": "image/webp",
    "bmp": "image/bmp",
}


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
    updated_at: datetime
    # 文件管理（第一阶段）：空间 + 所在文件夹（None = 该空间根目录）
    space: str = "document"
    folder_id: uuid.UUID | None = None
    # 结构化解析概览（第三阶段）：只有 space=structured 的列表会填这几个字段，
    # 普通文档恒为 None，因此既有调用方看到的 JSON 结构与之前一致（多几个 null 键）。
    structured_status: str | None = None
    structured_record_count: int | None = None
    structured_field_count: int | None = None
    structured_error_message: str | None = None
    # 结构化向量化进度（仅 space=structured 的列表填充）：pending / indexing /
    # completed / failed + 已写入条数，前端据此显示「开始向量化」按钮与进度条。
    structured_vector_status: str | None = None
    structured_vector_count: int | None = None
    structured_vector_error: str | None = None
    # 统一处理状态（第五阶段）：向量侧真实状态直接来自 documents 列，
    # 这样列表里就能区分「有切片」与「向量真的进了 Milvus」，不再靠 parse_status 猜。
    index_status: str = "not_indexed"
    vector_count: int = 0
    # 内容完全相同的其他文件（同库内，最多 5 个）：只用于重复提示，上传/替换响应才有值，
    # 列表不逐行回查（避免 N+1），需要时在处理概览里查看。
    duplicate_names: list[str] = []


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


class ProcessingStageOut(BaseModel):
    """流水线中的一个真实阶段（state 只取 done/active/pending/failed）。"""

    index: int
    label: str
    state: str


class DocumentProcessingOut(BaseModel):
    """文件 → 处理结果的统一概览（第五阶段要求 4）。

    字段全部由 ``src.utils.processing_status`` 从已落库的真实状态派生：
    没有新表、没有估算出来的百分比，也没有第二份解析结果。
    """

    doc_id: uuid.UUID
    file_name: str
    space: str
    pipeline: str
    status: str
    stage_index: int
    stage_label: str
    stage_total: int
    stages: list[ProcessingStageOut]
    reason: str = ""
    blocked_reason: str = ""
    retryable: bool = False
    retry_actions: list[str] = []
    next_action: str = "none"
    # 结果计数与追溯坐标（切片数 / 向量数 / 字段数 / 记录数…），键随流水线而不同
    results: dict[str, Any] = {}
    # 内容完全相同的其他文件（同库、最多 5 个）；空数组 = 没有重复或未检测
    duplicate_names: list[str] = []
    # 该文件参与的关联文件组（结构化空间文件才有）：知道它有没有被关联用过
    groups: list[dict[str, Any]] = []
    updated_at: datetime | None = None


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


class SaveContentRequest(BaseModel):
    """在线编辑保存请求：content 为完整文本，encoding 为原文件编码（可选）。"""

    content: str
    encoding: str | None = None


class SaveContentResponse(BaseModel):
    id: uuid.UUID
    file_size: int
    encoding: str
    parse_status: str
    chunk_count: int
    message: str


class ReplaceContentResponse(BaseModel):
    """文件替换结果：文档 id 不变，索引状态已置为待重建。"""

    id: uuid.UUID
    file_name: str
    file_size: int
    parse_status: str
    chunk_count: int
    index_status: str
    message: str


class MoveDocumentRequest(BaseModel):
    """移动文件：只改 folder_id，物理文件与 storage_path 全程不动。

    ``folder_id = null`` 表示移动到该空间的根目录。
    """

    folder_id: uuid.UUID | None = None


class RenameDocumentRequest(BaseModel):
    """重命名：只改列表展示用的 file_name，扩展名必须与原文档一致。"""

    file_name: str


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


async def _require_folder_in_space(
    db: AsyncSession, folder_id: uuid.UUID | None, space: str
) -> None:
    """目标文件夹必须存在且与文件同空间；None 表示空间根目录，无需校验。

    跨空间拦截是「两个页面不能混在一起」的服务端保证：前端只列出当前空间的
    文件夹，但直接调用接口同样无法把文件塞进另一个空间的目录。
    """
    if folder_id is None:
        return
    folder = await db.get(FileFolder, folder_id)
    if folder is None:
        raise NotFoundError("文件夹不存在")
    if folder.space != space:
        raise AppException(
            400, "目标文件夹与文件不属于同一空间，普通文档与结构化数据不能互相移动"
        )


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


async def _purge_document_cascade(db: AsyncSession, doc: Document) -> None:
    """彻底删除文档：Milvus 向量 → 原始文件 → PG 行（含 chunks 级联）。

    供手动 purge 与回收站过期清理共用，避免过期路径直接 db.delete 留下孤儿向量。
    向量清理失败必须中断：PG 行删除不可逆，留下孤儿向量会让问答引用到已删除文档。
    """
    try:
        from src.infrastructure.milvus_store import get_vector_store

        get_vector_store().delete_by_doc(str(doc.id))
    except Exception as exc:
        logger.error(f"文档向量清理失败 doc_id={doc.id}: {exc}")
        raise AppException(422, f"文档向量清理失败，无法彻底删除：{exc}") from exc

    try:
        from src.infrastructure.storage import get_storage

        if doc.storage_path:
            get_storage().delete(doc.storage_path)
    except Exception as exc:  # noqa: BLE001 - 文件清理失败不阻断数据库删除
        logger.warning(f"清理文件失败（不影响删除）: {exc}")

    # 关联数据（第四阶段）：文件行一删，成员关系就被外键级联清掉，所以必须在删除前
    # 反查受影响的文件组并作废它的正式关联记录（不留指向已删除文件的无效关系）。
    group_ids = await _invalidate_structured_groups(
        db, [doc.id], f"组内文件「{doc.file_name}」已被彻底删除，关联记录已作废", drop_records=True
    )

    # 第六阶段：结构化文件自己的向量（sr:<doc_id>）与受影响文件组的向量（sgr:<group_id>）
    # 一并清理。清理失败只记日志（PG 行即将删除，检索侧也会按记录存在性过滤）。
    from src.application.structured_vector_service import (
        clear_structured_document_vectors,
        clear_structured_group_vectors,
    )

    await clear_structured_document_vectors(doc.id)
    for group_id in group_ids:
        await clear_structured_group_vectors(group_id)

    await db.delete(doc)
    await db.flush()


# ── 内容写入与索引重建（在线编辑 / 文件替换共用）──────────────────────────────

_WRITE_FAILED = "write_failed"  # 原文件完好，旧索引仍与磁盘一致
_WRITE_MISMATCH = "write_mismatch"  # 已落盘但与提交内容不一致，按磁盘内容重建
_INDEX_RETRY_NEEDED = "index"  # 旧向量清理失败，需后台重建（提示用户可能短暂检索到旧内容）
_DISPATCH_FAILED = "dispatch"


def _require_not_processing(doc: Document) -> None:
    """解析排队/进行中禁止改写文件，避免与后台流水线竞态。"""
    if doc.parse_status in ("pending", "parsing"):
        raise AppException(409, "文档正在解析或等待解析，请完成后再修改文件")


def _require_reprocessable(doc: Document) -> None:
    """重试/重新处理入口的统一守卫（第五阶段要求 3）。

    服务层（ParseService / IndexingService）也有同样的校验，这里提前拦截是为了
    让「点重试」立刻拿到明确的 409，而不是派发后台任务后静默失败。

    注意：``parsing`` 不在此处判定——是否属于崩溃残留由服务层按
    ``is_stale_processing`` 决定，路由重复校验会把可重试的残留任务锁死。
    """
    if doc.deleted_at is not None:
        raise AppException(409, "文件已在回收站中，无法处理；如需继续请先从回收站还原")
    if not doc.storage_path:
        raise AppException(409, "该文档没有可处理的原始文件记录，无法重新处理")


def _require_size_limit(content: bytes) -> None:
    max_mb = int(get_system_value("max_file_size_mb"))
    if len(content) > max_mb * 1024 * 1024:
        raise AppException(400, f"文件超过 {max_mb}MB 限制，无法保存")


async def _write_and_reindex(
    db: AsyncSession,
    background_tasks: BackgroundTasks,
    doc: Document,
    content: bytes,
    *,
    action: str,
    file_name: str | None = None,
    reindex: bool = True,
) -> str | None:
    """原子写盘 → 清旧向量 → 清旧切片并重置状态 → 派发重新解析/向量化。

    返回 None 表示全部正常；`_WRITE_FAILED` 表示未触碰原文件（调用方按 500 处理）；
    其余值供调用方组装如实的提示文案。

    ``reindex=False`` 用于结构化数据空间：那里本来就没有切片与向量，
    只需覆盖文件并同步 file_size / file_name，不碰解析与索引状态。
    """
    from src.infrastructure.storage import get_storage

    storage = get_storage()
    try:
        await asyncio.to_thread(storage.replace, doc.storage_path, content)
    except Exception as exc:  # noqa: BLE001 - 写盘失败必须如实返回，原文件完好
        logger.error(f"文档文件{action}失败 doc_id={doc.id}: {exc}")
        return _WRITE_FAILED

    mismatch = False
    try:
        persisted = await asyncio.to_thread(storage.load, doc.storage_path)
        mismatch = persisted != content
    except Exception as exc:  # noqa: BLE001 - 回读失败按不一致处理，仍需让索引追上磁盘内容
        logger.error(f"文档{action}后回读校验失败 doc_id={doc.id}: {exc}")
        mismatch = True
    if mismatch:
        logger.error(f"文档{action}内容与落盘内容不一致 doc_id={doc.id}")

    if not reindex:
        doc.file_size = len(content)
        # 内容指纹随新字节更新：替换后的重复检测与旧内容无关
        doc.content_hash = hashlib.sha256(content).hexdigest()
        if file_name:
            doc.file_name = file_name
        await db.commit()
        await db.refresh(doc)
        # 结构化空间（第三阶段）：文件内容变了，字段结构与记录要按新内容重新识别。
        # 派发的是结构化解析（只写 PG，不碰向量），且服务层在解析成功前不动旧记录，
        # 所以新内容解析失败时，页面上仍是上一份可用的解析结果 + 一条失败原因。
        reparsed = _dispatch_structured_parse(background_tasks, doc)
        # 关联数据（第四阶段）：结构化文件的内容变了，字段结构与匹配结果都要重新校验，
        # 既有正式关联记录不再可信（保留供对照，但组转入 stale，必须重新确认才生成）
        if doc.space == STRUCTURED_SPACE:
            await _invalidate_structured_groups(
                db, [doc.id], f"组内文件「{doc.file_name}」已被替换，关联结果需重新校验并确认"
            )
            await db.commit()
        logger.info(
            f"文档{action}完成（不重建索引）doc_id={doc.id} size={len(content)} "
            f"已派发结构化重新解析={reparsed}"
        )
        return _WRITE_MISMATCH if mismatch else None

    index_error = ""
    try:
        from src.infrastructure.milvus_store import get_vector_store

        await asyncio.to_thread(lambda: get_vector_store().delete_by_doc(str(doc.id)))
    except Exception as exc:  # noqa: BLE001 - 清向量失败不阻塞重建：重建时 replace_doc 会清旧
        index_error = f"旧向量清理失败，重建期间可能仍检索到旧内容：{exc}"
        logger.error(f"{action}后清理旧向量失败 doc_id={doc.id}: {exc}")

    # 文件是最新事实：切片与索引状态一并重置，交由后台流水线按磁盘内容重建
    await db.execute(delete(Chunk).where(Chunk.doc_id == doc.id))
    doc.file_size = len(content)
    doc.content_hash = hashlib.sha256(content).hexdigest()
    if file_name:
        doc.file_name = file_name
    doc.parse_status = "pending"
    doc.chunk_count = 0
    doc.error_message = ""
    doc.index_status = INDEX_STATUS_STALE
    doc.vector_count = 0
    doc.indexed_at = None
    doc.index_error = index_error[:2000]
    doc.vector_model = None
    await db.commit()
    await db.refresh(doc)

    dispatch_ok = True
    try:
        _dispatch_parse(background_tasks, doc.id)
    except Exception:  # noqa: BLE001 - 文件已写入，派发失败需如实告知用户
        dispatch_ok = False
        logger.exception(f"文档{action}后派发解析失败 doc_id={doc.id}")
    logger.info(
        f"文档{action}完成 doc_id={doc.id} size={len(content)} "
        f"向量清理失败={bool(index_error)} 已派发重新解析={dispatch_ok}"
    )

    if mismatch:
        return _WRITE_MISMATCH
    if index_error:
        return _INDEX_RETRY_NEEDED
    if not dispatch_ok:
        return _DISPATCH_FAILED
    return None


def _finish_message(outcome: str | None, *, action: str = "保存", reindex: bool = True) -> str:
    if not reindex:
        return (
            f"文件已{action}（该空间不做切片与向量化；"
            "CSV / TSV / XLSX / JSON 会按新内容重新识别字段并生成记录）"
        )
    if outcome == _INDEX_RETRY_NEEDED:
        return (
            f"文件已{action}，但旧向量清理失败：索引重建完成前问答可能仍引用旧内容，"
            "请稍后点击「重新向量化」"
        )
    if outcome == _DISPATCH_FAILED:
        return f"文件已{action}，但后台解析任务派发失败，请点击「重新解析」重试"
    return f"文件已{action}，正在后台重新解析并更新向量索引"


# ── 端点实现 ────────────────────────────────────────────────────────────────

@router.get("", response_model=list[DocumentOut])
async def list_documents(
    request: Request,
    response: Response,
    kb_id: uuid.UUID | None = None,
    parse_status: str | None = None,
    space: str = Query("document", description="文件空间：document / structured"),
    folder_id: uuid.UUID | None = Query(None, description="只看该文件夹内的文件"),
    root_only: bool = Query(False, description="true = 只看空间根目录（folder_id IS NULL）"),
    related: bool | None = Query(
        None,
        description=(
            "仅结构化空间生效：true = 只看已被文件组引用的文件；"
            "false = 只看未被任何文件组引用的文件；不传 = 全部"
        ),
    ),
    limit: int | None = Query(None, ge=1, le=500),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
) -> list[DocumentOut]:
    """文档列表，支持按知识库 / 空间 / 文件夹筛选，可选 limit/offset 分页。

    不传 limit 时保持全量返回（兼容旧前端 / 下拉选择）；响应头 X-Total-Count
    始终给出当前过滤条件下的总数，供前端做真分页。

    空间隔离（第一阶段文件管理）：``space`` 默认 ``document``，因此未升级的调用方
    只会拿到普通文档，结构化数据不会混进旧列表；两个页面各自传自己的 space。
    ``folder_id`` 与 ``root_only`` 互斥语义：传 folder_id 只看该目录内容，
    传 root_only 只看未归入任何文件夹的文件，两者都不传则看整个空间。

    安全：
    - 基础过滤：Document.kb_id IN 当前用户可访问的知识库集合
    - 若用户额外传入 kb_id 参数：该 kb_id 也必须在可访问集合中，否则返回空列表
    """
    user: User = request.state.user
    accessible = await get_accessible_kb_ids(db, user)

    try:
        space_value = normalize_space(space)
    except ValueError as exc:
        raise AppException(400, str(exc)) from exc

    filters = [
        Document.kb_id.in_(accessible),
        Document.deleted_at.is_(None),
        Document.space == space_value,
    ]
    if folder_id is not None:
        filters.append(Document.folder_id == folder_id)
    elif root_only:
        filters.append(Document.folder_id.is_(None))
    if kb_id is not None:
        if kb_id not in accessible:
            response.headers["X-Total-Count"] = "0"
            return []  # 用户无权访问该 kb_id，返回空而非报错（400 由前端处理）
        filters.append(Document.kb_id == kb_id)
    if parse_status is not None:
        filters.append(Document.parse_status == parse_status)
    if related is not None and space_value == STRUCTURED_SPACE:
        # 「关联性结构化数据」页签只看文件组引用的文件，「非关联性」页签看其余文件，
        # 两个页签共用同一个结构化存储空间，但展示范围不再重叠。
        grouped_doc_ids = (
            select(StructuredGroupFile.doc_id)
            .join(StructuredGroup, StructuredGroup.id == StructuredGroupFile.group_id)
            .where(StructuredGroup.status != "deleted")
        )
        filters.append(
            Document.id.in_(grouped_doc_ids)
            if related
            else Document.id.not_in(grouped_doc_ids)
        )

    total = int(
        await db.scalar(select(func.count()).select_from(Document).where(*filters)) or 0
    )
    response.headers["X-Total-Count"] = str(total)

    stmt = select(Document).where(*filters).order_by(Document.created_at.desc()).offset(offset)
    if limit is not None:
        stmt = stmt.limit(limit)
    rows = (await db.scalars(stmt)).all()
    items = [DocumentOut.model_validate(doc) for doc in rows]
    # 结构化空间额外带上解析概览：列表页要显示「已识别 N 个字段 / M 条记录」，
    # 但普通文档列表不发这个查询，保证普通文档页面的行为与开销都不变。
    if space_value == STRUCTURED_SPACE and items:
        await _attach_structured_summary(db, [item.id for item in items], items)
    return items


async def _attach_structured_summary(
    db: AsyncSession, doc_ids: list[uuid.UUID], items: list[DocumentOut]
) -> None:
    """一次批量查询把 structured_parses 的关键计数并进列表，避免逐行 N+1。"""
    summary_rows = await db.execute(
        select(
            StructuredParse.doc_id,
            StructuredParse.status,
            StructuredParse.record_count,
            func.jsonb_array_length(StructuredParse.fields),
            StructuredParse.error_message,
            StructuredParse.vector_status,
            StructuredParse.vector_count,
            StructuredParse.vector_error,
        ).where(StructuredParse.doc_id.in_(doc_ids))
    )
    by_doc = {row[0]: row for row in summary_rows.all()}
    for item in items:
        found = by_doc.get(item.id)
        if found is None:
            continue  # 还没解析过（后台任务排队中）：保持 None，前端显示「未解析」
        item.structured_status = found[1]
        item.structured_record_count = found[2]
        item.structured_field_count = int(found[3] or 0)
        item.structured_error_message = found[4] or None
        # 向量化进度：前端用 vector_count / record_count 画出百分比进度条
        item.structured_vector_status = found[5] or None
        item.structured_vector_count = int(found[6] or 0)
        item.structured_vector_error = found[7] or None


@router.post("/upload", response_model=DocumentOut, status_code=201)
async def upload_document(
    request: Request,
    background_tasks: BackgroundTasks,
    kb_id: uuid.UUID = Form(...),
    file: UploadFile = File(...),
    source_type: str | None = Form(None),
    era: str | None = Form(None),
    space: str = Form("document"),
    folder_id: uuid.UUID | None = Form(None),
    db: AsyncSession = Depends(get_db),
) -> Document:
    """上传文档（PDF/DOCX/XLSX/PPTX/TXT/MD/CSV/TSV/JSON/HTML/图片）。

    可选来源标注：source_type（受控枚举）+ era；credibility_level 由后端按
    固定映射自动推导，不接受表单传入。

    文件管理参数（第一阶段）：
    - ``space``：document = 普通文档（进入解析 + 向量化）；
      structured = 结构化数据（**只存文件，不解析、不切片、不写 Milvus**，
      ``parse_status`` 记为 ``skipped``，关联解析属后续阶段）。
    - ``folder_id``：上传进哪个文件夹；必须与 space 同空间，None = 根目录。
      批量上传时前端对每个文件复用本接口，物理文件按 doc_id 命名，互不覆盖。

    安全：权限校验已集成到 DocumentService.upload 内部，
    校验顺序为：来源枚举 (400) → 文件类型 (400) → 文件大小 (400)
    → KB 存在性 (404) → KB 权限 (403)。

    BUG-004：上传属写操作，额外校验 KB 写角色（viewer 与公开库只读用户 403）。
    """
    user: User = request.state.user
    try:
        space_value = normalize_space(space)
    except ValueError as exc:
        raise AppException(400, str(exc)) from exc
    await _require_folder_in_space(db, folder_id, space_value)
    await _require_kb_write_access(db, user, kb_id)
    service = DocumentService(db)
    doc = await service.upload(
        kb_id=kb_id,
        file=file,
        user=user,
        source_type=source_type,
        era=era,
        space=space_value,
        folder_id=folder_id,
    )
    # 内容重复时服务层直接复用已有文档：不重复解析、不再新增副本与向量
    if getattr(doc, "deduplicated", False):
        logger.info(f"重复上传已复用已有文档，跳过解析派发 doc_id={doc.id}")
        return doc
    if is_parse_enabled(doc.space):
        _dispatch_parse(background_tasks, doc.id)
    elif _dispatch_structured_parse(background_tasks, doc):
        logger.info(
            f"结构化数据文件已派发结构化解析 doc_id={doc.id} 格式={doc.file_type}"
        )
    else:
        logger.info(
            f"结构化数据文件暂不参与任何解析 doc_id={doc.id} 格式={doc.file_type}"
        )
    return doc


def _dispatch_structured_parse(background_tasks: BackgroundTasks, doc: Document) -> bool:
    """结构化空间文件（第三阶段）派发「字段识别 + 记录生成」。

    与普通文档流水线分离：这里写 PostgreSQL 的 structured_parses /
    structured_records，不产生 chunks，``documents.parse_status`` 也保持
    ``skipped``（第二阶段的隔离标记继续生效）。解析成功后由服务层把每条记录
    同步进统一向量索引（复用第二阶段 Embedding + Milvus）。
    不在支持范围（CSV/TSV/XLSX/JSON）内的格式直接跳过。
    """
    from src.application.structured_runner import run_structured_parse_safely
    from src.utils.structured_data import is_structured_format

    if not is_structured_format(doc.file_type):
        return False
    background_tasks.add_task(run_structured_parse_safely, str(doc.id))
    return True


async def _invalidate_structured_groups(
    db: AsyncSession, doc_ids: list[uuid.UUID], reason: str, *, drop_records: bool = False
) -> list[uuid.UUID]:
    """组内文件被替换或删除 → 关联结果不再可信，必须重新校验（第四阶段）。

    **必须在删除 documents 行之前调用**：``structured_group_files.doc_id`` 是
    ON DELETE CASCADE，文件行一删成员关系就消失，之后再想反查「哪些文件组受影响」就没有依据了。

    ``drop_records=True`` 只用于彻底删除：关系已经指向不存在的文件，正式关联记录必须一并作废，
    不能留下无效关系；普通「替换文件」保留记录但把组标为 stale，提示用户重新校验。
    """
    if not doc_ids:
        return []
    from src.application.structured_group_service import StructuredGroupService

    return await StructuredGroupService(db).mark_stale_by_docs(doc_ids, reason, drop_records)


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


@router.get("/{doc_id}/content")
async def get_document_content(
    doc_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> Response:
    """返回文档原始内容（站内预览/下载使用，前端带鉴权取 Blob）。

    纯文本按实际编码返回 charset，避免 GBK 文件被强制按 UTF-8 显示成乱码。
    安全：校验文档所属知识库的访问权限；回收站中的文档不可读取。
    """
    user: User = request.state.user
    doc = await _get_doc_with_access_check(db, user, doc_id)
    if doc.deleted_at is not None:
        raise NotFoundError("文档不存在")
    if not doc.storage_path:
        raise NotFoundError("文档原始文件不存在")

    from src.infrastructure.storage import StorageError, get_storage

    try:
        content = await asyncio.to_thread(get_storage().load, doc.storage_path)
    except StorageError as exc:
        raise NotFoundError("文档原始文件不存在，可能已被清理") from exc

    file_type = (doc.file_type or "").lower()
    if file_type in TEXT_EXTENSIONS:
        try:
            _, encoding = decode_text(content)
            charset = encoding.replace("utf-8-sig", "utf-8")
        except TextDecodeError:
            charset = "utf-8"
        media_type = f"text/plain; charset={charset}"
    else:
        media_type = _DOC_MEDIA_TYPES.get(file_type, "application/octet-stream")
    headers = {
        "Content-Disposition": f"inline; filename*=UTF-8''{quote(doc.file_name, safe='')}",
        "Content-Length": str(len(content)),
        "Cache-Control": "private, no-store",
        "X-Content-Type-Options": "nosniff",
    }
    return Response(content=content, media_type=media_type, headers=headers)


@router.get("/{doc_id}/preview")
async def preview_document(
    doc_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """站内预览数据：文本/表格结构化内容或 Office 抽取结果。

    PDF/图片只返回 kind，由前端复用 /content 的 Blob 在弹窗内渲染。
    安全：校验文档所属知识库的访问权限；回收站中的文档不可读取。
    """
    user: User = request.state.user
    doc = await _get_doc_with_access_check(db, user, doc_id)
    if doc.deleted_at is not None:
        raise NotFoundError("文档不存在")
    if not doc.storage_path:
        raise NotFoundError("文档原始文件不存在")

    from src.infrastructure.storage import StorageError, get_storage

    file_type = (doc.file_type or "").lower()
    content = b""
    if file_type != "pdf" and file_type not in IMAGE_EXTENSIONS:
        try:
            content = await asyncio.to_thread(get_storage().load, doc.storage_path)
        except StorageError as exc:
            raise NotFoundError("文档原始文件不存在，可能已被清理") from exc

    payload = await asyncio.to_thread(
        build_preview, doc.file_name, doc.file_type, content
    )
    payload.update(
        {
            "id": str(doc.id),
            "file_size": doc.file_size,
            "parse_status": doc.parse_status,
        }
    )
    return payload


@router.post("/{doc_id}/content", response_model=SaveContentResponse)
async def save_document_content(
    doc_id: uuid.UUID,
    body: SaveContentRequest,
    request: Request,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
) -> SaveContentResponse:
    """保存在线编辑内容（TXT/MD/CSV/TSV/JSON/HTML）并重建检索数据。

    顺序保证「不丢原文件、不检索到旧内容」：
    1. 内容校验（JSON 语法 / HTML 结构 / 编码 / 大小）→ 失败直接 400，文件不变；
    2. 临时对象 + 原子替换覆盖存储文件 → 失败则原文件完好；
    3. 回读校验；
    4. 删除该文档旧向量（失败只降级提示，不阻塞索引重建）；
    5. 删除旧切片、重置解析/索引状态；
    6. 派发「解析 → 向量化」后台流水线（新内容据此重建 Milvus）。

    安全：校验 KB 访问权限 + 写角色（viewer 403）；解析排队/进行中禁止并发保存。
    """
    user: User = request.state.user
    doc = await _get_doc_with_access_check(db, user, doc_id)
    if doc.deleted_at is not None:
        raise NotFoundError("文档不存在")
    await _require_kb_write_access(db, user, doc.kb_id)

    file_type = (doc.file_type or "").lower()
    if file_type not in EDITABLE_EXTENSIONS:
        raise AppException(400, f".{file_type} 暂不支持在线编辑，请下载后修改并上传替换")
    _require_not_processing(doc)
    if not doc.storage_path:
        raise NotFoundError("文档原始文件不存在")
    if body.encoding and normalize_encoding(body.encoding) is None:
        raise AppException(400, f"不支持的编码：{body.encoding}")

    invalid = validate_text_document(file_type, body.content)
    if invalid:
        raise AppException(400, invalid)

    encoded, used_encoding = encode_text(body.content, body.encoding)
    _require_size_limit(encoded)

    reindex = is_parse_enabled(doc.space)
    outcome = await _write_and_reindex(
        db, background_tasks, doc, encoded, action="保存", reindex=reindex
    )
    if outcome == _WRITE_FAILED:
        raise AppException(500, "保存文件失败，原文件未被修改，请重试")
    if outcome == _WRITE_MISMATCH:
        raise AppException(500, "保存回读校验不一致，系统已按磁盘实际内容重建索引，请重新打开确认")

    return SaveContentResponse(
        id=doc.id,
        file_size=doc.file_size,
        encoding=used_encoding,
        parse_status=doc.parse_status,
        chunk_count=0,
        message=_finish_message(outcome, reindex=reindex),
    )


@router.post("/{doc_id}/replace", response_model=ReplaceContentResponse)
async def replace_document_content(
    doc_id: uuid.UUID,
    request: Request,
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
) -> ReplaceContentResponse:
    """用本地编辑后的文件替换原文档：**同 id、同知识库、同 storage_path**，不新增记录。

    校验顺序（任一失败都不触碰原文件）：
    格式必须与原文件一致 → 大小 → 内容与扩展名匹配（文件头/JSON/HTML 结构）
    → 原子替换存储 → 清旧向量 → 清旧切片并重置状态 → 派发重新解析与向量化。

    安全：校验 KB 访问权限 + 写角色（viewer 403）；解析排队/进行中禁止替换。
    """
    user: User = request.state.user
    doc = await _get_doc_with_access_check(db, user, doc_id)
    if doc.deleted_at is not None:
        raise NotFoundError("文档不存在")
    await _require_kb_write_access(db, user, doc.kb_id)
    _require_not_processing(doc)
    if not doc.storage_path:
        raise NotFoundError("文档原始文件不存在")

    file_type = (doc.file_type or "").lower()
    upload_name = file.filename or ""
    suffix = Path(upload_name).suffix.lstrip(".").lower()
    if suffix != file_type:
        raise AppException(
            400, f"替换文件必须与原文件格式一致（当前为 .{file_type}，收到 .{suffix or '未知'}）"
        )

    content = await file.read()
    if not content:
        raise AppException(400, "文件为空")
    _require_size_limit(content)
    invalid = validate_document_content(file_type, content)
    if invalid:
        raise AppException(400, f"文件校验失败：{invalid}")

    original_name = doc.file_name
    reindex = is_parse_enabled(doc.space)
    outcome = await _write_and_reindex(
        db,
        background_tasks,
        doc,
        content,
        action="替换",
        file_name=upload_name,
        reindex=reindex,
    )
    if outcome == _WRITE_FAILED:
        raise AppException(500, "替换文件写入失败，原文件未被修改，请重试")
    if outcome == _WRITE_MISMATCH:
        raise AppException(500, "替换回读校验不一致，系统已按磁盘实际内容重建索引，请重新打开确认")

    return ReplaceContentResponse(
        id=doc.id,
        file_name=doc.file_name or original_name,
        file_size=doc.file_size,
        parse_status=doc.parse_status,
        chunk_count=0,
        index_status=doc.index_status,
        message=_finish_message(outcome, action="替换", reindex=reindex),
    )


@router.post("/{doc_id}/move", response_model=DocumentOut)
async def move_document(
    doc_id: uuid.UUID,
    body: MoveDocumentRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> Document:
    """把文件移动到指定文件夹（``folder_id = null`` 回到空间根目录）。

    只更新 ``documents.folder_id``：``storage_path`` 与物理文件一律不动，
    所以移动既不产生第二份文件，也不可能把原文件搬丢；解析结果与向量继续有效。

    安全：KB 写角色（viewer 403）；跨空间移动一律 400，
    保证普通文档与结构化数据的文件不会互相混入。
    """
    user: User = request.state.user
    doc = await _get_doc_with_access_check(db, user, doc_id)
    if doc.deleted_at is not None:
        raise NotFoundError("文档不存在")
    await _require_kb_write_access(db, user, doc.kb_id)

    await _require_folder_in_space(db, body.folder_id, doc.space)
    if doc.folder_id != body.folder_id:
        doc.folder_id = body.folder_id
        await db.commit()
        await db.refresh(doc)
    return doc


@router.patch("/{doc_id}", response_model=DocumentOut)
async def rename_document(
    doc_id: uuid.UUID,
    body: RenameDocumentRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> Document:
    """重命名文件：只改列表展示用的 ``file_name``。

    约束（防止用重命名偷偷改变文件格式，导致扩展名与真实内容不符）：
    - 名称非空、不含路径分隔符、总长 ≤255；
    - 扩展名必须与 ``file_type`` 一致；省略扩展名时自动补回原扩展名。

    ``file_type`` / ``storage_path`` 不变，因此已入库的切片与向量无需重建。
    安全：KB 写角色（viewer 403）。
    """
    user: User = request.state.user
    doc = await _get_doc_with_access_check(db, user, doc_id)
    if doc.deleted_at is not None:
        raise NotFoundError("文档不存在")
    await _require_kb_write_access(db, user, doc.kb_id)

    raw = (body.file_name or "").strip()
    if not raw:
        raise AppException(400, "文件名不能为空")
    if any(ch in raw for ch in ("/", "\\", "\x00")):
        raise AppException(400, "文件名不能包含路径分隔符")

    suffix = (doc.file_type or "").lower()
    given_suffix = Path(raw).suffix.lstrip(".").lower()
    if not given_suffix:
        raw = f"{raw}.{suffix}" if suffix else raw
    elif suffix and given_suffix != suffix:
        raise AppException(
            400, f"重命名不能改变文件格式（当前为 .{suffix}）；格式变化请使用「替换文件」"
        )
    if len(raw) > 255:
        raise AppException(400, "文件名最长 255 个字符")

    if raw != doc.file_name:
        doc.file_name = raw
        await db.commit()
        await db.refresh(doc)
    return doc


@router.post("/{doc_id}/parse", response_model=DocumentOut, status_code=202)
async def parse_document(
    doc_id: uuid.UUID,
    request: Request,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
) -> Document:
    """手动触发解析/重新解析（失败重试入口，202 Accepted）。

    结构化数据空间的文件本阶段不参与解析与向量化，这里直接 400，
    避免误点「重新解析」后悄悄进入 RAG 索引。

    安全：校验文档所属知识库的访问权限 + 写角色（viewer 不得触发，BUG-004）。
    """
    user: User = request.state.user
    doc = await _get_doc_with_access_check(db, user, doc_id)
    await _require_kb_write_access(db, user, doc.kb_id)
    if not is_parse_enabled(doc.space):
        raise AppException(400, "结构化数据文件不参与解析与向量化，仅支持预览、下载与替换")
    _require_reprocessable(doc)
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


async def _group_summaries_for_doc(db: AsyncSession, doc_id: uuid.UUID) -> list[dict[str, Any]]:
    """该文件参与过哪些关联文件组（结构化文件的下游追溯）。"""
    rows = (
        await db.execute(
            select(StructuredGroup)
            .join(StructuredGroupFile, StructuredGroupFile.group_id == StructuredGroup.id)
            .where(StructuredGroupFile.doc_id == doc_id)
            .order_by(StructuredGroup.created_at)
        )
    ).scalars().all()
    groups = []
    for group in rows:
        relations = list(group.relations or [])
        groups.append(
            {
                "id": str(group.id),
                "name": group.name,
                "status": group.status,
                "relation_count": len(relations),
                "unconfirmed_count": sum(1 for r in relations if not r.get("confirmed")),
                "record_count": group.record_count,
                "error_message": group.error_message,
            }
        )
    return groups


@router.get("/{doc_id}/processing", response_model=DocumentProcessingOut)
async def get_processing_overview(
    doc_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> DocumentProcessingOut:
    """单个文件的处理概览：它处理过没有、走到哪一步、为什么失败、产生了什么结果。

    只读派生，不触发任何处理：状态来自 documents / structured_parses /
    structured_groups 里已有的列，因此不会与后台任务竞态，也不需要新表。
    ``results`` 给出结果计数（切片 / 向量 / 字段 / 记录），``groups`` 给出
    该文件被哪些关联文件组使用——用户据此判断「这个文件处理成功了吗」。

    重复检测只走内容 sha256（``duplicate_names``），文件名相同不算重复。
    安全：与详情接口一致，校验文档所属知识库的访问权限。
    """
    user: User = request.state.user
    doc = await _get_doc_with_access_check(db, user, doc_id)

    if doc.space == STRUCTURED_SPACE:
        parse = await db.scalar(select(StructuredParse).where(StructuredParse.doc_id == doc.id))
        view = structured_processing(
            doc, parse, format_supported=is_structured_format(doc.file_type)
        )
        groups = await _group_summaries_for_doc(db, doc.id)
    else:
        view = document_processing(doc)
        groups = []

    duplicate_names = await DocumentService(db).find_duplicates(doc)
    return DocumentProcessingOut.model_validate(
        {
            **view,
            "doc_id": doc.id,
            "file_name": doc.file_name,
            "space": doc.space,
            "duplicate_names": duplicate_names,
            "groups": groups,
            "updated_at": doc.updated_at,
        }
    )


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
    _require_reprocessable(doc)
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
    # 关联数据（第四阶段）：文件进回收站后不再参与关联，受影响的文件组要提示重新校验；
    # 从回收站恢复后仍需用户重新校验并确认，所以这里一并标为 stale（不删记录，保留供对照）
    if doc.space == STRUCTURED_SPACE:
        await _invalidate_structured_groups(
            db, [doc.id], f"组内文件「{doc.file_name}」已移入回收站，关联结果需重新校验"
        )
    await db.commit()
    # BUG-069：天数统一由运行时配置决定（DB 覆盖 .env），不再读固定的 settings
    days = int(get_system_value("trash_retention_days"))
    return {"id": str(doc_id), "deleted": True, "message": f"已移入回收站，{days}天内可恢复"}


@router.get("/trash/list", response_model=list[DocumentOut])
async def list_trash_documents(
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> list[Document]:
    """回收站文档列表（仅 admin，自动清理过期项）。"""
    from datetime import timedelta

    user: User = request.state.user
    if user.role != "admin":
        from src.core.exceptions import PermissionDeniedError
        raise PermissionDeniedError("仅管理员可查看回收站")

    # 清理过期项：必须走向量/文件清理，不能直接 db.delete 留孤儿向量
    cutoff = utcnow() - timedelta(days=int(get_system_value("trash_retention_days")))
    expired = (await db.scalars(select(Document).where(Document.deleted_at < cutoff))).all()
    removed = 0
    for d in expired:
        try:
            async with db.begin_nested():
                await _purge_document_cascade(db, d)
            removed += 1
        except Exception as exc:  # noqa: BLE001 - 单条失败不影响其它过期项
            logger.warning(f"过期文档清理失败 doc_id={d.id}: {exc}")
    if removed:
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
    await _purge_document_cascade(db, doc)
    await db.commit()
    return {"id": str(doc_id), "purged": True}
