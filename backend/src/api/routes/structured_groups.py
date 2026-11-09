"""关联结构化数据路由（第四阶段：文件组 → 结构识别 → 关系确认 → 关联记录）。

职责边界：
- 文件的上传 / 下载 / 预览 / 替换 / 重命名 / 删除**继续走 documents 与 folders 路由**；
  本路由只把已上传到结构化空间的文件组成「关联数据文件组」，在其上配置关系、校验并
  生成关联知识记录。单文件的字段识别与逐行记录仍由第三阶段 ``/structured/{doc_id}/...``
  提供，这里只读取那些结果，不重新解析文件。
- 「关联数据必须作为完整文件组处理」：一个组就是一个独立任务，正式关联记录只在
  ``POST /structured/groups/{gid}/confirm``（且所有关系都由用户确认）之后一次性生成。

与 RAG 的边界（第六阶段）：关系确认并生成关联记录后，由服务层把有效记录同步写入
统一向量索引（复用第二阶段 Milvus 与 Embedding）；本路由只负责派发「重新向量化」，
未确认 / 校验未通过 / 已失效的组不会留下可检索的向量。documents.parse_status
仍由第三阶段维护，普通文档链路不受影响。

安全：与 documents / structured 一致——读操作校验知识库访问权限，写操作额外校验写角色
（owner/admin/editor），viewer 一律 403。
"""

import uuid
from datetime import datetime
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends, Query, Request
from loguru import logger
from pydantic import BaseModel, ConfigDict
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.routes.documents import _require_kb_write_access, _validate_kb_id_access
from src.application.structured_group_service import StructuredGroupService
from src.application.structured_runner import (
    cancel_structured_job,
    is_structured_vector_job_active,
    run_group_vectorize_safely,
)
from src.core.deps import get_accessible_kb_ids
from src.core.exceptions import AppException, NotFoundError
from src.domain.models import (
    STRUCTURED_GROUP_CONFIRMED,
    VECTOR_STATUS_INDEXING,
    VECTOR_STATUS_PENDING,
    Document,
    KnowledgeBase,
    StructuredGroup,
    StructuredGroupFile,
    StructuredGroupRecord,
    User,
)
from src.infrastructure.database import get_db
from src.utils.processing_status import group_processing, is_stale_processing

router = APIRouter(prefix="/structured/groups", tags=["structured-groups"])

_MAX_PAGE_SIZE = 200
# 一次建组最多带多少个文件：防止误把整个知识库全选成一个无法人工审阅的巨型组
_MAX_GROUP_FILES = 30


# ── 请求体 ──────────────────────────────────────────────────────────────────


class GroupCreateIn(BaseModel):
    kb_id: uuid.UUID
    name: str = ""
    doc_ids: list[uuid.UUID]


class GroupFilesIn(BaseModel):
    doc_ids: list[uuid.UUID]


class GroupAliasIn(BaseModel):
    alias: str = ""


class GroupRenameIn(BaseModel):
    name: str


class RelationIn(BaseModel):
    from_doc_id: uuid.UUID
    from_field: str
    to_doc_id: uuid.UUID
    to_field: str
    kind: str = "one_to_many"
    # 用户手动选出的字段即视为已确认；不传时由服务层按 origin 决定默认值。
    # origin=suggested 仅用于兼容历史遗留关系，当前没有自动生成入口。
    confirmed: bool | None = None
    # 来源与评分只是展示/追溯用：origin 只允许 manual / suggested，评分由服务层夹紧
    origin: str = "manual"
    confidence: float | None = None
    score: int | None = None
    tier: str | None = None
    coverage: float | None = None
    row_coverage: float | None = None
    evidence: list[str] = []
    reason: str = ""


class RelationsIn(BaseModel):
    relations: list[RelationIn]


class RelationConfirmIn(BaseModel):
    # 关系 id 形如 "<doc_uuid>:字段-><doc_uuid>:字段"，含冒号，所以走请求体而非路径参数
    relation_id: str
    confirmed: bool = True


class GroupConfirmIn(BaseModel):
    # 不传时由服务层按「向外关联最多」自动选锚点（典型即选课表 / 饮片关联表）
    anchor_doc_id: uuid.UUID | None = None
    # 存在外键对不上的记录时必须显式 force，才允许带着提示生成正式记录
    force: bool = False


