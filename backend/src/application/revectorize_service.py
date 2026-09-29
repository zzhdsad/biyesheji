"""批量重新向量化任务（切换 embedding 模型后重建存量向量）。

为什么需要
----------
切换 embedding 模型后，Milvus 里的存量向量仍由旧模型生成，与新模型的查询向量
不在同一向量空间。逐篇点击"重新向量化"对最终用户不可接受，因此提供：

- 一键批量重建（后台异步执行，不阻塞 HTTP 请求）
- 进度可视化：总数 / 已完成 / 处理中 / 失败数 / 进度百分比
- 失败可重试（按失败文档 id 精确重建，不重复处理已成功的文档）

数据安全性
----------
- **只重写 Milvus 向量**，不删除也不修改 PostgreSQL 的文档 / 切片 / 资源数据
- 单篇文档的写入沿用 `BaseVectorStore.replace_doc`（先写新向量再清旧向量），
  因此任何时刻文档都至少有一份可用向量，不会出现"完全检索不到"的空窗
- 单篇失败只记入 failed_doc_ids，其余文档继续处理；失败文档保持旧向量可检索
"""

from __future__ import annotations

import asyncio
import threading
import uuid
from datetime import datetime, timedelta, timezone
from threading import Thread

from loguru import logger
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.core.config import settings
from src.core.exceptions import AppException
from src.domain.models import Chunk, Document, RevectorizeJob
from src.infrastructure.embedding import get_embedding
from src.infrastructure.milvus_store import VectorRow, get_vector_store
from src.application.indexing_service import IndexingService
from src.application.model_config_service import get_effective_config_cached
from src.application.vector_model import (
    key_from_config,
    stale_doc_ids,
)

# 进度写库频率：每处理 N 篇提交一次（避免每篇都 commit 拖慢任务）
_COMMIT_EVERY = 5

# 单批内允许向量化的切片数上限（保护内存：一批文档的切片可能很多）
_VECTORIZABLE_STATUS = ("success", "completed", "failed")


def _now() -> datetime:
    return datetime.now(timezone.utc)


# 孤儿任务判定：进行中任务的心跳（updated_at）超过该时长视为已中断
# （服务重启 / 容器被杀 / 进程退出时线程消失，任务会永远停在 running，
#  若不回收将永久阻塞后续任务）。
_ORPHAN_AFTER_SECONDS = 30 * 60


async def reap_orphan_jobs(db: AsyncSession) -> int:
    """把"心跳超时"的进行中任务标记为已中断，避免永久阻塞后续任务。

    Returns:
        被回收的任务数量。
    """
    cutoff = _now() - timedelta(seconds=_ORPHAN_AFTER_SECONDS)
    rows = (
        await db.scalars(
            select(RevectorizeJob).where(
                RevectorizeJob.status.in_(["pending", "running"]),
                RevectorizeJob.updated_at < cutoff,
            )
        )
    ).all()
    for job in rows:
        job.status = "failed"
        job.error_message = "任务中断（服务重启或进程退出），可重试未完成文档"
        job.finished_at = _now()
    if rows:
        await db.commit()
        logger.warning(f"回收 {len(rows)} 个中断的重新向量化任务")
    return len(rows)


async def _active_job(db: AsyncSession) -> RevectorizeJob | None:
    """是否存在进行中的任务（同一时刻只允许一个，避免并发写 Milvus）。"""
    row = (
        await db.scalars(
            select(RevectorizeJob)
            .where(RevectorizeJob.status.in_(["pending", "running"]))
            .order_by(RevectorizeJob.created_at.desc())
        )
    ).first()
    return row


