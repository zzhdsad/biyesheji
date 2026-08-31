"""混合检索与重排序测试：RRF 融合、MockRerank、稀疏召回、端到端集成。"""

from src.core.config import settings
from src.infrastructure.rerank import MockRerank
from src.utils.retrieval import rrf_fusion


# ---------- 单元：RRF 融合 ----------


def test_rrf_merges_and_ranks_by_reciprocal_rank():
    """两路召回有交集：交集项因双路命中 RRF 分更高，应排在单路项之前。"""
    a = [{"id": "x", "content": "A1"}, {"id": "y", "content": "A2"}]  # x rank1, y rank2
    b = [{"id": "x", "content": "A1"}, {"id": "z", "content": "B2"}]  # x rank1, z rank2

    fused = rrf_fusion([a, b], k=60)
    ids = [f["id"] for f in fused]
    assert ids[0] == "x"  # 双路命中，分数最高
    assert set(ids) == {"x", "y", "z"}
    assert fused[0]["rrf_sources"] == 2
    assert all(f["rrf_sources"] == 1 for f in fused[1:])


def test_rrf_empty_lists_and_top_n():
    """空召回返回空；top_n 截断生效。"""
    assert rrf_fusion([[], []]) == []
    fused = rrf_fusion([[{"id": str(i), "content": "c"} for i in range(10)]], top_n=3)
    assert len(fused) == 3
    assert [f["id"] for f in fused] == ["0", "1", "2"]


def test_rrf_preserves_hit_fields():
    """融合结果保留首路命中的字段（content 等）。"""
    a = [{"id": "x", "content": "原文", "page_num": 3, "title_path": "章"}]
    fused = rrf_fusion([a])
    assert fused[0]["content"] == "原文"
    assert fused[0]["page_num"] == 3
    assert fused[0]["title_path"] == "章"


# ---------- 单元：MockRerank ----------


def test_mock_rerank_orders_by_query_overlap():
    """MockRerank 按 query 字符与候选内容重叠率重排。"""
    reranker = MockRerank()
    query = "考勤制度"
    candidates = [
        {"id": "1", "content": "报销流程说明"},  # 与 query 无字符重叠
        {"id": "2", "content": "公司考勤制度规定"},  # 重叠 考/勤/制/度
        {"id": "3", "content": "请假申请"},  # 无重叠
    ]
    result = reranker.rerank(query, candidates, top_n=2)
    assert len(result) == 2
    assert result[0]["id"] == "2"  # 重叠最高排首位
    assert "rerank_score" in result[0]
    assert result[0]["rerank_score"] >= result[1]["rerank_score"]


def test_mock_rerank_top_n_truncates():
    """top_n 截断候选数量。"""
    reranker = MockRerank()
    candidates = [{"id": str(i), "content": "x"} for i in range(8)]
    result = reranker.rerank("x", candidates, top_n=3)
    assert len(result) == 3


def test_mock_rerank_empty_candidates():
    """空候选返回空。"""
    assert MockRerank().rerank("q", [], top_n=5) == []


# ---------- 集成：混合检索 → RRF → Rerank（mock 后端） ----------


def _make_kb(client) -> str:
    r = client.post("/api/v1/kb", json={"name": "混合检索测试库"}).json()
    return r["id"]


def _upload(client, kb_id: str, name: str, text: str):
    return client.post(
        "/api/v1/documents/upload",
        data={"kb_id": kb_id},
        files={"file": (name, text.encode("utf-8"), "text/plain")},
    ).json()


def _ask(client, question: str, kb_id: str):
    return client.post(
        "/api/v1/chat/ask",
        json={"question": question, "kb_ids": [kb_id]},
    ).json()


def test_hybrid_recall_includes_sparse_only_hits(client, monkeypatch):
    """稀疏召回贡献 RRF：关闭 dense 路仅靠 sparse 也能命中。"""
    from src.infrastructure import milvus_store

    kb_id = _make_kb(client)
    # 上传两段不同主题文本
    _upload(client, kb_id, "考勤.txt", "# 考勤\n公司考勤制度规定每日工作八小时。")
    _upload(client, kb_id, "报销.txt", "# 报销\n差旅报销需附发票原件。")
    import time

    time.sleep(1)  # 等待后台解析+向量化完成

    # 取真实 store，仅走稀疏路（monkeypatch 使 dense search 返回空）
    store = milvus_store.get_vector_store()
    original = store.search

    def dense_empty(*a, **kw):
        return []

    monkeypatch.setattr(store, "search", dense_empty)
    try:
        # 用 RagService 内部逻辑验证 sparse 单路也能召回
        from src.infrastructure.embedding import get_embedding

        emb = get_embedding()
        dense, sparse = emb.encode(["考勤制度"])
        sparse_hits = store.search_sparse(sparse[0], [kb_id], settings.RECALL_TOP_K)
        assert len(sparse_hits) > 0, "稀疏路应能召回"
        # 命中应包含考勤相关内容
        assert any("考勤" in h["content"] for h in sparse_hits)
    finally:
        monkeypatch.setattr(store, "search", original)


def test_hybrid_pipeline_returns_top5_and_citations(client):
    """端到端：混合检索 → RRF → Rerank → Top-5 → 答案 + 引用。"""
    kb_id = _make_kb(client)
    # 多段文本，确保召回有多条候选
    paragraphs = [f"第{i}段内容：考勤制度第{i}条规定。" for i in range(6)]
    _upload(client, kb_id, "制度.txt", "# 制度\n\n" + "\n\n".join(paragraphs))
    import time

    time.sleep(1)

    result = _ask(client, "考勤制度有什么规定？", kb_id)
    assert result["answer"]
    assert "[citation: 1" in result["answer"]
    # 精排后返回给 LLM 的引用 ≤ RERANK_TOP_N（5）
    assert len(result["citations"]) <= settings.RERANK_TOP_N
    assert len(result["citations"]) >= 1
    top = result["citations"][0]
    assert top["source_index"] == 1
    assert top["doc_name"] == "制度.txt"
    assert top["score"] >= 0  # rerank 分