# ── 响应体 ──────────────────────────────────────────────────────────────────


class GroupFileOut(BaseModel):
    """组内一个文件：结构识别结果 + 主键候选（全部只是候选，等用户确认）。"""

    doc_id: uuid.UUID
    file_name: str
    current_file_name: str
    alias: str
    order_no: int
    parse_status: str
    detected_format: str
    record_count: int
    anomaly_count: int
    error_message: str
    fields: list[str] = []
    field_specs: list[dict[str, Any]] = []
    key_candidates: list[dict[str, Any]] = []
    # 文件已被彻底删除或进了回收站：既有关系与校验结果不再可信
    removed: bool = False


class GroupOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    kb_id: uuid.UUID
    name: str
    status: str
    record_count: int
    error_message: str
    relation_count: int = 0
    unconfirmed_count: int = 0
    file_count: int = 0
    created_at: datetime | None = None
    updated_at: datetime | None = None
    validated_at: datetime | None = None
    confirmed_at: datetime | None = None
    # 第六阶段：统一向量索引状态（pending / indexing / completed / failed）
    vector_status: str = "pending"
    vector_count: int = 0
    vector_model: str | None = None
    vector_error: str = ""
    vectorized_at: datetime | None = None


class GroupDetailOut(BaseModel):
    group: GroupOut
    files: list[GroupFileOut]
    relations: list[dict[str, Any]]
    validation: dict[str, Any]


class GroupRecordOut(BaseModel):
    id: uuid.UUID
    seq_no: int
    anchor_doc_id: uuid.UUID | None
    knowledge_text: str
    anomaly_count: int = 0
    source_count: int = 0
    relation_count: int = 0


class GroupRecordPage(BaseModel):
    items: list[GroupRecordOut]
    total: int
    limit: int
    offset: int
    status: str
    record_count: int
    error_message: str = ""


class GroupRecordDetail(GroupRecordOut):
    group_id: uuid.UUID
    group_name: str
    anchor_file: str = ""
    anchor_record_id: uuid.UUID | None = None
    parts: list[dict[str, Any]] = []
    sources: list[dict[str, Any]] = []
    relation_ids: list[str] = []
    anomalies: list[str] = []
    created_at: datetime | None = None


class GroupProcessingOut(BaseModel):
    """文件组处理概览（第五阶段要求 2/3/4）：阶段、卡点原因、结果计数与成员解析状态。

    与 ``GET /documents/{id}/processing`` 用同一套状态词汇和阶段结构，
    数据全部来自 structured_groups / structured_parses 已有列的只读派生。
    比 ``/{group_id}`` 详情轻（不取字段清单与主键候选），供页面轮询进度使用。
    """

    group_id: uuid.UUID
    name: str
    pipeline: str
    status: str
    stage_index: int
    stage_label: str
    stage_total: int
    stages: list[dict[str, Any]]
    reason: str = ""
    blocked_reason: str = ""
    retryable: bool = False
    retry_actions: list[str] = []
    next_action: str = "none"
    results: dict[str, Any] = {}
    members: list[dict[str, Any]] = []
    updated_at: datetime | None = None


# ── 内部工具 ────────────────────────────────────────────────────────────────


async def _require_group_read(db: AsyncSession, user: User, group_id: uuid.UUID) -> StructuredGroup:
    group = await StructuredGroupService(db).get(group_id)
    await _validate_kb_id_access(db, user, group.kb_id)
    return group


async def _require_group_write(db: AsyncSession, user: User, group_id: uuid.UUID) -> StructuredGroup:
    group = await StructuredGroupService(db).get(group_id)
    await _validate_kb_id_access(db, user, group.kb_id)
    await _require_kb_write_access(db, user, group.kb_id)
    return group


def _group_out(group: StructuredGroup, file_count: int) -> GroupOut:
    relations = list(group.relations or [])
    return GroupOut(
        id=group.id,
        kb_id=group.kb_id,
        name=group.name,
        status=group.status,
        record_count=group.record_count,
        error_message=group.error_message or "",
        relation_count=len(relations),
        unconfirmed_count=sum(1 for r in relations if not r.get("confirmed")),
        file_count=file_count,
        created_at=group.created_at,
        updated_at=group.updated_at,
        validated_at=group.validated_at,
        confirmed_at=group.confirmed_at,
        vector_status=group.vector_status or "pending",
        vector_count=group.vector_count or 0,
        vector_model=group.vector_model,
        vector_error=group.vector_error or "",
        vectorized_at=group.vectorized_at,
    )


