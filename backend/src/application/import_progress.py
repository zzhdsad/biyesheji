"""通用导入进度上报（Redis 共享 + 进程内兜底）。

提交导入是一个同步长请求，前端在请求进行期间轮询该进度；完成后进度键
保留一段时间，且携带最终统计结果，使用户离开页面再回来时仍能恢复结果。

设计约束：
- 进度只用于展示，写失败绝不影响导入主流程；
- 写入按百分比节流，避免大文件每批 500 行都打一次 Redis；
- Redis 不可用时退化为进程内字典（单进程开发环境仍可用）。
"""

from __future__ import annotations

import json
import re
import time
from typing import Any

from loguru import logger

from src.core.config import settings

_KEY_PREFIX = "import:progress:"
# 完成后保留 2 小时：足够用户切换页面/刷新后回来查看结果
_TTL_SECONDS = 7200
# 同一阶段内，百分比至少前进 1 才写；再叠加时间节流
_MIN_INTERVAL_SECONDS = 0.25

_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

_memory: dict[str, dict[str, Any]] = {}
_client: Any = None
_client_unavailable = False


def valid_session_id(session_id: str) -> bool:
    return bool(_SESSION_ID_RE.match(session_id or ""))


def _key(session_id: str) -> str:
    return f"{_KEY_PREFIX}{session_id}"


def _get_client() -> Any | None:
    global _client, _client_unavailable
    if _client is None and not _client_unavailable:
        try:
            import redis.asyncio as aioredis

            _client = aioredis.from_url(
                settings.REDIS_URL, socket_connect_timeout=1, decode_responses=True
            )
        except Exception as exc:  # redis 包缺失/URL 非法：退化为进程内
            _client_unavailable = True
            logger.warning(f"导入进度 Redis 不可用，改用进程内进度：{exc}")
    return _client


class ImportProgress:
    """一次导入任务的进度写入器。"""

    def __init__(self, session_id: str) -> None:
        self.session_id = session_id
        self._last_percent = -1.0
        self._last_phase = ""
        self._last_write = 0.0

    async def update(
        self, *, phase: str, percent: float, message: str, force: bool = False
    ) -> None:
        percent = max(0.0, min(100.0, round(float(percent), 1)))
        now = time.monotonic()
        if not force:
            if phase == self._last_phase and percent - self._last_percent < 1.0:
                return
            if now - self._last_write < _MIN_INTERVAL_SECONDS:
                return
        await self._write(
            {
                "session_id": self.session_id,
                "phase": phase,
                "percent": percent,
                "message": message,
                "done": False,
                "failed": False,
                "updated_at": time.time(),
            }
        )
        self._last_percent = percent
        self._last_phase = phase
        self._last_write = now

    async def finish(self, result: dict[str, Any]) -> None:
        await self._write(
            {
                "session_id": self.session_id,
                "phase": "done",
                "percent": 100.0,
                "message": "导入完成",
                "done": True,
                "failed": False,
                "updated_at": time.time(),
                "result": result,
            }
        )

    async def fail(self, message: str) -> None:
        await self._write(
            {
                "session_id": self.session_id,
                "phase": "failed",
                "percent": 0.0,
                "message": message[:500],
                "done": False,
                "failed": True,
                "updated_at": time.time(),
            }
        )

    async def _write(self, payload: dict[str, Any]) -> None:
        key = _key(self.session_id)
        _memory[key] = payload
        client = _get_client()
        if client is None:
            return
        try:
            await client.set(
                key, json.dumps(payload, ensure_ascii=False, default=str), ex=_TTL_SECONDS
            )
        except Exception as exc:  # 进度写失败不阻断导入
            logger.warning(f"导入进度写入失败（不影响导入）：{exc}")


async def get_progress(session_id: str) -> dict[str, Any] | None:
    """读取进度；Redis 未命中时回退进程内。"""
    if not valid_session_id(session_id):
        return None
    key = _key(session_id)
    client = _get_client()
    if client is not None:
        try:
            raw = await client.get(key)
            if raw:
                return json.loads(raw)
        except Exception as exc:
            logger.warning(f"导入进度读取失败，回退进程内：{exc}")
    return _memory.get(key)


def reset_progress_memory() -> None:
    """清空进程内进度（测试用）。"""
    _memory.clear()
