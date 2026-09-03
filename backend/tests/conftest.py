"""全局测试配置。

测试鉴权方案：
- deps.py 中 get_current_user 函数内部有 TEST_MODE_ENABLED 全局开关
- 开启时跳过 JWT 校验直接返回测试管理员（TEST_USER）
- 这样即使 FastAPI 在路由注册时已经缓存了对 get_current_user 的引用，
  动态切换开关也能生效（不需要 patch Depends.dependency 属性）

模块级设置 TEST_MODE_ENABLED = True：所有业务测试自动跳过鉴权。
auth_client fixture：临时关闭测试模式，恢复真实 JWT 鉴权。
"""

import uuid

import pytest
from fastapi.testclient import TestClient

from src.core.config import settings
import src.core.deps as deps_mod  # noqa: E402
from src.core.deps import get_current_user as _REAL_GET_CURRENT_USER  # noqa: E402
from src.domain.models import User  # noqa: E402
from src.infrastructure import milvus_store, redis_client  # noqa: E402
from src.main import app  # noqa: E402
from tests.test_upload import PG_AVAILABLE, _init_db_standalone  # noqa: E402


# ── 启用测试模式（全局开关，无需 patch Depends）──────────────────────────────
# deps.get_current_user 内部会检查这个全局变量
deps_mod.TEST_MODE_ENABLED = True


def _probe_milvus() -> bool:
    try:
        from pymilvus import MilvusClient

        client = MilvusClient(uri=settings.MILVUS_URI, timeout=2)
        client.list_collections()
        return True
    except Exception:
        return False


MILVUS_AVAILABLE = _probe_milvus()


def _probe_redis() -> bool:
    try:
        import redis

        client = redis.from_url(settings.REDIS_URL, socket_connect_timeout=2)
        client.ping()
        client.aclose() if hasattr(client, "aclose") else client.close()
        return True
    except Exception:
        return False


REDIS_AVAILABLE = _probe_redis()


@pytest.fixture(autouse=True)
def _mock_embedding_backend(monkeypatch):
    monkeypatch.setattr(settings, "EMBEDDING_BACKEND", "mock")


@pytest.fixture(autouse=True)
def _mock_llm_backend(monkeypatch):
    monkeypatch.setattr(settings, "LLM_BACKEND", "mock")


@pytest.fixture(autouse=True)
def _mock_rerank_backend(monkeypatch):
    monkeypatch.setattr(settings, "RERANK_BACKEND", "mock")
    from src.infrastructure import rerank

    rerank.set_rerank(rerank.MockRerank())


@pytest.fixture(autouse=True, scope="module")
def vector_store():
    if MILVUS_AVAILABLE:
        store = milvus_store.MilvusStore()
    else:
        store = milvus_store.InMemoryVectorStore()
    milvus_store.set_vector_store(store)
    yield store
    milvus_store.set_vector_store(None)


@pytest.fixture(autouse=True, scope="module")
def conversation_cache():
    if REDIS_AVAILABLE:
        cache = redis_client.RedisConversationCache()
    else:
        cache = redis_client.InMemoryConversationCache()
    redis_client.set_conversation_cache(cache)
    yield cache
    redis_client.set_conversation_cache(None)


@pytest.fixture(scope="module")
def client():
    """业务 API 测试客户端（鉴权通过 TEST_MODE_ENABLED 全局开关跳过）。"""
    if PG_AVAILABLE:
        _init_db_standalone()
    with TestClient(app) as c:
        yield c


@pytest.fixture(scope="function")
def auth_client():
    """鉴权测试专用客户端（临时关闭测试模式，走真实 JWT 验签 + DB）。"""
    deps_mod.TEST_MODE_ENABLED = False
    try:
        if PG_AVAILABLE:
            _init_db_standalone()
        with TestClient(app) as c:
            yield c
    finally:
        deps_mod.TEST_MODE_ENABLED = True