def _record_out(row: StructuredGroupRecord) -> GroupRecordOut:
    return GroupRecordOut(
        id=row.id,
        seq_no=row.seq_no,
        anchor_doc_id=row.anchor_doc_id,
        knowledge_text=row.knowledge_text,
        anomaly_count=len(row.anomalies or []),
        source_count=len(row.sources or []),
        relation_count=len(row.relation_ids or []),
    )


async def _file_counts(db: AsyncSession, group_ids: list[uuid.UUID]) -> dict[str, int]:
    if not group_ids:
        return {}
    rows = await db.execute(
        select(StructuredGroupFile.group_id, func.count())
        .where(StructuredGroupFile.group_id.in_(group_ids))
        .group_by(StructuredGroupFile.group_id)
    )
    return {str(group_id): int(count) for group_id, count in rows.all()}


def _check_file_limit(doc_ids: list[uuid.UUID]) -> None:
    if len(doc_ids) > _MAX_GROUP_FILES:
        raise AppException(
            400, f"一次最多选择 {_MAX_GROUP_FILES} 个文件组成关联数据文件组"
        )


# ── 文件组列表 / 详情 ───────────────────────────────────────────────────────


@router.get("", response_model=list[GroupOut])
async def list_groups(
    request: Request,
    kb_id: uuid.UUID | None = Query(None, description="只列某个知识库下的文件组"),
    db: AsyncSession = Depends(get_db),
) -> list[GroupOut]:
    """关联数据文件组列表（默认覆盖当前用户可访问的全部知识库）。"""
    user: User = request.state.user
    filters = []
    if kb_id is not None:
        await _validate_kb_id_access(db, user, kb_id)
        filters.append(StructuredGroup.kb_id == kb_id)
    else:
        accessible = await get_accessible_kb_ids(db, user)
        if not accessible:
            return []
        filters.append(StructuredGroup.kb_id.in_(accessible))

    groups = (
        await db.scalars(
            select(StructuredGroup).where(*filters).order_by(StructuredGroup.created_at.desc())
        )
    ).all()
    counts = await _file_counts(db, [g.id for g in groups])
    return [_group_out(g, counts.get(str(g.id), 0)) for g in groups]


