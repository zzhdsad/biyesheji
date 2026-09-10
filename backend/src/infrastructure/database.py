"""异步数据库连接管理（懒加载：未启动 PostgreSQL 不影响应用启动）。"""

from collections.abc import AsyncIterator

from loguru import logger
from sqlalchemy import func, select
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
            pool_size=20,
            max_overflow=30,
            pool_timeout=60,
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
    """开发环境初始化：自动建表 + seed 默认管理员（生产环境请使用 Alembic 迁移）。

    仅当 users 表为空时创建默认管理员，避免覆盖已有数据。
    默认管理员首次登录需强制修改密码。
    """
    from src.domain.models import Base, User
    from src.core.security import hash_password

    engine = get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        logger.info("数据库表结构同步完成（create_all）")

    async with get_session_factory()() as session:
        user_count = await session.scalar(select(func.count()).select_from(User))
        if user_count == 0:
            session.add(
                User(
                    email=settings.DEFAULT_ADMIN_EMAIL,
                    username=settings.DEFAULT_ADMIN_USERNAME,
                    hashed_password=hash_password(settings.DEFAULT_ADMIN_PASSWORD),
                    role="admin",
                    name="系统管理员",
                    department="系统管理部",
                    must_change_password=True,
                )
            )
            await session.commit()
            logger.info(
                f"已创建默认管理员：{settings.DEFAULT_ADMIN_EMAIL} / "
                f"{settings.DEFAULT_ADMIN_USERNAME}（首次登录需修改密码）"
            )
        else:
            logger.info(f"users 表已有 {user_count} 条记录，跳过默认管理员创建")
