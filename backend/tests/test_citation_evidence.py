"""TASK-008 Stage 4-5：Citation + Evidence 扩展测试。

验证：
1. Resource Citation 携带 source_kind=resource + resource_type/resource_id/resource_name
2. Document Citation 携带 source_kind=document
3. evidence_level 分级正确：high / medium / insufficient
4. 兼容性：Citation 仍包含 doc_name/page_num/title_path/content/score
5. _hits_to_citations 和 _build_citations 一致性
"""

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.application.rag_service import (
    RagService,
    _evidence_level,
    _EVIDENCE_HIGH_THRESHOLD,
    _EVIDENCE_MEDIUM_THRESHOLD,
)
from src.infrastructure.embedding import MockEmbedding
from src.infrastructure.rerank import MockRerank


def _mock_db() -> AsyncMock:
    db = AsyncMock()
    result = MagicMock()
    result.all.return_value = []
    db.scalars.return_value = result
    return db


def _make_doc_hit(score=0.85) -> dict:
    return {
        "id": "chunk-1",
        "doc_id": str(uuid.uuid4()),
        "kb_id": str(uuid.uuid4()),
        "chunk_index": 0,
        "content": "太阳病，头痛发热",
        "page_num": 5,
        "title_path": "太阳病篇",
        "score": score,
        "dense_score": score,
        "rerank_score": score,
        "source_kind": "document",
        "doc_name": "伤寒论.pdf",
        "source_type": "经典古籍",
        "era": "汉",
        "credibility_level": 3,
    }


def _make_resource_hit(score=0.78, rtype="herb", rname="桂枝") -> dict:
    return {
        "id": "vec-" + uuid.uuid4().hex[:8],
        "doc_id": "a" * 64,  # SHA256
        "kb_id": str(uuid.uuid4()),
        "chunk_index": 0,
        "content": "桂枝，辛甘温，发汗解表",
        "page_num": None,
        "title_path": None,
        "score": score,
        "dense_score": score,
        "rerank_score": score,
        "source_kind": "resource",
        "resource_type": rtype,
        "resource_id": str(uuid.uuid4()),
        "resource_name": rname,
        "era": None,
        "source_type": None,
        "credibility_level": None,
        "doc_name": rname,
    }


# ── evidence_level 单元测试 ───────────────────────────────────────────────


def test_evidence_level_high():
    assert _evidence_level(0.9) == "high"
    assert _evidence_level(_EVIDENCE_HIGH_THRESHOLD) == "high"


def test_evidence_level_medium():
    assert _evidence_level(0.5) == "medium"
    assert _evidence_level(_EVIDENCE_MEDIUM_THRESHOLD) == "medium"


def test_evidence_level_insufficient():
    assert _evidence_level(0.1) == "insufficient"
    assert _evidence_level(0.0) == "insufficient"


# ── _hits_to_citations 测试 ────────────────────────────────────────────────


def test_hits_to_citations_resource():
    """Resource 命中 → Citation 携带 resource 扩展字段。"""
    db = _mock_db()
    svc = RagService(db, embedding=MockEmbedding(), rerank=MockRerank())
    hits = [_make_resource_hit(score=0.85, rtype="prescription", rname="桂枝汤")]
    citations = svc._hits_to_citations(hits)
    assert len(citations) == 1
    c = citations[0]
    assert c["source_kind"] == "resource"
    assert c["resource_type"] == "prescription"
    assert c["resource_id"] is not None
    assert c["resource_name"] == "桂枝汤"
    assert c["evidence_level"] == "high"
    # 兼容字段仍存在
    assert c["doc_name"] == "桂枝汤"
    assert "content" in c
    assert "score" in c


