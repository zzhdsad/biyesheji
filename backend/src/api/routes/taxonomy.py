"""分类与标签管理路由（TASK-002）。

- GET/POST/PUT/DELETE /categories
- GET/POST/PUT/DELETE /tags

权限：
- 查询：登录即可（protected_router 统一鉴权）
- 写操作：仅系统 admin
- 写操作接入现有 AuditService

删除保护通过 metadata 反射自动覆盖未来资源表：
- 任何含 category_id 列的表引用该分类 → 409
- 任何含 tag_id 列的关联表引用该标签 → 409
未来新增 herbs / herb_tags 等表时无需修改本文件。
"""

import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import literal, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.application.audit_service import AuditService
from src.core.exceptions import AppException
from src.domain.models import Base, Category, Tag
from src.infrastructure.database import get_db

router = APIRouter(tags=["taxonomy"])

RESOURCE_TYPES = {"herb", "prescription", "theory", "literature"}


# ── Schemas ──────────────────────────────────────────────────────────────────


class CategoryCreate(BaseModel):
    resource_type: str = Field(pattern="^(herb|prescription|theory|literature)$")
    name: str = Field(min_length=1, max_length=64)
    parent_id: uuid.UUID | None = None
    sort_order: int = Field(default=0, ge=0)
    description: str = Field(default="", max_length=2000)


class CategoryUpdate(BaseModel):
    # 修改不允许改 resource_type / parent_id（防止跨树、非法层级）
    name: str | None = Field(default=None, min_length=1, max_length=64)
    sort_order: int | None = Field(default=None, ge=0)
    description: str | None = Field(default=None, max_length=2000)


class CategoryOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    resource_type: str
    name: str
    parent_id: uuid.UUID | None = None
    sort_order: int
    description: str
    created_at: datetime
    updated_at: datetime
    children: list["CategoryOut"] = Field(default_factory=list)


class TagCreate(BaseModel):
    name: str = Field(min_length=1, max_length=32)
    color: str = Field(default="", max_length=16)
    description: str = Field(default="", max_length=2000)


class TagUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=32)
    color: str | None = Field(default=None, max_length=16)
    description: str | None = Field(default=None, max_length=2000)


class TagOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    color: str
    description: str
    created_at: datetime
    updated_at: datetime


# ── Helpers ──────────────────────────────────────────────────────────────────


def _get_client_ip(request: Request) -> str:
    return request.client.host if request.client else ""


async def _get_category_or_404(db: AsyncSession, category_id: uuid.UUID) -> Category:
    category = await db.get(Category, category_id)
    if category is None:
        raise HTTPException(status_code=404, detail="分类不存在")
    return category


async def _same_name_exists(
    db: AsyncSession,
    resource_type: str,
    parent_id: uuid.UUID | None,
    name: str,
    exclude_id: uuid.UUID | None = None,
) -> bool:
    """同一资源域 + 同一父节点（含根节点 NULL）下是否已存在同名分类。"""
    stmt = select(Category.id).where(
        Category.resource_type == resource_type,
        Category.name == name,
    )
    if exclude_id is not None:
        stmt = stmt.where(Category.id != exclude_id)
    # 根节点 parent_id IS NULL 需单独比较
    if parent_id is None:
        stmt = stmt.where(Category.parent_id.is_(None))
    else:
        stmt = stmt.where(Category.parent_id == parent_id)
    return await db.scalar(stmt.limit(1)) is not None


async def _is_category_referenced(
    db: AsyncSession, category_id: uuid.UUID
) -> bool:
    """反射 metadata：任何含 category_id 列的表存在引用即视为被使用。"""
    for table in Base.metadata.sorted_tables:
        col = table.c.get("category_id")
        if col is None:
            continue
        stmt = (
            select(literal(1))
            .select_from(table)
            .where(col == category_id)
            .limit(1)
        )
        if await db.scalar(stmt) is not None:
            return True
    return False


async def _is_tag_referenced(db: AsyncSession, tag_id: uuid.UUID) -> bool:
    """反射 metadata：任何含 tag_id 列的关联表存在引用即视为被使用。"""
    for table in Base.metadata.sorted_tables:
        col = table.c.get("tag_id")
        if col is None:
            continue
        stmt = (
            select(literal(1))
            .select_from(table)
            .where(col == tag_id)
            .limit(1)
        )
        if await db.scalar(stmt) is not None:
            return True
    return False


