"""方剂资源管理路由（TASK-004 Stage 3）。

- GET /prescriptions：登录用户均可浏览，支持 keyword / category_id / tag_id + 分页
- GET /prescriptions/{id}：详情（含组成 ingredients / 标签 / 分类）
- POST/PUT/DELETE /prescriptions：仅 admin，写操作接入 AuditService

与 TASK-003 herbs 同构：
- 关键词搜索仅使用 PostgreSQL（ILIKE），数组别名通过 unnest 逐元素匹配，
  不涉及 Milvus / Embedding / RAG
- 组成（PrescriptionIngredient）为带载荷关联对象：更新时先物理清空旧行
  再插入新行，避免同一 flush 内 INSERT 先于 DELETE 触发唯一键冲突
- 输出统一用 _prescription_to_out 显式构造；关联数据由模型级 lazy="selectin"
  预加载（ingredients→herb 链式预加载），不依赖异步隐式懒加载
"""

import uuid
from datetime import datetime
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import delete, exists, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from src.application.audit_service import AuditService
from src.core.exceptions import AppException
from src.domain.models import (
    Category,
    Herb,
    Prescription,
    PrescriptionIngredient,
    Tag,
)
from src.infrastructure.database import get_db

router = APIRouter(prefix="/prescriptions", tags=["prescriptions"])

_MAX_LIST_ITEMS = 20  # 别名数量上限
_MAX_INGREDIENTS = 50  # 组成药材数量上限
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


class IngredientIn(BaseModel):
    herb_id: uuid.UUID
    amount: Decimal | None = None
    unit: str = Field(default="", max_length=16)
    processing: str = Field(default="", max_length=255)
    role: str = Field(default="", max_length=32)
    sort_order: int = 0

    @field_validator("unit", "processing", "role")
    @classmethod
    def _strip(cls, v: str) -> str:
        return v.strip()