def test_hits_to_citations_document():
    """Document 命中 → Citation 标记 source_kind=document，无 resource 字段。"""
    db = _mock_db()
    svc = RagService(db, embedding=MockEmbedding(), rerank=MockRerank())
    hits = [_make_doc_hit(score=0.8)]
    citations = svc._hits_to_citations(hits)
    assert len(citations) == 1
    c = citations[0]
    assert c["source_kind"] == "document"
    assert "resource_type" not in c or c.get("resource_type") is None
    assert c["evidence_level"] == "high"
    # 兼容字段
    assert c["doc_name"] == "伤寒论.pdf"
    assert c["source_type"] == "经典古籍"
    assert c["era"] == "汉"
    assert c["credibility_level"] == 3


def test_hits_to_citations_mixed():
    """混合命中 → 两种 Citation 共存，可区分。"""
    db = _mock_db()
    svc = RagService(db, embedding=MockEmbedding(), rerank=MockRerank())
    hits = [
        _make_doc_hit(score=0.85),
        _make_resource_hit(score=0.75, rtype="herb", rname="桂枝"),
    ]
    citations = svc._hits_to_citations(hits)
    assert len(citations) == 2
    assert citations[0]["source_kind"] == "document"
    assert citations[1]["source_kind"] == "resource"
    assert citations[1]["resource_type"] == "herb"


def test_hits_to_citations_below_threshold_filtered():
    """score < RELEVANCE_THRESHOLD 的命中不进入 Citation。"""
    import src.core.config as config_mod
    original = config_mod.settings.RELEVANCE_THRESHOLD
    config_mod.settings.RELEVANCE_THRESHOLD = 0.5
    try:
        db = _mock_db()
        svc = RagService(db, embedding=MockEmbedding(), rerank=MockRerank())
        hits = [_make_resource_hit(score=0.3)]  # 低于阈值
        citations = svc._hits_to_citations(hits)
        assert len(citations) == 0
    finally:
        config_mod.settings.RELEVANCE_THRESHOLD = original


# ── _build_citations 一致性测试 ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_build_citations_resource_with_marker():
    """LLM 回答含 [citation: 1] → _build_citations 解析出 Resource Citation。"""
    db = _mock_db()
    svc = RagService(db, embedding=MockEmbedding(), rerank=MockRerank())
    hits = [_make_resource_hit(score=0.85, rtype="herb", rname="桂枝")]
    answer = "桂枝性味辛甘温[citation: 1, 0]。"
    citations = await svc._build_citations(answer, hits)
    assert len(citations) == 1
    c = citations[0]
    assert c["source_kind"] == "resource"
    assert c["resource_type"] == "herb"
    assert c["resource_name"] == "桂枝"
    assert c["source_index"] == 1
    assert c["evidence_level"] == "high"


@pytest.mark.asyncio
async def test_build_citations_fallback_all_when_no_marker():
    """LLM 回答无 [citation] 标记 → 兜底返回全部来源。"""
    db = _mock_db()
    svc = RagService(db, embedding=MockEmbedding(), rerank=MockRerank())
    hits = [
        _make_doc_hit(score=0.85),
        _make_resource_hit(score=0.7, rtype="theory", rname="阴阳学说"),
    ]
    answer = "阴阳是中医的基本理论。"  # 无 citation 标记
    citations = await svc._build_citations(answer, hits)
    assert len(citations) == 2  # 兜底返回全部
    kinds = {c["source_kind"] for c in citations}
    assert kinds == {"document", "resource"}


@pytest.mark.asyncio
async def test_build_citations_resource_below_threshold_filtered():
    """Resource 命中 score 低于阈值 → 不进入 Citation（即使 LLM 标注了）。"""
    import src.core.config as config_mod
    original = config_mod.settings.RELEVANCE_THRESHOLD
    config_mod.settings.RELEVANCE_THRESHOLD = 0.5
    try:
        db = _mock_db()
        svc = RagService(db, embedding=MockEmbedding(), rerank=MockRerank())
        hits = [_make_resource_hit(score=0.3)]  # 低于阈值
        answer = "答案[citation: 1, 0]"
        citations = await svc._build_citations(answer, hits)
        assert len(citations) == 0
    finally:
        config_mod.settings.RELEVANCE_THRESHOLD = original