async def create_job(
    db: AsyncSession,
    *,
    target_model: str,
    previous_model: str | None = None,
    kb_id: uuid.UUID | None = None,
    user_id: uuid.UUID | None = None,
    doc_ids: list[uuid.UUID] | None = None,
    limit: int | None = None,
) -> RevectorizeJob:
    """创建批量重新向量化任务（pending），随后由 start_job 后台执行。

    Args:
        doc_ids: 指定文档（重试失败项时使用）；为空则按"旧模型文档"自动筛选。
        limit: 仅在"自动筛选"时生效，用于小批量分批处理（避免一次拉起全量）。
              上限仍受 ``VECTORIZE_JOB_MAX_ITEMS`` 约束。
    """
    # 先回收中断的残留任务（服务重启场景），再判断是否有真正在跑的任务
    await reap_orphan_jobs(db)
    if await _active_job(db) is not None:
        raise AppException(409, "已有重新向量化任务在执行中，请等待其完成后再发起")

    if doc_ids is not None and not doc_ids:
        # 显式传入空列表 = 调用方筛选结果为空，绝不能悄悄退化成"全量重建"
        raise AppException(400, "指定的文档列表为空，未创建任务（请确认筛选条件）")
    ids = list(doc_ids) if doc_ids else await stale_doc_ids(db, target_model, kb_id)
    ids = [i for i in ids if isinstance(i, uuid.UUID)]
    if limit is not None and doc_ids is None:
        # 小批量分批处理：只取前 limit 篇，剩余文档留待下一批（仍可用"继续"处理）
        cap = min(max(0, int(limit)), settings.VECTORIZE_JOB_MAX_ITEMS)
        ids = ids[:cap]
    if not ids:
        raise AppException(400, "没有需要重新向量化的文档（存量向量均已是当前模型）")

    job = RevectorizeJob(
        target_model=target_model,
        previous_model=previous_model,
        kb_id=kb_id,
        status="pending",
        total=len(ids),
        processed=0,
        succeeded=0,
        failed=0,
        failed_doc_ids=[str(i) for i in ids],
        created_by=user_id,
    )
    db.add(job)
    await db.commit()
    await db.refresh(job)
    logger.info(f"创建批量重新向量化任务 job_id={job.id} total={job.total} target={target_model}")
    return job


# ── 取消信号（进程内）────────────────────────────────────────────────────────
# 后台任务以线程形式跑在当前进程内，因此用进程内事件做取消信号最可靠：
# worker 线程在长事务里读取 DB 状态可能命中旧快照，而事件是即时可见的。
# DB 中的 status=cancelled 仍保留，用于接口展示与跨进程兜底判定。
_CANCEL_FLAGS: dict[str, threading.Event] = {}
_CANCEL_LOCK = threading.Lock()


def request_cancel(job_id: uuid.UUID) -> None:
    """置位取消信号（立即对后台线程可见）。"""
    with _CANCEL_LOCK:
        _CANCEL_FLAGS.setdefault(str(job_id), threading.Event()).set()


def is_cancel_requested(job_id: uuid.UUID) -> bool:
    """任务是否已被请求取消。"""
    with _CANCEL_LOCK:
        ev = _CANCEL_FLAGS.get(str(job_id))
    return bool(ev and ev.is_set())


def _clear_cancel_flag(job_id: uuid.UUID) -> None:
    """任务结束后清理信号，避免字典无限增长。"""
    with _CANCEL_LOCK:
        _CANCEL_FLAGS.pop(str(job_id), None)


async def cancel_job(db: AsyncSession, job_id: uuid.UUID) -> RevectorizeJob:
    """请求取消进行中的任务（协作式取消）。

    只把状态置为 `cancelled`，由后台线程在处理完**当前这篇**后自行停止：
    - 已重建成功的文档保持新向量（不回滚，避免"取消即丢失已完成工作"）
    - 未处理的文档仍是旧模型向量，可再次发起批量重建
    - 不做强制 kill：中途强杀会留下"写到一半"的文档状态

    Raises:
        AppException: 404 任务不存在 / 400 任务已结束（无需取消）
    """
    job = await db.get(RevectorizeJob, job_id)
    if job is None:
        raise AppException(404, "任务不存在")
    if job.status not in ("pending", "running"):
        raise AppException(400, f"任务已结束（{job.status}），无需取消")
    job.status = "cancelled"
    job.error_message = "取消请求已提交，任务将在当前文档处理完成后停止"
    await db.commit()
    await db.refresh(job)
    # 先记录 DB 状态（供接口/展示），再置位进程内信号（供后台线程即时感知）
    request_cancel(job_id)
    logger.info(f"请求取消重新向量化任务 job_id={job_id}")
    return job


def start_job(job_id: uuid.UUID) -> None:
    """后台线程启动任务（立即返回，HTTP 请求不被阻塞）。"""
    Thread(target=_run_in_thread, args=(job_id,), name=f"revectorize-{job_id}", daemon=True).start()


