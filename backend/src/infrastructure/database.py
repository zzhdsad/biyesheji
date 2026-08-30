"""异步数据库连接管理（懒加载：未启动 PostgreSQL 不影响应用启动）。"""

from collections.abc import AsyncIterator

from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from src.core.config import settings

_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def get_engine() -> AsyncEngine:
    global _engine
    if _engine is None:
        _engine = create_async_engine(
            settings.DATABASE_URL,
            pool_pre_ping=True,
            pool_size=5,
            max_overflow=10,
        )
    return _engine


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    global _session_factory
    if _session_factory is None:
        _session_factory = async_sessionmaker(get_engine(), expire_on_commit=False)
    return _session_factory


async def get_db() -> AsyncIterator[AsyncSession]:
    """FastAPI 依赖：提供数据库会话。"""
    async with get_session_factory()() as session:
        yield session


async def init_db() -> None:
    """开发环境初始化：自动建表 + seed 默认管理员（生产环境请使用 Alembic 迁移）。"""
    from src.domain.models import Base, User

    engine = get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        logger.info("数据库表结构同步完成（create_all）")

    async with get_session_factory()() as session:
        exists = await session.scalar(
            select(User).where(User.email == settings.DEFAULT_ADMIN_EMAIL)
        )
        if exists is None:
            session.add(
                User(
                    email=settings.DEFAULT_ADMIN_EMAIL,
                    username="admin",
                    hashed_password="not-set-yet",  # TODO: 接入注册/JWT 后替换为真实哈希
                    role="admin",
                )
            )
            await session.commit()
            logger.info(f"已创建默认管理员：{settings.DEFAULT_ADMIN_EMAIL}")
