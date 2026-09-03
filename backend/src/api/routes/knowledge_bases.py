"""知识库管理路由：创建、列表、删除、成员权限。

安全规范（AGENTS.md §3 / TECH_DESIGN RBAC）：
- 所有端点受 protected_router 统一鉴权（Depends(get_current_user)）
- create_kb：owner_id 取当前登录用户
- list_kbs：按权限过滤（admin 全量 / owner / public / member）
- delete_kb：校验 owner 或 admin
"""

import uuid

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.config import settings
from src.core.deps import get_accessible_kb_ids
from src.core.exceptions import NotFoundError, PermissionDeniedError
from src.domain.models import KnowledgeBase, User
from src.infrastructure.database import get_db

router = APIRouter(prefix="/kb", tags=["knowledge-bases"])


class KnowledgeBaseCreate(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    description: str = Field(default="", max_length=2000)
    visibility: str = Field(default="private", pattern="^(public|private)$")


class KnowledgeBaseOut(KnowledgeBaseCreate):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    owner_id: uuid.UUID


@router.post("", response_model=KnowledgeBaseOut, status_code=201)
async def create_kb(
    payload: KnowledgeBaseCreate,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> KnowledgeBase:
    """创建知识库。

    安全：owner_id 取当前登录用户，不再硬编码 DEFAULT_ADMIN_EMAIL。
    """
    user: User = request.state.user
    kb = KnowledgeBase(**payload.model_dump(), owner_id=user.id)
    db.add(kb)
    await db.commit()
    await db.refresh(kb)
    return kb


@router.get("", response_model=list[KnowledgeBaseOut])
async def list_kbs(
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> list[KnowledgeBase]:
    """知识库列表。

    安全隔离（RBAC）：
    - admin 角色：返回全部知识库
    - 普通用户：owner ∪ member ∪ public 的并集
    """
    user: User = request.state.user
    accessible_ids = await get_accessible_kb_ids(db, user)
    rows = (
        await db.scalars(
            select(KnowledgeBase)
            .where(KnowledgeBase.id.in_(accessible_ids))
            .order_by(KnowledgeBase.created_at.desc())
        )
    ).all()
    return list(rows)


@router.delete("/{kb_id}")
async def delete_kb(
    kb_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """删除知识库（documents 外键 CASCADE 级联删除）。

    安全：仅 KB owner 或 admin 可删除。

    TODO：同时清理 Milvus 向量与 MinIO 文件。
    """
    user: User = request.state.user
    kb = await db.get(KnowledgeBase, kb_id)
    if kb is None:
        raise NotFoundError("知识库不存在")

    # 权限校验：admin 或 owner 可删
    if user.role != "admin" and kb.owner_id != user.id:
        raise PermissionDeniedError("无权删除该知识库")

    await db.delete(kb)
    await db.commit()
    return {"id": str(kb_id), "deleted": True}


@router.get("/{kb_id}/members")
async def list_members(kb_id: uuid.UUID, db: AsyncSession = Depends(get_db)) -> list[dict]:
    """协作成员列表。TODO: 查询 kb_members 表。"""
    return []


@router.post("/{kb_id}/members")
async def add_member(kb_id: uuid.UUID, user_id: str, role: str = "viewer") -> dict:
    """添加协作成员（role: owner/editor/viewer）。TODO: 查询并校验用户。"""
    if role not in {"owner", "editor", "viewer"}:
        from fastapi import HTTPException

        raise HTTPException(status_code=400, detail="非法角色")
    return {"kb_id": str(kb_id), "user_id": user_id, "role": role}