async def _process_doc_batch(
    session: AsyncSession,
    doc_ids: list[uuid.UUID],
    embedding,
    store,
    model_key: str,
    chunk_batch: int,
    batch_no: int,
) -> tuple[list[uuid.UUID], list[tuple[uuid.UUID, str]]]:
    """按**批**重建一批文档的向量（embedding 成批 → Milvus 成批写入 → 批量回写状态）。

    与旧实现（逐篇 IndexingService.run）的差别只在 IO 形态，语义完全一致：
    - 同一篇文档的切片在一次 embedding 批次里编码，向量内容不变；
    - 写入仍走 ``replace_docs_bulk``（先写新再清旧，BUG-046，无空窗期）；
    - 成功回写 ``documents.vector_model`` 与 ``parse_status=completed``（批量 UPDATE）。

    Args:
        doc_ids: 本批文档 id
        embedding: 当前生效的 embedding 实例（全局单例 BGE-M3）
        store: 向量库
        model_key: 本次写入的模型标识
        chunk_batch: 单次 embedding 的文本条数上限（限定内存峰值）
        batch_no: 批次序号（决定本批是否 flush）

    Returns:
        ``(成功文档 id 列表, [(失败文档 id, 失败原因)])``。
        单批内某篇文档"切片未就绪 / 无切片"不会中断其它文档。
    """
    docs = (
        await session.scalars(select(Document).where(Document.id.in_(doc_ids)))
    ).all()
    by_id = {d.id: d for d in docs}

    todo: list[Document] = []
    done: list[uuid.UUID] = []  # 已是当前模型向量 → 幂等跳过，不重复向量化
    bad: list[tuple[uuid.UUID, str]] = []
    for doc_id in doc_ids:
        doc = by_id.get(doc_id)
        if doc is None:
            bad.append((doc_id, "文档不存在"))
            continue
        if doc.parse_status not in _VECTORIZABLE_STATUS:
            bad.append((doc_id, f"切片未就绪（parse_status={doc.parse_status}）"))
            continue
        if doc.vector_model == model_key:
            # 断点续跑 / 重复执行：已是当前模型向量，不重复向量化
            done.append(doc_id)
            continue
        todo.append(doc)

    if not todo:
        return done, bad

    chunks = (
        await session.scalars(
            select(Chunk)
            .where(Chunk.doc_id.in_([d.id for d in todo]))
            .order_by(Chunk.doc_id, Chunk.chunk_index)
        )
    ).all()
    by_doc: dict[uuid.UUID, list[Chunk]] = {}
    for c in chunks:
        by_doc.setdefault(c.doc_id, []).append(c)

    pairs: list[tuple[Document, Chunk]] = []
    ok_docs: list[Document] = []
    for doc in todo:
        doc_chunks = by_doc.get(doc.id) or []
        if not doc_chunks:
            bad.append((doc.id, "文档无切片，请重新解析"))
            continue
        ok_docs.append(doc)
        pairs.extend((doc, c) for c in doc_chunks)

    if not pairs:
        return done, bad

    # 成批 embedding：一次编码 chunk_batch 条文本，避免单条调用把 CPU 打满、
    # 也避免一次性把整批文本全部驻留在内存
    dense_all: list[list[float]] = []
    sparse_all: list[dict] = []
    for i in range(0, len(pairs), chunk_batch):
        texts = [c.content for _, c in pairs[i : i + chunk_batch]]
        dense, sparse = await asyncio.to_thread(embedding.encode, texts)
        dense_all.extend(dense)
        sparse_all.extend(sparse)
    if len(dense_all) != len(pairs):
        raise AppException(
            422,
            f"Embedding 输出数量不一致：期望 {len(pairs)}，实际 {len(dense_all)}",
        )

    rows = [
        VectorRow(
            id=str(chunk.id),
            doc_id=str(chunk.doc_id),
            kb_id=str(chunk.kb_id),
            chunk_index=chunk.chunk_index,
            content=chunk.content,
            page_num=chunk.page_num,
            title_path=chunk.title_path,
            dense_vector=dv,
            sparse_vector=sv,
            source_type=chunk.source_type,
            credibility_level=chunk.credibility_level,
            embedding_model=model_key,
        )
        for (_, chunk), dv, sv in zip(pairs, dense_all, sparse_all)
    ]

    flush_every = max(1, settings.VECTORIZE_JOB_FLUSH_EVERY)
    await asyncio.to_thread(
        store.replace_docs_bulk,
        rows,
        flush=((batch_no + 1) % flush_every == 0),
    )
    # 批量回写文档状态（一次 UPDATE，一次提交，避免逐篇 commit）
    await session.execute(
        update(Document)
        .where(Document.id.in_([d.id for d in ok_docs]))
        .values(parse_status="completed", error_message="", vector_model=model_key)
    )
    return [d.id for d in ok_docs] + done, bad


