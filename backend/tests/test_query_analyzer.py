"""阶段十一：Query Analyzer 专项测试。

覆盖（对应需求 §20）：
1~7  herb / prescription / theory / literature / multi_source / general /
     unanswerable candidate
8    resource_types 判定与词表校验
9    keywords 提取
10   entities 提取（书名 / 方剂 / 中药 / 理论术语）
11   analyzer_version
12   非法分析输出 fallback
13   分析器异常 fallback
14   /chat/ask 集成返回 query_analysis
15   /chat/ask-stream 集成（start 事件携带 query_analysis）
16   QueryAnalysis Schema 校验
17   与阶段十 Evidence / Citation 结构兼容
18   Analyzer 不改变现有 Retrieval Strategy（两次运行检索参数与结果一致）
"""

import json
import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.application import evaluation_service as eval_mod
from src.application.evidence import RESOURCE_TYPE_LABELS
from src.application.query_analyzer import (
    ANALYZER_VERSION,
    QueryAnalyzer,
    analyze_query,
    analysis_from_dict,
    fallback_analysis,
    validate_query_analysis,
)
from src.application.question_types import QUESTION_TYPES
from src.application.rag_service import RagService
from src.core.config import settings
from src.infrastructure.embedding import MockEmbedding
from src.infrastructure.rerank import MockRerank
from tests.test_chat import _upload_doc
from tests.test_parse import _make_kb
from tests.test_upload import PG_AVAILABLE

HERB_Q = "金银花有什么功效？"
PRESCRIPTION_Q = "银翘散由哪些药物组成？"
THEORY_Q = "什么是阴阳五行学说？"
LITERATURE_Q = "《伤寒论》的成书背景是什么？"
MULTI_Q = "金银花在《本草纲目》中有什么记载？"
MULTI_THREE_Q = "麻杏石甘汤的组成、功用和治则分别是什么，出自哪部典籍？"
GENERAL_Q = "公司实行什么工时制度？"
UNANSWERABLE_Q = "明天上海的股票涨跌如何？"


# ── 1. herb question ────────────────────────────────────────────────────────


def test_herb_question():
    a = analyze_query(HERB_Q)
    assert a.question_type == "herb"
    assert a.question_type_label == "中药知识"
    assert a.resource_types == ["herb"]
    assert a.is_multi_source is False
    assert a.is_unanswerable_candidate is False
    assert a.is_valid is True
    assert a.fallback_reason is None


# ── 2. prescription question ────────────────────────────────────────────────


def test_prescription_question():
    a = analyze_query(PRESCRIPTION_Q)
    assert a.question_type == "prescription"
    assert a.resource_types == ["prescription"]
    assert a.is_multi_source is False


# ── 3. theory question ──────────────────────────────────────────────────────


def test_theory_question():
    a = analyze_query(THEORY_Q)
    assert a.question_type == "theory"
    assert a.resource_types == ["theory"]


# ── 4. literature question ──────────────────────────────────────────────────


def test_literature_question():
    a = analyze_query(LITERATURE_Q)
    assert a.question_type == "literature"
    assert a.resource_types == ["literature"]
    # 《》内书名识别为文献实体
    assert {"text": "伤寒论", "type": "literature"} in a.entities


# ── 5. multi_source question ────────────────────────────────────────────────


def test_multi_source_question():
    a = analyze_query(MULTI_Q)
    assert a.question_type == "multi_source"
    assert a.resource_types == ["herb", "literature"]
    assert a.is_multi_source is True


def test_multi_source_three_resource_types():
    """方剂 + 理论 + 文献 → multi_source，resource_types 全记录。"""
    a = analyze_query(MULTI_THREE_Q)
    assert a.question_type == "multi_source"
    assert a.is_multi_source is True
    assert a.resource_types == ["prescription", "theory", "literature"]


def test_multi_source_not_triggered_by_multiple_keywords():
    """需求 §8：不能仅因出现多个普通关键词就判定 multi_source。"""
    a = analyze_query("金银花的功效和主治分别是什么？")
    assert a.question_type == "herb"
    assert a.resource_types == ["herb"]
    assert a.is_multi_source is False


