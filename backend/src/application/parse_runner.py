"""解析执行器：在新事件循环中运行 ParseService。

供两类同步调用方复用（它们都没有运行中的事件循环，且 asyncpg 连接绑定循环）：
- FastAPI BackgroundTasks（PARSE_BACKEND=background，在线程池执行）
- Celery worker 任务（PARSE_BACKEND=celery）

每次执行创建独立 engine 并在结束后 dispose，避免跨循环复用连接。
"""

import asyncio
import uuid

from loguru import logger
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from src.core.config import settings


def run_parse(doc_id: str) -> dict:
    """执行解析，返回 {id, parse_status, chunk_count}。异常向上抛出（调用方决定重试策略）。"""

    async def _run() -> dict:
        from src.application.parse_service import ParseService

        engine = create_async_engine(settings.DATABASE_URL, pool_pre_ping=True)
        try:
            async with async_sessionmaker(engine, expire_on_commit=False)() as session:
                service = ParseService(session)
                doc = await service.run(uuid.UUID(doc_id))
                return {
                    "id": str(doc.id),
                    "parse_status": doc.parse_status,
                    "chunk_count": doc.chunk_count,
                }
        finally:
            await engine.dispose()

    logger.info(f"开始解析文档 doc_id={doc_id}")
    return asyncio.run(_run())