def _run_in_thread(job_id: uuid.UUID) -> None:
    """线程入口：独立事件循环 + 独立 engine（与 index_runner 一致的做法）。"""
    try:
        asyncio.run(_execute(job_id))
    except Exception:  # noqa: BLE001 - 兜底：任何异常都不应让线程静默消失
        logger.exception(f"批量重新向量化任务异常终止 job_id={job_id}")


async def _execute(job_id: uuid.UUID) -> None:
    engine = create_async_engine(settings.DATABASE_URL, pool_pre_ping=True)
    Session = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with Session() as session:
            job = await session.get(RevectorizeJob, job_id)
            if job is None:
                logger.error(f"任务不存在 job_id={job_id}")
                return
            # 竞态防护：线程启动慢于取消请求时，不得把已取消的状态覆盖回 running
            if is_cancel_requested(job_id) or job.status == "cancelled":
                job.status = "cancelled"
                job.started_at = _now()
                job.finished_at = _now()
                job.error_message = "任务已取消，未处理任何文档"
                await session.commit()
                logger.info(f"任务启动前已被取消 job_id={job_id}")
                return
            job.status = "running"
            job.started_at = _now()
            job.error_message = ""
            await session.commit()

            pending_ids = [uuid.UUID(x) for x in (job.failed_doc_ids or [])]
            job.failed_doc_ids = []
            job.failed = 0
            job.processed = 0
            job.succeeded = 0
            await session.commit()

            cfg = await get_effective_config_cached(session)
            model_key = key_from_config(cfg)
            embedding = get_embedding(cfg)
            store = get_vector_store()

            batch_size = max(1, settings.VECTORIZE_JOB_DOC_BATCH)
            chunk_batch = max(1, settings.VECTORIZE_JOB_CHUNK_BATCH)
            total_batches = (len(pending_ids) + batch_size - 1) // batch_size

            failures: list[str] = []
            errors: list[str] = []
            succeeded_count = 0
            failed_count = 0
            processed = 0
            cancelled = False

            for batch_no in range(total_batches):
                # 协作式取消：管理员点击"取消"后，当前批次处理完就停止。
                # 判定用进程内信号（即时可见）；DB 状态作为跨进程/兜底依据。
                current_status = await session.scalar(
                    select(RevectorizeJob.status).where(RevectorizeJob.id == job_id)
                )
                if is_cancel_requested(job_id) or current_status == "cancelled":
                    cancelled = True
                    logger.info(
                        f"任务被取消 job_id={job_id} 已完成 {processed}/{len(pending_ids)}，"
                        "剩余文档保持旧向量不做改动"
                    )
                    break

                batch_ids = pending_ids[batch_no * batch_size : (batch_no + 1) * batch_size]
                try:
                    ok_ids, bad = await _process_doc_batch(
                        session,
                        batch_ids,
                        embedding,
                        store,
                        model_key,
                        chunk_batch,
                        batch_no,
                    )
                except Exception as exc:  # noqa: BLE001 - 整批失败不得中断任务
                    # 降级为逐篇处理：把"整批一个原因"拆成"每篇各自的原因"，
                    # 单篇失败只影响该篇，其余继续（沿用 IndexingService 单篇路径）。
                    reason = str(exc)[:300]
                    logger.warning(
                        f"批量向量化失败 job_id={job_id} batch={batch_no + 1}: {reason}，"
                        "降级为逐篇处理"
                    )
                    await session.rollback()
                    job = await session.get(RevectorizeJob, job_id)
                    ok_ids, bad = [], []
                    for doc_id in batch_ids:
                        try:
                            await IndexingService(session).run(doc_id)
                            ok_ids.append(doc_id)
                        except Exception as single:  # noqa: BLE001
                            bad.append((doc_id, str(single)[:300]))
                succeeded_count += len(ok_ids)
                for doc_id, reason in bad:
                    failures.append(str(doc_id))
                    errors.append(f"{doc_id}: {reason}")
                    failed_count += 1
                    logger.warning(f"重新向量化失败 doc_id={doc_id}: {reason}")

                processed += len(batch_ids)
                job.processed = processed
                job.succeeded = succeeded_count
                job.failed = failed_count
                # 每批都把"剩余未完成 + 失败项"写回 DB：
                # 进程被强杀/容器崩溃时，仍能用「重试未完成项」从断点继续，
                # 而不必依赖重新扫描（重新扫描虽可行，但代价更高）。
                remaining = [str(i) for i in pending_ids[processed:]]
                job.failed_doc_ids = (failures + remaining)[:5000]
                job.error_message = "；".join(errors[-20:])[:2000] if errors else ""
                # 每批提交一次：进度落库 → 中断后可从"已完成的文档"之后继续
                await session.commit()
                logger.info(
                    f"[revectorize] job={job_id} batch={batch_no + 1}/{total_batches} "
                    f"processed={processed}/{len(pending_ids)} ok={succeeded_count} "
                    f"failed={failed_count}"
                )

            if cancelled:
                # 显式落库，避免残留"running"（worker 内存中的状态可能早于取消请求）
                job.status = "cancelled"
                remaining_ids = [str(i) for i in pending_ids[processed:]]
                job.failed_doc_ids = remaining_ids
                job.failed = len(remaining_ids)
                job.error_message = (
                    f"已取消：已完成 {processed}/{len(pending_ids)}，"
                    f"剩余 {len(remaining_ids)} 篇未处理（可用「重试失败项」继续）"
                )
            else:
                job.status = "succeeded" if job.failed == 0 else "partial"
            job.processed = processed or job.processed
            job.finished_at = _now()
            await session.commit()
            logger.info(
                f"批量重新向量化结束 job_id={job.id} status={job.status} total={job.total} "
                f"succeeded={job.succeeded} failed={job.failed}"
            )
    finally:
        _clear_cancel_flag(job_id)
        await engine.dispose()