def _category_to_out(
    node: Category, children: list[CategoryOut] | None = None
) -> CategoryOut:
    """显式构造输出，不访问 ORM 懒加载的 children 关系（避免 MissingGreenlet）。"""
    return CategoryOut(
        id=node.id,
        resource_type=node.resource_type,
        name=node.name,
        parent_id=node.parent_id,
        sort_order=node.sort_order,
        description=node.description,
        created_at=node.created_at,
        updated_at=node.updated_at,
        children=children or [],
    )


def _build_tree(nodes: list[Category]) -> list[CategoryOut]:
    """把平铺节点组装成树；同级按 sort_order、created_at 排序。"""
    by_parent: dict[uuid.UUID | None, list[Category]] = {}
    for node in nodes:
        by_parent.setdefault(node.parent_id, []).append(node)
    for siblings in by_parent.values():
        siblings.sort(key=lambda c: (c.sort_order, c.created_at))

    def attach(parent_id: uuid.UUID | None) -> list[CategoryOut]:
        return [
            _category_to_out(node, children=attach(node.id))
            for node in by_parent.get(parent_id, [])
        ]

    return attach(None)


# ── Categories ───────────────────────────────────────────────────────────────


@router.get("/categories", response_model=list[CategoryOut])
async def list_categories(
    resource_type: str | None = None,
    tree: bool = False,
    db: AsyncSession = Depends(get_db),
):
    """分类列表。可按 resource_type 过滤；tree=true 返回嵌套树。"""
    if resource_type is not None and resource_type not in RESOURCE_TYPES:
        raise HTTPException(
            status_code=400,
            detail=f"非法 resource_type，允许：{sorted(RESOURCE_TYPES)}",
        )

    stmt = select(Category).order_by(
        Category.resource_type, Category.sort_order, Category.created_at
    )
    if resource_type is not None:
        stmt = select(Category).where(
            Category.resource_type == resource_type
        ).order_by(Category.sort_order, Category.created_at)

    nodes = list((await db.scalars(stmt)).all())
    if tree:
        result = _build_tree(nodes)
    else:
        result = [_category_to_out(node) for node in nodes]
    return result


