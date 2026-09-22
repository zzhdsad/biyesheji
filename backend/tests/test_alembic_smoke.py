"""Alembic 迁移体系冒烟测试（TASK-001）。

数据安全约定：
- 配置 / metadata 测试纯离线，不连接数据库。
- 迁移往返测试使用一次性临时库 knowledge_platform_alembic_smoke，
  测试内自建自删（DROP ... WITH (FORCE)），绝不触碰开发库 knowledge_platform。
"""

import asyncio
from pathlib import Path

import pytest
from sqlalchemy.engine import make_url

from src.core.config import settings

BACKEND_DIR = Path(__file__).resolve().parents[1]
ALEMBIC_INI = BACKEND_DIR / "alembic.ini"

EXPECTED_TABLES = {
    "users",
    "knowledge_bases",
    "kb_members",
    "documents",
    "chunks",
    "conversations",
    "messages",
    "test_cases",
    "evaluation_results",
    "feedbacks",
    "model_configs",
    "audit_logs",
    "system_configs",
    # TASK-002
    "categories",
    "tags",
    # TASK-003
    "herbs",
    "herb_tags",
    # TASK-004
    "prescriptions",
    "prescription_ingredients",
    "prescription_tags",
    # TASK-005
    "theories",
    "theory_tags",
    # TASK-006
    "literatures",
    "literature_tags",
    # TASK-008
    "knowledge_base_resources",
    # TASK-009
    "evaluation_runs",
}

SMOKE_DB_NAME = "knowledge_platform_alembic_smoke"


def _admin_conn_kwargs() -> dict:
    """从应用 DATABASE_URL 解析连接参数，管理库固定为 postgres。"""
    url = make_url(settings.DATABASE_URL)
    return {
        "host": url.host or "localhost",
        "port": url.port or 5432,
        "user": url.username,
        "password": url.password,
        "database": "postgres",
    }


def _pg_available() -> bool:
    try:
        import asyncpg

        async def _probe() -> None:
            kwargs = _admin_conn_kwargs()
            kwargs["timeout"] = 3
            conn = await asyncpg.connect(**kwargs)
            await conn.close()

        asyncio.run(_probe())
        return True
    except Exception:
        return False


def _make_config():
    """构建指向 backend/alembic 的 Alembic Config（URL 由 env.py 读 settings）。"""
    from alembic.config import Config

    cfg = Config(str(ALEMBIC_INI))
    cfg.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    return cfg


def _list_public_tables(db_url: str) -> set[str]:
    """列出指定数据库 public schema 的全部表名。"""
    import asyncpg

    url = make_url(db_url)

    async def _run() -> set[str]:
        conn = await asyncpg.connect(
            host=url.host,
            port=url.port,
            user=url.username,
            password=url.password,
            database=url.database,
        )
        rows = await conn.fetch(
            "select tablename from pg_tables where schemaname = 'public'"
        )
        await conn.close()
        return {r["tablename"] for r in rows}

    return asyncio.run(_run())


def _recreate_smoke_db() -> None:
    import asyncpg

    async def _run() -> None:
        conn = await asyncpg.connect(**_admin_conn_kwargs())
        await conn.execute(f"DROP DATABASE IF EXISTS {SMOKE_DB_NAME} WITH (FORCE)")
        await conn.execute(f"CREATE DATABASE {SMOKE_DB_NAME}")
        await conn.close()

    asyncio.run(_run())


def _drop_smoke_db() -> None:
    import asyncpg

    async def _run() -> None:
        conn = await asyncpg.connect(**_admin_conn_kwargs())
        await conn.execute(f"DROP DATABASE IF EXISTS {SMOKE_DB_NAME} WITH (FORCE)")
        await conn.close()

    asyncio.run(_run())


# ── 1. 配置可加载 ────────────────────────────────────────────────────────────


def test_alembic_config_loads() -> None:
    from alembic.script import ScriptDirectory

    assert ALEMBIC_INI.exists(), "backend/alembic.ini 不存在"

    cfg = _make_config()
    script = ScriptDirectory.from_config(cfg)
    heads = script.get_heads()
    assert len(heads) == 1, f"应只有单一 head，实际：{heads}"

    # 迁移链可能已有多版（TASK-002 后 head 不再是 baseline）；
    # 改为遍历全部 revision，断言恰好一个 down_revision 为 None 的根迁移。
    roots = [
        rev for rev in script.walk_revisions() if rev.down_revision is None
    ]
    assert len(roots) == 1, f"应恰好一个 baseline 迁移，实际：{[r.revision for r in roots]}"


# ── 2. metadata 可正确加载 ───────────────────────────────────────────────────


def test_metadata_tables_complete() -> None:
    from src.domain.models import Base

    assert set(Base.metadata.tables.keys()) == EXPECTED_TABLES


# ── 3/4. 迁移可执行 upgrade / downgrade（独立临时库，不影响开发库） ──────────


@pytest.mark.skipif(not _pg_available(), reason="PostgreSQL 不可用，跳过迁移往返测试")
def test_upgrade_downgrade_roundtrip(monkeypatch) -> None:
    from alembic import command
    from alembic.script import ScriptDirectory

    _recreate_smoke_db()

    dev_url = make_url(settings.DATABASE_URL)
    # 注意：str(URL) 会把密码掩码为 ***，必须用 render_as_string(hide_password=False)
    smoke_url = dev_url.set(database=SMOKE_DB_NAME).render_as_string(hide_password=False)
    # env.py 的 _database_url() 优先读 settings.DATABASE_URL，
    # monkeypatch 后迁移将作用于临时库而非开发库
    monkeypatch.setattr(settings, "DATABASE_URL", smoke_url)

    cfg = _make_config()
    head_id = ScriptDirectory.from_config(cfg).get_heads()[0]

    try:
        # upgrade head → 15 业务表 + alembic_version
        command.upgrade(cfg, "head")
        tables = _list_public_tables(smoke_url)
        assert tables == EXPECTED_TABLES | {"alembic_version"}

        # 版本号写入正确
        import asyncpg

        async def _version() -> str:
            url = make_url(smoke_url)
            conn = await asyncpg.connect(
                host=url.host,
                port=url.port,
                user=url.username,
                password=url.password,
                database=url.database,
            )
            v = await conn.fetchval("select version_num from alembic_version")
            await conn.close()
            return v

        assert asyncio.run(_version()) == head_id

        # downgrade base → 仅剩 alembic_version
        command.downgrade(cfg, "base")
        tables = _list_public_tables(smoke_url)
        assert tables == {"alembic_version"}

        # 再次 upgrade 验证可重复执行
        command.upgrade(cfg, "head")
        tables = _list_public_tables(smoke_url)
        assert tables == EXPECTED_TABLES | {"alembic_version"}
    finally:
        # 清理临时库，不影响开发库
        _drop_smoke_db()