async def latest_job(db: AsyncSession) -> RevectorizeJob | None:
    """最近一次任务（供设置页展示进度）。"""
    return (
        await db.scalars(
            select(RevectorizeJob).order_by(RevectorizeJob.created_at.desc())
        )
    ).first()


async def retry_failed(
    db: AsyncSession,
    job_id: uuid.UUID,
    user_id: uuid.UUID | None = None,
) -> RevectorizeJob:
    """重试指定任务的失败项（新建任务，保留历史）。"""
    job = await db.get(RevectorizeJob, job_id)
    if job is None:
        raise AppException(404, "任务不存在")
    if not job.failed_doc_ids:
        raise AppException(400, "该任务没有失败项，无需重试")

    ids: list[uuid.UUID] = []
    for raw in job.failed_doc_ids:
        try:
            ids.append(uuid.UUID(str(raw)))
        except ValueError:
            continue
    if not ids:
        raise AppException(400, "失败项为空或格式非法，无法重试")

    return await create_job(
        db,
        target_model=job.target_model,
        previous_model=job.previous_model,
        kb_id=job.kb_id,
        user_id=user_id,
        doc_ids=ids,
    )


def job_to_dict(job: RevectorizeJob | None) -> dict | None:
    """任务 → 前端展示结构（含进度百分比）。"""
    if job is None:
        return None
    percent = 0
    if job.total:
        percent = int(job.processed * 100 / job.total)
    batch_size = max(1, settings.VECTORIZE_JOB_DOC_BATCH)
    total_batches = (job.total + batch_size - 1) // batch_size if job.total else 0
    current_batch = min(job.processed // batch_size + 1, total_batches) if job.total else 0
    return {
        "job_id": str(job.id),
        "job_type": "document",
        "status": job.status,
        "batch_size": batch_size,
        "total_batches": total_batches,
        "current_batch": current_batch if job.status in ("pending", "running") else total_batches,
        "target_model": job.target_model,
        "previous_model": job.previous_model,
        "kb_id": str(job.kb_id) if job.kb_id else None,
        "total": job.total,
        "processed": job.processed,
        "succeeded": job.succeeded,
        "failed": job.failed,
        "failed_doc_ids": list(job.failed_doc_ids or []),
        # 处理中 = 总数 - 已完成
        "processing": max(job.total - job.processed, 0),
        "percent": percent,
        "error_message": job.error_message,
        "created_at": job.created_at.isoformat() if job.created_at else None,
        "finished_at": job.finished_at.isoformat() if job.finished_at else None,
    }


async def vector_model_summary(db: AsyncSession, config: dict | None) -> dict:
    """设置页展示用的"存量向量模型"概览。"""
    current = key_from_config(config)
    stale_ids = await stale_doc_ids(db, current)
    return {"current_model_key": current, "stale_documents": len(stale_ids)}
