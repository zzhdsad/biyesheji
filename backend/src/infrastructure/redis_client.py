"""对话历史缓存抽象（依赖倒置）。

TECH_DESIGN Redis 键结构：
    conversation:{conv_id}:history → 最近 N 轮消息（24h TTL）

缓存定位：
- Redis 为读加速层，PostgreSQL 仍为唯一事实源。
- 读：先查 Redis；未命中回源 PG 并回填缓存。
- 写：持久化 PG 后同步追加到 Redis（失败仅告警，不阻断主流程）。

数据结构：Redis List，每个元素为 JSON({"role","content"})，按时间升序。
裁剪策略：每次追加后 LTRIM 保留最近 2*HISTORY_WINDOW 条（= 最近 N 轮）。
"""

import json
import uuid
from abc import ABC, abstractmethod
from collections import deque
from dataclasses import dataclass

from loguru import logger

from src.core.config import settings

_CACHE_KEY = "conversation:{conv_id}:history"


def _key(conv_id: uuid.UUID | str) -> str:
    return _CACHE_KEY.format(conv_id=conv_id)


@dataclass
class HistoryEntry:
    """历史消息（OpenAI messages 格式子集）。"""

    role: str  # user / assistant
    content: str

    def as_dict(self) -> dict:
        return {"role": self.role, "content": self.content}


class BaseConversationCache(ABC):
    """对话历史缓存接口。"""

    @abstractmethod
    async def get_history(self, conv_id: uuid.UUID | str) -> list[dict]:
        """读取历史消息，按时间升序返回；缓存未命中返回空列表。"""

    @abstractmethod
    async def append_message(
        self, conv_id: uuid.UUID | str, role: str, content: str
    ) -> None:
        """追加一条消息并裁剪至最近 N 轮、刷新 TTL。"""

    @abstractmethod
    async def warm(
        self, conv_id: uuid.UUID | str, messages: list[dict]
    ) -> None:
        """回源后回填缓存（覆盖式写入）。"""

    @abstractmethod
    async def delete(self, conv_id: uuid.UUID | str) -> None:
        """清除指定会话缓存（会话删除时调用）。"""


class RedisConversationCache(BaseConversationCache):
    """Redis List 实现：key=conversation:{conv_id}:history，24h TTL。"""

    def __init__(self) -> None:
        import redis.asyncio as aioredis  # 延迟导入，无 Redis 环境不阻断启动

        self._client = aioredis.from_url(
            settings.REDIS_URL, socket_connect_timeout=2, decode_responses=True
        )

    async def get_history(self, conv_id: uuid.UUID | str) -> list[dict]:
        try:
            raw = await self._client.lrange(_key(conv_id), 0, -1)
        except Exception as exc:  # Redis 不可用：回源兜底
            logger.warning(f"会话缓存读失败 conv={conv_id}：{exc}")
            return []
        history: list[dict] = []
        for item in raw:
            try:
                history.append(json.loads(item))
            except (json.JSONDecodeError, TypeError):
                continue
        return history

    async def append_message(
        self, conv_id: uuid.UUID | str, role: str, content: str
    ) -> None:
        key = _key(conv_id)
        keep = max(settings.HISTORY_WINDOW * 2, 0)
        try:
            pipe = self._client.pipeline()
            pipe.rpush(key, json.dumps({"role": role, "content": content}, ensure_ascii=False))
            if keep > 0:
                pipe.ltrim(key, -keep, -1)  # 保留最近 N 轮（2*WINDOW 条）
            pipe.expire(key, settings.HISTORY_TTL_SECONDS)
            await pipe.execute()
        except Exception as exc:  # 缓存写失败不阻断主流程
            logger.warning(f"会话缓存写失败 conv={conv_id}：{exc}")

    async def warm(
        self, conv_id: uuid.UUID | str, messages: list[dict]
    ) -> None:
        if not messages:  # 空历史不回填，避免每轮新会话重复回源
            return
        key = _key(conv_id)
        keep = max(settings.HISTORY_WINDOW * 2, 0)
        try:
            pipe = self._client.pipeline()
            pipe.delete(key)
            pipe.rpush(
                key,
                *[json.dumps(m, ensure_ascii=False) for m in messages],
            )
            if keep > 0:
                pipe.ltrim(key, -keep, -1)
            pipe.expire(key, settings.HISTORY_TTL_SECONDS)
            await pipe.execute()
        except Exception as exc:
            logger.warning(f"会话缓存回填失败 conv={conv_id}：{exc}")

    async def delete(self, conv_id: uuid.UUID | str) -> None:
        try:
            await self._client.delete(_key(conv_id))
        except Exception as exc:
            logger.warning(f"会话缓存删除失败 conv={conv_id}：{exc}")


class InMemoryConversationCache(BaseConversationCache):
    """内存实现（测试/无 Redis 环境）：与 Redis 行为等价。"""

    def __init__(self) -> None:
        self._store: dict[str, deque[dict]] = {}

    async def get_history(self, conv_id: uuid.UUID | str) -> list[dict]:
        return list(self._store.get(_key(conv_id), []))

    async def append_message(
        self, conv_id: uuid.UUID | str, role: str, content: str
    ) -> None:
        key = _key(conv_id)
        dq = self._store.setdefault(key, deque())
        dq.append({"role": role, "content": content})
        keep = max(settings.HISTORY_WINDOW * 2, 0)
        while keep > 0 and len(dq) > keep:
            dq.popleft()

    async def warm(
        self, conv_id: uuid.UUID | str, messages: list[dict]
    ) -> None:
        if not messages:
            return
        key = _key(conv_id)
        self._store[key] = deque(messages)
        keep = max(settings.HISTORY_WINDOW * 2, 0)
        while keep > 0 and len(self._store[key]) > keep:
            self._store[key].popleft()

    async def delete(self, conv_id: uuid.UUID | str) -> None:
        self._store.pop(_key(conv_id), None)


_cache: BaseConversationCache | None = None


def get_conversation_cache() -> BaseConversationCache:
    """工厂（进程级单例）。默认 Redis，连接失败自动降级内存。"""
    global _cache
    if _cache is None:
        try:
            _cache = RedisConversationCache()
        except Exception as exc:  # redis 包缺失或 URL 非法
            logger.warning(f"Redis 不可用，降级内存缓存：{exc}")
            _cache = InMemoryConversationCache()
    return _cache


def set_conversation_cache(cache: BaseConversationCache | None) -> None:
    """替换全局缓存实现（测试注入用）。"""
    global _cache
    _cache = cache
