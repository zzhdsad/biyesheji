"""资源批量挂载 + 向量化任务（中药 / 方剂 / 理论 / 文献 → 知识库 → Milvus）。

为什么需要
----------
资源（Herb/Prescription/Theory/Literature）只有同时满足

    KnowledgeBaseResource 挂载  +  Milvus 中存在当前模型的向量

才会进入 RAG 检索链路。逐条挂载实测约 0.08 条/秒（每条一次 Milvus
``replace_doc`` + 一次 flush + 一次 commit），6398 味中药就要 20 小时，
中途崩溃还得从头再来。本模块把它改造成**批量、可续跑、可重试**的任务。

任务模型
--------
复用既有 ``import_jobs``（ImportJob，生命周期 pending → processing →
completed / failed / cancelled），**不新建第二套任务体系**；
资源批量任务就是一个 ``dataset_id = "resource-mount:<type>"`` 的导入任务，
因此 ``/admin/import/jobs`` 现有的进度/失败明细接口直接可用。

幂等与续跑（不依赖内存变量）
--------------------------
- 任务启动时把待处理清单从 ``failed_items`` 取出并清空；
- 每批完成后把「未处理完的 + 失败的」写回 ``failed_items`` 并 commit；
- 因此进程崩溃 / 重启 / 主动停止后，再次「继续任务」即可从数据库记录的位置继续；
- 已完成项（已挂载且已有当前模型向量）在候选筛选阶段就被排除，不会重复向量化。

批处理形态
--------
    资源查询 → 分批创建 KnowledgeBaseResource → 批量 canonical text
    → 批量 embedding（受 VECTORIZE_JOB_CHUNK_BATCH 限制）
    → 批量 Milvus 写入（replace_docs_bulk，每批一次 flush）→ 批量 commit

不重新实现 ResourceVectorService：canonical text / chunk / 稳定向量 ID 全部
复用它的既有函数，单条降级路径也直接调用 ``vectorize_and_store``。
"""

from __future__ import annotations

import asyncio
import threading
import uuid
from collections.abc import Sequence
from datetime import datetime, timezone
from typing import Any

from loguru import logger
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.application import import_service
from src.application.model_config_service import get_effective_config_cached
from src.application.resource_vector_service import (
    ResourceVectorService,
    build_canonical_text,
    chunk_resource,
    make_doc_id,
    make_vector_id,
)
from src.application.vector_model import key_from_config
from src.core.config import settings
from src.core.exceptions import AppException
from src.core.source_meta import credibility_for
from src.domain.models import (
    Herb,
    ImportJob,
    KnowledgeBase,
    KnowledgeBaseResource,
    Literature,
    Prescription,
    Theory,
)
from src.infrastructure.embedding import get_embedding
from src.infrastructure.milvus_store import VectorRow, get_vector_store

# 资源类型 → 模型（与 ResourceVectorService.RESOURCE_TYPES 对齐）
RESOURCE_MODELS: dict[str, type] = {
    "herb": Herb,
    "prescription": Prescription,
    "theory": Theory,
    "literature": Literature,
}

RESOURCE_LABELS: dict[str, str] = {
    "herb": "中药",
    "prescription": "方剂",
    "theory": "理论",
    "literature": "文献",
}

# 资源批量任务的 dataset_id 前缀（与"数据集导入"区分，便于列表/统计识别）
DATASET_PREFIX = "resource-mount"

# 已挂载资源扫描分片（控制单次 SQL / Milvus 查询规模）
_SCAN_CHUNK = 500


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _dataset_id(resource_type: str) -> str:
    return f"{DATASET_PREFIX}:{resource_type}"


async def _active_job(db: AsyncSession) -> ImportJob | None:
    """是否有进行中的导入/向量化任务（同一时刻只允许一个，避免并发写 Milvus）。"""
    return (
        await db.scalars(
            select(ImportJob)
            .where(ImportJob.status.in_(["pending", "processing"]))
            .order_by(ImportJob.created_at.desc())
        )
    ).first()


def _pending_marker(resource_id: uuid.UUID | str, name: str = "") -> dict[str, Any]:
    """待处理清单条目（JSON 结构；``pending=True`` 与失败项区分）。"""
    return {"resource_id": str(resource_id), "name": name, "pending": True}


