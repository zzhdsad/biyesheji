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
    """开发环境初始化：seed 默认管理员（**不建表**，Schema 统一由 Alembic 管理）。

    变更说明：原先此处会执行 Base.metadata.create_all，导致开发热重载/测试直接
    按 ORM 元数据改写数据库结构（本次重构中曾因此意外在主库建出 5 张新表）。
    现在只做「Schema 是否就绪」的校验，缺表时提示执行 alembic upgrade head，
    不再自动建表；同时校验目标库，避免误连主库写入。

    仅当 users 表为空时创建默认管理员，避免覆盖已有数据。
    默认管理员首次登录需强制修改密码。
    """
    from sqlalchemy import inspect

    from src.core.security import hash_password
    from src.domain.models import User
    from src.infrastructure.db_safety import assert_safe_database

    assert_safe_database(settings.DATABASE_URL, purpose="应用启动初始化")

    engine = get_engine()

    def _table_exists(sync_conn, name: str) -> bool:
        return inspect(sync_conn).has_table(name)

    async with engine.begin() as conn:
        if not await conn.run_sync(_table_exists, "users"):
            logger.error(
                "users 表不存在：Schema 只能由 Alembic 管理，请先执行 "
                "`alembic upgrade head`（当前目标库：{}）",
                engine.url.database,
            )
            return
        logger.info("数据库 Schema 校验通过（由 Alembic 管理，不执行 create_all）")

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
