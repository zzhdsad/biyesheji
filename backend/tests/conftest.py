"""全局测试配置。

- 向量化统一用 mock 后端（确定性伪向量），避免测试依赖 BGE-M3 模型下载。
- Milvus 服务可用时用真实 MilvusStore，否则注入内存实现，保证全流程可测。
"""

import pytest
from fastapi.testclient import TestClient

from src.core.config import settings
from src.infrastructure import milvus_store
from src.main import app
from tests.test_upload import PG_AVAILABLE, _init_db_standalone


def _probe_milvus() -> bool:
    """探测 Milvus 是否可连接（2s 超时）。"""
    try:
        from pymilvus import MilvusClient

        client = MilvusClient(uri=settings.MILVUS_URI, timeout=2)
        client.list_collections()
        return True
    except Exception:
        return False


MILVUS_AVAILABLE = _probe_milvus()


@pytest.fixture(autouse=True)
def _mock_embedding_backend(monkeypatch):
    """所有测试默认 mock 向量化（相同文本输出恒定）。"""
    monkeypatch.setattr(settings, "EMBEDDING_BACKEND", "mock")


@pytest.fixture(autouse=True)
def vector_store():
    """注入向量库实现：Milvus 可用 → 真实 store；否则内存实现。"""
    if MILVUS_AVAILABLE:
        store = milvus_store.MilvusStore()
    else:
        store = milvus_store.InMemoryVectorStore()
    milvus_store.set_vector_store(store)
    yield store
    milvus_store.set_vector_store(None)


@pytest.fixture(scope="module")
def client():
    """API 测试客户端（需 PostgreSQL 时先独立建表，不依赖应用 lifespan）。"""
    if PG_AVAILABLE:
        _init_db_standalone()
    with TestClient(app) as c:
        yield c
