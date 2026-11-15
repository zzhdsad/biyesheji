"""第二轮 Batch 2（删除生命周期与证据可信性）回归测试。

覆盖：
- BUG-006 文档 purge 必须真正删除 Milvus 向量，且失败可见（不再静默残留）
- BUG-007 KB purge 必须清理该 KB 下全部向量（含 Document chunk）
- BUG-046 重新向量化先插后删（写入失败不会让向量全灭）
- BUG-015 Reflection 引用编号按 source_index 解析（不与过滤后位序错位）
- BUG-016 Prompt / Evidence / Citation 三处编号统一

环境：PostgreSQL 用例需要 PG；向量侧沿用 conftest 注入的
InMemoryVectorStore（未连接真实 Milvus），不伪造 Milvus 结果。
"""

import asyncio

import pytest

from src.application.evidence import build_evidence, hit_to_evidence
from src.application.rag_service import RagService
from src.application.self_reflection import SelfReflection
from src.infrastructure.embedding import MockEmbedding
from src.infrastructure.milvus_store import (
    InMemoryVectorStore,
    VectorRow,
    VectorStoreError,
)
from src.infrastructure.rerank import MockRerank
from tests.test_citation_evidence import _make_doc_hit, _mock_db
from tests.test_parse import _make_kb
from tests.test_upload import PG_AVAILABLE

pytestmark = pytest.mark.skipif(not PG_AVAILABLE, reason="PostgreSQL 未启动")


# ── 工具 ────────────────────────────────────────────────────────────────────