# ── 6. general question ─────────────────────────────────────────────────────


def test_general_question():
    a = analyze_query(GENERAL_Q)
    assert a.question_type == "general"
    assert a.resource_types == []
    assert a.is_multi_source is False
    assert a.is_unanswerable_candidate is False
    # 无中医线索但仍在词表内（不拒答、不臆断）
    assert a.question_type in QUESTION_TYPES


# ── 7. unanswerable candidate ───────────────────────────────────────────────


def test_unanswerable_candidate():
    a = analyze_query(UNANSWERABLE_Q)
    assert a.question_type == "unanswerable"
    assert a.is_unanswerable_candidate is True
    assert a.resource_types == []


def test_unanswerable_candidate_is_only_a_flag():
    """candidate 只是标记：不包含任何拒答/检索决策字段，也不改动阈值。"""
    a = analyze_query(UNANSWERABLE_Q)
    payload = a.to_dict()
    # 无 should_refuse / retrieval_strategy / top_k 等决策字段
    assert not {"should_refuse", "retrieval_strategy", "top_k", "use_hyde"} & set(payload)
    assert payload["is_unanswerable_candidate"] is True
    # 既有 Relevance Gate 阈值未被 Analyzer 修改
    assert settings.RELEVANCE_THRESHOLD == pytest.approx(
        settings.RELEVANCE_THRESHOLD
    )
    assert a.features["out_of_domain_hits"] == ["股票"]


def test_unanswerable_candidate_with_tcm_signal():
    """同时含领域线索与超范围线索：以资源类型为准，但仍标记 candidate。"""
    a = analyze_query("金银花的股票代码是多少？")
    assert a.question_type == "herb"
    assert a.resource_types == ["herb"]
    assert a.is_unanswerable_candidate is True


# ── 8. resource_types ───────────────────────────────────────────────────────


def test_resource_types_always_in_vocabulary():
    for q in (HERB_Q, PRESCRIPTION_Q, THEORY_Q, LITERATURE_Q, MULTI_Q,
              MULTI_THREE_Q, GENERAL_Q, UNANSWERABLE_Q, ""):
        a = analyze_query(q)
        assert set(a.resource_types) <= set(RESOURCE_TYPE_LABELS)
        assert a.question_type in QUESTION_TYPES


def test_question_type_vocabulary_shared_with_stage9():
    """阶段十一与阶段九共用同一份 question_type 词表（不重复定义）。"""
    assert eval_mod.QUESTION_TYPES is QUESTION_TYPES
    assert eval_mod.QUESTION_TYPE_LABELS["herb"] == "中药知识"


# ── 9. keywords ─────────────────────────────────────────────────────────────


def test_keywords_extraction():
    a = analyze_query("金银花的性味归经是什么？")
    assert "金银花" in a.keywords
    assert "性味" in a.keywords
    assert "归经" in a.keywords
    # 停用词不进入关键词
    assert not ({"什么", "的", "是"} & set(a.keywords))
    # 关键词去重且保持顺序稳定
    assert len(a.keywords) == len(set(a.keywords))


# ── 10. entities ────────────────────────────────────────────────────────────


def test_entities_extraction_four_types():
    ents = {e["text"]: e["type"] for e in analyze_query(MULTI_THREE_Q).entities}
    assert ents.get("麻杏石甘汤") == "prescription"
    assert ents.get("治则") == "theory"

    herb_ents = analyze_query("甘草和黄芩的配伍禁忌是什么？").entities
    assert {"甘草", "黄芩"} <= {e["text"] for e in herb_ents}
    assert all(e["type"] == "herb" for e in herb_ents)

    lit_ents = analyze_query("《黄帝内经》中如何论述阴阳？").entities
    assert {"text": "黄帝内经", "type": "literature"} in lit_ents
    assert {"text": "阴阳", "type": "theory"} in lit_ents


