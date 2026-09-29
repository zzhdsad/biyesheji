"""测试数据清理（管理员，安全优先）。

区分依据（不猜测、不靠名字）：
- **真实导入数据**：`import_batch_id` 非空（由导入中心写入，可用 batch_id 精确管理/回滚）
- **手工 / 历史测试数据**：`import_batch_id` 为空（本系统此前为演示录入的条目）

安全约束：
- 默认 `dry_run=True`（只统计不删）；真正删除必须 `confirm=True`
- 绝不触碰 users / roles / 系统配置 / 审计日志 / 分类与标签字典
- 删除走完整生命周期：Milvus 向量 → chunks → Document 文件 → 资源挂载 → KG 节点 → 业务行
"""

from __future__ import annotations

import uuid
from typing import Any

from loguru import logger
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.application.resource_vector_service import ResourceVectorService
from src.core.exceptions import AppException
from src.domain.models import (
    Chunk,
    Document,
    Herb,
    KgEdge,
    KgNode,
    KnowledgeBase,
    KnowledgeBaseResource,
    Literature,
    Prescription,
    Theory,
)
from src.infrastructure.storage import get_storage
from src.infrastructure.milvus_store import get_vector_store

# 资源类型 → (模型, 表名展示名)
RESOURCE_MODELS: dict[str, type] = {
    "herb": Herb,
    "prescription": Prescription,
    "theory": Theory,
    "literature": Literature,
}

_BATCH = 200


async def _count(session: AsyncSession, model, *, imported: bool | None = None) -> int:
    stmt = select(func.count()).select_from(model)
    if imported is True:
        stmt = stmt.where(model.import_batch_id.is_not(None))
    elif imported is False:
        stmt = stmt.where(model.import_batch_id.is_(None))
    return int(await session.scalar(stmt) or 0)


def _milvus_total() -> int | None:
    """Milvus 集合内的向量总数（取不到返回 None，不影响清理统计）。"""
    try:
        store = get_vector_store()
        client = store._get_client()  # noqa: SLF001 - 统计接口未在抽象层暴露
        if not client.has_collection(store.COLLECTION):  # type: ignore[attr-defined]
            return 0
        stats = client.get_collection_stats(store.COLLECTION)  # type: ignore[attr-defined]
        return int(stats.get("row_count", 0)) if isinstance(stats, dict) else None
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"Milvus 统计失败（忽略）：{exc}")
        return None


async def collect_stats(session: AsyncSession) -> dict[str, Any]:
    """统计各类数据总量，并按「真实导入 / 手工测试」拆分。"""
    kb_rows = (await session.scalars(select(KnowledgeBase))).all()
    kb_ids = [k.id for k in kb_rows]
    imported_kb_ids: set[uuid.UUID] = set()
    if kb_ids:
        rows = await session.execute(
            select(Document.kb_id)
            .where(Document.kb_id.in_(kb_ids), Document.import_batch_id.is_not(None))
            .distinct()
        )
        imported_kb_ids = {r for r in rows.scalars().all()}

    chunks_total = int(await session.scalar(select(func.count()).select_from(Chunk)) or 0)
    chunks_imported = int(
        await session.scalar(
            select(func.count())
            .select_from(Chunk)
            .join(Document, Document.id == Chunk.doc_id)
            .where(Document.import_batch_id.is_not(None))
        )
        or 0
    )

    resources: dict[str, dict[str, int]] = {}
    for name, model in RESOURCE_MODELS.items():
        resources[name] = {
            "total": await _count(session, model),
            "imported": await _count(session, model, imported=True),
            "test": await _count(session, model, imported=False),
        }

    docs_total = await _count(session, Document)
    docs_imported = await _count(session, Document, imported=True)

    return {
        "knowledge_bases": {
            "total": len(kb_rows),
            "with_imported_data": len(imported_kb_ids),
            "test_only": len([k for k in kb_rows if k.id not in imported_kb_ids]),
        },
        "resources": resources,
        "documents": {
            "total": docs_total,
            "imported": docs_imported,
            "test": docs_total - docs_imported,
        },
        "chunks": {
            "total": chunks_total,
            "imported": chunks_imported,
            "test": chunks_total - chunks_imported,
        },
        "milvus_vectors": {"total": _milvus_total(), "note": "资源向量 + 文档切片向量合计"},
        "rule": "import_batch_id 非空 = 真实导入数据；为空 = 手工/历史测试数据（本次清理目标）",
    }


