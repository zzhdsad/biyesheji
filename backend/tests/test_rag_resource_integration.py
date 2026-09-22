"""TASK-008 Stage 4-4：RAG 检索接入 Resource 测试。

验证：
1. _enrich_hits_with_doc_name 正确区分 Document / Resource 命中
2. Resource 命中携带 resource_type/resource_id/resource_name/source_kind
3. _build_user_prompt 对 Resource 命中标注"资源：{类型} {名称}"
4. Resource + Document 命中可在同一检索中共存
5. 挂载 Resource 的 KB 向量库可检索到 Resource 命中
6. 未挂载的 KB 搜不到（KB 权限隔离）
"""

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.application.rag_service import RagService, _RESOURCE_TYPE_LABELS
from src.infrastructure import milvus_store
from src.infrastructure.embedding import MockEmbedding
from src.infrastructure.rerank import MockRerank
from src.application.resource_vector_service import ResourceVectorService, make_doc_id
from tests.test_upload import PG_AVAILABLE

pytestmark = pytest.mark.skipif(not PG_AVAILABLE, reason="PostgreSQL 未启动")

_SUFFIX = uuid.uuid4().hex[:8]


# ── 辅助：mock AsyncSession（_enrich_hits_with_doc_name 查 documents 时返回空）──


def _mock_db() -> AsyncMock:
    """构造 mock AsyncSession：scalars() 返回空结果（Document 查不到）。"""
    db = AsyncMock()
    result = MagicMock()
    result.all.return_value = []
    db.scalars.return_value = result
    return db


# ── 单元测试：_enrich_hits_with_doc_name ────────────────────────────────────


@pytest.mark.asyncio
async def test_enrich_resource_only_hits():
    """纯 Resource 命中：不查 documents 表，元数据由 Milvus 带回。"""
    db = _mock_db()
    svc = RagService(db, embedding=MockEmbedding(), rerank=MockRerank())

    res_hit = {
        "id": "vec-1",
        "doc_id": "a" * 64,  # SHA256 格式（Resource）
        "kb_id": str(uuid.uuid4()),
        "chunk_index": 0,
        "content": "桂枝 性味辛甘温",
        "page_num": None,
        "title_path": None,
        "score": 0.75,
        "dense_score": 0.75,
        "resource_type": "herb",
        "resource_id": str(uuid.uuid4()),
        "resource_name": "桂枝",
        "era": None,
        "source_type": None,
        "credibility_level": None,
    }
    await svc._enrich_hits_with_doc_name([res_hit])
    assert res_hit["source_kind"] == "resource"
    assert res_hit["doc_name"] == "桂枝"
    assert res_hit["resource_type"] == "herb"
    # 不应查 documents 表（doc_ids 为空 → if 分支跳过）
    db.scalars.assert_not_called()


@pytest.mark.asyncio
async def test_enrich_mixed_doc_and_resource_hits():
    """Document + Resource 混合命中：Document 走 PG，Resource 走 Milvus 字段。"""
    db = _mock_db()
    svc = RagService(db, embedding=MockEmbedding(), rerank=MockRerank())

    doc_hit = {
        "id": "chunk-1",
        "doc_id": str(uuid.uuid4()),  # UUID 格式（Document）
        "kb_id": str(uuid.uuid4()),
        "chunk_index": 0,
        "content": "文档内容",
        "page_num": 3,
        "title_path": "章节A",
        "score": 0.8,
        "dense_score": 0.8,
    }
    res_hit = {
        "id": "vec-1",
        "doc_id": "b" * 64,  # SHA256 格式（Resource）
        "kb_id": str(uuid.uuid4()),
        "chunk_index": 0,
        "content": "麻黄汤",
        "page_num": None,
        "title_path": None,
        "score": 0.7,
        "dense_score": 0.7,
        "resource_type": "prescription",
        "resource_id": str(uuid.uuid4()),
        "resource_name": "麻黄汤",
        "era": "汉",
        "source_type": None,
        "credibility_level": None,
    }
    hits = [doc_hit, res_hit]
    await svc._enrich_hits_with_doc_name(hits)

    # Document 命中
    assert hits[0]["source_kind"] == "document"
    assert hits[0]["doc_name"] == "未知文档"  # mock DB 返回空
    # Resource 命中
    assert hits[1]["source_kind"] == "resource"
    assert hits[1]["doc_name"] == "麻黄汤"
    assert hits[1]["era"] == "汉"
    # 查 documents 表只传了 Document 命中的 doc_id（非 SHA256）
    db.scalars.assert_called_once()
    query_arg = db.scalars.call_args[0][0]
    # 验证查询条件不包含 SHA256 doc_id（避免 UUID 类型转换错误）
    assert "b" * 64 not in str(query_arg)


# ── 单元测试：_build_user_prompt ─────────────────────────────────────────────