class PrescriptionCreate(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    aliases: list[str] = Field(default_factory=list)
    category_id: uuid.UUID | None = None
    efficacy: str = Field(default="", max_length=5000)
    indications: str = Field(default="", max_length=5000)
    usage_method: str = Field(default="", max_length=255)
    source: str = Field(default="", max_length=255)
    description: str = Field(default="", max_length=10000)
    ingredients: list[IngredientIn] = Field(default_factory=list)
    tag_ids: list[uuid.UUID] = Field(default_factory=list)

    @field_validator("aliases")
    @classmethod
    def _check_aliases(cls, v: list[str]) -> list[str]:
        return _normalize_str_list(v, _MAX_LIST_ITEMS)

    @field_validator("ingredients")
    @classmethod
    def _check_ingredients(cls, v: list[IngredientIn]) -> list[IngredientIn]:
        if len(v) > _MAX_INGREDIENTS:
            raise ValueError(f"组成最多 {_MAX_INGREDIENTS} 味药材")
        return v

    @field_validator("tag_ids")
    @classmethod
    def _check_tag_ids(cls, v: list[uuid.UUID]) -> list[uuid.UUID]:
        result = list(dict.fromkeys(v))
        if len(result) > _MAX_TAGS:
            raise ValueError(f"标签最多 {_MAX_TAGS} 个")
        return result

    @field_validator(
        "name", "efficacy", "indications", "usage_method", "source", "description"
    )
    @classmethod
    def _strip(cls, v: str) -> str:
        return v.strip()


class PrescriptionUpdate(BaseModel):
    # 全部可选；未提供 vs 显式空值由 model_dump(exclude_unset=True) 区分
    name: str | None = Field(default=None, min_length=1, max_length=128)
    aliases: list[str] | None = None
    category_id: uuid.UUID | None = None
    efficacy: str | None = Field(default=None, max_length=5000)
    indications: str | None = Field(default=None, max_length=5000)
    usage_method: str | None = Field(default=None, max_length=255)
    source: str | None = Field(default=None, max_length=255)
    description: str | None = Field(default=None, max_length=10000)
    ingredients: list[IngredientIn] | None = None
    tag_ids: list[uuid.UUID] | None = None

    @field_validator("aliases")
    @classmethod
    def _check_aliases(cls, v: list[str] | None) -> list[str] | None:
        return None if v is None else _normalize_str_list(v, _MAX_LIST_ITEMS)

    @field_validator("ingredients")
    @classmethod
    def _check_ingredients(
        cls, v: list[IngredientIn] | None
    ) -> list[IngredientIn] | None:
        if v is None:
            return None
        if len(v) > _MAX_INGREDIENTS:
            raise ValueError(f"组成最多 {_MAX_INGREDIENTS} 味药材")
        return v

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


class PrescriptionIngredientOut(BaseModel):
    id: uuid.UUID
    herb_id: uuid.UUID
    herb_name: str
    amount: float | None
    unit: str
    processing: str
    role: str
    sort_order: int


class PrescriptionOut(BaseModel):
    id: uuid.UUID
    name: str
    aliases: list[str]
    category_id: uuid.UUID | None
    category: CategoryBrief | None
    efficacy: str
    indications: str
    usage_method: str
    source: str
    description: str
    ingredients: list[PrescriptionIngredientOut]
    tags: list[TagBrief]
    created_at: datetime
    updated_at: datetime


class PrescriptionListResponse(BaseModel):
    items: list[PrescriptionOut]
    total: int
    limit: int
    offset: int


# ── Helpers ──────────────────────────────────────────────────────────────────


def _get_client_ip(request: Request) -> str:
    return request.client.host if request.client else ""


def _prescription_to_out(prescription: Prescription) -> PrescriptionOut:
    """显式构造输出；ingredients(含 herb)/tags/category 必须已预加载。"""
    category = prescription.category
    return PrescriptionOut(
        id=prescription.id,
        name=prescription.name,
        aliases=list(prescription.aliases or []),
        category_id=prescription.category_id,
        category=(
            CategoryBrief(id=category.id, name=category.name)
            if category is not None
            else None
        ),
        efficacy=prescription.efficacy,
        indications=prescription.indications,
        usage_method=prescription.usage_method,
        source=prescription.source,
        description=prescription.description,
        ingredients=[
            PrescriptionIngredientOut(
                id=i.id,
                herb_id=i.herb_id,
                herb_name=i.herb.name if i.herb is not None else "",
                amount=float(i.amount) if i.amount is not None else None,
                unit=i.unit,
                processing=i.processing,
                role=i.role,
                sort_order=i.sort_order,
            )
            for i in prescription.ingredients
        ],
        tags=[
            TagBrief(id=t.id, name=t.name, color=t.color)
            for t in prescription.tags
        ],
        created_at=prescription.created_at,
        updated_at=prescription.updated_at,
    )


async def _validate_prescription_category(
    db: AsyncSession, category_id: uuid.UUID
) -> None:
    """分类必须存在且属于 prescription 资源域，否则 400。"""
    category = await db.get(Category, category_id)
    if category is None:
        raise HTTPException(status_code=400, detail="指定的分类不存在")
    if category.resource_type != "prescription":
        raise HTTPException(
            status_code=400, detail="只能选择资源类型为方剂的分类"
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


async def _build_ingredients(
    db: AsyncSession, items: list[IngredientIn]
) -> list[PrescriptionIngredient]:
    """校验并物化组成行：重复 herb_id 400；herb 不存在 400（批量查询，无 N+1）。"""
    if not items:
        return []
    herb_ids = [i.herb_id for i in items]
    if len(set(herb_ids)) != len(herb_ids):
        raise HTTPException(status_code=400, detail="组成中存在重复药材")
    found = set(
        (await db.scalars(select(Herb.id).where(Herb.id.in_(herb_ids)))).all()
    )
    if len(found) != len(herb_ids):
        missing = ", ".join(str(i) for i in herb_ids if i not in found)
        raise HTTPException(
            status_code=400, detail=f"部分药材不存在：{missing}"
        )
    return [
        PrescriptionIngredient(
            herb_id=i.herb_id,
            amount=i.amount,
            unit=i.unit,
            processing=i.processing,
            role=i.role,
            sort_order=i.sort_order,
        )
        for i in items
    ]


async def _name_exists(
    db: AsyncSession, name: str, exclude_id: uuid.UUID | None = None
) -> bool:
    stmt = select(Prescription.id).where(Prescription.name == name)
    if exclude_id is not None:
        stmt = stmt.where(Prescription.id != exclude_id)
    return await db.scalar(stmt.limit(1)) is not None


def _array_ilike(column, pattern: str):
    """EXISTS(SELECT 1 FROM unnest(column) AS item WHERE item ILIKE pattern)。

    column_valued 的 FROM（unnest）以本数组列为参数，自动与外层行关联，
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
            (Prescription.name.ilike(pattern))
            | _array_ilike(Prescription.aliases, pattern)
            | (Prescription.efficacy.ilike(pattern))
            | (Prescription.indications.ilike(pattern))
            | (Prescription.description.ilike(pattern))
            | (Prescription.usage_method.ilike(pattern))
        )
    if category_id is not None:
        conditions.append(Prescription.category_id == category_id)
    if tag_id is not None:
        # EXISTS，避免 JOIN 产生重复行
        conditions.append(Prescription.tags.any(Tag.id == tag_id))
    return conditions


# ── Endpoints ────────────────────────────────────────────────────────────────


@router.get("", response_model=PrescriptionListResponse)
async def list_prescriptions(
    keyword: str | None = None,
    category_id: uuid.UUID | None = None,
    tag_id: uuid.UUID | None = None,
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    db: AsyncSession = Depends(get_db),
):
    """方剂列表：关键词搜索 + 分类/标签筛选 + 分页（created_at DESC, id DESC）。"""
    conditions = _build_conditions(keyword, category_id, tag_id)

    total = await db.scalar(
        select(func.count()).select_from(Prescription).where(*conditions)
    )
    stmt = (
        select(Prescription)
        .where(*conditions)
        .order_by(Prescription.created_at.desc(), Prescription.id.desc())
        .limit(limit)
        .offset(offset)
    )
    prescriptions = list((await db.scalars(stmt)).all())
    return PrescriptionListResponse(
        items=[_prescription_to_out(p) for p in prescriptions],
        total=total or 0,
        limit=limit,
        offset=offset,
    )


@router.get("/{prescription_id}", response_model=PrescriptionOut)
async def get_prescription(
    prescription_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
):
    """方剂详情；不存在 404。"""
    prescription = await db.get(Prescription, prescription_id)
    if prescription is None:
        raise HTTPException(status_code=404, detail="方剂不存在")
    return _prescription_to_out(prescription)


@router.post("", response_model=PrescriptionOut, status_code=201)
async def create_prescription(
    payload: PrescriptionCreate,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """新建方剂（仅 admin）。"""
    user = request.state.user
    if user.role != "admin":
        raise AppException(403, "仅管理员可管理方剂")

    if await _name_exists(db, payload.name):
        raise AppException(409, "同名方剂已存在")

    if payload.category_id is not None:
        await _validate_prescription_category(db, payload.category_id)

    ingredients = await _build_ingredients(db, payload.ingredients)
    tags = (
        await _load_tags_by_ids(db, payload.tag_ids)
        if payload.tag_ids
        else []
    )

    prescription = Prescription(
        name=payload.name,
        aliases=payload.aliases,
        category_id=payload.category_id,
        efficacy=payload.efficacy,
        indications=payload.indications,
        usage_method=payload.usage_method,
        source=payload.source,
        description=payload.description,
        ingredients=ingredients,
        tags=tags,
    )
    db.add(prescription)
    try:
        await db.flush()
    except IntegrityError:
        # 并发下同名等约束冲突 → 409，不直接抛 500
        await db.rollback()
        raise AppException(409, "同名方剂已存在")

    audit = AuditService(db)
    await audit.log(
        operator_id=user.id,
        operator_name=user.username,
        operation="create",
        target_type="prescription",
        target_id=str(prescription.id),
        detail={"name": prescription.name},
        ip=_get_client_ip(request),
    )

    # audit.log() 内部 commit 会使对象过期，重新查询获取最终状态
    stored = await db.scalar(
        select(Prescription).where(Prescription.id == prescription.id)
    )
    return _prescription_to_out(stored)


@router.put("/{prescription_id}", response_model=PrescriptionOut)
async def update_prescription(
    prescription_id: uuid.UUID,
    payload: PrescriptionUpdate,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """更新方剂（仅 admin）：partial update。

    - ingredients 提供时整体替换（先清空旧行再插入，规避唯一键冲突）
    - tag_ids 提供时整体替换；未提供时两者均保持原状
    """
    user = request.state.user
    if user.role != "admin":
        raise AppException(403, "仅管理员可管理方剂")

    prescription = await db.get(Prescription, prescription_id)
    if prescription is None:
        raise HTTPException(status_code=404, detail="方剂不存在")

    data = payload.model_dump(exclude_unset=True)
    changed: dict = {}

    if "name" in data and data["name"] != prescription.name:
        if await _name_exists(db, data["name"], exclude_id=prescription.id):
            raise AppException(409, "同名方剂已存在")
        prescription.name = data["name"]
        changed["name"] = data["name"]

    if "category_id" in data and data["category_id"] != prescription.category_id:
        if data["category_id"] is not None:
            await _validate_prescription_category(db, data["category_id"])
        prescription.category_id = data["category_id"]
        changed["category_id"] = (
            str(data["category_id"]) if data["category_id"] else None
        )

    if "ingredients" in data:
        new_rows = await _build_ingredients(db, payload.ingredients or [])
        # 整体替换：立即物理清空旧行（synchronize_session 使旧行脱离会话），
        # 新行直接 add_all，避免同一 flush 内 INSERT 先于 DELETE 的唯一键冲突
        await db.execute(
            delete(PrescriptionIngredient).where(
                PrescriptionIngredient.prescription_id == prescription.id
            )
        )
        for row in new_rows:
            row.prescription_id = prescription.id
        db.add_all(new_rows)
        changed["ingredients"] = len(new_rows)

    if "tag_ids" in data:
        tags = (
            await _load_tags_by_ids(db, data["tag_ids"])
            if data["tag_ids"]
            else []
        )
        # 集合整体替换；未提供时不会进入此分支（保持原关系）
        prescription.tags = tags
        changed["tag_ids"] = [str(i) for i in data["tag_ids"]]

    for field_name in (
        "aliases",
        "efficacy",
        "indications",
        "usage_method",
        "source",
        "description",
    ):
        if field_name in data:
            value = data[field_name]
            if getattr(prescription, field_name) != value:
                setattr(prescription, field_name, value)
                changed[field_name] = value

    try:
        await db.flush()
    except IntegrityError:
        await db.rollback()
        raise AppException(409, "同名方剂已存在")

    audit = AuditService(db)
    await audit.log(
        operator_id=user.id,
        operator_name=user.username,
        operation="update",
        target_type="prescription",
        target_id=str(prescription.id),
        detail=changed,
        ip=_get_client_ip(request),
    )

    # 批量 delete 的 synchronize_session 不更新父对象已加载的集合，
    # 且 expire_on_commit=False 下 commit 不会使其过期；若原地替换过组成，
    # 重查前先 expire 该集合，强制从 DB 重载最终状态
    if "ingredients" in data:
        db.expire(prescription, ["ingredients"])

    stored = await db.scalar(
        select(Prescription).where(Prescription.id == prescription.id)
    )
    return _prescription_to_out(stored)


@router.delete("/{prescription_id}", status_code=204)
async def delete_prescription(
    prescription_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """删除方剂（仅 admin）。

    组成行与 prescription_tags 关联随 DB CASCADE / ORM 级联自动清理；
    herb_id 为 RESTRICT，不会触碰中药数据。
    """
    user = request.state.user
    if user.role != "admin":
        raise AppException(403, "仅管理员可管理方剂")

    prescription = await db.get(Prescription, prescription_id)
    if prescription is None:
        raise HTTPException(status_code=404, detail="方剂不存在")

    await db.delete(prescription)
    await db.flush()

    audit = AuditService(db)
    await audit.log(
        operator_id=user.id,
        operator_name=user.username,
        operation="delete",
        target_type="prescription",
        target_id=str(prescription.id),
        detail={"name": prescription.name},
        ip=_get_client_ip(request),
    )
