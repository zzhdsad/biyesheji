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
from src.infrastructure.db_safety import assert_safe_database, assert_test_database  # noqa: E402
from src.main import app  # noqa: E402
from tests.test_upload import PG_AVAILABLE, _init_db_standalone  # noqa: E402


# ── 测试套件级保险：禁止在未显式放行时连接主库 ──────────────────────────────
# 集成测试会真实建知识库 / 上传文档 / 写导入记录，过去直接连 settings.DATABASE_URL，
# 指向主库时即污染生产数据。这里在收集阶段就拦截，需连主库时必须显式 DB_ALLOW_MAIN=1。
assert_safe_database(settings.DATABASE_URL, purpose="pytest 测试套件")
# 开发库（kp_dev）同样拦截：测试会创建真实知识库，数量变多会把用户知识库
# 挤出前端渲染窗口。需连开发库排障时用 DB_ALLOW_DEV_TESTS=1 显式放行。
assert_test_database(settings.DATABASE_URL, purpose="pytest 测试套件")


def _ensure_test_user() -> None:
    """把测试模式的固定 TEST_USER 真正落库。

    为什么必须有这一步：测试模式下的当前用户是 deps.TEST_USER（**游离对象，
    不在 users 表**），而 knowledge_bases.owner_id 有外键约束。
    过去测试能跑通，是因为历史测试直接连主库、把 TEST_USER 这行写进了主库——
    即测试依赖生产库里的残留数据，换成隔离库后立即 ForeignKeyViolation。
    这里在套件启动时按目标库补齐，使测试不再依赖任何外部残留。
    """
    import asyncio

    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from src.core.deps import TEST_USER

    async def _run() -> None:
        engine = create_async_engine(settings.DATABASE_URL)
        try:
            async with async_sessionmaker(engine)() as session:
                existing = await session.scalar(select(User).where(User.id == TEST_USER.id))
                if existing is not None:
                    return
                same_name = await session.scalar(
                    select(User).where(User.username == TEST_USER.username)
                )
                if same_name is not None:
                    # 库里已有同名管理员（init_db seed）：对齐 id，避免重复插入
                    TEST_USER.id = same_name.id
                    return
                session.add(
                    User(
                        id=TEST_USER.id,
                        email=TEST_USER.email,
                        username=TEST_USER.username,
                        hashed_password="",
                        role="admin",
                        name="测试管理员",
                        department="测试部",
                        must_change_password=False,
                    )
                )
                await session.commit()
        finally:
            await engine.dispose()

    asyncio.run(_run())


if PG_AVAILABLE:
    _ensure_test_user()

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


@pytest.fixture(scope="session", autouse=True)
def _cleanup_test_knowledge_bases():
    """测试套件结束后删除本次测试新建的知识库（文档与向量一并清理）。

    为什么需要：集成测试会真实 `POST /knowledge-bases` 建库却从不删除；一旦在
    开发库上跑测试（DB_ALLOW_DEV_TESTS=1），用户的知识库列表会被几十个
    「解析集成测试库 / HyDE 测试库 / 评估测试KB / 混合检索测试库」塞满。
    这里在套件开始时记录已有库 id，结束时只删新增的那些，不碰用户已有知识库。
    """
    if not PG_AVAILABLE:
        yield
        return

    import asyncio

    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    async def _kb_ids() -> set[str]:
        engine = create_async_engine(settings.DATABASE_URL)
        try:
            async with engine.connect() as conn:
                return set(
                    (await conn.execute(text("select id::text from knowledge_bases"))).scalars()
                )
        finally:
            await engine.dispose()

    async def _cleanup(keep: set[str]) -> None:
        from src.infrastructure.milvus_store import MilvusStore

        store = MilvusStore() if MILVUS_AVAILABLE else None
        engine = create_async_engine(settings.DATABASE_URL)
        try:
            async with engine.begin() as conn:
                ids = list(
                    (await conn.execute(text("select id::text from knowledge_bases"))).scalars()
                )
                extra = [i for i in ids if i not in keep]
                for kb_id in extra:
                    if store is None:
                        continue
                    try:
                        await asyncio.to_thread(store.delete_by_kb, kb_id)
                    except Exception:  # noqa: BLE001 - 向量清理失败不阻断库删除
                        pass
                if extra:
                    await conn.execute(
                        text("delete from knowledge_bases where id::text = any(:ids)"),
                        {"ids": extra},
                    )
        finally:
            await engine.dispose()

    before = asyncio.run(_kb_ids())
    yield
    asyncio.run(_cleanup(before))


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
def _force_mock_model_config(monkeypatch):
    """确保运行时配置使用 mock 后端，避免 DB 中残留的 flagembedding 配置覆盖
    conftest 的 settings mock，导致测试加载真实 BGE-M3 模型。

    patch get_effective_config_cached 使其从 settings 动态构建配置：
    - 后端强制 mock
    - hyde_enabled 跟随 settings.HYDE_ENABLED（测试可 monkeypatch 控制）
    """
    import src.application.model_config_service as mcs

    async def _mock_cached(db):
        # 产品已移除 mock 选项（DB 默认值 / API 校验 / 前端选项均无 mock），工厂仅保留
        # 环境级 mock 逃生舱供测试使用；同时必须给出非空的 base_url/model，
        # 否则 /chat 会因 BUSINESS_RULES §10「模型未配置」返回 503。
            return {
                "llm_provider": "mock",
                "llm_base_url": "http://mock-llm.invalid/v1",
                "llm_model": "mock-model",
                "llm_api_key": "mock-key",
                "embedding_backend": "mock",
            "embedding_model": "mock",
            "embedding_device": "cpu",
            "rerank_backend": "mock",
            "rerank_model": "mock",
            "rerank_device": "cpu",
            "hyde_enabled": settings.HYDE_ENABLED,
            "hyde_backend": "mock",
            "hyde_model": "mock",
            "hyde_base_url": "",
        }

    # 清除缓存，确保不会读到旧的 flagembedding 配置
    mcs._config_cache = None
    # 同时 patch model_config_service 和 rag_service（rag_service 通过 from import 绑定了引用）
    monkeypatch.setattr(mcs, "get_effective_config_cached", _mock_cached)
    import src.application.rag_service as rag_mod
    monkeypatch.setattr(rag_mod, "get_effective_config_cached", _mock_cached)
    # indexing_service 同样是 `from ... import` 绑定：不 patch 的话向量化会走真实
    # BGE-M3（下载/加载 2.3GB），既拖慢测试又依赖外网。
    import src.application.indexing_service as index_mod

    monkeypatch.setattr(index_mod, "get_effective_config_cached", _mock_cached)
    yield
    mcs._config_cache = None


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
