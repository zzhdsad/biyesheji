"""向量化入库功能测试。

单元：MockEmbedding 确定性、InMemoryVectorStore 增删计数。
集成（需 PostgreSQL）：解析→向量化流水线至 completed、Milvus/内存库行校验、
reindex 幂等、失败回写与恢复。conftest 自动注入 mock 向量化与向量库实现
（Milvus 可用时为真实 MilvusStore，否则内存实现）。
"""

import uuid

import pytest

from src.infrastructure.embedding import MockEmbedding
from src.infrastructure.milvus_store import InMemoryVectorStore, VectorRow, VectorStoreError
from src.main import app  # noqa: F401  确保 app 导入一致
from tests.test_upload import PG_AVAILABLE
from tests.test_parse import _make_kb  # 复用知识库创建


# ---------- 单元：MockEmbedding ----------


def test_mock_embedding_deterministic_and_normalized():
    emb = MockEmbedding(dim=64)
    texts = ["员工手册第一章", "考勤制度：每日八小时"]
    d1, s1 = emb.encode(texts)
    d2, s2 = emb.encode(texts)

    # 确定性：相同文本输出恒定
    assert d1 == d2
    assert s1 == s2
    # 维度与归一化
    assert all(len(v) == 64 for v in d1)
    for vec in d1:
        norm = sum(x * x for x in vec) ** 0.5
        assert abs(norm - 1.0) < 1e-6
    # 稀疏向量非空且权重为正
    assert all(len(sv) > 0 for sv in s1)
    assert all(w > 0 for sv in s1 for w in sv.values())


def test_mock_embedding_different_texts_differ():
    emb = MockEmbedding(dim=32)
    d, _ = emb.encode(["完全不同的内容A", "另一些文字B"])
    assert d[0] != d[1]


# ---------- 单元：InMemoryVectorStore ----------


def _row(doc_id: str, chunk_id: str) -> VectorRow:
    return VectorRow(
        id=chunk_id,
        doc_id=doc_id,
        kb_id="kb-1",
        chunk_index=0,
        content="内容",
        page_num=None,
        title_path=None,
        dense_vector=[0.1] * 4,
        sparse_vector={1: 0.5},
    )


def test_inmemory_store_insert_delete_count():
    store = InMemoryVectorStore()
    assert store.count_by_doc("d1") == 0
    store.insert([_row("d1", "c1"), _row("d1", "c2"), _row("d2", "c3")])
    assert store.count_by_doc("d1") == 2
    assert store.count_by_doc("d2") == 1
    # 幂等：同 id 覆盖
    store.insert([_row("d1", "c1")])
    assert store.count_by_doc("d1") == 2
    store.delete_by_doc("d1")
    assert store.count_by_doc("d1") == 0
    assert store.count_by_doc("d2") == 1


# ---------- 集成：上传 → 解析 → 向量化 → completed ----------


def _upload_txt(client, kb_id: str, name: str = "向量化.txt") -> str:
    text = "# 手册\n\n" + "\n\n".join(f"第{i}段，" + "内容" * 200 + "。" for i in range(3))
    resp = client.post(
        "/api/v1/documents/upload",
        data={"kb_id": kb_id},
        files={"file": (name, text.encode("utf-8"), "text/plain")},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


@pytest.mark.skipif(not PG_AVAILABLE, reason="PostgreSQL 未启动")
def test_upload_auto_vectorize_to_completed(client, vector_store):
    """上传后自动流水线：parse_status=completed，向量库行数/字段与 chunks 一致。"""
    kb_id = _make_kb(client)
    doc_id = _upload_txt(client, kb_id)

    detail = client.get(f"/api/v1/documents/{doc_id}").json()
    assert detail["parse_status"] == "completed", detail

    chunks = client.get(f"/api/v1/documents/{doc_id}/chunks").json()
    assert chunks
    assert vector_store.count_by_doc(doc_id) == len(chunks) == detail["chunk_count"]

    rows = {r["id"]: r for r in vector_store.query_rows(doc_id)}
    assert set(rows) == {c["id"] for c in chunks}
    for c in chunks:
        row = rows[c["id"]]
        assert row["doc_id"] == doc_id
        assert row["kb_id"] == kb_id
        assert row["chunk_index"] == c["chunk_index"]
        assert row["content"] == c["content"]
        assert row["title_path"] == c["title_path"]
        assert len(row["dense_vector"]) == 1024
        assert row["sparse_vector"]


@pytest.mark.skipif(not PG_AVAILABLE, reason="PostgreSQL 未启动")
def test_reindex_idempotent(client, vector_store):
    """completed 文档重新向量化：幂等重写，行数与向量内容不变。"""
    kb_id = _make_kb(client)
    doc_id = _upload_txt(client, kb_id)
    before = {r["id"]: r["dense_vector"] for r in vector_store.query_rows(doc_id)}
    assert before

    resp = client.post(f"/api/v1/documents/{doc_id}/reindex")
    assert resp.status_code == 202, resp.text

    detail = client.get(f"/api/v1/documents/{doc_id}").json()
    assert detail["parse_status"] == "completed"
    after = {r["id"]: r["dense_vector"] for r in vector_store.query_rows(doc_id)}
    assert after == before  # mock 向量确定性 → 重写结果一致


@pytest.mark.skipif(not PG_AVAILABLE, reason="PostgreSQL 未启动")
def test_vectorize_failure_marks_failed_and_recovers(client, vector_store):
    """向量化异常 → failed + error_message；failed 时 reindex 409；重新解析后恢复 completed。"""
    kb_id = _make_kb(client)
    doc_id = _upload_txt(client, kb_id)
    assert client.get(f"/api/v1/documents/{doc_id}").json()["parse_status"] == "completed"

    # 模拟 Milvus 写入失败（手动注入实例方法并恢复，避免 monkeypatch.undo 波及全局 fixture）
    original_insert = vector_store.insert

    def boom(rows):
        raise VectorStoreError("模拟 Milvus 宕机")

    vector_store.insert = boom
    try:
        client.post(f"/api/v1/documents/{doc_id}/parse")
        detail = client.get(f"/api/v1/documents/{doc_id}").json()
        assert detail["parse_status"] == "failed"
        assert "向量化失败" in detail["error_message"]

        # failed 状态不允许 reindex（需先解析）
        resp = client.post(f"/api/v1/documents/{doc_id}/reindex")
        assert resp.status_code == 409
    finally:
        vector_store.insert = original_insert

    # 恢复：重新解析后向量化成功
    client.post(f"/api/v1/documents/{doc_id}/parse")
    detail = client.get(f"/api/v1/documents/{doc_id}").json()
    assert detail["parse_status"] == "completed", detail


@pytest.mark.skipif(not PG_AVAILABLE, reason="PostgreSQL 未启动")
def test_reindex_missing_document_returns_404(client):
    resp = client.post(f"/api/v1/documents/{uuid.uuid4()}/reindex")
    assert resp.status_code == 404
