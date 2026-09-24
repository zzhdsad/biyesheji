"""知识库管理路由：创建、列表、编辑、删除（回收站）、成员管理、转移所有权。

BUSINESS_RULES §3 知识库：
- 创建者自动为 Owner
- 公开/私有可见性
- 删除移入回收站（默认7天，可配置1-30天）
- 知识库角色：Owner/Admin/Editor/Viewer
- 成员管理：添加/批量添加/移除/修改角色/转移所有权
- 边界：最后一个 Owner 不可移除或降级

安全规范：
- 所有端点受 protected_router 统一鉴权
- list_kbs 按权限过滤（admin 全量 / owner / public / member）
- 增删改校验 owner 或 admin
"""

import uuid
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from loguru import logger
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.application.audit_service import AuditService
from src.core.config import settings
from src.core.deps import get_accessible_kb_ids
from src.core.exceptions import AppException, NotFoundError, PermissionDeniedError
from src.domain.models import (
    Herb,
    KBMember,
    KnowledgeBase,
    KnowledgeBaseResource,
    Literature,
    Prescription,
    Theory,
    User,
)
from src.infrastructure.database import get_db
from src.utils.timeutil import utcnow

router = APIRouter(prefix="/kb", tags=["knowledge-bases"])

KB_ROLES = {"owner", "admin", "editor", "viewer"}
TRASH_RETENTION_DAYS = settings.TRASH_RETENTION_DAYS

# TASK-008：KB 可挂载的传统资源类型（多态关联的 resource_type 受控词表）
RESOURCE_TYPES = ("herb", "prescription", "theory", "literature")


# ── Schemas ──────────────────────────────────────────────────────────────────


class KnowledgeBaseCreate(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    description: str = Field(default="", max_length=2000)
    visibility: str = Field(default="private", pattern="^(public|private)$")


class KnowledgeBaseOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    description: str
    visibility: str
    owner_id: uuid.UUID
    deleted_at: datetime | None = None


class KnowledgeBaseUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=128)
    description: str | None = Field(default=None, max_length=2000)
    visibility: str | None = Field(default=None, pattern="^(public|private)$")


class MemberOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    kb_id: uuid.UUID
    user_id: uuid.UUID
    role: str


class MemberAddRequest(BaseModel):
    user_id: uuid.UUID
    role: str = Field(default="viewer")

    @classmethod
    def _role_valid(cls, v: str) -> str:
        if v not in KB_ROLES:
            raise ValueError(f"角色必须是 {KB_ROLES} 之一")
        return v


class MemberBatchAddRequest(BaseModel):
    user_ids: list[uuid.UUID] = Field(min_length=1)
    role: str = Field(default="viewer")


class MemberRoleUpdate(BaseModel):
    role: str

    @classmethod
    def _role_valid(cls, v: str) -> str:
        if v not in {"admin", "editor", "viewer"}:
            raise ValueError("仅可切换为 admin/editor/viewer")
        return v


class TransferOwnershipRequest(BaseModel):
    new_owner_user_id: uuid.UUID


# ── Helpers ──────────────────────────────────────────────────────────────────


def _get_client_ip(request: Request) -> str:
    return request.client.host if request.client else ""


async def _require_kb_owner_or_admin(db: AsyncSession, request: Request, kb_id: uuid.UUID) -> KnowledgeBase:
    """校验当前用户是 KB owner 或系统 admin。返回知识库。"""
    user: User = request.state.user
    kb = await db.get(KnowledgeBase, kb_id)
    if kb is None or kb.deleted_at is not None:
        raise NotFoundError("知识库不存在")
    if user.role != "admin" and kb.owner_id != user.id:
        raise PermissionDeniedError("仅知识库 Owner 或管理员可操作")
    return kb


async def _require_kb_member_or_admin(db: AsyncSession, request: Request, kb_id: uuid.UUID) -> KnowledgeBase:
    """校验当前用户可访问该知识库（member/owner/admin）。返回知识库。"""
    user: User = request.state.user
    kb = await db.get(KnowledgeBase, kb_id)
    if kb is None or kb.deleted_at is not None:
        raise NotFoundError("知识库不存在")
    if user.role == "admin":
        return kb
    accessible = await get_accessible_kb_ids(db, user)
    if kb_id not in accessible:
        raise PermissionDeniedError("无权访问该知识库")
    return kb