def _failure_item(resource_id: uuid.UUID | str, name: str, reason: str) -> dict[str, Any]:
    return {"resource_id": str(resource_id), "name": name, "reason": str(reason)[:300]}


def _extract_ids(items: Sequence[dict[str, Any]]) -> list[uuid.UUID]:
    """从 failed_items 解析资源 id（跳过格式非法项）。"""
    ids: list[uuid.UUID] = []
    for item in items or []:
        raw = (item or {}).get("resource_id")
        if not raw:
            continue
        try:
            ids.append(uuid.UUID(str(raw)))
        except ValueError:
            continue
    return ids


# ── 候选筛选（已完成 → 跳过）────────────────────────────────────────────────


async def pending_ids(
    db: AsyncSession,
    kb_id: uuid.UUID,
    resource_type: str,
    limit: int | None = None,
    *,
    store=None,
    model_key: str | None = None,
) -> list[uuid.UUID]:
    """列出"尚未进入检索链路"的资源 id（有序，保证续跑稳定）。

    判定口径：
    1. 未挂载到该 KB 的资源 → 待处理；
    2. 已挂载但 Milvus 中**没有当前模型向量**的资源 → 待处理（补向量）；
    3. 已挂载且已有当前模型向量 → 已完成，跳过（不重复向量化）。

    Args:
        limit: 返回条数上限（None 时用 VECTORIZE_JOB_MAX_ITEMS 安全阀）
        store/model_key: 传入才会做第 2 步的向量存在性检查（创建任务时可选）
    """
    model = RESOURCE_MODELS[resource_type]
    cap = min(limit or settings.VECTORIZE_JOB_MAX_ITEMS, settings.VECTORIZE_JOB_MAX_ITEMS)
    mounted_sq = select(KnowledgeBaseResource.resource_id).where(
        KnowledgeBaseResource.knowledge_base_id == kb_id,
        KnowledgeBaseResource.resource_type == resource_type,
    )
    unmounted = list(
        (
            await db.scalars(
                select(model.id).where(model.id.not_in(mounted_sq)).order_by(model.id).limit(cap)
            )
        ).all()
    )
    if len(unmounted) >= cap or store is None or not model_key:
        return unmounted

    # 已挂载但缺当前模型向量（上次任务中断 / 向量被清理）→ 补向量
    need = cap - len(unmounted)
    missing: list[uuid.UUID] = []
    offset = 0
    while len(missing) < need:
        chunk = list(
            (
                await db.scalars(
                    select(model.id)
                    .where(model.id.in_(mounted_sq))
                    .order_by(model.id)
                    .offset(offset)
                    .limit(_SCAN_CHUNK)
                )
            ).all()
        )
        if not chunk:
            break
        offset += len(chunk)
        doc_ids = [make_doc_id(resource_type, rid, kb_id) for rid in chunk]
        try:
            have = await asyncio.to_thread(store.vectorized_doc_ids, doc_ids, model_key)
        except Exception:  # noqa: BLE001 - 幂等判断失败按"待处理"处理，不阻断任务
            have = set()
        for rid, doc_id in zip(chunk, doc_ids):
            if doc_id not in have:
                missing.append(rid)
                if len(missing) >= need:
                    break
    return unmounted + missing


# ── 任务创建 / 查询 ──────────────────────────────────────────────────────────


