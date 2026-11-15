"""结构化数据（space=structured）解析结果路由（第三阶段：非关联结构化数据）。

职责：把「文件 → 字段识别 → 记录生成」的结果读出来给结构化数据页面预览，
并提供一个手动重新解析的入口。文件的上传 / 下载 / 预览 / 替换 / 移动 / 重命名 /
删除**继续走 documents 与 folders 路由**，这里不重复实现文件管理。

与 RAG 的边界：
- 解析结果只落 PostgreSQL；第六阶段新增 ``POST /{doc_id}/revectorize``，
  由后台任务复用第二阶段的 Embedding + Milvus 把现有记录写入统一向量索引
  （不重新解析文件，不新 slice，不新建向量库）。
- 只接受 ``space=structured`` 的文件；普通文档仍由 ``/documents/{id}/parse`` 处理。

安全：与 documents 一致，全部端点校验文档所属知识库的访问权限（读）或写权限（重新解析），
越权访问返回 403/404 而不是泄露他人知识库的记录内容。
"""

import uuid
from datetime import datetime
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends, Query, Request
from loguru import logger
from pydantic import BaseModel, ConfigDict
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.routes.documents import (
    _get_doc_with_access_check,
    _require_kb_write_access,
    _require_reprocessable,
)
from src.application.structured_runner import (
    cancel_structured_job,
    is_structured_vector_job_active,
    run_structured_parse_safely,
    run_structured_vectorize_safely,
)
from src.application.structured_vector_service import (
    VECTOR_CANCEL_MARKER,
    resume_checkpoint,
)
from src.core.exceptions import AppException, NotFoundError
from src.core.file_spaces import STRUCTURED_SPACE
from src.domain.models import (
    STRUCTURED_PARSE_PARSING,
    STRUCTURED_PARSE_SUCCESS,
    VECTOR_STATUS_INDEXING,
    VECTOR_STATUS_PENDING,
    Document,
    FileFolder,
    StructuredParse,
    StructuredRecord,
    User,
)
from src.infrastructure.database import get_db
from src.utils.processing_status import is_stale_processing
from src.utils.structured_data import is_structured_format

router = APIRouter(prefix="/structured", tags=["structured"])

# 面包屑回溯上限：与 folders 一致，仅防御脏数据造成的死循环
_MAX_ANCESTOR_DEPTH = 100
_MAX_PAGE_SIZE = 200


class StructuredFieldOut(BaseModel):
    """识别出来的一个字段：名称 / 推断类型 / 空值数，全部来自文件本身。"""

    name: str
    type: str
    null_count: int = 0
    count: int = 0
    sheet: str | None = None
    note: str | None = None


class StructuredParseOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    doc_id: uuid.UUID
    file_name: str
    folder_path: str = ""
    status: str
    detected_format: str
    encoding: str
    fields: list[StructuredFieldOut] = []
    field_count: int = 0
    record_count: int = 0
    read_count: int = 0
    failed_count: int = 0
    skipped_count: int = 0
    anomaly_count: int = 0
    truncated: bool = False
    error_message: str = ""
    parsed_at: datetime | None = None
    # 该格式是否属于本阶段支持的结构化解析范围（csv/tsv/xlsx/json）
    supported: bool = True
    # 第六阶段：统一向量索引状态（pending / indexing / completed / failed）
    vector_status: str = "pending"
    vector_count: int = 0
    vector_model: str | None = None
    vector_error: str = ""
    vectorized_at: datetime | None = None


class StructuredRecordOut(BaseModel):
    """预览列表里的一条记录（不含完整 payload，详情接口再取）。"""

    id: uuid.UUID
    row_no: int
    sheet_name: str | None
    json_path: str | None
    record_label: str
    knowledge_text: str
    anomalies: list[str]
    field_count: int = 0
    empty_field_count: int = 0


class StructuredRecordPage(BaseModel):
    items: list[StructuredRecordOut]
    total: int
    limit: int
    offset: int
    status: str
    error_message: str = ""


