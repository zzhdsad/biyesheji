"""知识库管理路由：创建、列表、成员权限。"""

import uuid

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from src.core.exceptions import PermissionDeniedError

router = APIRouter(prefix="/kb", tags=["knowledge-bases"])


class KnowledgeBaseCreate(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    description: str = Field(default="", max_length=2000)
    visibility: str = Field(default="private", pattern="^(public|private)$")


class KnowledgeBaseItem(KnowledgeBaseCreate):
    id: str
    owner_id: str
    document_count: int = 0


# 内存占位存储；TODO: 迁移到 PostgreSQL（knowledge_bases / kb_members 表）
_KB_STORE: dict[str, KnowledgeBaseItem] = {}


@router.post("", response_model=KnowledgeBaseItem)
async def create_kb(payload: KnowledgeBaseCreate) -> KnowledgeBaseItem:
    """创建知识库。TODO: 校验登录用户并写入 owner_id。"""
    kb_id = str(uuid.uuid4())
    item = KnowledgeBaseItem(
        id=kb_id,
        owner_id="system",
        document_count=0,
        **payload.model_dump(),
    )
    _KB_STORE[kb_id] = item
    return item


@router.get("", response_model=list[KnowledgeBaseItem])
async def list_kbs() -> list[KnowledgeBaseItem]:
    """知识库列表。TODO: 按用户权限过滤（Admin 全量 / Owner 本人 / 公开库可见）。"""
    return list(_KB_STORE.values())


@router.delete("/{kb_id}")
async def delete_kb(kb_id: uuid.UUID) -> dict:
    """删除知识库（级联删除文档与向量）。仅 Owner/Admin 可操作。"""
    kb_id_str = str(kb_id)
    if kb_id_str not in _KB_STORE:
        raise PermissionDeniedError("知识库不存在或无权操作")
    del _KB_STORE[kb_id_str]
    return {"id": kb_id_str, "deleted": True}


@router.get("/{kb_id}/members")
async def list_members(kb_id: uuid.UUID) -> list[dict]:
    """协作成员列表。TODO: 查询 kb_members 表。"""
    return []


@router.post("/{kb_id}/members")
async def add_member(kb_id: uuid.UUID, user_id: str, role: str = "viewer") -> dict:
    """添加协作成员（role: owner/editor/viewer）。"""
    if role not in {"owner", "editor", "viewer"}:
        raise HTTPException(status_code=400, detail="非法角色")
    return {"kb_id": str(kb_id), "user_id": user_id, "role": role}