def _doc_scope(imported: bool | None, batch_ids: list[str] | None):
    stmt = select(Document.id, Document.kb_id, Document.storage_path).where(Document.deleted_at.is_(None))
    if batch_ids:
        stmt = stmt.where(Document.import_batch_id.in_(batch_ids))
    elif imported is False:
        stmt = stmt.where(Document.import_batch_id.is_(None))
    elif imported is True:
        stmt = stmt.where(Document.import_batch_id.is_not(None))
    return stmt


async def plan_cleanup(
    session: AsyncSession,
    *,
    batch_ids: list[str] | None = None,
    include_test_data: bool = True,
    include_knowledge_bases: bool = False,
) -> dict[str, Any]:
    """清理计划（dry run）：只统计将被删除的对象，不做任何修改。"""
    batch_ids = batch_ids or []
    scope_desc = f"批次 {batch_ids}" if batch_ids else ("手工/测试数据" if include_test_data else "无")

    doc_rows = (
        (await session.execute(_doc_scope(False if not batch_ids else None, batch_ids or None))).all()
        if include_test_data or batch_ids
        else []
    )
    doc_ids = [r[0] for r in doc_rows]

    resource_plan: dict[str, int] = {}
    for name, model in RESOURCE_MODELS.items():
        stmt = select(func.count()).select_from(model)
        if batch_ids:
            stmt = stmt.where(model.import_batch_id.in_(batch_ids))
        else:
            stmt = stmt.where(model.import_batch_id.is_(None))
        resource_plan[name] = int(await session.scalar(stmt) or 0)

    kb_plan = 0
    if include_knowledge_bases:
        imported_kb = {
            r
            for r in (
                await session.scalars(
                    select(Document.kb_id).where(Document.import_batch_id.is_not(None)).distinct()
                )
            ).all()
        }
        kb_plan = int(
            await session.scalar(
                select(func.count()).select_from(KnowledgeBase).where(KnowledgeBase.id.not_in(imported_kb))
            )
            or 0
        ) if imported_kb else int(await session.scalar(select(func.count()).select_from(KnowledgeBase)) or 0)

    return {
        "dry_run": True,
        "scope": scope_desc,
        "documents": len(doc_ids),
        "chunks": int(
            await session.scalar(select(func.count()).select_from(Chunk).where(Chunk.doc_id.in_(doc_ids)))
            or 0
        )
        if doc_ids
        else 0,
        "resources": resource_plan,
        "knowledge_bases": kb_plan,
        "protected": ["users", "roles", "system_configs", "model_configs", "audit_logs", "categories", "tags"],
    }