class StructuredRecordDetail(StructuredRecordOut):
    doc_id: uuid.UUID
    file_name: str
    file_type: str
    folder_path: str = ""
    payload: dict[str, Any] = {}
    created_at: datetime | None = None


def _require_structured(doc: Document) -> None:
    if doc.space != STRUCTURED_SPACE:
        raise AppException(
            400, "该文件属于普通文档空间，请使用文档解析与切片接口，结构化记录接口只服务结构化数据"
        )


async def _folder_path(db: AsyncSession, folder_id: uuid.UUID | None) -> str:
    """把 folder_id 回溯成「根/一级/二级」形式的目录链，供来源追溯展示。"""
    if folder_id is None:
        return ""
    names: list[str] = []
    cursor: uuid.UUID | None = folder_id
    for _ in range(_MAX_ANCESTOR_DEPTH):
        if cursor is None:
            break
        folder = await db.get(FileFolder, cursor)
        if folder is None:
            break
        names.append(folder.name)
        cursor = folder.parent_id
    return " / ".join(reversed(names))


def _record_out(row: StructuredRecord) -> StructuredRecordOut:
    payload = row.payload or {}
    empty = sum(1 for v in payload.values() if v is None or (isinstance(v, str) and not v.strip()))
    return StructuredRecordOut(
        id=row.id,
        row_no=row.row_no,
        sheet_name=row.sheet_name,
        json_path=row.json_path,
        record_label=row.record_label,
        knowledge_text=row.knowledge_text,
        anomalies=list(row.anomalies or []),
        field_count=len(payload),
        empty_field_count=empty,
    )


