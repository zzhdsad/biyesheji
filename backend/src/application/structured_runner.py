"""结构化数据解析执行器：在新事件循环中运行 StructuredParseService。

与 parse_runner 同样的形态（自建 engine + asyncio.run + dispose），因为
FastAPI BackgroundTasks 在线程池里执行，没有可复用的事件循环，而 asyncpg
连接绑定创建它的循环。

解析结果落库后由 StructuredParseService 调用向量同步（复用第二阶段 Embedding +
Milvus）；本执行器自身不直接操作向量库，向量化失败不影响解析结果。
"""

import asyncio
import itertools
import queue
import threading
import time
import uuid
from datetime import datetime, timezone

from loguru import logger
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from src.core.config import settings


def run_structured_parse(doc_id: str) -> dict:
    """执行结构化解析，返回 {id, status, record_count}。异常向上抛出由调用方记录。"""

    async def _run() -> dict:
        from src.application.structured_parse_service import StructuredParseService

        engine = create_async_engine(settings.DATABASE_URL, pool_pre_ping=True)
        try:
            async with async_sessionmaker(engine, expire_on_commit=False)() as session:
                row = await StructuredParseService(session).run(uuid.UUID(doc_id))
                return {
                    "id": str(row.doc_id),
                    "status": row.status,
                    "record_count": row.record_count,
                }
        finally:
            await engine.dispose()

    logger.info(f"开始解析结构化数据文件 doc_id={doc_id}")
    return asyncio.run(_run())


def run_structured_parse_safely(doc_id: str) -> None:
    """BackgroundTasks 入口：失败已由服务回写 failed 状态与原因，此处只记日志。

    后台任务异常不能冒泡（会打断响应收尾），也不影响已有解析结果。
    """
    try:
        run_structured_parse(doc_id)
    except Exception:  # noqa: BLE001
        logger.exception(f"后台结构化解析任务执行失败 doc_id={doc_id}")


# ── 第六阶段：手动重新向量化（结构化记录 / 关联记录）─────────────────────────
# 只重建向量，不重新解析文件、不重新生成关联记录；写入走 replace_doc（先写新
# 再清旧），失败时旧向量仍在，状态如实落 failed，可再次重试。
#
# 并发约束：批量上传会让多个文件的解析几乎同时成功，若每个解析都直接跑整份
# Embedding，多路 BGE-M3 会把 CPU 占满，事件循环拿不到时间片（健康检查超时）。
# 所有结构化向量化任务（文件 / 文件组）统一进入**单消费者优先队列**：
# 一次只跑一个，其余保持 pending，前一个结束（成功或失败）后才开始下一个。
#
# 优先级：用户新触发（上传解析完成 / 手动重新向量化 / 组确认）为 0，先执行；
# 进程启动时续跑的存量 pending 为 1，靠后执行。否则新上传会被排在十几小时的
# 历史积压之后，"继续上传也检索不到"的问题依然存在。同优先级内按入队顺序执行。


def _run_vectorize(kind: str, target_id: str) -> int:
    async def _run() -> int:
        from src.application.structured_vector_service import StructuredVectorService

        engine = create_async_engine(settings.DATABASE_URL, pool_pre_ping=True)
        try:
            async with async_sessionmaker(engine, expire_on_commit=False)() as session:
                service = StructuredVectorService(session)
                should_cancel = lambda: is_structured_vector_job_cancelled(  # noqa: E731
                    kind, target_id
                )
                if kind == "group":
                    return await service.vectorize_group(
                        uuid.UUID(target_id), should_cancel
                    )
                return await service.vectorize_document(
                    uuid.UUID(target_id), should_cancel
                )
        finally:
            await engine.dispose()

    return asyncio.run(_run())


_PRIORITY_USER = 0  # 用户新触发：优先
_PRIORITY_RESUME = 1  # 启动续跑的存量任务：靠后
# 元素：(优先级, 入队序号, (kind, target_id))；序号保证同优先级内 FIFO
_JOB_QUEUE: "queue.PriorityQueue[tuple[int, int, tuple[str, str]]]" = queue.PriorityQueue()
_JOB_SEQ = itertools.count()
_JOBS_LOCK = threading.Lock()
# 已入队或正在执行的任务键；同一键不重复派发
_JOBS_ACTIVE: set[tuple[str, str]] = set()
# 正在执行（已出队）的键：执行期间数据又变化时，结束后必须用最新记录补跑一次
_JOBS_RUNNING: set[tuple[str, str]] = set()
_JOBS_RERUN: set[tuple[str, str]] = set()
# 用户已请求停止的键：排队中的任务直接跳过，执行中的由服务层在批次之间退出
_CANCELLED: set[tuple[str, str]] = set()
_WORKER_LOCK = threading.Lock()
_WORKER: threading.Thread | None = None


