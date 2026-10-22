"""会话历史缓存单元测试。

覆盖：append/warm/get 的顺序与裁剪、TTL（Redis）、空历史不回填、
Redis 与内存实现行为一致性。
"""

import asyncio
import uuid

import pytest

from src.core.config import settings
from src.infrastructure.redis_client import InMemoryConversationCache
from tests.conftest import REDIS_AVAILABLE


def _conv_id() -> str:
    return str(uuid.uuid4())


# ---------- InMemory 实现（始终运行） ----------


@pytest.mark.anyio
async def test_inmem_append_get_order_and_trim():
    """内存缓存：追加顺序保留、超过 2*N 轮裁剪掉最早消息、角色交替。"""
    cache = InMemoryConversationCache()
    cid = _conv_id()
    total = settings.HISTORY_WINDOW * 2 + 3  # 超出窗口
    for i in range(total):
        await cache.append_message(cid, "user" if i % 2 == 0 else "assistant", f"m{i}")
    history = await cache.get_history(cid)
    assert len(history) == settings.HISTORY_WINDOW * 2  # 裁剪至最近 N 轮
    # 最早 total-2N 条被裁掉，保留尾部 2N 条
    keep_from = total - settings.HISTORY_WINDOW * 2
    assert history[0]["content"] == f"m{keep_from}"
    assert history[-1]["content"] == f"m{total - 1}"
    roles = [h["role"] for h in history]
    # 角色严格交替（user/assistant 相邻不同）
    assert all(roles[k] != roles[k + 1] for k in range(len(roles) - 1))


@pytest.mark.anyio
async def test_inmem_warm_overwrites_and_skips_empty():
    """回填：覆盖旧内容；空历史不回填（避免新会话每轮重复回源）。"""
    cache = InMemoryConversationCache()
    cid = _conv_id()
    await cache.append_message(cid, "user", "旧")
    await cache.warm(cid, [{"role": "user", "content": "新"}, {"role": "assistant", "content": "答"}])
    assert [h["content"] for h in await cache.get_history(cid)] == ["新", "答"]

    cid2 = _conv_id()
    await cache.warm(cid2, [])  # 空：不写入
    assert await cache.get_history(cid2) == []


@pytest.mark.anyio
async def test_inmem_delete():
    cache = InMemoryConversationCache()
    cid = _conv_id()
    await cache.append_message(cid, "user", "x")
    await cache.delete(cid)
    assert await cache.get_history(cid) == []


@pytest.mark.anyio
async def test_inmem_isolated_by_conversation():
    """不同会话缓存隔离。"""
    cache = InMemoryConversationCache()
    c1, c2 = _conv_id(), _conv_id()
    await cache.append_message(c1, "user", "A")
    await cache.append_message(c2, "user", "B")
    assert await cache.get_history(c1) == [{"role": "user", "content": "A"}]
    assert await cache.get_history(c2) == [{"role": "user", "content": "B"}]


# ---------- Redis 实现（Redis 可用时运行） ----------


@pytest.mark.skipif(not REDIS_AVAILABLE, reason="Redis 未启动")
@pytest.mark.anyio
async def test_redis_roundtrip_and_ttl():
    """Redis：追加→读取顺序正确、TTL 被设置（>0 且 ≤ TTL）。"""
    from src.infrastructure.redis_client import RedisConversationCache

    cache = RedisConversationCache()
    cid = _conv_id()
    await cache.delete(cid)  # 清理可能残留
    await cache.append_message(cid, "user", "你好")
    await cache.append_message(cid, "assistant", "在的")
    history = await cache.get_history(cid)
    assert [h["content"] for h in history] == ["你好", "在的"]

    # TTL 被设置
    import redis.asyncio as aioredis

    client = aioredis.from_url(settings.REDIS_URL, decode_responses=True)
    try:
        key = f"conversation:{cid}:history"
        ttl = await client.ttl(key)
        assert 0 < ttl <= settings.HISTORY_TTL_SECONDS
    finally:
        await client.aclose()
    await cache.delete(cid)


@pytest.mark.skipif(not REDIS_AVAILABLE, reason="Redis 未启动")
@pytest.mark.anyio
async def test_redis_get_on_missing_key_returns_empty():
    """Redis：不存在的 key 返回空列表（触发回源兜底）。"""
    from src.infrastructure.redis_client import RedisConversationCache

    cache = RedisConversationCache()
    assert await cache.get_history(_conv_id()) == []