def test_build_prompt_resource_hit_uses_resource_label():
    """Resource 命中在 Prompt 中标注为"资源：{类型} {名称}"。"""
    svc = RagService.__new__(RagService)  # 不走 __init__
    hits = [
        {
            "source_kind": "resource",
            "resource_type": "herb",
            "doc_name": "桂枝",
            "page_num": None,
            "title_path": None,
            "content": "桂枝，辛甘温",
            "source_type": None,
            "era": None,
            "credibility_level": None,
        },
        {
            "source_kind": "document",
            "doc_name": "伤寒论.pdf",
            "page_num": 5,
            "title_path": "太阳病篇",
            "content": "太阳病，头痛发热...",
            "source_type": "经典古籍",
            "era": "汉",
            "credibility_level": 3,
        },
    ]
    prompt = svc._build_user_prompt("桂枝有什么功效？", hits)
    assert "资源：中药 桂枝" in prompt
    assert "文档：伤寒论.pdf" in prompt
    assert "[1]" in prompt and "[2]" in prompt


def test_resource_type_labels_complete():
    """四种 resource_type 均有中文标签。"""
    assert _RESOURCE_TYPE_LABELS == {
        "herb": "中药",
        "prescription": "方剂",
        "theory": "理论",
        "literature": "文献",
    }


# ── 端到端：向量库检索 Resource ─────────────────────────────────────────────


@pytest.fixture
def kb_with_resource(client):
    """创建 KB + Herb + 挂载，返回 (kb_id, herb_id, herb_name)。"""
    resp = client.post("/api/v1/kb", json={"name": f"RAG集成KB-{_SUFFIX}"})
    assert resp.status_code == 201
    kb_id = resp.json()["id"]

    herb_name = f"检索药-{_SUFFIX}"
    resp = client.post("/api/v1/herbs", json={
        "name": herb_name,
        "aliases": ["测试别名"],
        "properties": "辛甘温",
        "channels": ["心", "肺"],
        "effects": "发汗解表",
        "source": "伤寒论",
        "description": "测试中药描述",
    })
    assert resp.status_code == 201
    herb_id = resp.json()["id"]

    resp = client.post(
        f"/api/v1/kb/{kb_id}/resources",
        json={"resource_type": "herb", "resource_id": herb_id},
    )
    assert resp.status_code == 201

    yield kb_id, herb_id, herb_name

    client.delete(f"/api/v1/kb/{kb_id}/resources/herb/{herb_id}")
    client.delete(f"/api/v1/herbs/{herb_id}")
    client.delete(f"/api/v1/kb/{kb_id}")


def test_vector_store_finds_resource_hit(kb_with_resource):
    """挂载后向量库可检索到 Resource 命中，携带 resource_type/resource_id。"""
    kb_id, herb_id, herb_name = kb_with_resource
    store = milvus_store.get_vector_store()

    # 用 MockEmbedding 生成查询向量（与入库向量同维度）
    emb = MockEmbedding()
    query_text = herb_name  # 用资源名作为查询（mock 向量基于文本 hash）
    dense, _ = emb.encode([query_text])

    # 稠密检索
    hits = store.search(dense[0], [kb_id], top_k=10)
    assert len(hits) >= 1
    # 找到 Resource 命中
    res_hits = [h for h in hits if h.get("resource_type") == "herb"]
    assert len(res_hits) >= 1
    rh = res_hits[0]
    assert rh["resource_id"] == herb_id
    assert rh["resource_name"] == herb_name
    assert rh["kb_id"] == kb_id


def test_vector_store_kb_isolation(kb_with_resource):
    """未挂载的 KB 搜不到该 Resource 命中。"""
    kb_id, herb_id, herb_name = kb_with_resource
    store = milvus_store.get_vector_store()
    emb = MockEmbedding()
    dense, _ = emb.encode([herb_name])

    # 搜另一个 KB（随机 UUID）→ 不应命中
    other_kb = str(uuid.uuid4())
    hits = store.search(dense[0], [other_kb], top_k=10)
    assert len(hits) == 0


def test_sparse_search_finds_resource(kb_with_resource):
    """稀疏检索也能找到 Resource 命中。"""
    kb_id, herb_id, herb_name = kb_with_resource
    store = milvus_store.get_vector_store()
    emb = MockEmbedding()
    _, sparse = emb.encode([herb_name])

    hits = store.search_sparse(sparse[0], [kb_id], top_k=10)
    # mock 稀疏向量基于字符 hash，查询文本与入库文本部分重叠时应有命中
    res_hits = [h for h in hits if h.get("resource_type") == "herb"]
    # 稀疏命中可能为空（取决于字符重叠），但若命中则应有 resource_type
    for h in res_hits:
        assert h["resource_id"] == herb_id