def test_entities_not_over_triggered_by_suffix():
    """方剂名中的药名字串（银翘 ⊂ 银翘散）不应被重复识别为中药。"""
    ents = analyze_query(PRESCRIPTION_Q).entities
    assert ents == [{"text": "银翘散", "type": "prescription"}]


# ── 11. analyzer_version ────────────────────────────────────────────────────


def test_analyzer_version():
    a = analyze_query(HERB_Q)
    assert a.analyzer_version == ANALYZER_VERSION == "rule-v1"
    assert a.to_dict()["analyzer_version"] == "rule-v1"
    # 确定性：同一 query 多次分析结果一致（便于评测复现）
    assert analyze_query(HERB_Q).to_dict() == a.to_dict()


# ── 12. invalid analyzer output fallback ────────────────────────────────────


def test_validate_query_analysis_rejects_invalid():
    assert validate_query_analysis("not a dict") is None
    assert validate_query_analysis({"question_type": "unknown_type"}) is None
    assert validate_query_analysis(
        {"question_type": "herb", "resource_types": ["not_a_resource"]}
    ) is None
    assert validate_query_analysis(
        {"question_type": "herb", "entities": [{"no_text": 1}]}
    ) is None
    assert validate_query_analysis({"question_type": "herb"}) is None  # 缺 version
    ok = validate_query_analysis(
        {
            "query": "x",
            "question_type": "herb",
            "resource_types": ["herb"],
            "keywords": ["金银花"],
            "entities": [{"text": "金银花", "type": "herb"}],
            "analyzer_version": "llm-v1",
        }
    )
    assert ok is not None and ok.question_type == "herb"


def test_analysis_from_dict_fallback_on_invalid():
    a = analysis_from_dict({"question_type": "bad"}, query=HERB_Q)
    assert a.is_valid is False
    assert a.fallback_reason == "invalid_analysis_output"
    assert a.question_type == "general"
    assert a.resource_types == []
    assert a.is_multi_source is False
    assert a.is_unanswerable_candidate is False


# ── 13. analyzer exception fallback ─────────────────────────────────────────


def test_analyzer_exception_fallback(monkeypatch):
    import src.application.query_analyzer as qa

    def boom(_query):  # noqa: ANN001
        raise RuntimeError("analyzer broken")

    monkeypatch.setattr(qa, "_extract_entities", boom)
    a = analyze_query(HERB_Q)
    assert a.is_valid is False
    assert a.question_type == "general"
    assert a.resource_types == []
    assert a.is_multi_source is False
    assert a.is_unanswerable_candidate is False
    assert "analyzer_exception" in (a.fallback_reason or "")


def test_empty_query_fallback():
    a = analyze_query("   ")
    assert a.is_valid is False
    assert a.fallback_reason == "empty_query"
    assert a.question_type == "general"


def test_fallback_analysis_contract():
    a = fallback_analysis("任意问题", "test")
    assert a.question_type == "general"
    assert a.resource_types == []
    assert a.is_multi_source is False
    assert a.is_unanswerable_candidate is False
    assert a.is_valid is False


# ── 16. QueryAnalysis Schema 校验 ───────────────────────────────────────────


def test_query_analysis_schema_fields():
    from src.api.routes.chat import QueryAnalysisOut

    payload = analyze_query(MULTI_Q).to_dict()
    assert set(payload) == {
        "query",
        "question_type",
        "question_type_label",
        "resource_types",
        "is_multi_source",
        "is_unanswerable_candidate",
        "keywords",
        "entities",
        "features",
        "analyzer_version",
        "is_valid",
        "fallback_reason",
    }
    model = QueryAnalysisOut(**payload)
    assert model.question_type == "multi_source"
    assert model.resource_types == ["herb", "literature"]
    assert isinstance(model.is_multi_source, bool)
    assert isinstance(model.keywords, list)
    assert isinstance(model.entities, list)
    # features 内含查询特征（长度 / 信号强度 / 实体数）
    assert payload["features"]["signal_scores"]["herb"] >= 2
    assert payload["features"]["entity_count"] == 2
    assert payload["features"]["has_book_marker"] is True