def _worker_loop() -> None:
    while True:
        _, _, (kind, target_id) = _JOB_QUEUE.get()
        key = (kind, target_id)
        with _JOBS_LOCK:
            # 入队后又被用户停止：不再执行（真正的 pending 状态已由路由回写）
            skip = key not in _JOBS_ACTIVE
            if skip:
                _CANCELLED.discard(key)
                _JOBS_RERUN.discard(key)
            else:
                _JOBS_RUNNING.add(key)
        if skip:
            _JOB_QUEUE.task_done()
            continue
        try:
            count = _run_vectorize(kind, target_id)
            logger.info(f"结构化向量化完成 kind={kind} id={target_id} vectors={count}")
        except Exception:  # noqa: BLE001 - 单个任务失败不能拖垮队列
            logger.exception(f"结构化向量化任务执行失败 kind={kind} id={target_id}")
        finally:
            with _JOBS_LOCK:
                _JOBS_RUNNING.discard(key)
                _JOBS_ACTIVE.discard(key)
                _CANCELLED.discard(key)
                rerun = key in _JOBS_RERUN
                _JOBS_RERUN.discard(key)
            _JOB_QUEUE.task_done()
            if rerun:
                _enqueue(kind, target_id)


def _ensure_worker() -> None:
    global _WORKER
    with _WORKER_LOCK:
        if _WORKER is None or not _WORKER.is_alive():
            _WORKER = threading.Thread(
                target=_worker_loop, name="structured-vectorize", daemon=True
            )
            _WORKER.start()


def _enqueue(kind: str, target_id: str, *, priority: int = _PRIORITY_USER) -> bool:
    key = (kind, target_id)
    with _JOBS_LOCK:
        if key in _JOBS_RUNNING:
            # 已出队执行中：执行期读到的可能是旧记录，结束后补跑一次
            _JOBS_RERUN.add(key)
            logger.info(
                f"结构化向量化执行中，标记为完成后重跑 kind={kind} id={target_id}"
            )
            return False
        if key in _JOBS_ACTIVE:
            # 仍在队列中：任务开始时会重新读记录，无需重复执行
            logger.info(f"结构化向量化已在队列中，忽略重复派发 kind={kind} id={target_id}")
            return False
        _JOBS_ACTIVE.add(key)
    _JOB_QUEUE.put((priority, next(_JOB_SEQ), key))
    _ensure_worker()
    return True


def is_structured_vector_job_active(kind: str, target_id: str) -> bool:
    """该文件 / 文件组是否已在等待队列或正在执行（路由防重复派发用）。"""
    with _JOBS_LOCK:
        return (kind, target_id) in _JOBS_ACTIVE


def is_structured_vector_job_cancelled(kind: str, target_id: str) -> bool:
    """服务层批次之间查询：用户是否已请求停止该任务。"""
    with _JOBS_LOCK:
        return (kind, target_id) in _CANCELLED


def cancel_structured_job(kind: str, target_id: str) -> str:
    """请求停止向量化任务。

    返回：
    - ``queued``：还在等待队列中，已移出执行集合（路由需把状态回写 pending）
    - ``running``：正在编码，已设置停止标记，服务层会在下一批之间退出并清理
    - ``idle``：没有等待或执行中的任务
    """
    key = (kind, target_id)
    with _JOBS_LOCK:
        if key in _JOBS_RUNNING:
            _CANCELLED.add(key)
            return "running"
        if key in _JOBS_ACTIVE:
            _JOBS_ACTIVE.discard(key)
            return "queued"
        return "idle"


def enqueue_structured_document(doc_id: str, *, priority: int = _PRIORITY_USER) -> bool:
    return _enqueue("document", doc_id, priority=priority)


def enqueue_structured_group(group_id: str, *, priority: int = _PRIORITY_USER) -> bool:
    return _enqueue("group", group_id, priority=priority)


def run_structured_vectorize_safely(doc_id: str) -> None:
    """BackgroundTasks 入口：只入队，Embedding 由单消费者线程顺序执行。"""
    enqueue_structured_document(doc_id)