@router.get("/{doc_id}/parse", response_model=StructuredParseOut)
async def get_parse_result(
    doc_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> StructuredParseOut:
    """读取该文件的解析概览（字段清单 + 记录/异常统计）。

    还没有解析记录时返回 status=pending 的空概览：前端据此显示「尚未解析」，
    比 404 更适合「刚上传、后台任务还没跑完」这个正常中间态。
    """
    user: User = request.state.user
    doc = await _get_doc_with_access_check(db, user, doc_id)
    _require_structured(doc)

    row = await db.scalar(select(StructuredParse).where(StructuredParse.doc_id == doc.id))
    raw_fields: list[dict[str, Any]] = list(row.fields) if row and row.fields else []
    folder_path = await _folder_path(db, doc.folder_id)
    return StructuredParseOut(
        doc_id=doc.id,
        file_name=doc.file_name,
        folder_path=folder_path,
        status=row.status if row else "pending",
        detected_format=row.detected_format if row else "",
        encoding=row.encoding if row else "",
        fields=[StructuredFieldOut.model_validate(f) for f in raw_fields],
        field_count=len(raw_fields),
        record_count=row.record_count if row else 0,
        read_count=row.read_count if row else 0,
        failed_count=row.failed_count if row else 0,
        skipped_count=row.skipped_count if row else 0,
        anomaly_count=row.anomaly_count if row else 0,
        truncated=row.truncated if row else False,
        error_message=row.error_message if row else "",
        parsed_at=row.parsed_at if row else None,
        supported=is_structured_format(doc.file_type),
        vector_status=row.vector_status if row else "pending",
        vector_count=row.vector_count if row else 0,
        vector_model=row.vector_model if row else None,
        vector_error=row.vector_error if row else "",
        vectorized_at=row.vectorized_at if row else None,
    )


@router.get("/{doc_id}/records", response_model=StructuredRecordPage)
async def list_records(
    doc_id: uuid.UUID,
    request: Request,
    limit: int = Query(20, ge=1, le=_MAX_PAGE_SIZE),
    offset: int = Query(0, ge=0),
    keyword: str | None = Query(None, description="按知识记录文本模糊过滤"),
    only_anomaly: bool = Query(False, description="只看含空值或异常提示的记录"),
    db: AsyncSession = Depends(get_db),
) -> StructuredRecordPage:
    """分页浏览记录：**每次只取一页**，不做全量加载，避免大文件拖垮页面。"""
    user: User = request.state.user
    doc = await _get_doc_with_access_check(db, user, doc_id)
    _require_structured(doc)

    row = await db.scalar(select(StructuredParse).where(StructuredParse.doc_id == doc.id))
    filters = [StructuredRecord.doc_id == doc.id]
    if keyword:
        filters.append(StructuredRecord.knowledge_text.ilike(f"%{keyword.strip()}%"))
    if only_anomaly:
        # anomalies 始终是 JSONB 数组（默认空数组），用数组长度筛出含提示的记录
        filters.append(func.jsonb_array_length(StructuredRecord.anomalies) > 0)
    total = int(
        await db.scalar(select(func.count()).select_from(StructuredRecord).where(*filters)) or 0
    )
    rows = (
        await db.scalars(
            select(StructuredRecord)
            .where(*filters)
            .order_by(StructuredRecord.seq_no.asc())
            .offset(offset)
            .limit(limit)
        )
    ).all()
    return StructuredRecordPage(
        items=[_record_out(r) for r in rows],
        total=total,
        limit=limit,
        offset=offset,
        status=row.status if row else "pending",
        error_message=row.error_message if row else "",
    )


@router.get("/{doc_id}/records/{record_id}", response_model=StructuredRecordDetail)
async def get_record(
    doc_id: uuid.UUID,
    record_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> StructuredRecordDetail:
    """单条记录详情：原始字段与值 + 来源（文件 / 文件夹 / 行号或 JSON 路径）。

    record_id 必须属于 doc_id，跨文件访问一律 404，避免用别人的 doc_id 猜 id。
    """
    user: User = request.state.user
    doc = await _get_doc_with_access_check(db, user, doc_id)
    _require_structured(doc)

    row = await db.get(StructuredRecord, record_id)
    if row is None or row.doc_id != doc.id:
        raise NotFoundError("记录不存在")
    base = _record_out(row)
    return StructuredRecordDetail(
        **base.model_dump(),
        doc_id=doc.id,
        file_name=doc.file_name,
        file_type=doc.file_type,
        folder_path=await _folder_path(db, doc.folder_id),
        payload=dict(row.payload or {}),
        created_at=row.created_at,
    )


@router.post("/{doc_id}/reparse", response_model=StructuredParseOut, status_code=202)
async def reparse_document(
    doc_id: uuid.UUID,
    request: Request,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
) -> StructuredParseOut:
    """手动（重新）解析：派发后台任务后立即返回当前概览。

    - 只处理结构化空间文件；新解析成功才整份替换记录，失败时旧结果原样保留；
    - 写权限校验与上传/替换一致（viewer 与公开库只读用户 403）；
    - 不支持的格式直接 400，避免派发一个注定失败的任务；
    - 正在解析中返回 409（由服务层守卫给出）。

    状态推进交给服务层：后台任务自己把概览置为 parsing 并提交，所以这里不预判状态，
    前端轮询 ``GET /structured/{doc_id}/parse`` 就能看到 pending → parsing → success。
    """
    user: User = request.state.user
    doc = await _get_doc_with_access_check(db, user, doc_id)
    _require_structured(doc)
    _require_reprocessable(doc)
    if not is_structured_format(doc.file_type):
        raise AppException(400, f"该格式暂不支持结构化解析：{doc.file_type}")
    await _require_kb_write_access(db, user, doc.kb_id)

    row = await db.scalar(select(StructuredParse).where(StructuredParse.doc_id == doc.id))
    # 崩溃残留（长时间停在 parsing）放行，否则失败任务永远无法重试；口径与服务层一致
    if row is not None and row.status == STRUCTURED_PARSE_PARSING and not is_stale_processing(
        row.updated_at or row.created_at
    ):
        raise AppException(409, "该文件正在解析中，请等待完成后再试")

    background_tasks.add_task(run_structured_parse_safely, str(doc.id))
    logger.info(f"已派发结构化解析 doc_id={doc.id} file={doc.file_name}")
    return await get_parse_result(doc_id, request, db)


@router.post("/{doc_id}/revectorize", response_model=StructuredParseOut, status_code=202)
async def revectorize_document(
    doc_id: uuid.UUID,
    request: Request,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
) -> StructuredParseOut:
    """重新向量化：把该文件**现有**的知识记录重写入统一向量索引。

    - 不重新解析文件、不改动记录内容；写入走 replace_doc（先写新再清旧）；
    - 只有解析成功且有记录时才允许（没有解析事实就没有可索引的知识）；
    - 已在等待队列或正在向量化时返回 409，避免重复派发；崩溃残留（超过 30 分钟
      未推进）放行；
    - 写权限校验与重新解析一致。
    """
    user: User = request.state.user
    doc = await _get_doc_with_access_check(db, user, doc_id)
    _require_structured(doc)
    await _require_kb_write_access(db, user, doc.kb_id)
    if doc.deleted_at is not None:
        raise AppException(409, "文件已在回收站中，无法向量化；如需继续请先还原")

    row = await db.scalar(select(StructuredParse).where(StructuredParse.doc_id == doc.id))
    if row is None or row.status != STRUCTURED_PARSE_SUCCESS:
        raise AppException(409, "该文件还没有成功的结构化解析结果，请先完成解析")
    if not row.record_count:
        raise AppException(400, "该文件没有可向量化的知识记录")
    if (
        row.vector_status == VECTOR_STATUS_INDEXING
        and not is_stale_processing(row.updated_at or row.created_at)
    ) or is_structured_vector_job_active("document", str(doc.id)):
        raise AppException(409, "该文件已在向量化队列中或正在向量化，请稍后刷新查看结果")

    # 续跑：上次被用户停止的 pending 保留计数和停止标记；其余情况从 0 重建。
    resume_count = resume_checkpoint(
        row.vector_status, row.vector_count, row.vector_error
    )
    await db.execute(
        update(StructuredParse)
        .where(StructuredParse.doc_id == doc.id)
        .values(
            vector_status=VECTOR_STATUS_PENDING,
            vector_count=resume_count,
            vector_error=VECTOR_CANCEL_MARKER if resume_count else "",
        )
    )
    await db.commit()
    background_tasks.add_task(run_structured_vectorize_safely, str(doc.id))
    logger.info(f"已派发结构化重新向量化 doc_id={doc.id} file={doc.file_name}")
    return await get_parse_result(doc_id, request, db)


@router.post("/{doc_id}/cancel-vectorize", response_model=StructuredParseOut, status_code=202)
async def cancel_vectorize_document(
    doc_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> StructuredParseOut:
    """停止该文件正在等待或执行的向量化。

    排队中的任务直接移出队列并复位 pending；执行中的任务设置停止标记，服务层在
    下一批记录之间退出，清理本次已写入的部分向量后回到 pending（可重新开始）。
    """
    user: User = request.state.user
    doc = await _get_doc_with_access_check(db, user, doc_id)
    _require_structured(doc)
    await _require_kb_write_access(db, user, doc.kb_id)

    row = await db.scalar(select(StructuredParse).where(StructuredParse.doc_id == doc.id))
    outcome = cancel_structured_job("document", str(doc.id))
    if outcome == "idle":
        stale = (
            row is not None
            and row.vector_status == VECTOR_STATUS_INDEXING
            and is_stale_processing(row.updated_at or row.created_at)
        )
        if not stale:
            raise AppException(409, "该文件当前没有等待或正在执行的向量化任务")

    if row is not None:
        await db.execute(
            update(StructuredParse)
            .where(StructuredParse.doc_id == doc.id)
            .values(
                vector_status=VECTOR_STATUS_PENDING,
                vector_count=row.vector_count,
                vector_error=VECTOR_CANCEL_MARKER,
            )
        )
        await db.commit()
    logger.info(f"已请求停止结构化向量化 doc_id={doc.id} outcome={outcome}")
    return await get_parse_result(doc_id, request, db)