def _upload_txt(client, kb_id: str, name: str = "batch2.txt") -> str:
    text = "# 手册\n\n" + "\n\n".join(
        f"第{i}段，" + "内容" * 200 + "。" for i in range(3)
    )
    resp = client.post(
        "/api/v1/documents/upload",
        data={"kb_id": kb_id},
        files={"file": (name, text.encode("utf-8"), "text/plain")},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def _row(doc_id: str, chunk_id: str) -> VectorRow:
    return VectorRow(
        id=chunk_id,
        doc_id=doc_id,
        kb_id="kb-batch2",
        chunk_index=0,
        content="内容",
        page_num=None,
        title_path=None,
        dense_vector=[0.1] * 4,
        sparse_vector={1: 0.5},
    )


# ── BUG-006：文档 purge 清理向量 + 失败可见 ──────────────────────────────────


def test_document_purge_deletes_vectors(client, vector_store):
    """purge 后该文档向量必须清零（旧实现调用了不存在的 delete_by_doc_id）。"""
    kb_id = _make_kb(client)
    doc_id = _upload_txt(client, kb_id)
    assert vector_store.count_by_doc(doc_id) > 0, "上传后应有向量"

    resp = client.delete(f"/api/v1/documents/{doc_id}/purge")
    assert resp.status_code == 200, resp.text
    # Milvus 的删除在 flush 前对查询不可见（本部署实测：no-flush=3 / flush=0），
    # 统计前显式 flush，避免把"删除尚未落盘"误判为"清理失败"。
    vector_store.flush()
    assert vector_store.count_by_doc(doc_id) == 0, "purge 后向量应被清理"


def test_document_purge_fails_loud_when_vector_cleanup_fails(
    client, vector_store, monkeypatch
):
    """向量清理失败必须 422 且保留 PG 记录：不允许留下孤儿向量。"""
    kb_id = _make_kb(client)
    doc_id = _upload_txt(client, kb_id)

    def _boom(_doc_id: str) -> None:
        raise VectorStoreError("Milvus 不可用（测试注入）")

    monkeypatch.setattr(vector_store, "delete_by_doc", _boom)

    resp = client.delete(f"/api/v1/documents/{doc_id}/purge")
    assert resp.status_code == 422, resp.text
    # PG 记录仍在（未产生"PG 已删但向量残留"）
    assert client.get(f"/api/v1/documents/{doc_id}").status_code == 200


# ── BUG-007：KB purge 清理该 KB 下全部向量 ───────────────────────────────────


def test_kb_purge_deletes_document_vectors(client, vector_store):
    """KB purge 后该 KB 下 Document chunk 向量必须清零（旧实现只清 Resource）。"""
    kb_id = _make_kb(client)
    doc_id = _upload_txt(client, kb_id)
    assert vector_store.count_by_doc(doc_id) > 0

    assert client.delete(f"/api/v1/kb/{kb_id}").status_code == 200
    resp = client.delete(f"/api/v1/kb/{kb_id}/purge")
    assert resp.status_code == 200, resp.text
    vector_store.flush()  # 同上：删除需落盘后才对查询可见
    assert vector_store.count_by_doc(doc_id) == 0, "KB purge 后文档向量应被清理"


def test_kb_purge_fails_loud_when_vector_cleanup_fails(
    client, vector_store, monkeypatch
):
    """KB 向量清理失败 → 422，KB 不被删除（避免不可逆且残留）。"""
    kb_id = _make_kb(client)

    def _boom(_kb_id: str) -> None:
        raise VectorStoreError("Milvus 不可用（测试注入）")

    monkeypatch.setattr(vector_store, "delete_by_kb", _boom)

    assert client.delete(f"/api/v1/kb/{kb_id}").status_code == 200  # 软删进回收站
    resp = client.delete(f"/api/v1/kb/{kb_id}/purge")
    assert resp.status_code == 422, resp.text
    trash = client.get("/api/v1/kb/trash").json()
    assert any(item["id"] == kb_id for item in trash), "KB 应仍在回收站"


# ── BUG-046：先插后删（写入失败不丢旧向量）──────────────────────────────────


def test_replace_doc_inserts_before_deleting_stale():
    """整体替换后条数正确，多余旧分片被清理。"""
    store = InMemoryVectorStore()
    store.replace_doc("d1", [_row("d1", "c1"), _row("d1", "c2"), _row("d1", "c3")])
    assert store.count_by_doc("d1") == 3

    store.replace_doc("d1", [_row("d1", "c1"), _row("d1", "c2")])
    assert store.count_by_doc("d1") == 2
    assert set(store.ids_by_doc("d1")) == {"c1", "c2"}


def test_replace_doc_keeps_old_vectors_when_insert_fails(monkeypatch):
    """插入失败时旧向量必须仍在（旧实现先 delete 会导致向量全灭）。"""
    store = InMemoryVectorStore()
    store.replace_doc("d1", [_row("d1", "c1"), _row("d1", "c2")])

    def _boom(_rows):  # noqa: ANN001
        raise VectorStoreError("写入失败（测试注入）")

    monkeypatch.setattr(store, "insert", _boom)
    with pytest.raises(VectorStoreError):
        store.replace_doc("d1", [_row("d1", "c9")])
    assert store.count_by_doc("d1") == 2, "写入失败后旧向量必须保留"


# ── BUG-015：Reflection 按 source_index 解析引用编号 ────────────────────────


def _evidence(source_index: int) -> dict:
    hit = _make_doc_hit(score=0.9)
    hit["id"] = f"chunk-{source_index}"
    return hit_to_evidence(hit, source_index)


def test_reflection_uses_source_index_not_position(monkeypatch):
    """evidence 编号不连续时（过滤产生），引用编号必须按 source_index 解析。"""
    import src.application.self_reflection as sr

    fake = [_evidence(2), _evidence(5)]
    monkeypatch.setattr(sr, "build_evidence", lambda hits: fake)

    # [citation:5] 指向第二条证据（编号为 5）→ 合法
    decision = SelfReflection().evaluate("答案是太阳病[citation: 5, 0]", [])
    assert "citation_out_of_range" not in decision.issues, decision.issues
    assert decision.details["cited_evidence_count"] == 1
    assert decision.details["invalid_citation_count"] == 0

    # [citation:3] 不存在 → 越界
    decision = SelfReflection().evaluate("答案是太阳病[citation: 3, 0]", [])
    assert "citation_out_of_range" in decision.issues
    assert decision.details["invalid_citation_count"] == 1


def test_reflection_prompt_numbering_matches_source_index():
    """LLM 复核/一致性提示中的编号必须等于 source_index（与答案同一空间）。"""
    from src.application.self_reflection import build_revision_messages

    fake = [_evidence(2), _evidence(5)]
    user = build_revision_messages("问题？", fake, "草稿答案")[1]["content"]
    assert "[2]" in user and "[5]" in user
    assert "[1]" not in user, "不得按过滤后的位序重新编号"


# ── BUG-016：Prompt / Evidence / Citation 编号统一 ──────────────────────────


@pytest.mark.asyncio
async def test_prompt_evidence_citation_share_numbering(monkeypatch):
    """低于阈值的命中不进入编号空间，答案的引用编号必有对应卡片。"""
    import src.core.config as config_mod

    original = config_mod.settings.RELEVANCE_THRESHOLD
    config_mod.settings.RELEVANCE_THRESHOLD = 0.5
    try:
        high = _make_doc_hit(score=0.9)
        low = _make_doc_hit(score=0.1)
        low["id"] = "chunk-low"
        hits = [high, low]

        svc = RagService(_mock_db(), embedding=MockEmbedding(), rerank=MockRerank())

        # Evidence：只保留可展示命中，编号连续
        evidence = build_evidence(hits)
        assert [e["source_index"] for e in evidence] == [1]

        # Prompt：只为可展示命中编号（模型不可能引用没有卡片的编号）
        prompt = svc._build_user_prompt("问题？", hits)
        assert "[1]" in prompt
        assert "[2]" not in prompt

        # Citation：显式引用 [citation:1] → 有且仅有一张卡片
        citations = await svc._build_citations("答案[citation: 1, 0]", hits)
        assert len(citations) == 1
        assert citations[0]["source_index"] == 1
    finally:
        config_mod.settings.RELEVANCE_THRESHOLD = original
