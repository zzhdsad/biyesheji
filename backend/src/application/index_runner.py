"""向量化执行器：在新事件循环中运行 IndexingService（与 parse_runner 同模式）。

供两类同步调用方复用（它们都没有运行中的事件循环，且 asyncpg 连接绑定循环）：
- FastAPI BackgroundTasks（PARSE_BACKEND=background）
- Celery worker 任务（PARSE_BACKEND=celery）
"""

import asyncio
import threading
import uuid

from loguru import logger
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from src.core.config import settings

# 全局并发上限：BackgroundTasks 无内置限流，批量 reindex（几十个请求）
# 会同时各起一个 asyncio.run + 独立 DB 引擎 + 并发 encode，直接压垮服务。
_VECTORIZE_SEM = threading.Semaphore(2)


def run_vectorize(doc_id: str) -> dict:
    """执行向量化，返回 {id, parse_status, vector_count}。异常向上抛出（调用方决定重试策略）。"""

    async def _run() -> dict:
        from src.application.indexing_service import IndexingService

        engine = create_async_engine(settings.DATABASE_URL, pool_pre_ping=True)
        try:
            async with async_sessionmaker(engine, expire_on_commit=False)() as session:
                service = IndexingService(session)
                doc = await service.run(uuid.UUID(doc_id))
                return {
                    "id": str(doc.id),
                    "parse_status": doc.parse_status,
                    "vector_count": doc.chunk_count,
                }
        finally:
            await engine.dispose()

    logger.info(f"开始向量化文档 doc_id={doc_id}")
    with _VECTORIZE_SEM:
        return asyncio.run(_run())
