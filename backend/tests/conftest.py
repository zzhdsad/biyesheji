"""全局测试配置。

- 向量化统一用 mock 后端（确定性伪向量），避免测试依赖 BGE-M3 模型下载。
- Milvus 服务可用时用真实 MilvusStore，否则注入内存实现。
- 会话缓存：Redis 可用 → 真实缓存；否则内存实现。
- 鉴权：**模块加载时直接 monkeypatch deps.get_current_user**，跳过真实鉴权。
  任何测试文件内 new TestClient(app) 都自动跳过鉴权。
  test_auth.py 用 auth_client fixture（module scope）恢复真实函数。
"""

import uuid

import pytest
from fastapi.testclient import TestClient

from src.core.config import settings
import src.core.deps as _deps_mod  # noqa: E402 — 顶层导入，后续模块赋值
from src.core.deps import get_current_user as _REAL_GET_CURRENT_USER  # noqa: E402
from src.domain.models import User  # noqa: E402
from src.infrastructure import milvus_store, redis_client  # noqa: E402
from src.main import app  # noqa: E402
from tests.test_upload import PG_AVAILABLE, _init_db_standalone  # noqa: E402


# ── 模块级鉴权跳过（永久生效，不受 fixture 生命周期影响） ───────────────────────

async def _fake_get_current_user(
    request=None,
    authorization=None,
    db=None,
) -> User:
    u = User(
        id=uuid.UUID("00000000-0000-0000-0000-000000000001"),
        email="test@test.com",
        username="tester",
        hashed_password="",
        role="admin",
    )
    return u


# 1) 改 deps 模块属性（覆盖任何后续 import）
_deps_mod.get_current_user = _fake_get_current_user

# 2) 关键：直接改 protected_router 里 Depends.dependency 已保存的引用
#    （因为 FastAPI 在路由组装时把函数引用拷贝进 Depends.dependency，
#     改模块属性不会影响已经保存的引用）
import src.api.routes as _routes_mod  # noqa: E402

for _dep in _routes_mod.protected_router.dependencies:
    if hasattr(_dep, "dependency"):
        object.__setattr__(_dep, "dependency", _fake_get_current_user)


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


@pytest.fixture(autouse=True)
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
    """业务 API 测试客户端（鉴权已在模块级跳过）。"""
    if PG_AVAILABLE:
        _init_db_standalone()
    with TestClient(app) as c:
        yield c


@pytest.fixture(scope="module")
def auth_client():
    """鉴权测试专用客户端（恢复真实 get_current_user，走 JWT 验签 + DB）。"""
    import src.core.deps as deps_mod

    deps_mod.get_current_user = _REAL_GET_CURRENT_USER
    for _dep in _routes_mod.protected_router.dependencies:
        if hasattr(_dep, "dependency"):
            object.__setattr__(_dep, "dependency", _REAL_GET_CURRENT_USER)
    print(f"\n[auth_client SETUP] protected_router.deps[0].dependency is REAL? "
          f"{_routes_mod.protected_router.dependencies[0].dependency is _REAL_GET_CURRENT_USER}")
    try:
        if PG_AVAILABLE:
            _init_db_standalone()
        with TestClient(app) as c:
            yield c
    finally:
        deps_mod.get_current_user = _fake_get_current_user
        for _dep in _routes_mod.protected_router.dependencies:
            if hasattr(_dep, "dependency"):
                object.__setattr__(_dep, "dependency", _fake_get_current_user)
        print(f"[auth_client TEARDOWN] protected_router.deps[0].dependency is FAKE? "
              f"{_routes_mod.protected_router.dependencies[0].dependency is _fake_get_current_user}")