@router.post("", response_model=GroupOut, status_code=201)
async def create_group(
    payload: GroupCreateIn,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> GroupOut:
    """把若干结构化文件组成一个关联数据文件组（= 一个独立的关联导入任务）。

    - 至少 2 个文件：单个文件请直接用第三阶段的独立解析，不会被当成关联数据；
    - 只接受结构化数据空间、且不在回收站里的文件；
    - 建组后状态为 draft，未经「校验 + 确认」不会生成任何正式关联记录。
    """
    user: User = request.state.user
    _check_file_limit(payload.doc_ids)
    if await db.get(KnowledgeBase, payload.kb_id) is None:
        raise NotFoundError("知识库不存在")
    await _require_kb_write_access(db, user, payload.kb_id)
    group = await StructuredGroupService(db).create_group(
        payload.kb_id, payload.name, payload.doc_ids
    )
    return _group_out(group, len(payload.doc_ids))


@router.get("/{group_id}", response_model=GroupDetailOut)
async def get_group(
    group_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> GroupDetailOut:
    """文件组详情：成员清单 + 各文件结构识别（字段/类型/记录数/主键候选）+ 关系与校验报告。"""
    user: User = request.state.user
    await _require_group_read(db, user, group_id)
    data = await StructuredGroupService(db).detail(group_id)
    group = data["group"]
    return GroupDetailOut(
        group=_group_out(group, len(data["files"])),
        files=[GroupFileOut.model_validate(f) for f in data["files"]],
        relations=data["relations"],
        validation=data["validation"],
    )


@router.get("/{group_id}/processing", response_model=GroupProcessingOut)
async def get_group_processing(
    group_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> GroupProcessingOut:
    """文件组处理概览：走到哪一步、为什么停在这里、已经产生了什么结果。

    只读，不触发任何解析或生成：阶段与状态全部由组内文件的真实解析结果、
    关系是否已确认、校验报告是否存在、关联记录条数推出。
    ``next_action`` 直接告诉前端下一步该做什么（手动配置关联 / 确认 / 校验 / 生成）。
    """
    user: User = request.state.user
    group = await _require_group_read(db, user, group_id)
    files = await StructuredGroupService(db).file_statuses(group_id)
    view = group_processing(group, files)
    return GroupProcessingOut.model_validate(
        {
            **view,
            "group_id": group.id,
            "name": group.name,
            "updated_at": group.updated_at,
            "members": [
                {
                    "doc_id": f["doc_id"],
                    "file_name": f["current_file_name"] or f["file_name"],
                    "alias": f["alias"],
                    "parse_status": f["parse_status"],
                    "record_count": f["record_count"],
                    "error_message": f["error_message"],
                    "removed": f["removed"],
                }
                for f in files
            ],
        }
    )


@router.patch("/{group_id}", response_model=GroupOut)
async def rename_group(
    group_id: uuid.UUID,
    payload: GroupRenameIn,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> GroupOut:
    """重命名文件组（只改展示名，不动成员、关系与已生成的记录）。"""
    user: User = request.state.user
    group = await _require_group_write(db, user, group_id)
    group.name = payload.name.strip()
    await db.commit()
    await db.refresh(group)
    counts = await _file_counts(db, [group.id])
    return _group_out(group, counts.get(str(group.id), 0))


@router.delete("/{group_id}")
async def delete_group(
    group_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """删除文件组与它的关联记录。

    只清理关联层（structured_groups / _group_files / _group_records），**不删除组内文件
    本身**：那些文件仍属于结构化数据空间，第三阶段的单文件解析结果继续可用。
    """
    user: User = request.state.user
    await _require_group_write(db, user, group_id)
    await StructuredGroupService(db).delete_group(group_id)
    logger.info(f"关联数据文件组已删除 id={group_id} 用户={user.id}")
    return {"id": str(group_id), "deleted": True}


# ── 成员维护 ────────────────────────────────────────────────────────────────


@router.post("/{group_id}/files", response_model=GroupOut)
async def add_group_files(
    group_id: uuid.UUID,
    payload: GroupFilesIn,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> GroupOut:
    """向文件组补充成员文件（组构成变化后必须重新校验关系）。"""
    user: User = request.state.user
    _check_file_limit(payload.doc_ids)
    await _require_group_write(db, user, group_id)
    group = await StructuredGroupService(db).add_files(group_id, payload.doc_ids)
    counts = await _file_counts(db, [group.id])
    return _group_out(group, counts.get(str(group.id), 0))


@router.patch("/{group_id}/files/{doc_id}", response_model=GroupOut)
async def set_group_file_alias(
    group_id: uuid.UUID,
    doc_id: uuid.UUID,
    payload: GroupAliasIn,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> GroupOut:
    """给组内文件起角色名（如「学生表」「选课表」），只影响关联文本可读性。"""
    user: User = request.state.user
    await _require_group_write(db, user, group_id)
    await StructuredGroupService(db).set_file_alias(group_id, doc_id, payload.alias)
    data = await StructuredGroupService(db).detail(group_id)
    return _group_out(data["group"], len(data["files"]))


@router.delete("/{group_id}/files/{doc_id}", response_model=GroupOut)
async def remove_group_file(
    group_id: uuid.UUID,
    doc_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> GroupOut:
    """从文件组移出成员（不删除文件本身）。

    引用该文件的关系一并删除，避免留下无效关系；剩余文件不足 2 个时既有正式记录作废，
    组转入 stale，必须重新校验并确认。
    """
    user: User = request.state.user
    await _require_group_write(db, user, group_id)
    group = await StructuredGroupService(db).remove_file(group_id, doc_id)
    counts = await _file_counts(db, [group.id])
    return _group_out(group, counts.get(str(group.id), 0))


# ── 结构识别与关系配置 ──────────────────────────────────────────────────────


@router.put("/{group_id}/relations", response_model=GroupOut)
async def save_group_relations(
    group_id: uuid.UUID,
    payload: RelationsIn,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> GroupOut:
    """整份保存关系配置（只能由用户手动选择两侧文件与关联字段）。

    关系一变，校验报告立即作废；若此前已生成正式关联记录，整组转 stale，
    必须重新校验并再次确认——不允许「旧记录 + 新关系」混在一起。
    """
    user: User = request.state.user
    await _require_group_write(db, user, group_id)
    relations = [r.model_dump() for r in payload.relations]
    group = await StructuredGroupService(db).save_relations(group_id, relations)
    counts = await _file_counts(db, [group.id])
    return _group_out(group, counts.get(str(group.id), 0))


@router.post("/{group_id}/relations/confirmation", response_model=GroupOut)
async def confirm_group_relation(
    group_id: uuid.UUID,
    payload: RelationConfirmIn,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> GroupOut:
    """逐条确认/取消确认某条关系（兼容历史遗留的未确认关系）。"""
    user: User = request.state.user
    await _require_group_write(db, user, group_id)
    group = await StructuredGroupService(db).set_relation_confirmation(
        group_id, payload.relation_id, payload.confirmed
    )
    counts = await _file_counts(db, [group.id])
    return _group_out(group, counts.get(str(group.id), 0))


@router.post("/{group_id}/validate")
async def validate_group_relations(
    group_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """正式生成前的校验报告：主键重复 / 外键缺失 / 外键无法匹配 / 孤立记录 /
    字段类型不一致 / 重复关联关系 / 未经确认的关系。

    只报告，不修改也不丢弃任何原始记录；报告里每条关系都带匹配、未匹配、重复与孤立计数。
    """
    user: User = request.state.user
    await _require_group_write(db, user, group_id)
    return await StructuredGroupService(db).validate(group_id)


@router.post("/{group_id}/confirm")
async def confirm_group(
    group_id: uuid.UUID,
    payload: GroupConfirmIn,
    request: Request,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """按已确认关系生成正式关联知识记录（用户确认后才落库）。

    - 存在结构错误（文件/字段已不存在）或未确认关系 → 400，不生成；
    - 存在外键对不上且未传 ``force`` → 409，前端须先展示明细再让用户明确知情；
      force 之后仍会保留这些原始行并附带提示，不丢行、不改值；
    - 生成在内存里算完再整份替换（删旧 + 插新同一事务），任何失败都回滚并保留旧结果；
    - 关联事实提交后置 ``vector_status=pending`` 并入队：真正开始 Embedding 才转为
      indexing，接口立即返回；失败只把状态记为 failed，旧向量保留到新向量替换成功为止。
    """
    user: User = request.state.user
    await _require_group_write(db, user, group_id)
    result = await StructuredGroupService(db).confirm(
        group_id,
        str(payload.anchor_doc_id) if payload.anchor_doc_id else None,
        payload.force,
        sync_vectors=False,
    )
    await db.execute(
        update(StructuredGroup)
        .where(StructuredGroup.id == group_id)
        .values(vector_status=VECTOR_STATUS_PENDING, vector_error="")
    )
    await db.commit()
    # 不自动向量化：确认后仅置 pending，等用户在列表里点「开始向量化」再派发，
    # 避免一次确认就把整组记录排进长时间 CPU 队列。
    logger.info(
        f"关联记录已确认生成，待手动向量化 group_id={group_id} "
        f"条数={result['record_count']} 用户={user.id}"
    )
    return {
        "group_id": str(group_id),
        **result,
        "vector_status": VECTOR_STATUS_PENDING,
    }


@router.post("/{group_id}/revectorize", response_model=GroupOut, status_code=202)
async def revectorize_group(
    group_id: uuid.UUID,
    request: Request,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
) -> GroupOut:
    """手动重建文件组的统一向量索引（派发后台任务后立即返回）。

    - 只有 ``status=confirmed`` 且已有正式关联记录的组才允许写入：未确认或已失效
      （stale / draft）的组一律 409，避免把不可信关联送进检索；
    - 已在等待队列或正在索引返回 409，崩溃残留（超过 30 分钟未推进）放行；
    - 先置 ``vector_status=pending`` 再入队（真正开始才转 indexing），失败时服务层
      只记 failed，旧向量保留到新向量替换成功为止，不会出现「先清空再生成」的空窗。
    """
    user: User = request.state.user
    group = await _require_group_write(db, user, group_id)
    if group.status != STRUCTURED_GROUP_CONFIRMED:
        raise AppException(409, "关联关系未确认或已失效，不能写入向量索引")
    if not group.record_count:
        raise AppException(400, "该文件组还没有可向量化的关联记录，请先生成")
    if (
        group.vector_status == VECTOR_STATUS_INDEXING
        and not is_stale_processing(group.updated_at)
    ) or is_structured_vector_job_active("group", str(group_id)):
        raise AppException(409, "该文件组已在向量化队列中或正在向量化，请等待完成后再试")

    await db.execute(
        update(StructuredGroup)
        .where(StructuredGroup.id == group_id)
        .values(vector_status=VECTOR_STATUS_PENDING, vector_error="")
    )
    await db.commit()
    await db.refresh(group)

    background_tasks.add_task(run_group_vectorize_safely, str(group_id))
    logger.info(f"已派发关联数据向量化 group_id={group_id} 用户={user.id}")
    counts = await _file_counts(db, [group.id])
    return _group_out(group, counts.get(str(group.id), 0))


@router.post("/{group_id}/cancel-vectorize", response_model=GroupOut, status_code=202)
async def cancel_vectorize_group(
    group_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> GroupOut:
    """停止该文件组正在等待或执行的向量化。

    排队中的任务直接移出队列并复位 pending；执行中的任务设置停止标记，服务层在
    下一批记录之间退出，清理本次已写入的部分向量后回到 pending（可重新开始）。
    """
    user: User = request.state.user
    group = await _require_group_write(db, user, group_id)
    outcome = cancel_structured_job("group", str(group_id))
    if outcome == "idle":
        stale = (
            group.vector_status == VECTOR_STATUS_INDEXING
            and is_stale_processing(group.updated_at)
        )
        if not stale:
            raise AppException(409, "该文件组当前没有等待或正在执行的向量化任务")

    await db.execute(
        update(StructuredGroup)
        .where(StructuredGroup.id == group_id)
        .values(
            vector_status=VECTOR_STATUS_PENDING,
            vector_count=0,
            vector_error="已停止向量化",
        )
    )
    await db.commit()
    await db.refresh(group)
    logger.info(f"已请求停止关联数据向量化 group_id={group_id} outcome={outcome}")
    counts = await _file_counts(db, [group.id])
    return _group_out(group, counts.get(str(group.id), 0))


# ── 关联记录读取 ────────────────────────────────────────────────────────────


@router.get("/{group_id}/records", response_model=GroupRecordPage)
async def list_group_records(
    group_id: uuid.UUID,
    request: Request,
    limit: int = Query(20, ge=1, le=_MAX_PAGE_SIZE),
    offset: int = Query(0, ge=0),
    keyword: str | None = Query(None, description="按关联知识文本模糊过滤"),
    only_anomaly: bool = Query(False, description="只看带未匹配/空值等提示的记录"),
    db: AsyncSession = Depends(get_db),
) -> GroupRecordPage:
    """分页浏览关联知识记录（每次只取一页，不做全量加载）。"""
    user: User = request.state.user
    group = await _require_group_read(db, user, group_id)
    rows, total = await StructuredGroupService(db).list_records(
        group_id, limit, offset, keyword, only_anomaly
    )
    return GroupRecordPage(
        items=[_record_out(r) for r in rows],
        total=total,
        limit=limit,
        offset=offset,
        status=group.status,
        record_count=group.record_count,
        error_message=group.error_message or "",
    )


@router.get("/{group_id}/records/{record_id}", response_model=GroupRecordDetail)
async def get_group_record(
    group_id: uuid.UUID,
    record_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> GroupRecordDetail:
    """单条关联记录详情：参与的文件、原始字段与值、行号/工作表/JSON 路径、关联字段与关系。

    record_id 必须属于该文件组，跨组访问一律 404。
    """
    user: User = request.state.user
    group = await _require_group_read(db, user, group_id)
    row = await db.get(StructuredGroupRecord, record_id)
    if row is None or row.group_id != group.id:
        raise NotFoundError("关联记录不存在")

    anchor_name = ""
    if row.anchor_doc_id is not None:
        doc = await db.get(Document, row.anchor_doc_id)
        anchor_name = doc.file_name if doc else ""
    return GroupRecordDetail(
        **_record_out(row).model_dump(),
        group_id=group.id,
        group_name=group.name,
        anchor_file=anchor_name,
        anchor_record_id=row.anchor_record_id,
        parts=list(row.parts or []),
        sources=list(row.sources or []),
        relation_ids=list(row.relation_ids or []),
        anomalies=list(row.anomalies or []),
        created_at=row.created_at,
    )


