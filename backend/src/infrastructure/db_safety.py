"""数据库目标安全校验：防止开发 / 测试 / 迁移误连主库执行写操作。

背景（本次重构实际踩到的问题）：
项目存在两处「运行时自动建表」入口——应用 lifespan 的 `init_db()` 与测试的
`_init_db_standalone()`——它们都直接使用 `settings.DATABASE_URL`。当该 URL 指向
主库时，开发热重载与自动化测试会直接写主库；本次重构就因此意外在主库建出了
5 张新表。Schema 现已统一交由 Alembic 管理，这两处都不应再建表。

本模块只做**目标库校验**，不改变连接行为：
- 主库（默认 knowledge_platform，可用 MAIN_DATABASE_NAME 覆盖）视为受保护目标；
- dev / test 环境下对其执行迁移、测试、清理、写入，必须显式设 `DB_ALLOW_MAIN=1`；
- 校验失败直接抛错，错误信息只输出**脱敏**后的目标（不含密码）。
"""

from __future__ import annotations

import os
from functools import lru_cache

from loguru import logger
from sqlalchemy.engine import make_url

DEFAULT_MAIN_DB_NAME = "knowledge_platform"
ENV_ALLOW_MAIN = "DB_ALLOW_MAIN"
ENV_MAIN_DB_NAME = "MAIN_DATABASE_NAME"

_TRUTHY = frozenset({"1", "true", "yes", "on"})


@lru_cache(maxsize=1)
def main_database_name() -> str:
    """受保护的主库名（可用环境变量覆盖，便于换库名部署）。"""
    return (os.getenv(ENV_MAIN_DB_NAME) or DEFAULT_MAIN_DB_NAME).strip() or DEFAULT_MAIN_DB_NAME


def database_name(url: str) -> str:
    """从连接串解析库名；解析失败返回空串（由调用方按「未知」处理）。"""
    try:
        return make_url(url).database or ""
    except Exception:  # noqa: BLE001 - 非法连接串不应导致启动失败
        return ""


def redact(url: str) -> str:
    """脱敏连接串：保留驱动/主机/库名，隐藏账号密码。"""
    try:
        parsed = make_url(url)
        host = parsed.host or ""
        if parsed.port:
            host = f"{host}:{parsed.port}"
        return f"{parsed.drivername}://{parsed.username or ''}:***@{host}/{parsed.database or ''}"
    except Exception:  # noqa: BLE001
        return "<无法解析的连接串>"


def is_main_database(url: str) -> bool:
    """是否指向受保护的主库。"""
    name = database_name(url)
    return bool(name) and name == main_database_name()


def allow_main_enabled() -> bool:
    """是否已通过环境变量显式放行主库操作。"""
    return (os.getenv(ENV_ALLOW_MAIN) or "").strip().lower() in _TRUTHY


def assert_safe_database(url: str, purpose: str, *, allow_main: bool | None = None) -> None:
    """校验目标库是否允许执行指定操作。

    :param purpose: 操作目的（如「Alembic 迁移」「自动化测试」），用于错误信息
    :param allow_main: 显式覆盖放行；None 表示读取 DB_ALLOW_MAIN 环境变量
    :raises RuntimeError: 目标为主库且未显式放行时
    """
    if not is_main_database(url):
        return

    allowed = allow_main_enabled() if allow_main is None else allow_main
    if allowed:
        logger.warning(
            f"[db_safety] 已放行在受保护主库执行「{purpose}」：{redact(url)}"
            f"（{ENV_ALLOW_MAIN} 已开启，请确保这是有意为之）"
        )
        return

    raise RuntimeError(
        f"拒绝在受保护的主库执行「{purpose}」：{redact(url)}。"
        f"请改用隔离数据库（例如新建 kp_dev / kp_test 并执行 alembic upgrade head），"
        f"或在确认无误后设置环境变量 {ENV_ALLOW_MAIN}=1 显式放行。"
    )
