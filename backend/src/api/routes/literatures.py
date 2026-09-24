"""中医文献资源管理路由（TASK-006 Stage 4）。

- GET /literatures：登录用户均可浏览，支持 keyword / category_id / tag_id + 分页
- GET /literatures/{id}：详情
- POST/PUT/DELETE /literatures：仅 admin，写操作接入 AuditService

与 herbs / prescriptions / theories 同构：
- 关键词搜索仅使用 PostgreSQL（ILIKE），数组别名通过 unnest 逐元素匹配，
  不涉及 Milvus / Embedding / RAG
- Literature 是著作级文献条目（著录元数据 + 人工整理正文），与
  Document（KB 内文件 + 解析/向量化流水线）相互独立，本路由不设任何关联
- 输出统一用 _literature_to_out 显式构造；关联数据由模型级 lazy="selectin"
  预加载，不依赖异步隐式懒加载
"""

import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import exists, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from src.application.audit_service import AuditService
from src.core.deps import get_client_ip
from src.core.exceptions import AppException
from src.domain.models import Category, Literature, Tag
from src.infrastructure.database import get_db

router = APIRouter(prefix="/literatures", tags=["literatures"])

_MAX_LIST_ITEMS = 20  # 别名数量上限
_MAX_TAGS = 20  # 标签数量上限


# ── Schemas ──────────────────────────────────────────────────────────────────


def _normalize_str_list(values: list[str], limit: int) -> list[str]:
    """去空白、去空串、去重（保序），并限制数量。"""
    result: list[str] = []
    for raw in values:
        item = raw.strip()
        if item and item not in result:
            result.append(item)
    if len(result) > limit:
        raise ValueError(f"最多允许 {limit} 项")
    return result