async def create_job(
    db: AsyncSession,
    *,
    resource_type: str,
    kb_id: uuid.UUID,
    limit: int | None = None,
    user_id: uuid.UUID | None = None,
) -> ImportJob:
    """创建资源批量挂载+向量化任务（pending），待处理清单落库。

    Raises:
        AppException: 400 参数非法 / 404 知识库不存在 / 409 已有任务在执行
    """
    if resource_type not in RESOURCE_MODELS:
        raise AppException(400, f"不支持的资源类型：{resource_type}")
    kb = await db.get(KnowledgeBase, kb_id)
    if kb is None:
        raise AppException(404, "知识库不存在")

    await import_service.reap_orphan_jobs(db)
    if await _active_job(db) is not None:
        raise AppException(409, "已有导入/向量化任务在执行中，请等待其完成后再发起")

    cfg = await get_effective_config_cached(db)
    model_key = key_from_config(cfg)
    ids = await pending_ids(
        db,
        kb_id,
        resource_type,
        limit,
        store=get_vector_store(),
        model_key=model_key,
    )
    if not ids:
        raise AppException(400, "没有需要挂载/向量化的资源（均已挂载且已有当前模型向量）")

    job = ImportJob(
        batch_id=uuid.uuid4().hex[:16],
        dataset_id=_dataset_id(resource_type),
        dataset_name=f"资源批量挂载·{RESOURCE_LABELS[resource_type]}",
        source_file="",
        target_type=resource_type,
        kb_id=kb_id,
        status="pending",
        total=len(ids),
        # 待处理清单落库：任务启动后取出并清空，进度每批写回（断点续跑依据）
        failed_items=[_pending_marker(i) for i in ids],
        created_by=user_id,
    )
    db.add(job)
    await db.commit()
    await db.refresh(job)
    logger.info(
        f"创建资源批量挂载任务 job_id={job.id} type={resource_type} kb={kb_id} total={len(ids)}"
    )
    return job


async def resume_job(
    db: AsyncSession,
    job_id: uuid.UUID,
    user_id: uuid.UUID | None = None,
) -> ImportJob:
    """继续/重试任务：把未处理完的与失败的条目重新排队（不重复已成功的）。

    断点续跑入口：任务崩溃 / 重启 / 主动停止后，未完成条目仍留在
    ``failed_items`` 中，这里把它们重新置为 pending 并后台启动。
    """
    job = await db.get(ImportJob, job_id)
    if job is None:
        raise AppException(404, "任务不存在")
    if job.status in ("pending", "processing"):
        raise AppException(400, f"任务仍在执行中（{job.status}），无需继续")
    ids = _extract_ids(job.failed_items or [])
    if not ids:
        raise AppException(400, "没有未完成/失败的条目，无需继续")
    job.status = "pending"
    job.error_message = ""
    job.finished_at = None
    if user_id is not None:
        job.created_by = user_id
    await db.commit()
    await db.refresh(job)
    logger.info(f"继续资源批量挂载任务 job_id={job_id} 剩余 {len(ids)} 条")
    return job


async def latest_job(db: AsyncSession, resource_type: str | None = None) -> ImportJob | None:
    """最近一次资源批量任务（前端轮询进度）。"""
    stmt = select(ImportJob)
    if resource_type:
        stmt = stmt.where(ImportJob.dataset_id == _dataset_id(resource_type))
    else:
        stmt = stmt.where(ImportJob.dataset_id.like(f"{DATASET_PREFIX}:%"))
    return (await db.scalars(stmt.order_by(ImportJob.created_at.desc()))).first()


