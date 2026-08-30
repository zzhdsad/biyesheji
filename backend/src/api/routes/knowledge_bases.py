"""知识库管理路由：创建、列表、删除、成员权限。"""

import uuid

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.config import settings
from src.core.exceptions import NotFoundError
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
    db: AsyncSession = Depends(get_db),
) -> KnowledgeBase:
    """创建知识库。

    TODO: 接入 JWT 鉴权后 owner_id 取当前登录用户，当前使用默认管理员占位。
    """
    owner = await db.scalar(select(User).where(User.email == settings.DEFAULT_ADMIN_EMAIL))
    if owner is None:
        raise NotFoundError("系统未初始化：默认管理员不存在（需启动 PostgreSQL）")
    kb = KnowledgeBase(**payload.model_dump(), owner_id=owner.id)
    db.add(kb)
    await db.commit()
    await db.refresh(kb)
    return kb


@router.get("", response_model=list[KnowledgeBaseOut])
async def list_kbs(db: AsyncSession = Depends(get_db)) -> list[KnowledgeBase]:
    """知识库列表。

    TODO: 按用户权限过滤（Admin 全量 / Owner 本人 / 公开库可见）。
    """
    rows = (
        await db.scalars(select(KnowledgeBase).order_by(KnowledgeBase.created_at.desc()))
    ).all()
    return list(rows)


@router.delete("/{kb_id}")
async def delete_kb(kb_id: uuid.UUID, db: AsyncSession = Depends(get_db)) -> dict:
    """删除知识库（documents 外键 CASCADE 级联删除）。

    TODO: 校验当前用户为 Owner/Admin；同时清理 Milvus 向量与 MinIO 文件。
    """
    kb = await db.get(KnowledgeBase, kb_id)
    if kb is None:
        raise NotFoundError("知识库不存在")
    await db.delete(kb)
    await db.commit()
    return {"id": str(kb_id), "deleted": True}


@router.get("/{kb_id}/members")
async def list_members(kb_id: uuid.UUID) -> list[dict]:
    """协作成员列表。TODO: 查询 kb_members 表。"""
    return []


@router.post("/{kb_id}/members")
async def add_member(kb_id: uuid.UUID, user_id: str, role: str = "viewer") -> dict:
    """添加协作成员（role: owner/editor/viewer）。TODO: 查询并校验用户。"""
    if role not in {"owner", "editor", "viewer"}:
        raise HTTPException(status_code=400, detail="非法角色")
    return {"kb_id": str(kb_id), "user_id": user_id, "role": role}