# ── 14/15/17/18：API 集成 ───────────────────────────────────────────────────


def _mock_db() -> AsyncMock:
    db = AsyncMock()
    result = MagicMock()
    result.all.return_value = []
    db.scalars.return_value = result
    return db


def _svc() -> RagService:
    return RagService(_mock_db(), embedding=MockEmbedding(), rerank=MockRerank())


def _resource_hit(score=0.8, rtype="herb", rname="金银花"):
    return {
        "id": f"vec-{uuid.uuid4().hex[:8]}",
        "doc_id": "a" * 64,
        "kb_id": str(uuid.uuid4()),
        "content": f"{rname} 的证据文本",
        "page_num": None,
        "title_path": None,
        "score": score,
        "dense_score": score,
        "rerank_score": score,
        "source_kind": "resource",
        "resource_type": rtype,
        "resource_id": str(uuid.uuid4()),
        "resource_name": rname,
        "doc_name": rname,
    }


_MIXED_HITS = [
    _resource_hit(score=0.85, rtype="herb", rname="金银花"),
    _resource_hit(score=0.7, rtype="literature", rname="本草纲目"),
]


def _patch_retrieve(monkeypatch, hits):
    """固定检索命中，保证 API 级断言确定性（不改动检索算法）。"""

    async def _fake_retrieve(
        self, kb_ids, question, strategy=None, resource_types=None, analysis=None,
    ):  # noqa: ARG001
        await RagService._ensure_components(self)  # 仍走真实组件装配
        return [dict(h) for h in hits]

    monkeypatch.setattr(RagService, "_retrieve", _fake_retrieve)


def _parse_sse(text: str) -> list[tuple[str, dict]]:
    events: list[tuple[str, dict]] = []
    event = ""
    data = ""
    for line in text.split("\n"):
        if line.startswith("event: "):
            event = line[7:].strip()
        elif line.startswith("data: "):
            data += line[6:]
        elif line == "":
            if event and data:
                events.append((event, json.loads(data)))
            event = ""
            data = ""
    return events


@pytest.mark.skipif(not PG_AVAILABLE, reason="PostgreSQL 未启动")
def test_ask_returns_query_analysis(client, monkeypatch):
    """POST /chat/ask：新增 query_analysis，旧字段全部保留。"""
    import src.application.rag_service as rag_mod

    _patch_retrieve(monkeypatch, _MIXED_HITS)

    class _MultiCiteLLM:
        async def chat(self, messages):  # noqa: ANN001
            return "回答[citation: 1, 0][citation: 2, 0]"

        async def chat_stream(self, messages):  # noqa: ANN001
            yield "回答"

    monkeypatch.setattr(rag_mod, "get_llm", lambda config: _MultiCiteLLM())

    kb_id = _make_kb(client)
    resp = client.post("/api/v1/chat/ask", json={"question": MULTI_Q, "kb_ids": [kb_id]})
    assert resp.status_code == 200, resp.text
    data = resp.json()

    # 阶段十及更早字段全部健在
    for key in ("answer", "citations", "evidence", "evidence_groups", "evidence_summary"):
        assert key in data, f"缺失旧字段 {key}"
    assert data["citations"][0]["doc_name"] == "金银花"
    assert data["evidence_groups"]

    # 阶段十一新增字段
    qa_out = data["query_analysis"]
    assert qa_out["question_type"] == "multi_source"
    assert qa_out["resource_types"] == ["herb", "literature"]
    assert qa_out["is_multi_source"] is True
    assert qa_out["question_type_label"] == "多来源知识"
    assert qa_out["analyzer_version"] == "rule-v1"
    assert qa_out["is_valid"] is True
    assert qa_out["query"] == MULTI_Q


