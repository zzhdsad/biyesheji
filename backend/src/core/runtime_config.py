"""运行时系统配置统一入口：DB（system_configs 单行表）覆盖 .env 默认值。

背景（与既有规则一致）：
- 六个系统级参数（回收站保留期 / 文件大小限制 / 检索 Top-K / 精排 Top-N /
  相似度拒答阈值 / 多轮历史轮数）原先只由 ``core.config.Settings``
  （即 .env）决定，前端 /settings/system 保存的值只入库、不生效。
- 本模块把这些参数的"取值"收敛到一个入口：**优先 DB，DB 无值回落 .env**。

为什么用"进程内快照"而不是每次查库：
- 消费点里有多处**同步上下文**（``evidence.displayable_hits``、
  ``retrieval_strategies.resolve_retrieval_config``、``rag_service._is_relevant``）
  以及拿不到 DB session 的 ``redis_client``，无法 await；
- 因此采用"异步刷新 + 同步取值"：请求进来时由一个统一的钩子按 TTL 刷新快照，
  业务代码在任何上下文都能 O(1) 同步取值，且不会出现 data race。

刷新时机（三处）：
1. 启动：``main.py`` lifespan 预热；
2. 请求：统一的 HTTP 中间件按 TTL（默认 30s）校验并刷新，覆盖多副本场景；
3. 写入：``PUT /settings/system`` 与 ``POST /settings/system/reset`` 提交后
   调用 :func:`invalidate_system_config` 立即失效。

DB 不可用时：保持上一次快照（首次则回落到 .env 默认值）并告警，不阻断服务。
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from loguru import logger

from src.core.config import settings

# 字段名大写即 Settings 中对应属性名，二者一一对应（唯一定义处）
SYSTEM_CONFIG_FIELDS: tuple[str, ...] = (
    "trash_retention_days",
    "max_file_size_mb",
    "recall_top_k",
    "rerank_top_n",
    "relevance_threshold",
    "history_window",
)

# 快照有效期：兼顾一致性（多副本/外部改库）与开销
CACHE_TTL_SECONDS: float = 30.0

_snapshot: dict[str, Any] | None = None
_snapshot_at: float = 0.0
_from_db: bool = False
_warned: bool = False
_lock: asyncio.Lock | None = None


def _get_lock() -> asyncio.Lock:
    """锁延迟到事件循环中再建（避免跨事件循环绑定）。"""
    global _lock
    if _lock is None:
        _lock = asyncio.Lock()
    return _lock


def system_config_defaults() -> dict[str, Any]:
    """.env 默认值（Settings）—— DB 无值时的回落来源。"""
    return {name: getattr(settings, name.upper()) for name in SYSTEM_CONFIG_FIELDS}


def get_system_config() -> dict[str, Any]:
    """同步读取运行时配置快照（**不访问 DB**，可在同步上下文调用）。

    快照尚未载入时返回 .env 默认值并固化，保证调用方永远拿到完整字典。
    """
    global _snapshot, _snapshot_at
    if _snapshot is None:
        _snapshot = system_config_defaults()
        _snapshot_at = time.monotonic()
    return _snapshot


def get_system_value(name: str) -> Any:
    """读取单个配置项（同步、O(1)）。未知字段回落到 .env 默认值。"""
    cfg = get_system_config()
    if name in cfg:
        return cfg[name]
    return getattr(settings, name.upper())


async def load_system_config(force: bool = False) -> dict[str, Any]:
    """从 DB 载入配置并写入快照。

    Args:
        force: True 时忽略 TTL 强制重查（写入端使缓存失效后调用）。

    Returns:
        生效中的完整配置字典（DB 缺失字段自动回落 .env 默认值）。
    """
    global _snapshot, _snapshot_at, _from_db, _warned

    now = time.monotonic()
    if not force and _from_db and _snapshot is not None and now - _snapshot_at < CACHE_TTL_SECONDS:
        return _snapshot

    async with _get_lock():
        # 双检：并发请求只查一次库
        now = time.monotonic()
        if not force and _from_db and _snapshot is not None and now - _snapshot_at < CACHE_TTL_SECONDS:
            return _snapshot

        values = system_config_defaults()
        try:
            from src.infrastructure.database import get_session_factory

            async with get_session_factory()() as session:
                from src.domain.models import SystemConfig

                row = await session.get(SystemConfig, 1)
                if row is not None:
                    for name in SYSTEM_CONFIG_FIELDS:
                        current = getattr(row, name)
                        if current is not None:
                            values[name] = current
                _from_db = True
        except Exception as exc:
            # DB 不可用：保留上一次快照（首轮则继续用 .env 默认值），不阻断服务
            if not _warned:
                logger.warning(f"运行时系统配置载入失败，沿用当前值: {exc}")
                _warned = True
            if _snapshot is None:
                _snapshot = values
                _snapshot_at = time.monotonic()
            return _snapshot

        if _snapshot is None:
            logger.info(f"运行时系统配置已载入：{values}")
        _snapshot = values
        _snapshot_at = time.monotonic()
        _warned = False
        return _snapshot


async def refresh_if_stale() -> None:
    """TTL 到期时刷新快照（供 HTTP 中间件 / 后台任务在 async 上下文调用）。"""
    if _from_db and _snapshot is not None and time.monotonic() - _snapshot_at < CACHE_TTL_SECONDS:
        return
    await load_system_config()


def invalidate_system_config() -> None:
    """使快照失效（配置写入后立即调用），下次读取重新查库。"""
    global _snapshot_at, _from_db
    _snapshot_at = 0.0
    _from_db = False


def reset_system_config_snapshot(values: dict[str, Any] | None = None) -> None:
    """测试用：直接写入快照并视为已生效（避免依赖 DB）。"""
    global _snapshot, _snapshot_at, _from_db, _warned
    _snapshot = dict(values) if values else system_config_defaults()
    _snapshot_at = time.monotonic()
    _from_db = True
    _warned = False