def run_group_vectorize_safely(group_id: str) -> None:
    """BackgroundTasks 入口：只入队，Embedding 由单消费者线程顺序执行。"""
    enqueue_structured_group(group_id)


# ── 进程重启后的遗留状态修复与 pending 续跑 ─────────────────────────────────
# 队列是进程内内存队列，进程退出即丢失。启动时在线程里做两件事：
# 1. 修复遗留 indexing：先检查 Milvus 是否已有完整向量，数量一致 → completed，
#    否则 → pending（不重复写入，旧向量不会被提前清空）；
# 2. 重新派发 pending：解析成功但尚未完成向量化的文件 / 已确认但未向量化的
#    文件组重新入队，由单消费者 FIFO 串行执行。没有这一步，重启期间丢失的
#    任务会永远停在 pending，表现为「结构化数据导入成功但问答检索不到」。


async def _recover_stale() -> None:
    from src.application.structured_vector_service import (
        structured_doc_id,
        structured_group_doc_id,
    )
    from src.domain.models import (
        STRUCTURED_GROUP_CONFIRMED,
        VECTOR_STATUS_COMPLETED,
        VECTOR_STATUS_INDEXING,
        VECTOR_STATUS_PENDING,
        StructuredGroup,
        StructuredGroupRecord,
        StructuredParse,
        StructuredRecord,
    )
    from src.infrastructure.milvus_store import get_vector_store

    now = datetime.now(timezone.utc)
    store = get_vector_store()
    engine = create_async_engine(settings.DATABASE_URL, pool_pre_ping=True)
    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as session:
            fixed = 0
            parses = (
                await session.scalars(
                    select(StructuredParse).where(
                        StructuredParse.vector_status == VECTOR_STATUS_INDEXING
                    )
                )
            ).all()
            for row in parses:
                expected = int(
                    await session.scalar(
                        select(func.count())
                        .select_from(StructuredRecord)
                        .where(
                            StructuredRecord.doc_id == row.doc_id,
                            func.length(func.btrim(StructuredRecord.knowledge_text)) > 0,
                        )
                    )
                    or 0
                )
                try:
                    existing = await asyncio.to_thread(
                        store.count_by_doc, structured_doc_id(row.doc_id)
                    )
                except Exception as exc:  # noqa: BLE001 - Milvus 不可用时按 pending 处理
                    logger.warning(f"遗留向量检查失败 doc_id={row.doc_id}: {exc}")
                    existing = 0
                values: dict = {
                    "vector_status": VECTOR_STATUS_PENDING,
                    "vector_count": 0,
                    "vector_error": "",
                }
                if expected > 0 and existing == expected:
                    values.update(
                        vector_status=VECTOR_STATUS_COMPLETED,
                        vector_count=existing,
                        vectorized_at=now,
                    )
                await session.execute(
                    update(StructuredParse)
                    .where(StructuredParse.doc_id == row.doc_id)
                    .values(**values)
                )
                fixed += 1
                logger.info(
                    f"遗留结构化向量状态修复 doc_id={row.doc_id} expected={expected} "
                    f"existing={existing} -> {values['vector_status']}"
                )

            groups = (
                await session.scalars(
                    select(StructuredGroup).where(
                        StructuredGroup.vector_status == VECTOR_STATUS_INDEXING
                    )
                )
            ).all()
            for group in groups:
                expected = 0
                if group.status == STRUCTURED_GROUP_CONFIRMED:
                    expected = int(
                        await session.scalar(
                            select(func.count())
                            .select_from(StructuredGroupRecord)
                            .where(
                                StructuredGroupRecord.group_id == group.id,
                                func.length(func.btrim(StructuredGroupRecord.knowledge_text)) > 0,
                            )
                        )
                        or 0
                    )
                try:
                    existing = await asyncio.to_thread(
                        store.count_by_doc, structured_group_doc_id(group.id)
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning(f"遗留向量检查失败 group_id={group.id}: {exc}")
                    existing = 0
                values = {
                    "vector_status": VECTOR_STATUS_PENDING,
                    "vector_count": 0,
                    "vector_error": "",
                }
                if expected > 0 and existing == expected:
                    values.update(
                        vector_status=VECTOR_STATUS_COMPLETED,
                        vector_count=existing,
                        vectorized_at=now,
                    )
                await session.execute(
                    update(StructuredGroup)
                    .where(StructuredGroup.id == group.id)
                    .values(**values)
                )
                fixed += 1
                logger.info(
                    f"遗留关联向量状态修复 group_id={group.id} expected={expected} "
                    f"existing={existing} -> {values['vector_status']}"
                )

            if fixed:
                await session.commit()
                logger.info(f"结构化向量遗留状态修复完成：{fixed} 条")
    finally:
        await engine.dispose()


async def _resume_pending() -> None:
    """重新派发 pending 的结构化向量化任务。

    向量化队列是进程内内存队列，重启后未执行的任务会永久停在 pending；
    这正是「解析成功但问答检索不到资料」的直接原因。启动时把仍然有效、
    解析成功 / 组已确认且确有记录的 pending 任务重新入队：

    - 文件：join documents 过滤回收站文件与 ``space != structured`` 的行，
      避免把已删除或挪出结构化空间的文件重新向量化；
    - 文件组：只有 ``status == confirmed`` 才允许存在向量（与同步服务约束一致），
      未确认 / stale 的组不会入队。

    没有记录（record_count == 0）的解析结果也不入队，避免空任务占用队列。
    已入队 / 正在执行的键由 ``_enqueue`` 去重，本函数可安全重复调用。
    """
    from src.core.file_spaces import STRUCTURED_SPACE
    from src.domain.models import (
        STRUCTURED_GROUP_CONFIRMED,
        STRUCTURED_PARSE_SUCCESS,
        VECTOR_STATUS_PENDING,
        Document,
        StructuredGroup,
        StructuredParse,
    )

    engine = create_async_engine(settings.DATABASE_URL, pool_pre_ping=True)
    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as session:
            doc_ids = (
                await session.scalars(
                    select(StructuredParse.doc_id)
                    .join(Document, Document.id == StructuredParse.doc_id)
                    .where(
                        StructuredParse.vector_status == VECTOR_STATUS_PENDING,
                        StructuredParse.status == STRUCTURED_PARSE_SUCCESS,
                        StructuredParse.record_count > 0,
                        Document.deleted_at.is_(None),
                        Document.space == STRUCTURED_SPACE,
                    )
                    .order_by(StructuredParse.updated_at.asc())
                )
            ).all()
            group_ids = (
                await session.scalars(
                    select(StructuredGroup.id)
                    .where(
                        StructuredGroup.vector_status == VECTOR_STATUS_PENDING,
                        StructuredGroup.status == STRUCTURED_GROUP_CONFIRMED,
                        StructuredGroup.record_count > 0,
                    )
                    .order_by(StructuredGroup.updated_at.asc())
                )
            ).all()
    finally:
        await engine.dispose()

    dispatched = 0
    # 存量续跑用低优先级：新上传 / 手动重新向量化可以插到前面，不必等完整积压
    for doc_id in doc_ids:
        if enqueue_structured_document(str(doc_id), priority=_PRIORITY_RESUME):
            dispatched += 1
    for group_id in group_ids:
        if enqueue_structured_group(str(group_id), priority=_PRIORITY_RESUME):
            dispatched += 1

    if doc_ids or group_ids:
        logger.info(
            f"pending 结构化向量化任务续跑：文件 {len(doc_ids)} 个 / "
            f"文件组 {len(group_ids)} 个，本次入队 {dispatched} 个（单消费者串行执行）"
        )
    else:
        logger.info("无 pending 结构化向量化任务需要续跑")


async def _recover_and_resume(*, resume_pending: bool) -> None:
    """先修复遗留 indexing；按开关决定是否重新派发 pending。"""
    await _recover_stale()
    if resume_pending:
        await _resume_pending()


def recover_stale_vector_jobs(*, resume_pending: bool = False) -> None:
    """启动入口：修复遗留 indexing。

    默认**不**自动续跑 pending：向量化改为用户手动触发（页面「开始向量化」按钮），
    避免一次上传/重启就把几十万条记录排进队列长时间占用 CPU。
    """
    for attempt in range(3):
        try:
            asyncio.run(_recover_and_resume(resume_pending=resume_pending))
            return
        except Exception:  # noqa: BLE001 - 启动恢复失败不能影响服务启动
            logger.exception(f"结构化向量遗留状态修复 / pending 续跑失败（第 {attempt + 1} 次）")
            if attempt < 2:
                time.sleep(3)