@pytest.mark.skipif(not PG_AVAILABLE, reason="PostgreSQL 未启动")
def test_ask_stream_returns_query_analysis_in_start(client, monkeypatch):
    """POST /chat/ask-stream：start 事件携带 query_analysis，事件名与顺序不变。"""
    _patch_retrieve(monkeypatch, _MIXED_HITS)
    kb_id = _make_kb(client)
    resp = client.post(
        "/api/v1/chat/ask-stream", json={"question": HERB_Q, "kb_ids": [kb_id]}
    )
    assert resp.status_code == 200, resp.text
    events = _parse_sse(resp.text)
    names = [e for e, _ in events]
    # 事件序列保持：start → citations → delta×N → done
    assert names[0] == "start"
    assert names[1] == "citations"
    assert names[-1] == "done"

    start = events[0][1]
    assert start["conversation_id"]
    assert start["query_analysis"]["question_type"] == "herb"
    assert start["query_analysis"]["resource_types"] == ["herb"]
    # citations 事件结构不变（阶段十字段仍在）
    assert events[1][1]["citations"]
    assert "evidence_groups" in events[1][1]


@pytest.mark.skipif(not PG_AVAILABLE, reason="PostgreSQL 未启动")
def test_analyzer_does_not_change_retrieval_strategy(client, monkeypatch):
    """证明：Query Analyzer 开启与否，Baseline 检索链路与结果完全一致。

    同一 query 跑两次：① 正常分析 ② 强制 Analyzer 异常（走 fallback）。
    两次的检索调用参数、命中数、引用、答案必须一致，且检索配置未被修改。
    """
    import src.application.rag_service as rag_mod

    calls: list[tuple[list[str], str]] = []
    original_retrieve = RagService._retrieve

    async def _recording_retrieve(
        self, kb_ids, question, strategy=None, resource_types=None, analysis=None,
    ):
        calls.append(([str(k) for k in kb_ids], question))
        return await original_retrieve(
            self, kb_ids, question, strategy=strategy, resource_types=resource_types,
            analysis=analysis,
        )

    monkeypatch.setattr(RagService, "_retrieve", _recording_retrieve)

    config_before = (
        settings.RECALL_TOP_K,
        settings.RRF_K,
        settings.RERANK_TOP_N,
        settings.RELEVANCE_THRESHOLD,
    )

    kb_id = _make_kb(client)
    _upload_doc(client, kb_id, "考勤制度.txt")
    question = "公司实行什么工时制度？"

    first = client.post(
        "/api/v1/chat/ask", json={"question": question, "kb_ids": [kb_id]}
    ).json()
    assert first["query_analysis"]["question_type"] == "general"

    # 强制 Analyzer 异常 → fallback（模拟 Analyzer 完全失效）
    def boom(_self, _query):
        raise RuntimeError("analyzer down")

    monkeypatch.setattr(rag_mod.QueryAnalyzer, "analyze", boom)
    second = client.post(
        "/api/v1/chat/ask", json={"question": question, "kb_ids": [kb_id]}
    ).json()

    # ② 走了 fallback，但请求不失败
    assert second["query_analysis"]["question_type"] == "general"
    assert second["query_analysis"]["is_valid"] is False

    # 检索行为一致：同样的 kb_ids 与原始问题（Analyzer 未改写 query）
    assert len(calls) == 2
    assert calls[0] == calls[1]
    assert calls[0][1] == question
    # 检索配置未被 Analyzer 改动
    assert config_before == (
        settings.RECALL_TOP_K,
        settings.RRF_K,
        settings.RERANK_TOP_N,
        settings.RELEVANCE_THRESHOLD,
    )
    # 检索结果与答案一致（Baseline 未改变）
    assert [c["chunk_id"] for c in first["citations"]] == [
        c["chunk_id"] for c in second["citations"]
    ]
    assert first["answer"] == second["answer"]


@pytest.mark.skipif(not PG_AVAILABLE, reason="PostgreSQL 未启动")
def test_service_analyze_query_available():
    """RagService.analyze_query 可在检索前独立调用（与检索解耦）。"""
    svc = _svc()
    a = svc.analyze_query(MULTI_Q)
    assert a.question_type == "multi_source"
    assert isinstance(svc.analyzer, QueryAnalyzer)
