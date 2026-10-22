"""Alembic 迁移环境：asyncpg 异步模式。

- 数据库 URL 统一读取 src.core.config.settings.DATABASE_URL（.env 单一来源），
  不在 alembic.ini 中硬编码账号密码。
- target_metadata 使用项目现有 Base.metadata（src/domain/models.py），
  与业务 ORM 保持单一来源。
"""

import asyncio
import sys
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

# 保证无论从哪个目录调用 alembic，都能导入 backend/src 下的包
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.core.config import settings  # noqa: E402
from src.domain.models import Base  # noqa: E402

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# 目标 metadata：与业务 ORM 一致（TASK-001 要求）
target_metadata = Base.metadata


def _database_url() -> str:
    """优先使用应用配置（.env 注入）；为空时回退 alembic.ini 的 sqlalchemy.url。"""
    return settings.DATABASE_URL or config.get_main_option("sqlalchemy.url")


def run_migrations_offline() -> None:
    """离线模式：仅生成 SQL 脚本，不连接数据库。"""
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        # autogenerate 时对比类型与 server_default 变化，减少漏检
        compare_type=True,
        compare_server_default=True,
    )

    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
        compare_server_default=True,
    )

    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    """异步模式：asyncpg 引擎执行迁移，与业务运行时同驱动，零额外依赖。"""
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        url=_database_url(),
        poolclass=pool.NullPool,
    )

    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)

    await connectable.dispose()


def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