# ── 知识库 CRUD ──────────────────────────────────────────────────────────────


@router.post("", response_model=KnowledgeBaseOut, status_code=201)
async def create_kb(
    payload: KnowledgeBaseCreate,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> KnowledgeBase:
    """创建知识库。创建者自动为 Owner，同时写入 KBMember 记录。"""
    user: User = request.state.user
    kb = KnowledgeBase(**payload.model_dump(), owner_id=user.id)
    db.add(kb)
    await db.flush()
    # 创建者自动成为 Owner 成员
    db.add(KBMember(kb_id=kb.id, user_id=user.id, role="owner"))
    await db.commit()
    await db.refresh(kb)

    audit = AuditService(db)
    await audit.log(
        operator_id=user.id, operator_name=user.username,
        operation="create", target_type="kb", target_id=str(kb.id),
        detail={"name": kb.name, "visibility": kb.visibility},
        ip=_get_client_ip(request),
    )
    return kb


@router.get("", response_model=list[KnowledgeBaseOut])
async def list_kbs(
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> list[KnowledgeBase]:
    """知识库列表（按权限过滤，排除回收站）。"""
    user: User = request.state.user
    accessible_ids = await get_accessible_kb_ids(db, user)
    rows = (
        await db.scalars(
            select(KnowledgeBase)
            .where(KnowledgeBase.id.in_(accessible_ids), KnowledgeBase.deleted_at.is_(None))
            .order_by(KnowledgeBase.created_at.desc())
        )
    ).all()
    return list(rows)


@router.put("/{kb_id}", response_model=KnowledgeBaseOut)
async def update_kb(
    kb_id: uuid.UUID,
    payload: KnowledgeBaseUpdate,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> KnowledgeBase:
    """编辑知识库元信息（仅 owner 或 admin）。"""
    user: User = request.state.user
    kb = await _require_kb_owner_or_admin(db, request, kb_id)

    changes = payload.model_dump(exclude_unset=True)
    old_values = {k: getattr(kb, k) for k in changes}
    for field, value in changes.items():
        setattr(kb, field, value)
    await db.commit()
    await db.refresh(kb)

    audit = AuditService(db)
    await audit.log(
        operator_id=user.id, operator_name=user.username,
        operation="update", target_type="kb", target_id=str(kb.id),
        detail={"old": old_values, "new": changes},
        ip=_get_client_ip(request),
    )
    return kb


@router.delete("/{kb_id}")
async def delete_kb(
    kb_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """删除知识库 → 移入回收站（软删除，{retention} 天内可恢复）。

    仅 Owner 或 admin 可删除。移入回收站后列表和检索中均不可见。
    """
    user: User = request.state.user
    kb = await _require_kb_owner_or_admin(db, request, kb_id)

    kb.deleted_at = utcnow()
    await db.commit()

    audit = AuditService(db)
    await audit.log(
        operator_id=user.id, operator_name=user.username,
        operation="delete", target_type="kb", target_id=str(kb.id),
        detail={"name": kb.name, "retention_days": TRASH_RETENTION_DAYS},
        ip=_get_client_ip(request),
    )
    return {"id": str(kb_id), "deleted": True, "message": f"已移入回收站，{TRASH_RETENTION_DAYS} 天内可恢复"}


# ── 知识库回收站 ──────────────────────────────────────────────────────────────


@router.get("/trash", response_model=list[KnowledgeBaseOut])
async def list_trash_kbs(
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> list[KnowledgeBase]:
    """回收站知识库列表（仅 admin，自动清理过期项）。"""
    user: User = request.state.user
    if user.role != "admin":
        raise PermissionDeniedError("仅管理员可查看回收站")

    # 清理过期项
    cutoff = utcnow() - timedelta(days=TRASH_RETENTION_DAYS)
    expired = (await db.scalars(select(KnowledgeBase).where(KnowledgeBase.deleted_at < cutoff))).all()
    for kb in expired:
        await db.delete(kb)
    if expired:
        await db.commit()

    rows = (
        await db.scalars(
            select(KnowledgeBase)
            .where(KnowledgeBase.deleted_at.is_not(None))
            .order_by(KnowledgeBase.deleted_at.desc())
        )
    ).all()
    return list(rows)


@router.post("/{kb_id}/restore", response_model=KnowledgeBaseOut)
async def restore_kb(
    kb_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> KnowledgeBase:
    """从回收站恢复知识库（仅 admin）。"""
    user: User = request.state.user
    if user.role != "admin":
        raise PermissionDeniedError("仅管理员可恢复")

    kb = await db.get(KnowledgeBase, kb_id)
    if kb is None or kb.deleted_at is None:
        raise NotFoundError("知识库不在回收站中")

    kb.deleted_at = None
    await db.commit()
    await db.refresh(kb)

    audit = AuditService(db)
    await audit.log(
        operator_id=user.id, operator_name=user.username,
        operation="restore", target_type="kb", target_id=str(kb.id),
        detail={"name": kb.name}, ip=_get_client_ip(request),
    )
    return kb


@router.delete("/{kb_id}/purge")
async def purge_kb(
    kb_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """彻底删除知识库（仅 admin，不可恢复）。

    Stage 4-6 + BUG-007：purge 前清理该 KB 下的**全部**向量：
    - Resource 向量（按 KBR 逐条清理，KBR 记录随 KB 删除 CASCADE 清理）；
    - Document 向量（按 kb_id 批量清理，覆盖文档 chunk 与任何遗留行）。

    清理失败即中断 purge：KB 删除不可逆，残留向量会以"未知文档"形式被检索到。
    """
    user: User = request.state.user
    if user.role != "admin":
        raise PermissionDeniedError("仅管理员可彻底删除")

    kb = await db.get(KnowledgeBase, kb_id)
    if kb is None:
        raise NotFoundError("知识库不存在")
    # BUG-043：purge 是不可逆硬删除，必须先经过软删（回收站保护期）。
    # 旧实现允许直接彻底删除活跃知识库，绕过回收站的可恢复窗口与审计链路。
    if kb.deleted_at is None:
        raise AppException(400, "只能彻底删除回收站中的知识库，请先执行删除移入回收站")
    kb_name = kb.name

    # 1) Resource 向量（KBR 记录随 KB 删除 CASCADE 清理）
    try:
        from src.application.resource_vector_service import ResourceVectorService
        svc = ResourceVectorService()
        await svc.cleanup_kb_resource_vectors(db, kb_id)
    except Exception as exc:
        logger.error(f"KB purge 前清理 Resource 向量失败 kb_id={kb_id}: {exc}")
        raise AppException(422, f"知识库资源向量清理失败，无法彻底删除：{exc}") from exc

    # 2) BUG-007：该 KB 下的全部向量（Document chunk 等），按 kb_id 兜底清理
    try:
        from src.infrastructure.milvus_store import get_vector_store
        get_vector_store().delete_by_kb(str(kb_id))
    except Exception as exc:
        logger.error(f"KB purge 前清理知识库向量失败 kb_id={kb_id}: {exc}")
        raise AppException(422, f"知识库向量清理失败，无法彻底删除：{exc}") from exc

    await db.delete(kb)
    await db.commit()

    audit = AuditService(db)
    await audit.log(
        operator_id=user.id, operator_name=user.username,
        operation="purge", target_type="kb", target_id=str(kb_id),
        detail={"name": kb_name}, ip=_get_client_ip(request),
    )
    return {"id": str(kb_id), "purged": True}


# ── 成员管理 ────────────────────────────────────────────────────────────────


@router.get("/{kb_id}/members", response_model=list[MemberOut])
async def list_members(
    kb_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> list[KBMember]:
    """知识库成员列表（Owner/Admin/Editor/Viewer）。

    需要 KB 访问权限。owner_id 对应的用户作为 Owner 角色展示。
    """
    await _require_kb_member_or_admin(db, request, kb_id)
    rows = (
        await db.scalars(
            select(KBMember).where(KBMember.kb_id == kb_id)
        )
    ).all()
    return list(rows)


@router.post("/{kb_id}/members", response_model=MemberOut, status_code=201)
async def add_member(
    kb_id: uuid.UUID,
    payload: MemberAddRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> KBMember:
    """添加成员（仅 Owner 或 KB Admin）。

    边界规则：已存在的用户自动跳过并提示（此处直接 409 提示）。
    不能添加 owner 角色（仅通过转移所有权实现）。
    """
    user: User = request.state.user
    kb = await _require_kb_owner_or_admin(db, request, kb_id)

    if payload.role == "owner":
        raise HTTPException(status_code=400, detail="不能直接添加 Owner，请使用转移所有权")

    # 校验目标用户存在且未被删除
    target = await db.scalar(
        select(User).where(User.id == payload.user_id, User.deleted_at.is_(None))
    )
    if target is None:
        raise NotFoundError("用户不存在")

    # 检查是否已是成员
    existing = await db.scalar(
        select(KBMember).where(
            KBMember.kb_id == kb_id, KBMember.user_id == payload.user_id
        )
    )
    if existing is not None:
        raise HTTPException(status_code=409, detail="该用户已是知识库成员")

    member = KBMember(kb_id=kb_id, user_id=payload.user_id, role=payload.role)
    db.add(member)
    await db.commit()
    await db.refresh(member)

    audit = AuditService(db)
    await audit.log(
        operator_id=user.id, operator_name=user.username,
        operation="add_member", target_type="kb", target_id=str(kb_id),
        detail={"user_id": str(payload.user_id), "role": payload.role},
        ip=_get_client_ip(request),
    )
    return member


@router.post("/{kb_id}/members/batch")
async def batch_add_members(
    kb_id: uuid.UUID,
    payload: MemberBatchAddRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """批量添加成员（仅 Owner 或 KB Admin）。已存在的用户自动跳过。"""
    user: User = request.state.user
    await _require_kb_owner_or_admin(db, request, kb_id)

    if payload.role == "owner":
        raise HTTPException(status_code=400, detail="不能批量添加 Owner")

    added, skipped = [], []
    for uid in payload.user_ids:
        existing = await db.scalar(
            select(KBMember).where(
                KBMember.kb_id == kb_id, KBMember.user_id == uid
            )
        )
        if existing is not None:
            skipped.append(str(uid))
            continue
        target = await db.scalar(
            select(User).where(User.id == uid, User.deleted_at.is_(None))
        )
        if target is None:
            skipped.append(str(uid))
            continue
        db.add(KBMember(kb_id=kb_id, user_id=uid, role=payload.role))
        added.append(str(uid))
    await db.commit()

    audit = AuditService(db)
    await audit.log(
        operator_id=user.id, operator_name=user.username,
        operation="batch_add_members", target_type="kb", target_id=str(kb_id),
        detail={"added": added, "skipped": skipped, "role": payload.role},
        ip=_get_client_ip(request),
    )
    return {"added": len(added), "skipped": len(skipped), "added_ids": added, "skipped_ids": skipped}


@router.delete("/{kb_id}/members/{user_id}")
async def remove_member(
    kb_id: uuid.UUID,
    user_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """移除成员（仅 Owner 或 KB Admin，二次确认由前端处理）。

    边界规则：最后一个 Owner 不可被移除。
    移除后其历史文档和问答记录保留。
    """
    user: User = request.state.user
    kb = await _require_kb_owner_or_admin(db, request, kb_id)

    member = await db.scalar(
        select(KBMember).where(
            KBMember.kb_id == kb_id, KBMember.user_id == user_id
        )
    )
    if member is None:
        raise NotFoundError("该用户不是知识库成员")

    # 边界：最后一个 Owner 不可移除
    if member.role == "owner":
        owner_count = (
            await db.scalars(
                select(KBMember).where(
                    KBMember.kb_id == kb_id, KBMember.role == "owner"
                )
            )
        ).all()
        if len(owner_count) <= 1:
            raise HTTPException(
                status_code=400,
                detail="最后一个 Owner 不可移除，请先转移所有权",
            )

    await db.delete(member)
    await db.commit()

    audit = AuditService(db)
    await audit.log(
        operator_id=user.id, operator_name=user.username,
        operation="remove_member", target_type="kb", target_id=str(kb_id),
        detail={"removed_user_id": str(user_id)}, ip=_get_client_ip(request),
    )
    return {"kb_id": str(kb_id), "removed_user_id": str(user_id)}


@router.put("/{kb_id}/members/{user_id}", response_model=MemberOut)
async def update_member_role(
    kb_id: uuid.UUID,
    user_id: uuid.UUID,
    payload: MemberRoleUpdate,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> KBMember:
    """修改成员角色（仅 Owner 或 KB Admin，在 admin/editor/viewer 间切换）。

    边界：最后一个 Owner 不可降级。
    """
    user: User = request.state.user
    kb = await _require_kb_owner_or_admin(db, request, kb_id)

    if payload.role not in {"admin", "editor", "viewer"}:
        raise HTTPException(status_code=400, detail="仅可切换为 admin/editor/viewer")

    member = await db.scalar(
        select(KBMember).where(
            KBMember.kb_id == kb_id, KBMember.user_id == user_id
        )
    )
    if member is None:
        raise NotFoundError("该用户不是知识库成员")

    # 边界：最后一个 Owner 不可降级
    if member.role == "owner":
        owner_count = (
            await db.scalars(
                select(KBMember).where(
                    KBMember.kb_id == kb_id, KBMember.role == "owner"
                )
            )
        ).all()
        if len(owner_count) <= 1:
            raise HTTPException(
                status_code=400,
                detail="最后一个 Owner 不可降级，请先转移所有权",
            )

    old_role = member.role
    member.role = payload.role
    await db.commit()
    await db.refresh(member)

    audit = AuditService(db)
    await audit.log(
        operator_id=user.id, operator_name=user.username,
        operation="update_member_role", target_type="kb", target_id=str(kb_id),
        detail={"user_id": str(user_id), "old_role": old_role, "new_role": payload.role},
        ip=_get_client_ip(request),
    )
    return member


@router.post("/{kb_id}/transfer-ownership")
async def transfer_ownership(
    kb_id: uuid.UUID,
    payload: TransferOwnershipRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """转移所有权（仅 Owner）。

    将 Owner 角色转移给其他成员，原 Owner 自动降级为 Admin。
    目标用户必须是当前知识库的成员。
    """
    user: User = request.state.user
    kb = await db.get(KnowledgeBase, kb_id)
    if kb is None or kb.deleted_at is not None:
        raise NotFoundError("知识库不存在")

    # 仅系统 admin 或当前 KB owner 可转移
    if user.role != "admin" and kb.owner_id != user.id:
        raise PermissionDeniedError("仅 Owner 可转移所有权")

    # 校验目标用户是成员
    target_member = await db.scalar(
        select(KBMember).where(
            KBMember.kb_id == kb_id, KBMember.user_id == payload.new_owner_user_id
        )
    )
    if target_member is None:
        raise NotFoundError("目标用户不是知识库成员，请先添加为成员")

    # BUG-044：KBMember 行会随用户软删/停用而残留（成员表不级联），
    # 仅校验成员关系会把所有权转移给已删除或已停用的账号 → 知识库变成孤儿。
    target_user = await db.get(User, payload.new_owner_user_id)
    if (
        target_user is None
        or target_user.deleted_at is not None
        or not target_user.is_active
    ):
        raise AppException(400, "目标用户不存在或已删除/停用，无法转移所有权")

    # 获取当前 Owner 成员记录
    old_owner_member = await db.scalar(
        select(KBMember).where(
            KBMember.kb_id == kb_id, KBMember.role == "owner"
        )
    )

    # 执行转移
    if old_owner_member is not None:
        old_owner_member.role = "admin"  # 原 Owner 降级为 Admin
    target_member.role = "owner"
    kb.owner_id = payload.new_owner_user_id
    await db.commit()

    audit = AuditService(db)
    await audit.log(
        operator_id=user.id, operator_name=user.username,
        operation="transfer_ownership", target_type="kb", target_id=str(kb_id),
        detail={
            "old_owner": str(user.id),
            "new_owner": str(payload.new_owner_user_id),
        },
        ip=_get_client_ip(request),
    )
    return {"kb_id": str(kb_id), "new_owner_id": str(payload.new_owner_user_id)}


# ── 资源挂载（TASK-008）─────────────────────────────────────────────────────
# 多态关联：resource_type + resource_id，不在此层建立到具体资源表的 ORM relationship。
# 资源存在性与类型-UUID 匹配由应用层显式查询对应表校验。


class ResourceMountRequest(BaseModel):
    """挂载请求体：resource_type 受控词表 + UUID。"""

    resource_type: str = Field(min_length=1, max_length=16)
    resource_id: uuid.UUID


class ResourceMountedOut(BaseModel):
    """挂载记录输出：含真实资源名（由对应资源表查询填充，非 resource_id 推测）。"""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    knowledge_base_id: uuid.UUID
    resource_type: str
    resource_id: uuid.UUID
    resource_name: str
    created_at: datetime


class ResourceListResponse(BaseModel):
    items: list[ResourceMountedOut]
    total: int
    limit: int
    offset: int


# resource_type → ModelCls 映射；保持显式 if/elif，避免动态 getattr 带来的类型混淆
_RESOURCE_MODELS = {
    "herb": Herb,
    "prescription": Prescription,
    "theory": Theory,
    "literature": Literature,
}


def _validate_resource_type(resource_type: str) -> None:
    """resource_type 必须在受控词表内，否则 400。

    与 herbs.py 的 _validate_prescription_category / MemberAddRequest._role_valid
    同构：显式 400 而非依赖 Pydantic validator。
    """
    if resource_type not in RESOURCE_TYPES:
        raise HTTPException(
            status_code=400,
            detail=f"resource_type 必须是 {list(RESOURCE_TYPES)} 之一",
        )


async def _resolve_resource_name(
    db: AsyncSession, resource_type: str, resource_id: uuid.UUID
) -> str | None:
    """按 resource_type 路由到对应表，查询主键并返回 name；不存在返回 None。

    严格匹配：resource_type='herb' 只查 herbs 表；不会"任一表存在即通过"。
    """
    obj = await _load_resource(db, resource_type, resource_id)
    return obj.name if obj is not None else None


async def _load_resource(
    db: AsyncSession, resource_type: str, resource_id: uuid.UUID
):
    """按 resource_type 路由到对应表，加载资源实例；不存在返回 None。

    Stage 4-3：挂载后向量化需要资源实例（字段映射生成 Canonical Text），
    不再只取 name。
    """
    model_cls = _RESOURCE_MODELS.get(resource_type)
    if model_cls is None:
        return None
    return await db.get(model_cls, resource_id)


async def _enrich_resources_with_names(
    db: AsyncSession, items: list[KnowledgeBaseResource]
) -> list[ResourceMountedOut]:
    """批量解析 resource_name（按 resource_type 分组查询，避免 N+1）。

    与 _enrich_hits_with_doc_name 同样采用按类型分组批量 SELECT 模式。
    """
    by_type: dict[str, list[uuid.UUID]] = {}
    for it in items:
        by_type.setdefault(it.resource_type, []).append(it.resource_id)

    # 按 resource_type 分组批量查 name
    name_by_type: dict[str, dict[uuid.UUID, str]] = {}
    for rtype, rids in by_type.items():
        model_cls = _RESOURCE_MODELS.get(rtype)
        if model_cls is None:
            name_by_type[rtype] = {}
            continue
        rows = (
            await db.scalars(select(model_cls).where(model_cls.id.in_(rids)))
        ).all()
        name_by_type[rtype] = {r.id: r.name for r in rows}

    return [
        ResourceMountedOut(
            id=it.id,
            knowledge_base_id=it.knowledge_base_id,
            resource_type=it.resource_type,
            resource_id=it.resource_id,
            resource_name=name_by_type.get(it.resource_type, {}).get(it.resource_id, ""),
            created_at=it.created_at,
        )
        for it in items
    ]


@router.get("/{kb_id}/resources", response_model=ResourceListResponse)
async def list_kb_resources(
    kb_id: uuid.UUID,
    request: Request,
    resource_type: str | None = None,
    # BUG-033：分页参数加边界约束（与 herbs/prescriptions 等列表端点一致），
    # 避免 limit=0/负数/超大值导致的空列表或全表拉取
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    db: AsyncSession = Depends(get_db),
) -> ResourceListResponse:
    """查看 KB 已挂载资源列表（Owner/Admin/Editor/Viewer 均可读）。

    支持 resource_type 过滤 + 分页。resource_name 由对应资源表批量查询填充。
    """
    await _require_kb_member_or_admin(db, request, kb_id)

    # 过滤条件：resource_type 非法时显式 400（而非 422），与 MemberAddRequest 一致
    if resource_type is not None:
        _validate_resource_type(resource_type)

    base = select(KnowledgeBaseResource).where(
        KnowledgeBaseResource.knowledge_base_id == kb_id
    )
    count_stmt = select(KnowledgeBaseResource).where(
        KnowledgeBaseResource.knowledge_base_id == kb_id
    )
    if resource_type is not None:
        base = base.where(KnowledgeBaseResource.resource_type == resource_type)
        count_stmt = count_stmt.where(
            KnowledgeBaseResource.resource_type == resource_type
        )

    total = (
        await db.scalar(
            select(func.count()).select_from(count_stmt.subquery())
        )
        or 0
    )

    rows = (
        await db.scalars(
            base.order_by(KnowledgeBaseResource.created_at.desc()).limit(limit).offset(offset)
        )
    ).all()
    items = list(rows)
    out = await _enrich_resources_with_names(db, items)
    return ResourceListResponse(
        items=out, total=total, limit=limit, offset=offset
    )


@router.post("/{kb_id}/resources", response_model=ResourceMountedOut, status_code=201)
async def mount_resource(
    kb_id: uuid.UUID,
    payload: ResourceMountRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> ResourceMountedOut:
    """挂载传统资源到 KB（仅 Owner 或 KB Admin）。

    校验链：KB 存在 → 当前用户有挂载权限 → resource_type 合法
    → 对应资源表中 resource_id 真实存在（严格类型匹配）→ 无重复挂载 → INSERT
    → 向量化并写入 Milvus（Stage 4-3）→ COMMIT。

    数据库 UNIQUE 作为最终保护；应用层提前判断以返回 409 而非 500。
    向量化在 COMMIT 前执行：失败时 KBR INSERT 一并回滚，KBR 与向量保持一致。
    """
    import asyncio

    from loguru import logger

    from src.application.resource_vector_service import ResourceVectorService

    user: User = request.state.user
    await _require_kb_owner_or_admin(db, request, kb_id)

    _validate_resource_type(payload.resource_type)

    # 严格匹配：只在对应表查询 resource_id；不"任一表存在即通过"
    resource = await _load_resource(
        db, payload.resource_type, payload.resource_id
    )
    if resource is None:
        raise NotFoundError(
            f"{payload.resource_type} (id={payload.resource_id}) 不存在"
        )
    resource_name = resource.name

    # 重复挂载检查：应用层提前判断，返回 409
    existing = await db.scalar(
        select(KnowledgeBaseResource).where(
            KnowledgeBaseResource.knowledge_base_id == kb_id,
            KnowledgeBaseResource.resource_type == payload.resource_type,
            KnowledgeBaseResource.resource_id == payload.resource_id,
        )
    )
    if existing is not None:
        raise HTTPException(status_code=409, detail="该资源已挂载到此知识库")

    record = KnowledgeBaseResource(
        knowledge_base_id=kb_id,
        resource_type=payload.resource_type,
        resource_id=payload.resource_id,
    )
    db.add(record)
    await db.flush()  # 分配 id，不 commit；向量化失败时整体回滚

    # Stage 4-3：向量化并写入向量库（CPU/IO 密集放线程池）
    try:
        svc = ResourceVectorService()
        await asyncio.to_thread(
            svc.vectorize_and_store,
            resource,
            kb_id=kb_id,
            resource_type=payload.resource_type,
        )
    except Exception as exc:
        logger.error(
            f"资源向量化失败 {payload.resource_type}={payload.resource_id}: {exc}"
        )
        raise HTTPException(
            status_code=422,
            detail=f"资源向量化失败：{exc}",
        ) from exc

    await db.commit()
    await db.refresh(record)

    audit = AuditService(db)
    await audit.log(
        operator_id=user.id,
        operator_name=user.username,
        operation="mount_resource",
        target_type="kb",
        target_id=str(kb_id),
        detail={
            "resource_type": payload.resource_type,
            "resource_id": str(payload.resource_id),
            "resource_name": resource_name,
        },
        ip=_get_client_ip(request),
    )
    return ResourceMountedOut(
        id=record.id,
        knowledge_base_id=record.knowledge_base_id,
        resource_type=record.resource_type,
        resource_id=record.resource_id,
        resource_name=resource_name,
        created_at=record.created_at,
    )


@router.delete("/{kb_id}/resources/{resource_type}/{resource_id}")
async def unmount_resource(
    kb_id: uuid.UUID,
    resource_type: str,
    resource_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """取消挂载（仅 Owner 或 KB Admin）。

    使用 (resource_type, resource_id) 作为自然复合键，与
    DELETE /kb/{kb_id}/members/{user_id} 风格一致：用户视角的标识是资源本身，
    而非关联行 id。同时匹配 kb_id + resource_type + resource_id 三元组，
    不允许通过其他 KB 的挂载关系删除本 KB 的记录。

    Stage 4-3：删除 KBR 前先按 doc_id 删除 Milvus 向量（幂等）。
    doc_id 按 (Resource+KB) 维度生成，不影响其他 KB 同一资源的向量。
    向量删除失败时抛出异常，KBR 不会被删除，用户可重试卸载。
    """
    import asyncio

    from loguru import logger

    from src.application.resource_vector_service import ResourceVectorService

    user: User = request.state.user
    await _require_kb_owner_or_admin(db, request, kb_id)

    _validate_resource_type(resource_type)

    record = await db.scalar(
        select(KnowledgeBaseResource).where(
            KnowledgeBaseResource.knowledge_base_id == kb_id,
            KnowledgeBaseResource.resource_type == resource_type,
            KnowledgeBaseResource.resource_id == resource_id,
        )
    )
    if record is None:
        raise NotFoundError("该资源未挂载到此知识库")

    # Stage 4-3：先删除向量（幂等），再删 KBR；失败则 KBR 保留，用户可重试
    try:
        svc = ResourceVectorService()
        await asyncio.to_thread(
            svc.delete_vectors,
            kb_id=kb_id,
            resource_type=resource_type,
            resource_id=resource_id,
        )
    except Exception as exc:
        logger.error(
            f"资源向量删除失败 {resource_type}={resource_id} kb={kb_id}: {exc}"
        )
        raise HTTPException(
            status_code=422,
            detail=f"资源向量删除失败：{exc}",
        ) from exc

    await db.delete(record)
    await db.commit()

    audit = AuditService(db)
    await audit.log(
        operator_id=user.id,
        operator_name=user.username,
        operation="unmount_resource",
        target_type="kb",
        target_id=str(kb_id),
        detail={
            "resource_type": resource_type,
            "resource_id": str(resource_id),
        },
        ip=_get_client_ip(request),
    )
    return {
        "kb_id": str(kb_id),
        "resource_type": resource_type,
        "resource_id": str(resource_id),
        "unmounted": True,
    }
