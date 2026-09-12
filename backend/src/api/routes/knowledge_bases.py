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

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.application.audit_service import AuditService
from src.core.config import settings
from src.core.deps import get_accessible_kb_ids
from src.core.exceptions import NotFoundError, PermissionDeniedError
from src.domain.models import KBMember, KnowledgeBase, User
from src.infrastructure.database import get_db

router = APIRouter(prefix="/kb", tags=["knowledge-bases"])

KB_ROLES = {"owner", "admin", "editor", "viewer"}
TRASH_RETENTION_DAYS = settings.TRASH_RETENTION_DAYS


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

    kb.deleted_at = datetime.utcnow()
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
    cutoff = datetime.utcnow() - timedelta(days=TRASH_RETENTION_DAYS)
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
    """彻底删除知识库（仅 admin，不可恢复）。"""
    user: User = request.state.user
    if user.role != "admin":
        raise PermissionDeniedError("仅管理员可彻底删除")

    kb = await db.get(KnowledgeBase, kb_id)
    if kb is None:
        raise NotFoundError("知识库不存在")
    kb_name = kb.name
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