class LiteratureCreate(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    aliases: list[str] = Field(default_factory=list)
    category_id: uuid.UUID | None = None
    author: str = Field(default="", max_length=255)
    dynasty: str = Field(default="", max_length=32)
    summary: str = Field(default="", max_length=500)
    content: str = Field(default="", max_length=50000)
    source: str = Field(default="", max_length=255)
    tag_ids: list[uuid.UUID] = Field(default_factory=list)

    @field_validator("aliases")
    @classmethod
    def _check_aliases(cls, v: list[str]) -> list[str]:
        return _normalize_str_list(v, _MAX_LIST_ITEMS)

    @field_validator("tag_ids")
    @classmethod
    def _check_tag_ids(cls, v: list[uuid.UUID]) -> list[uuid.UUID]:
        result = list(dict.fromkeys(v))
        if len(result) > _MAX_TAGS:
            raise ValueError(f"标签最多 {_MAX_TAGS} 个")
        return result

    @field_validator("name", "author", "dynasty", "summary", "content", "source")
    @classmethod
    def _strip(cls, v: str) -> str:
        return v.strip()


class LiteratureUpdate(BaseModel):
    # 全部可选；未提供 vs 显式空值由 model_dump(exclude_unset=True) 区分
    name: str | None = Field(default=None, min_length=1, max_length=128)
    aliases: list[str] | None = None
    category_id: uuid.UUID | None = None
    author: str | None = Field(default=None, max_length=255)
    dynasty: str | None = Field(default=None, max_length=32)
    summary: str | None = Field(default=None, max_length=500)
    content: str | None = Field(default=None, max_length=50000)
    source: str | None = Field(default=None, max_length=255)
    tag_ids: list[uuid.UUID] | None = None

    @field_validator("aliases")
    @classmethod
    def _check_aliases(cls, v: list[str] | None) -> list[str] | None:
        return None if v is None else _normalize_str_list(v, _MAX_LIST_ITEMS)

    @field_validator("tag_ids")
    @classmethod
    def _check_tag_ids(cls, v: list[uuid.UUID] | None) -> list[uuid.UUID] | None:
        if v is None:
            return None
        result = list(dict.fromkeys(v))
        if len(result) > _MAX_TAGS:
            raise ValueError(f"标签最多 {_MAX_TAGS} 个")
        return result


class CategoryBrief(BaseModel):
    id: uuid.UUID
    name: str


class TagBrief(BaseModel):
    id: uuid.UUID
    name: str
    color: str


class LiteratureOut(BaseModel):
    id: uuid.UUID
    name: str
    aliases: list[str]
    category_id: uuid.UUID | None
    category: CategoryBrief | None
    author: str
    dynasty: str
    summary: str
    content: str
    source: str
    tags: list[TagBrief]
    created_at: datetime
    updated_at: datetime


class LiteratureListResponse(BaseModel):
    items: list[LiteratureOut]
    total: int
    limit: int
    offset: int


# ── Helpers ──────────────────────────────────────────────────────────────────


def _get_client_ip(request: Request) -> str:
    """审计用客户端 IP（实现统一到 src.core.deps.get_client_ip，BUG-072）。"""
    return get_client_ip(request)

def _literature_to_out(literature: Literature) -> LiteratureOut:
    """显式构造输出；tags/category 必须已预加载（模型 lazy=selectin）。"""
    category = literature.category
    return LiteratureOut(
        id=literature.id,
        name=literature.name,
        aliases=list(literature.aliases or []),
        category_id=literature.category_id,
        category=(
            CategoryBrief(id=category.id, name=category.name)
            if category is not None
            else None
        ),
        author=literature.author,
        dynasty=literature.dynasty,
        summary=literature.summary,
        content=literature.content,
        source=literature.source,
        tags=[
            TagBrief(id=t.id, name=t.name, color=t.color)
            for t in literature.tags
        ],
        created_at=literature.created_at,
        updated_at=literature.updated_at,
    )


async def _validate_literature_category(
    db: AsyncSession, category_id: uuid.UUID
) -> None:
    """分类必须存在且属于 literature 资源域，否则 400。"""
    category = await db.get(Category, category_id)
    if category is None:
        raise HTTPException(status_code=400, detail="指定的分类不存在")
    if category.resource_type != "literature":
        raise HTTPException(
            status_code=400, detail="只能选择资源类型为文献的分类"
        )


async def _load_tags_by_ids(
    db: AsyncSession, tag_ids: list[uuid.UUID]
) -> list[Tag]:
    """一次性加载标签；任一不存在即 400（无 N+1）。"""
    tags = list(
        (await db.scalars(select(Tag).where(Tag.id.in_(tag_ids)))).all()
    )
    if len(tags) != len(tag_ids):
        found = {t.id for t in tags}
        missing = [str(i) for i in tag_ids if i not in found]
        raise HTTPException(
            status_code=400, detail=f"部分标签不存在：{', '.join(missing)}"
        )
    by_id = {t.id: t for t in tags}
    return [by_id[i] for i in tag_ids]


async def _name_exists(
    db: AsyncSession, name: str, exclude_id: uuid.UUID | None = None
) -> bool:
    stmt = select(Literature.id).where(Literature.name == name)
    if exclude_id is not None:
        stmt = stmt.where(Literature.id != exclude_id)
    return await db.scalar(stmt.limit(1)) is not None


def _array_ilike(column, pattern: str):
    """EXISTS(SELECT 1 FROM unnest(column) AS item WHERE item ILIKE pattern)。

    column_valued 的 FROM（unnest）以本数组列为参数，自动与外层 Literature 行关联，
    渲染为相关子查询；不触发异步隐式懒加载。
    """
    item = func.unnest(column).column_valued("item")
    return exists().where(item.ilike(pattern))


def _build_conditions(
    keyword: str | None,
    category_id: uuid.UUID | None,
    tag_id: uuid.UUID | None,
) -> list:
    conditions: list = []
    if keyword:
        pattern = f"%{keyword.strip()}%"
        conditions.append(
            (Literature.name.ilike(pattern))
            | _array_ilike(Literature.aliases, pattern)
            | (Literature.author.ilike(pattern))
            | (Literature.dynasty.ilike(pattern))
            | (Literature.summary.ilike(pattern))
            | (Literature.content.ilike(pattern))
            | (Literature.source.ilike(pattern))
        )
    if category_id is not None:
        conditions.append(Literature.category_id == category_id)
    if tag_id is not None:
        # EXISTS，避免 JOIN 产生重复行
        conditions.append(Literature.tags.any(Tag.id == tag_id))
    return conditions


# ── Endpoints ────────────────────────────────────────────────────────────────


@router.get("", response_model=LiteratureListResponse)
async def list_literatures(
    keyword: str | None = None,
    category_id: uuid.UUID | None = None,
    tag_id: uuid.UUID | None = None,
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    db: AsyncSession = Depends(get_db),
):
    """文献列表：关键词搜索 + 分类/标签筛选 + 分页（created_at DESC, id DESC）。"""
    conditions = _build_conditions(keyword, category_id, tag_id)

    total = await db.scalar(
        select(func.count()).select_from(Literature).where(*conditions)
    )
    stmt = (
        select(Literature)
        .where(*conditions)
        .order_by(Literature.created_at.desc(), Literature.id.desc())
        .limit(limit)
        .offset(offset)
    )
    literatures = list((await db.scalars(stmt)).all())
    return LiteratureListResponse(
        items=[_literature_to_out(l) for l in literatures],
        total=total or 0,
        limit=limit,
        offset=offset,
    )


@router.get("/{literature_id}", response_model=LiteratureOut)
async def get_literature(
    literature_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
):
    """文献详情；不存在 404。"""
    literature = await db.get(Literature, literature_id)
    if literature is None:
        raise HTTPException(status_code=404, detail="文献不存在")
    return _literature_to_out(literature)


@router.post("", response_model=LiteratureOut, status_code=201)
async def create_literature(
    payload: LiteratureCreate,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """新建文献（仅 admin）。"""
    user = request.state.user
    if user.role != "admin":
        raise AppException(403, "仅管理员可管理文献")

    if await _name_exists(db, payload.name):
        raise AppException(409, "同名文献已存在")

    if payload.category_id is not None:
        await _validate_literature_category(db, payload.category_id)

    tags = (
        await _load_tags_by_ids(db, payload.tag_ids)
        if payload.tag_ids
        else []
    )

    literature = Literature(
        name=payload.name,
        aliases=payload.aliases,
        category_id=payload.category_id,
        author=payload.author,
        dynasty=payload.dynasty,
        summary=payload.summary,
        content=payload.content,
        source=payload.source,
        tags=tags,
    )
    db.add(literature)
    try:
        await db.flush()
    except IntegrityError:
        # 并发下同名等约束冲突 → 409，不直接抛 500
        await db.rollback()
        raise AppException(409, "同名文献已存在")

    audit = AuditService(db)
    await audit.log(
        operator_id=user.id,
        operator_name=user.username,
        operation="create",
        target_type="literature",
        target_id=str(literature.id),
        detail={"name": literature.name},
        ip=_get_client_ip(request),
    )

    # audit.log() 内部 commit 会使对象过期，重新查询获取最终状态
    stored = await db.scalar(
        select(Literature).where(Literature.id == literature.id)
    )
    return _literature_to_out(stored)


@router.put("/{literature_id}", response_model=LiteratureOut)
async def update_literature(
    literature_id: uuid.UUID,
    payload: LiteratureUpdate,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """更新文献（仅 admin）：partial update，tag_ids 提供时整体替换。"""
    user = request.state.user
    if user.role != "admin":
        raise AppException(403, "仅管理员可管理文献")

    literature = await db.get(Literature, literature_id)
    if literature is None:
        raise HTTPException(status_code=404, detail="文献不存在")

    data = payload.model_dump(exclude_unset=True)
    changed: dict = {}

    if "name" in data and data["name"] != literature.name:
        if await _name_exists(db, data["name"], exclude_id=literature.id):
            raise AppException(409, "同名文献已存在")
        literature.name = data["name"]
        changed["name"] = data["name"]

    if "category_id" in data and data["category_id"] != literature.category_id:
        if data["category_id"] is not None:
            await _validate_literature_category(db, data["category_id"])
        literature.category_id = data["category_id"]
        changed["category_id"] = (
            str(data["category_id"]) if data["category_id"] else None
        )

    if "tag_ids" in data:
        tags = (
            await _load_tags_by_ids(db, data["tag_ids"])
            if data["tag_ids"]
            else []
        )
        # 集合整体替换；未提供时不会进入此分支（保持原关系）
        literature.tags = tags
        changed["tag_ids"] = [str(i) for i in data["tag_ids"]]

    for field_name in (
        "aliases",
        "author",
        "dynasty",
        "summary",
        "content",
        "source",
    ):
        if field_name in data:
            value = data[field_name]
            if getattr(literature, field_name) != value:
                setattr(literature, field_name, value)
                changed[field_name] = value

    try:
        await db.flush()
    except IntegrityError:
        await db.rollback()
        raise AppException(409, "同名文献已存在")

    # Stage 4-6：更新后重新向量化所有已挂载的 KB（best-effort，失败不阻塞更新）
    try:
        from src.application.resource_vector_service import ResourceVectorService
        svc = ResourceVectorService()
        await svc.revectorize_all_mounts(db, literature, "literature")
        # BUG-047：重新向量化失败必须留痕（审计详情），不能静默过期
        if svc.last_revectorize_failures:
            changed["_vector_revectorize_failed"] = svc.last_revectorize_failures
    except Exception:
        import logging
        logging.getLogger(__name__).error(
            f"literature 更新后重新向量化失败 id={literature.id}",
            exc_info=True,
        )

    audit = AuditService(db)
    await audit.log(
        operator_id=user.id,
        operator_name=user.username,
        operation="update",
        target_type="literature",
        target_id=str(literature.id),
        detail=changed,
        ip=_get_client_ip(request),
    )

    stored = await db.scalar(
        select(Literature).where(Literature.id == literature.id)
    )
    return _literature_to_out(stored)


@router.delete("/{literature_id}", status_code=204)
async def delete_literature(
    literature_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """删除文献（仅 admin）；literature_tags 关联随 DB CASCADE 自动清理。

    Stage 4-6：删除前清理所有 KBR 关联及 Milvus vectors。
    """
    user = request.state.user
    if user.role != "admin":
        raise AppException(403, "仅管理员可管理文献")

    literature = await db.get(Literature, literature_id)
    if literature is None:
        raise HTTPException(status_code=404, detail="文献不存在")

    # Stage 4-6：删除资源前清理 KBR + 向量（失败则阻止删除）
    try:
        from src.application.resource_vector_service import ResourceVectorService
        svc = ResourceVectorService()
        await svc.cleanup_resource_mounts(db, "literature", literature.id)
    except Exception as exc:
        raise AppException(
            422, f"资源向量清理失败，无法删除：{exc}"
        ) from exc

    # BUG-009：同步清理该资源的知识图谱节点（边随 FK CASCADE 删除）
    try:
        from src.application.kg_service import KgService
        await KgService(db).delete_resource_nodes("literature", literature.id)
    except Exception as exc:
        import logging
        logging.getLogger(__name__).error(
            f"literature KG 节点清理失败 id={literature.id}: {exc}",
            exc_info=True,
        )

    await db.delete(literature)
    await db.flush()

    audit = AuditService(db)
    await audit.log(
        operator_id=user.id,
        operator_name=user.username,
        operation="delete",
        target_type="literature",
        target_id=str(literature.id),
        detail={"name": literature.name},
        ip=_get_client_ip(request),
    )