def job_to_dict(job: ImportJob | None) -> dict[str, Any] | None:
    """任务 → 前端结构（在导入任务字段之上补充批次信息）。"""
    data = import_service.job_to_dict(job)
    if data is None:
        return None
    batch_size = max(1, settings.VECTORIZE_JOB_RESOURCE_BATCH)
    total = data["total"]
    data["job_type"] = "resource"
    data["resource_type"] = job.target_type
    data["batch_size"] = batch_size
    data["total_batches"] = (total + batch_size - 1) // batch_size if total else 0
    data["current_batch"] = min(data["processed"] // batch_size + 1, data["total_batches"]) if total else 0
    data["processing"] = max(total - data["processed"], 0)
    return data


async def stats(db: AsyncSession, kb_id: uuid.UUID | None = None) -> dict[str, Any]:
    """各资源类型的总数 / 已挂载 / 未挂载（供界面选择范围）。"""
    items: list[dict[str, Any]] = []
    for rtype, model in RESOURCE_MODELS.items():
        total = int(await db.scalar(select(func.count()).select_from(model)) or 0)
        stmt = select(func.count()).select_from(KnowledgeBaseResource).where(
            KnowledgeBaseResource.resource_type == rtype
        )
        if kb_id is not None:
            stmt = stmt.where(KnowledgeBaseResource.knowledge_base_id == kb_id)
        mounted = int(await db.scalar(stmt) or 0)
        items.append(
            {
                "resource_type": rtype,
                "label": RESOURCE_LABELS[rtype],
                "total": total,
                "mounted": mounted,
                "unmounted": max(total - mounted, 0),
            }
        )
    return {"kb_id": str(kb_id) if kb_id else None, "items": items}


# ── 执行 ────────────────────────────────────────────────────────────────────


def start_job(job_id: uuid.UUID) -> None:
    """后台线程启动任务（HTTP 立即返回）。"""
    threading.Thread(
        target=_run_in_thread, args=(job_id,), name=f"resource-mount-{job_id}", daemon=True
    ).start()


def _run_in_thread(job_id: uuid.UUID) -> None:
    try:
        asyncio.run(_execute(job_id))
    except Exception:  # noqa: BLE001 - 兜底：任何异常都不应让线程静默消失
        logger.exception(f"资源批量挂载任务异常终止 job_id={job_id}")


async def _execute(job_id: uuid.UUID) -> None:
    engine = create_async_engine(settings.DATABASE_URL, pool_pre_ping=True)
    Session = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with Session() as session:
            job = await session.get(ImportJob, job_id)
            if job is None:
                logger.error(f"资源批量任务不存在 job_id={job_id}")
                return
            if import_service.is_cancel_requested(job_id) or job.status == "cancelled":
                job.status = "cancelled"
                job.started_at = _now()
                job.finished_at = _now()
                job.error_message = "任务已取消，未处理任何资源"
                await session.commit()
                return

            # 取出待处理清单（DB 持久化）→ 清空 → 每批写回剩余，保证可续跑
            pending = _extract_ids(job.failed_items or [])
            job.status = "processing"
            job.started_at = _now()
            job.error_message = ""
            job.total = len(pending)
            job.processed = 0
            job.succeeded = 0
            job.failed = 0
            job.skipped = 0
            job.failed_items = []
            await session.commit()
            if not pending:
                job.status = "completed"
                job.finished_at = _now()
                await session.commit()
                return

            rtype = job.target_type
            kb_id = job.kb_id
            if rtype not in RESOURCE_MODELS or kb_id is None:
                job.status = "failed"
                job.error_message = f"任务参数非法：type={rtype} kb_id={kb_id}"
                job.finished_at = _now()
                await session.commit()
                return

            cfg = await get_effective_config_cached(session)
            model_key = key_from_config(cfg)
            embedding = get_embedding(cfg)
            store = get_vector_store()
            batch_size = max(1, settings.VECTORIZE_JOB_RESOURCE_BATCH)
            chunk_batch = max(1, settings.VECTORIZE_JOB_CHUNK_BATCH)
            total_batches = (len(pending) + batch_size - 1) // batch_size

            failures: list[dict[str, Any]] = []
            succeeded = skipped = 0
            processed = 0
            cancelled = False

            for batch_no in range(total_batches):
                if import_service.is_cancel_requested(job_id):
                    cancelled = True
                    logger.info(f"资源批量任务被取消 job_id={job_id} 已完成 {processed}/{len(pending)}")
                    break
                batch_ids = pending[batch_no * batch_size : (batch_no + 1) * batch_size]
                try:
                    result = await _process_batch(
                        session,
                        batch_ids,
                        rtype,
                        kb_id,
                        embedding,
                        store,
                        model_key,
                        chunk_batch,
                        batch_no,
                    )
                    succeeded += len(result["succeeded"])
                    skipped += len(result["skipped"])
                    failures.extend(result["failures"])
                except Exception as exc:  # noqa: BLE001 - 整批失败不得中断任务
                    # 降级逐条处理：把"整批一个原因"拆成"每条各自的原因"
                    logger.warning(
                        f"资源批量挂载失败 job_id={job_id} batch={batch_no + 1}: {exc}，降级为逐条处理"
                    )
                    await session.rollback()
                    job = await session.get(ImportJob, job_id)
                    for rid in batch_ids:
                        try:
                            state = await _process_one(session, rid, rtype, kb_id)
                            if state == "skipped":
                                skipped += 1
                            else:
                                succeeded += 1
                        except Exception as single:  # noqa: BLE001
                            failures.append(_failure_item(rid, "", single))

                processed += len(batch_ids)
                job.processed = processed
                job.succeeded = succeeded
                job.skipped = skipped
                job.failed = len(failures)
                # 剩余待处理 + 失败项写回 DB：中断后「继续任务」即可接着跑
                remaining = [_pending_marker(i) for i in pending[processed:]]
                job.failed_items = (failures + remaining)[-1000:]
                await session.commit()
                logger.info(
                    f"[resource-mount] job={job_id} batch={batch_no + 1}/{total_batches} "
                    f"processed={processed}/{len(pending)} ok={succeeded} skip={skipped} "
                    f"failed={len(failures)}"
                )

            job.processed = processed
            job.succeeded = succeeded
            job.skipped = skipped
            job.failed = len(failures)
            if cancelled:
                job.status = "cancelled"
                remaining = [_pending_marker(i) for i in pending[processed:]]
                job.failed_items = (failures + remaining)[-1000:]
                job.error_message = (
                    f"已停止：已处理 {processed}/{len(pending)}，"
                    f"剩余 {len(remaining)} 条未处理（可用「继续任务」从断点继续）"
                )
            else:
                job.status = "completed" if succeeded + skipped > 0 else "failed"
                job.failed_items = failures[-1000:]
                job.error_message = (
                    "；".join(f["reason"] for f in failures[-5:])[:2000] if failures else ""
                )
            job.finished_at = _now()
            await session.commit()
            logger.info(
                f"资源批量挂载结束 job_id={job.id} type={rtype} status={job.status} "
                f"total={job.total} succeeded={succeeded} skipped={skipped} failed={len(failures)}"
            )
    finally:
        import_service._clear_cancel_flag(job_id)  # noqa: SLF001 - 复用既有清理逻辑
        await engine.dispose()


async def _process_batch(
    session: AsyncSession,
    resource_ids: list[uuid.UUID],
    resource_type: str,
    kb_id: uuid.UUID,
    embedding,
    store,
    model_key: str,
    chunk_batch: int,
    batch_no: int,
) -> dict[str, Any]:
    """批量处理一批资源：挂载 → 批量 embedding → 批量写 Milvus → 批量提交。

    Returns:
        ``{"succeeded": [id], "skipped": [id], "failures": [item]}``
    """
    model = RESOURCE_MODELS[resource_type]
    resources = (
        await session.scalars(select(model).where(model.id.in_(resource_ids)))
    ).all()
    by_id = {r.id: r for r in resources}

    # 0) 已完成（已有当前模型向量）→ 跳过，绝不重复向量化
    #    （断点续跑 / 重复执行时最关键的一步：只补没做完的）
    doc_id_by_rid = {rid: make_doc_id(resource_type, rid, kb_id) for rid in resource_ids}
    try:
        have_vectors = await asyncio.to_thread(
            store.vectorized_doc_ids, list(doc_id_by_rid.values()), model_key
        )
    except Exception:  # noqa: BLE001 - 判断失败按"未完成"处理，不阻断任务
        have_vectors = set()

    # 1) 批量构建 canonical text + chunk（复用既有规则，不重新实现）
    texts: list[str] = []
    pairs: list[tuple[Any, Any]] = []  # (resource, ResourceChunk)
    skipped: list[uuid.UUID] = []
    missing: list[uuid.UUID] = []
    empty: list[uuid.UUID] = []
    for rid in resource_ids:
        obj = by_id.get(rid)
        if obj is None:
            missing.append(rid)
            continue
        if doc_id_by_rid[rid] in have_vectors:
            skipped.append(rid)  # 已有向量：只补挂载，不重新编码
            continue
        canonical = build_canonical_text(obj)
        chunks = chunk_resource(obj, canonical)
        if not chunks:
            # 空文本无检索价值：跳过（不挂载、不向量化，禁止伪造内容）
            skipped.append(rid)
            empty.append(rid)
            continue
        for ch in chunks:
            pairs.append((obj, ch))
            texts.append(ch.content)

    if not pairs:
        failures = [_failure_item(rid, "", "资源不存在") for rid in missing]
        return {"succeeded": [], "skipped": skipped, "failures": failures}

    # 2) 批量 embedding（受 chunk_batch 限制，内存有上界）
    dense_all: list[list[float]] = []
    sparse_all: list[dict] = []
    for i in range(0, len(texts), chunk_batch):
        dense, sparse = await asyncio.to_thread(embedding.encode, texts[i : i + chunk_batch])
        dense_all.extend(dense)
        sparse_all.extend(sparse)
    if len(dense_all) != len(pairs):
        raise AppException(
            422, f"Embedding 输出数量不一致：期望 {len(pairs)}，实际 {len(dense_all)}"
        )

    # 3) 组装向量行（沿用 ResourceVectorService 的稳定 ID 规则）
    credibility = credibility_for(None)
    rows: list[VectorRow] = []
    for (obj, ch), dv, sv in zip(pairs, dense_all, sparse_all):
        rows.append(
            VectorRow(
                id=make_vector_id(resource_type, obj.id, kb_id, ch.chunk_index),
                doc_id=make_doc_id(resource_type, obj.id, kb_id),
                kb_id=str(kb_id),
                chunk_index=ch.chunk_index,
                content=ch.content,
                page_num=None,
                title_path=ch.title_path,
                dense_vector=dv,
                sparse_vector=sv,
                source_type=None,
                credibility_level=credibility,
                resource_type=resource_type,
                resource_id=str(obj.id),
                resource_name=(getattr(obj, "name", "") or ""),
                era=None,
                embedding_model=model_key,
            )
        )

    # 4) 批量挂载（已有挂载不重复插入：先查存在集合 + 唯一约束兜底）
    mounted_ids = {
        r
        for r in (
            await session.scalars(
                select(KnowledgeBaseResource.resource_id).where(
                    KnowledgeBaseResource.knowledge_base_id == kb_id,
                    KnowledgeBaseResource.resource_type == resource_type,
                    KnowledgeBaseResource.resource_id.in_(resource_ids),
                )
            )
        ).all()
    }
    # 已挂载的不重复插入（唯一约束兜底）；空文本资源不挂载（无检索价值）
    blocked = set(missing) | set(empty)
    to_mount = [rid for rid in resource_ids if rid not in mounted_ids and rid not in blocked]
    for rid in to_mount:
        session.add(
            KnowledgeBaseResource(
                knowledge_base_id=kb_id,
                resource_type=resource_type,
                resource_id=rid,
            )
        )
    await session.flush()

    # 5) 批量写入 Milvus（先写新再清旧，每批一次 flush）
    flush_every = max(1, settings.VECTORIZE_JOB_FLUSH_EVERY)
    await asyncio.to_thread(
        store.replace_docs_bulk, rows, flush=((batch_no + 1) % flush_every == 0)
    )
    # 6) 批量提交：挂载与向量同一事务生效
    await session.commit()

    succeeded = sorted({obj.id for obj, _ in pairs})
    failures = [_failure_item(rid, "", "资源不存在") for rid in missing]
    return {"succeeded": succeeded, "skipped": skipped, "failures": failures}


async def _process_one(
    session: AsyncSession,
    resource_id: uuid.UUID,
    resource_type: str,
    kb_id: uuid.UUID,
) -> str:
    """单条降级路径（整批失败时精确定位失败项）：复用 ResourceVectorService。

    Returns:
        "succeeded" / "skipped"（空资源无文本）
    """
    model = RESOURCE_MODELS[resource_type]
    obj = await session.get(model, resource_id)
    if obj is None:
        raise AppException(404, "资源不存在")
    exists = await session.scalar(
        select(KnowledgeBaseResource).where(
            KnowledgeBaseResource.knowledge_base_id == kb_id,
            KnowledgeBaseResource.resource_type == resource_type,
            KnowledgeBaseResource.resource_id == resource_id,
        )
    )
    if exists is None:
        session.add(
            KnowledgeBaseResource(
                knowledge_base_id=kb_id,
                resource_type=resource_type,
                resource_id=resource_id,
            )
        )
        await session.flush()
    svc = ResourceVectorService()
    rows = await asyncio.to_thread(
        svc.vectorize_and_store, obj, kb_id=kb_id, resource_type=resource_type
    )
    await session.commit()
    return "succeeded" if rows else "skipped"