@router.post("/categories", response_model=CategoryOut, status_code=201)
async def create_category(
    payload: CategoryCreate,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    user = request.state.user
    if user.role != "admin":
        raise AppException(403, "仅管理员可管理分类")

    # 父节点：必须存在且属于同一资源域（禁止跨树挂载）
    if payload.parent_id is not None:
        parent = await db.get(Category, payload.parent_id)
        if parent is None:
            raise HTTPException(status_code=400, detail="指定的父分类不存在")
        if parent.resource_type != payload.resource_type:
            raise HTTPException(
                status_code=400, detail="父分类与新分类的资源类型不一致"
            )

    if await _same_name_exists(
        db, payload.resource_type, payload.parent_id, payload.name
    ):
        raise AppException(409, "同一父分类下已存在同名分类")

    category = Category(
        resource_type=payload.resource_type,
        name=payload.name,
        parent_id=payload.parent_id,
        sort_order=payload.sort_order,
        description=payload.description,
    )
    db.add(category)
    await db.flush()

    audit = AuditService(db)
    await audit.log(
        operator_id=user.id,
        operator_name=user.username,
        operation="create",
        target_type="category",
        target_id=str(category.id),
        detail={
            "resource_type": category.resource_type,
            "name": category.name,
            "parent_id": str(category.parent_id) if category.parent_id else None,
        },
        ip=_get_client_ip(request),
    )

    await db.refresh(category)
    return _category_to_out(category)


@router.put("/categories/{category_id}", response_model=CategoryOut)
async def update_category(
    category_id: uuid.UUID,
    payload: CategoryUpdate,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    user = request.state.user
    if user.role != "admin":
        raise AppException(403, "仅管理员可管理分类")

    category = await _get_category_or_404(db, category_id)

    if (
        payload.name is not None
        and payload.name != category.name
        and await _same_name_exists(
            db,
            category.resource_type,
            category.parent_id,
            payload.name,
            exclude_id=category.id,
        )
    ):
        raise AppException(409, "同一父分类下已存在同名分类")

    changed: dict = {}
    for field_name in ("name", "sort_order", "description"):
        value = getattr(payload, field_name)
        if value is not None and getattr(category, field_name) != value:
            changed[field_name] = value
            setattr(category, field_name, value)

    await db.flush()

    audit = AuditService(db)
    await audit.log(
        operator_id=user.id,
        operator_name=user.username,
        operation="update",
        target_type="category",
        target_id=str(category.id),
        detail=changed,
        ip=_get_client_ip(request),
    )

    await db.refresh(category)
    return _category_to_out(category)


@router.delete("/categories/{category_id}", status_code=204)
async def delete_category(
    category_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    user = request.state.user
    if user.role != "admin":
        raise AppException(403, "仅管理员可管理分类")

    category = await _get_category_or_404(db, category_id)

    # 有子分类 → 409
    child = await db.scalar(
        select(Category.id).where(Category.parent_id == category.id).limit(1)
    )
    if child is not None:
        raise AppException(409, "该分类下存在子分类，请先处理子分类")

    # 已被资源引用 → 409
    if await _is_category_referenced(db, category.id):
        raise AppException(409, "该分类已被资源引用，无法删除")

    await db.delete(category)
    await db.flush()

    audit = AuditService(db)
    await audit.log(
        operator_id=user.id,
        operator_name=user.username,
        operation="delete",
        target_type="category",
        target_id=str(category.id),
        detail={"resource_type": category.resource_type, "name": category.name},
        ip=_get_client_ip(request),
    )


# ── Tags ─────────────────────────────────────────────────────────────────────


@router.get("/tags", response_model=list[TagOut])
async def list_tags(
    keyword: str | None = None,
    limit: int = 100,
    offset: int = 0,
    db: AsyncSession = Depends(get_db),
):
    """标签列表，支持关键词模糊查询与分页。"""
    stmt = select(Tag).order_by(Tag.created_at.desc())
    if keyword:
        stmt = stmt.where(Tag.name.ilike(f"%{keyword}%"))
    stmt = stmt.limit(min(max(limit, 1), 200)).offset(max(offset, 0))
    return list((await db.scalars(stmt)).all())


@router.post("/tags", response_model=TagOut, status_code=201)
async def create_tag(
    payload: TagCreate,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    user = request.state.user
    if user.role != "admin":
        raise AppException(403, "仅管理员可管理标签")

    exists = await db.scalar(
        select(Tag.id).where(Tag.name == payload.name).limit(1)
    )
    if exists is not None:
        raise AppException(409, "同名标签已存在")

    tag = Tag(
        name=payload.name, color=payload.color, description=payload.description
    )
    db.add(tag)
    await db.flush()

    audit = AuditService(db)
    await audit.log(
        operator_id=user.id,
        operator_name=user.username,
        operation="create",
        target_type="tag",
        target_id=str(tag.id),
        detail={"name": tag.name},
        ip=_get_client_ip(request),
    )

    await db.refresh(tag)
    return tag


@router.put("/tags/{tag_id}", response_model=TagOut)
async def update_tag(
    tag_id: uuid.UUID,
    payload: TagUpdate,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    user = request.state.user
    if user.role != "admin":
        raise AppException(403, "仅管理员可管理标签")

    tag = await db.get(Tag, tag_id)
    if tag is None:
        raise HTTPException(status_code=404, detail="标签不存在")

    if (
        payload.name is not None
        and payload.name != tag.name
        and await db.scalar(
            select(Tag.id)
            .where(Tag.name == payload.name, Tag.id != tag.id)
            .limit(1)
        )
        is not None
    ):
        raise AppException(409, "同名标签已存在")

    changed: dict = {}
    for field_name in ("name", "color", "description"):
        value = getattr(payload, field_name)
        if value is not None and getattr(tag, field_name) != value:
            changed[field_name] = value
            setattr(tag, field_name, value)

    await db.flush()

    audit = AuditService(
        db
    )
    await audit.log(
        operator_id=user.id,
        operator_name=user.username,
        operation="update",
        target_type="tag",
        target_id=str(tag.id),
        detail=changed,
        ip=_get_client_ip(request),
    )

    await db.refresh(tag)
    return tag


@router.delete("/tags/{tag_id}", status_code=204)
async def delete_tag(
    tag_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    user = request.state.user
    if user.role != "admin":
        raise AppException(403, "仅管理员可管理标签")

    tag = await db.get(Tag, tag_id)
    if tag is None:
        raise HTTPException(status_code=404, detail="标签不存在")

    if await _is_tag_referenced(db, tag.id):
        raise AppException(409, "该标签已被资源引用，无法删除")

    await db.delete(tag)
    await db.flush()

    audit = AuditService(db)
    await audit.log(
        operator_id=user.id,
        operator_name=user.username,
        operation="delete",
        target_type="tag",
        target_id=str(tag.id),
        detail={"name": tag.name},
        ip=_get_client_ip(request),
    )
