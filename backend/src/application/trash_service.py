"""统一回收站（软删除）生命周期：删除 → 回收站 → 恢复 / 彻底删除。

背景（管理中心统一改造）：
此前只有 knowledge_bases / documents / users 三处各写了一份软删除 + 回收站 +
过期清理，逻辑复制粘贴、口径不一致；中药 / 方剂 / 中医理论 / 文献 / 分类 / 标签
则是**直接物理删除**（``await db.delete(obj)``），删除即不可恢复、且绕过向量与
知识图谱清理。

本模块把这六类资源（+ 分类 / 标签）的生命周期收敛到同一实现：
- 软删除：置 ``deleted_at``（NULL = 正常）
- 恢复：置回 NULL
- 彻底删除：先清理 KB 挂载 + Milvus 向量 + KG 节点，再删行；单条失败只影响该条
- 过期清理：回收站列表 / 清空时按 ``trash_retention_days`` 自动清掉超期项

设计约束（与既有规则一致，不新增第二套体系）：
- 保留天数**每次实时读取** ``get_system_value("trash_retention_days")``，
  不做模块级常量副本（BUG-069 教训）；
- 不做 TRUNCATE / 全表 DELETE；所有删除都必须先经过软删除（回收站保护期）；
- 彻底删除前必须清理 Milvus 向量，避免留下"检索得到但数据已不存在"的孤儿向量。
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from typing import Any

from loguru import logger
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.runtime_config import get_system_value
from src.domain.models import (
    Category,
    Herb,
    Literature,
    Prescription,
    Tag,
    Theory,
    herb_tags,
    literature_tags,
    prescription_tags,
    theory_tags,
)
from src.utils.timeutil import utcnow

# 四类可挂载进知识库的传统资源（与 KnowledgeBaseResource.resource_type 一致）
RESOURCE_TYPES: tuple[str, ...] = ("herb", "prescription", "theory", "literature")

MODEL_BY_RESOURCE_TYPE: dict[str, type] = {
    "herb": Herb,
    "prescription": Prescription,
    "theory": Theory,
    "literature": Literature,
}

# 引用 categories.category_id 的资源表（分类彻底删除前需解除引用）
_CATEGORY_REF_MODELS: tuple[type, ...] = (Herb, Prescription, Theory, Literature)
# 引用 tags.id 的关联表（标签彻底删除前需清理，FK 为 RESTRICT）
_TAG_LINK_TABLES = (herb_tags, prescription_tags, theory_tags, literature_tags)


def retention_days() -> int:
    """回收站保留天数（实时读运行时配置，默认 7）。"""
    try:
        return int(get_system_value("trash_retention_days"))
    except Exception:  # noqa: BLE001 - 配置异常不应阻断删除链路
        return 7


def retention_cutoff() -> datetime:
    return utcnow() - timedelta(days=retention_days())


# ── 软删除 / 恢复 ────────────────────────────────────────────────────────────


async def soft_delete_many(db: AsyncSession, model: type, ids: list[uuid.UUID]) -> int:
    """批量软删除（只处理仍在活跃状态的记录），返回实际删除条数。"""
    if not ids:
        return 0
    result = await db.execute(
        update(model)
        .where(model.id.in_(ids), model.deleted_at.is_(None))
        .values(deleted_at=utcnow())
    )
    return int(result.rowcount or 0)


async def restore_many(db: AsyncSession, model: type, ids: list[uuid.UUID]) -> int:
    """批量恢复（只处理确实在回收站中的记录），返回实际恢复条数。"""
    if not ids:
        return 0
    result = await db.execute(
        update(model)
        .where(model.id.in_(ids), model.deleted_at.is_not(None))
        .values(deleted_at=None)
    )
    return int(result.rowcount or 0)


# ── 回收站列表 / 过期清理 ────────────────────────────────────────────────────


async def purge_expired(db: AsyncSession, model: type, *, resource_type: str | None = None) -> int:
    """清掉超过保留期的回收站条目（彻底删除，不可恢复）。返回清理条数。"""
    cutoff = retention_cutoff()
    rows = (
        await db.scalars(
            select(model).where(model.deleted_at.is_not(None), model.deleted_at < cutoff)
        )
    ).all()
    if not rows:
        return 0
    removed = 0
    for row in rows:
        try:
            if await _purge_one(db, model, row.id, resource_type=resource_type):
                removed += 1
        except Exception as exc:  # noqa: BLE001 - 单条失败不影响其余清理
            logger.warning(f"回收站过期项清理失败 {model.__name__}={row.id}: {exc}")
    if removed:
        await db.commit()
    return removed


async def list_trash(
    db: AsyncSession,
    model: type,
    *,
    limit: int | None = None,
    offset: int = 0,
    resource_type: str | None = None,
) -> tuple[list[Any], int]:
    """回收站列表（进入时先清理超期项），返回 (rows, total)。"""
    await purge_expired(db, model, resource_type=resource_type)
    where = model.deleted_at.is_not(None)
    total = int(
        await db.scalar(select(func.count()).select_from(model).where(where)) or 0
    )
    stmt = select(model).where(where).order_by(model.deleted_at.desc())
    if limit is not None:
        stmt = stmt.limit(limit).offset(offset)
    rows = list((await db.scalars(stmt)).all())
    return rows, total


# ── 彻底删除 ────────────────────────────────────────────────────────────────


async def purge_ids(
    db: AsyncSession,
    model: type,
    ids: list[uuid.UUID],
    *,
    resource_type: str | None = None,
) -> dict[str, Any]:
    """批量彻底删除（**只处理回收站中的记录**）。

    单条失败（向量清理失败 / 仍被外键引用）只记录原因并继续处理其余记录，
    不会让整批操作处于半完成状态——每条都在独立 savepoint 内完成。
    """
    purged: list[str] = []
    failed: list[dict[str, str]] = []
    for rid in ids:
        try:
            ok = await _purge_one(db, model, rid, resource_type=resource_type)
        except Exception as exc:  # noqa: BLE001
            ok = False
            logger.warning(f"彻底删除失败 {model.__name__}={rid}: {exc}")
            failed.append({"id": str(rid), "reason": str(exc)[:300]})
        if ok:
            purged.append(str(rid))
    if purged:
        await db.commit()
    return {"purged": purged, "failed": failed, "total": len(purged)}


async def purge_all(
    db: AsyncSession,
    model: type,
    *,
    resource_type: str | None = None,
) -> dict[str, Any]:
    """一键清空回收站：彻底删除当前回收站中的全部条目（跳过超期已清理项）。"""
    rows = (
        await db.scalars(select(model).where(model.deleted_at.is_not(None)))
    ).all()
    return await purge_ids(
        db, model, [r.id for r in rows], resource_type=resource_type
    )


async def _purge_one(
    db: AsyncSession,
    model: type,
    obj_id: uuid.UUID,
    *,
    resource_type: str | None = None,
) -> bool:
    """在独立 savepoint 内彻底删除一条；失败回滚该条，不影响其它记录。"""
    async with db.begin_nested():
        obj = await db.get(model, obj_id)
        if obj is None or obj.deleted_at is None:
            return False
        if resource_type:
            await _cleanup_resource_side_effects(db, resource_type, obj_id)
        elif model is Tag:
            await _detach_tag(db, obj_id)
        elif model is Category:
            await _detach_category(db, obj_id)
        await db.delete(obj)
        await db.flush()
    return True


async def _cleanup_resource_side_effects(
    db: AsyncSession, resource_type: str, resource_id: uuid.UUID
) -> None:
    """彻底删除资源前：清理 KB 挂载 + Milvus 向量（失败即中断），再清 KG 节点。"""
    from src.application.resource_vector_service import ResourceVectorService

    await ResourceVectorService().cleanup_resource_mounts(db, resource_type, resource_id)

    try:
        from src.application.kg_service import KgService

        await KgService(db).delete_resource_nodes(resource_type, resource_id)
    except Exception as exc:  # noqa: BLE001 - KG 清理失败不阻断删除
        logger.warning(f"KG 节点清理失败 {resource_type}={resource_id}: {exc}")


async def _detach_tag(db: AsyncSession, tag_id: uuid.UUID) -> None:
    """标签彻底删除前：清理四张 *_tags 关联行（FK 为 RESTRICT）。"""
    from sqlalchemy import delete as sa_delete

    for table in _TAG_LINK_TABLES:
        await db.execute(sa_delete(table).where(table.c.tag_id == tag_id))


async def _detach_category(db: AsyncSession, category_id: uuid.UUID) -> None:
    """分类彻底删除前：解除资源引用 + 把子节点提升为顶层（避免 RESTRICT 阻断）。

    不做级联删除：引用该分类的资源只是失去分类归属（category_id = NULL），
    子节点升为顶层，均不丢失数据。
    """
    for model in _CATEGORY_REF_MODELS:
        await db.execute(
            update(model)
            .where(model.category_id == category_id)
            .values(category_id=None)
        )
    await db.execute(
        update(Category).where(Category.parent_id == category_id).values(parent_id=None)
    )


# ── 影响范围统计（删除前展示）────────────────────────────────────────────────


async def trash_impact(db: AsyncSession, model: type, ids: list[uuid.UUID]) -> dict[str, int]:
    """删除前的影响范围：目标条数 + 关联挂载数（供前端二次确认展示）。"""
    try:
        from src.domain.models import KnowledgeBaseResource

        mounted = 0
        if model in MODEL_BY_RESOURCE_TYPE.values():
            rtype = next(
                k for k, v in MODEL_BY_RESOURCE_TYPE.items() if v is model
            )
            mounted = int(
                await db.scalar(
                    select(func.count())
                    .select_from(KnowledgeBaseResource)
                    .where(
                        KnowledgeBaseResource.resource_type == rtype,
                        KnowledgeBaseResource.resource_id.in_(ids),
                    )
                )
                or 0
            )
    except Exception:  # noqa: BLE001
        mounted = 0
    return {"targets": len(ids), "kb_mounts": mounted}