async def execute_cleanup(
    session: AsyncSession,
    *,
    batch_ids: list[str] | None = None,
    include_test_data: bool = True,
    include_knowledge_bases: bool = False,
    confirm: bool = False,
) -> dict[str, Any]:
    """执行清理（必须 confirm=True）。

    生命周期顺序（自下而上删除，避免孤儿数据）：
    1. Milvus 文档向量 → 2. chunks → 3. 存储文件 → 4. documents
    5. 资源向量 → 6. KB 挂载 → 7. KG 节点（边级联）→ 8. 资源行
    9. 可选：仅含测试数据的知识库
    """
    if not confirm:
        raise AppException(400, "高风险操作：必须显式传 confirm=true（建议先调用统计接口确认范围）")

    batch_ids = batch_ids or []
    store = get_vector_store()
    storage = get_storage()
    deleted: dict[str, int] = {
        "documents": 0,
        "chunks": 0,
        "storage_files": 0,
        "milvus_doc_vectors": 0,
        "resources": 0,
        "kg_nodes": 0,
        "kb_mounts": 0,
        "milvus_resource_vectors": 0,
        "knowledge_bases": 0,
    }
    errors: list[str] = []

    # ── 文档侧 ──────────────────────────────────────────────────────────────
    if include_test_data or batch_ids:
        rows = (
            await session.execute(_doc_scope(False if not batch_ids else None, batch_ids or None))
        ).all()
        for doc_id, _kb, storage_path in rows:
            try:
                store.delete_by_doc(str(doc_id))
                deleted["milvus_doc_vectors"] += 1
            except Exception as exc:  # noqa: BLE001
                errors.append(f"Milvus 删除失败 doc={doc_id}: {exc}")
            try:
                await session.execute(delete(Chunk).where(Chunk.doc_id == doc_id))
            except Exception as exc:  # noqa: BLE001
                errors.append(f"chunks 删除失败 doc={doc_id}: {exc}")
            if storage_path:
                try:
                    storage.delete(storage_path)
                    deleted["storage_files"] += 1
                except Exception:  # noqa: BLE001 - 文件缺失不影响 DB 清理
                    pass
        doc_ids = [r[0] for r in rows]
        for i in range(0, len(doc_ids), _BATCH):
            chunk_ids = doc_ids[i : i + _BATCH]
            await session.execute(delete(Document).where(Document.id.in_(chunk_ids)))
            deleted["documents"] += len(chunk_ids)
        await session.commit()

    # ── 资源侧 ──────────────────────────────────────────────────────────────
    rsv = ResourceVectorService(session)
    for rtype, model in RESOURCE_MODELS.items():
        stmt = select(model.id)
        if batch_ids:
            stmt = stmt.where(model.import_batch_id.in_(batch_ids))
        else:
            stmt = stmt.where(model.import_batch_id.is_(None))
        ids = list((await session.scalars(stmt)).all())
        for i in range(0, len(ids), _BATCH):
            part = ids[i : i + _BATCH]
            # 6. KB 挂载（含其资源向量）
            mounts = list(
                (
                    await session.scalars(
                        select(KnowledgeBaseResource).where(
                            KnowledgeBaseResource.resource_type == rtype,
                            KnowledgeBaseResource.resource_id.in_(part),
                        )
                    )
                ).all()
            )
            for m in mounts:
                try:
                    rsv.delete_vectors(
                        kb_id=m.knowledge_base_id, resource_type=rtype, resource_id=m.resource_id
                    )
                    deleted["milvus_resource_vectors"] += 1
                except Exception as exc:  # noqa: BLE001
                    errors.append(f"资源向量删除失败 {rtype}/{m.resource_id}: {exc}")
            if mounts:
                await session.execute(
                    delete(KnowledgeBaseResource).where(
                        KnowledgeBaseResource.resource_type == rtype,
                        KnowledgeBaseResource.resource_id.in_(part),
                    )
                )
                deleted["kb_mounts"] += len(mounts)
            # 7. KG 节点（边由 FK 级联）
            kg_ids = list(
                (
                    await session.scalars(
                        select(KgNode.id).where(
                            KgNode.resource_type == rtype, KgNode.resource_id.in_(part)
                        )
                    )
                ).all()
            )
            if kg_ids:
                await session.execute(delete(KgNode).where(KgNode.id.in_(kg_ids)))
                deleted["kg_nodes"] += len(kg_ids)
            # 8. 资源行（多对多标签由关联表 CASCADE）
            await session.execute(delete(model).where(model.id.in_(part)))
            deleted["resources"] += len(part)
        if ids:
            await session.commit()

    # ── 知识库（仅"不含任何真实导入文档"的库）──────────────────────────────
    if include_knowledge_bases:
        imported_kb = {
            r
            for r in (
                await session.scalars(
                    select(Document.kb_id).where(Document.import_batch_id.is_not(None)).distinct()
                )
            ).all()
        }
        stmt = select(KnowledgeBase.id)
        if imported_kb:
            stmt = stmt.where(KnowledgeBase.id.not_in(imported_kb))
        kb_ids = list((await session.scalars(stmt)).all())
        for kb_id in kb_ids:
            try:
                store.delete_by_kb(str(kb_id))
            except Exception as exc:  # noqa: BLE001
                errors.append(f"KB 向量删除失败 kb={kb_id}: {exc}")
            await session.execute(delete(KnowledgeBase).where(KnowledgeBase.id == kb_id))
            deleted["knowledge_bases"] += 1
        if kb_ids:
            await session.commit()

    logger.info(f"测试数据清理完成：{deleted} 错误 {len(errors)} 条")
    return {"deleted": deleted, "errors": errors[:50], "error_count": len(errors)}
