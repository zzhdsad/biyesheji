"""阶段十二：Dynamic Router 专项测试。

覆盖（对应需求 §22 / §23）：
【Router 基础】
1   herb → herb_focused
2   prescription → prescription_focused
3   theory → theory_focused
4   literature → literature_focused
5   multi_source → multi_source
6   general → baseline_hybrid
7   unanswerable candidate → 不直接拒答
8   Router fallback → baseline_hybrid（非法分析 / Router 异常 / 未知策略）
【Resource Filter】
9~12 各聚焦策略确实产生对应 resource_type filter
13  multi_source 支持多 resource type + Document
【Strategy】
14  每个 strategy 参数真实存在（Registry / 取值可读）
15  不同 strategy 产生不同配置（不是"换名字"）
16  baseline_hybrid 与全局 settings 一致（旧行为不变）
17  未知 strategy → get_strategy 返回 None
【确定性】
18  相同 QueryAnalysis 重复路由结果完全一致
【集成】
19  /chat/ask 返回 query_analysis + router_decision
20  /chat/ask-stream start 含 router_decision 且事件顺序不变
21  Router 不改变 Evidence / Citation 结构
22  Analyzer → Router → Retrieval 执行顺序正确
23  Analyzer 异常 → Router fallback → baseline_hybrid（/chat/ask 不失败）
24  Router 异常 → baseline_hybrid（/chat/ask 不失败）
25  RouterDecision Schema 校验（Pydantic）
【实验能力】
26  EvaluationRun.retrieval_strategy 复用：固定策略真正驱动检索参数
27  use_dynamic_router 逐条记录策略 + config_snapshot 记录 router_version
28  GET /evaluation/strategies 返回策略清单

说明：
- 单元测试不依赖任何外部中间件（Milvus / PostgreSQL / LLM）
- 集成测试沿用项目既有方式：TestClient + PostgreSQL（PG_AVAILABLE 开关）+
  固定检索命中（未连接真实 Milvus，不伪造 Milvus 结果）
"""

import json
import uuid
from dataclasses import replace

import pytest

from src.application.dynamic_router import (
    DYNAMIC_ROUTER_STRATEGY,
    ROUTER_VERSION,
    DynamicRouter,
    RouterDecision,
    fallback_decision,
)
from src.application.evaluation_service import (
    BASELINE_RETRIEVAL_STRATEGY,
    EvaluationService,
)
from src.application.query_analyzer import QueryAnalyzer, analyze_query
from src.application.rag_service import RagService
from src.application.retrieval_strategies import (
    BASELINE_STRATEGY,
    STRATEGIES,
    filter_hits,
    get_strategy,
    matches_resource,
    resolve_retrieval_config,
    resolve_resource_types,
    strategy_names,
)
from src.core.config import settings
from src.infrastructure.milvus_store import InMemoryVectorStore, VectorRow
from tests.test_chat import _upload_doc
from tests.test_evaluation_experiment import (  # noqa: F401  复用既有评测夹具/工具
    _case,
    _session,
    eval_kb,
)
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


def _route(query: str) -> RouterDecision:
    """端到端分析+路由的便捷函数（走真实 Analyzer → 真实 Router）。"""
    return DynamicRouter().route(analyze_query(query))


# ── 1~6. question_type → strategy ───────────────────────────────────────────


def test_herb_routes_to_herb_focused():
    d = _route(HERB_Q)
    assert d.strategy_name == "herb_focused"
    assert d.question_type == "herb"
    assert d.resource_types == ["herb"]
    assert d.router_version == ROUTER_VERSION
    assert d.is_valid is True and d.fallback_reason is None


def test_prescription_routes_to_prescription_focused():
    d = _route(PRESCRIPTION_Q)
    assert d.strategy_name == "prescription_focused"
    assert d.resource_types == ["prescription"]


def test_theory_routes_to_theory_focused():
    d = _route(THEORY_Q)
    assert d.strategy_name == "theory_focused"
    assert d.resource_types == ["theory"]


def test_literature_routes_to_literature_focused():
    d = _route(LITERATURE_Q)
    assert d.strategy_name == "literature_focused"
    assert d.resource_types == ["literature"]


def test_multi_source_routes_to_multi_source():
    d = _route(MULTI_Q)
    assert d.strategy_name == "multi_source"
    assert d.resource_types == ["herb", "literature"]


def test_general_routes_to_baseline():
    d = _route(GENERAL_Q)
    assert d.question_type == "general"
    assert d.strategy_name == BASELINE_STRATEGY
    # Baseline 不做任何资源过滤（与阶段十一及之前一致）
    assert d.resource_filter["enabled"] is False


# ── 7. unanswerable candidate → 不拒答 ──────────────────────────────────────


def test_unanswerable_candidate_does_not_trigger_refusal():
    """需求 §10：unanswerable candidate 只做标记，Router 仍正常选择策略。"""
    analysis = analyze_query(UNANSWERABLE_Q)
    assert analysis.is_unanswerable_candidate is True
    assert analysis.question_type == "unanswerable"

    d = DynamicRouter().route(analysis)
    assert d.strategy_name == BASELINE_STRATEGY
    assert d.is_valid is True
    # RouterDecision 不携带任何拒答 / 门控决策字段（Evidence Gate 属阶段十四）
    payload = d.to_dict()
    for forbidden in ("should_refuse", "abstain", "refuse", "evidence_gate"):
        assert forbidden not in payload
        assert forbidden not in payload["retrieval_config"]


def test_router_reason_is_explainable():
    """RouterDecision 必须可解释：reason 含 question_type 与 resource_types。"""
    d = _route(MULTI_Q)
    assert "question_type=multi_source" in d.reason
    assert "resource_types=['herb', 'literature']" in d.reason
    assert d.strategy_description
    for key in ("strategy_name", "reason", "question_type", "resource_types",
                "router_version"):
        assert key in d.to_dict(), f"缺少必要字段 {key}"


# ── 8. Router fallback ──────────────────────────────────────────────────────


def test_fallback_on_invalid_query_analysis():
    """非法 question_type（不在受控词表内）→ baseline_hybrid，不抛异常。"""
    analysis = replace(analyze_query(HERB_Q), question_type="not_a_type")
    d = DynamicRouter().route(analysis)
    assert d.strategy_name == BASELINE_STRATEGY
    assert d.is_valid is False
    assert d.fallback_reason == "invalid_query_analysis"


def test_fallback_on_router_exception(monkeypatch):
    """Router 内部异常 → baseline_hybrid（Router 不是单点故障）。"""
    import src.application.dynamic_router as router_mod

    def boom(*_args, **_kwargs):
        raise RuntimeError("router boom")

    monkeypatch.setattr(router_mod, "build_resource_filter", boom)
    d = _route(HERB_Q)
    assert d.strategy_name == BASELINE_STRATEGY
    assert d.is_valid is False
    assert "router_exception" in (d.fallback_reason or "")


def test_fallback_on_unknown_strategy(monkeypatch):
    """策略不在注册表中 → fallback 到 Baseline。"""
    import src.application.dynamic_router as router_mod

    monkeypatch.setitem(router_mod._STRATEGY_BY_QUESTION_TYPE, "general", "no_such_strategy")
    d = _route(GENERAL_Q)
    assert d.strategy_name == BASELINE_STRATEGY
    assert d.is_valid is False
    assert d.fallback_reason and d.fallback_reason.startswith("unknown_strategy")


def test_fallback_decision_shape():
    d = fallback_decision(None, "unit_test")
    assert d.strategy_name == BASELINE_STRATEGY
    assert d.question_type == "general"
    assert d.resource_types == []
    assert d.is_valid is False
    assert d.router_version == ROUTER_VERSION
    # Baseline 配置：不做资源过滤
    assert d.resource_filter["enabled"] is False


# ── 9~12. resource filter ───────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("query", "resource_type"),
    [
        (HERB_Q, "herb"),
        (PRESCRIPTION_Q, "prescription"),
        (THEORY_Q, "theory"),
        (LITERATURE_Q, "literature"),
    ],
)
def test_focused_strategy_produces_resource_filter(query, resource_type):
    d = _route(query)
    rf = d.resource_filter
    assert rf["enabled"] is True
    assert rf["resource_types"] == [resource_type]
    # 不得破坏 Document 检索
    assert rf["include_document"] is True
    # 命中过滤：保留目标资源 + Document，过滤其它资源
    hits = [
        {"resource_type": resource_type, "id": "h1"},
        {"resource_type": "other", "id": "h2"},
        {"id": "h3"},  # Document（resource_type 为空）
    ]
    assert [h["id"] for h in filter_hits(hits, (resource_type,), True)] == ["h1", "h3"]


# ── 13. multi_source 多资源 + Document ──────────────────────────────────────


def test_multi_source_supports_multiple_resource_types_and_document():
    d = _route(MULTI_THREE_Q)
    types = d.resource_filter["resource_types"]
    assert types == ["prescription", "theory", "literature"]
    assert len(types) >= 2
    assert d.resource_filter["include_document"] is True

    strategy = get_strategy("multi_source")
    assert strategy.resource_types_from_analysis is True
    # 运行时解析：取 QueryAnalysis.resource_types
    assert resolve_resource_types(strategy, ["herb", "literature"]) == ("herb", "literature")
    # 分析未给出资源类型 → 退化为不过滤（等价 Baseline）
    assert resolve_resource_types(strategy, []) is None

    hits = [
        {"resource_type": "prescription", "id": "h1"},
        {"resource_type": "herb", "id": "h2"},
        {"resource_type": "literature", "id": "h3"},
        {"id": "h4"},
    ]
    kept = [h["id"] for h in filter_hits(hits, ("prescription", "herb", "literature"), True)]
    assert kept == ["h1", "h2", "h3", "h4"]


def test_filter_hits_noop_without_resource_types():
    hits = [{"resource_type": "herb", "id": "h1"}, {"id": "h2"}]
    assert filter_hits(hits, None, True) == hits
    assert matches_resource({"resource_type": "herb"}, None, True) is True
    # include_document=False 时才会丢弃 Document 命中
    assert matches_resource({}, ("herb",), False) is False


def test_store_search_applies_resource_filter():
    """真实组件（InMemoryVectorStore）：资源过滤在召回层生效。"""

    def row(cid: str, rtype: str | None) -> VectorRow:
        return VectorRow(
            id=cid,
            doc_id="d1",
            kb_id="kb1",
            chunk_index=0,
            content="内容",
            page_num=None,
            title_path=None,
            dense_vector=[0.1] * 4,
            sparse_vector={1: 0.5},
            resource_type=rtype,
        )

    store = InMemoryVectorStore()
    store.insert([row("c1", "herb"), row("c2", "prescription"), row("c3", None)])

    all_ids = [h["id"] for h in store.search([0.1] * 4, ["kb1"], 10)]
    assert sorted(all_ids) == ["c1", "c2", "c3"]  # 不传过滤 = Baseline 行为
    herb_ids = [h["id"] for h in store.search([0.1] * 4, ["kb1"], 10, ["herb"], True)]
    assert sorted(herb_ids) == ["c1", "c3"]
    no_doc = [h["id"] for h in store.search([0.1] * 4, ["kb1"], 10, ["herb"], False)]
    assert no_doc == ["c1"]
    sparse_ids = [
        h["id"] for h in store.search_sparse({1: 0.5}, ["kb1"], 10, ["herb"], True)
    ]
    assert sorted(sparse_ids) == ["c1", "c3"]


# ── 14~17. strategy 抽象 ────────────────────────────────────────────────────


def test_registry_contains_six_strategies():
    """阶段十二的六类基础策略必须仍在注册表中（阶段十三可增补，不得删除）。"""
    assert {
        "baseline_hybrid",
        "herb_focused",
        "prescription_focused",
        "theory_focused",
        "literature_focused",
        "multi_source",
    }.issubset(set(strategy_names()))
    for name, s in STRATEGIES.items():
        assert s.name == name
        assert s.description
        cfg = resolve_retrieval_config(s)
        assert isinstance(cfg["recall_top_k"], int) and cfg["recall_top_k"] > 0
        assert isinstance(cfg["rerank_top_k"], int) and cfg["rerank_top_k"] > 0
        assert isinstance(cfg["rrf_k"], int) and cfg["rrf_k"] > 0
        assert isinstance(cfg["hyde_enabled"], bool)
        assert isinstance(cfg["dense_enabled"], bool)
        assert isinstance(cfg["sparse_enabled"], bool)


def test_strategy_parameters_are_real_and_different():
    """需求 §8：策略差异必须真实存在，不能只是换名字。"""
    cfgs = {name: resolve_retrieval_config(s) for name, s in STRATEGIES.items()}
    # 六套配置互不相同（含 strategy_name，整体作为实验快照可比）
    signatures = {json.dumps(c, sort_keys=True, ensure_ascii=False) for c in cfgs.values()}
    assert len(signatures) == len(STRATEGIES)

    herb = cfgs["herb_focused"]
    theory = cfgs["theory_focused"]
    literature = cfgs["literature_focused"]
    multi = cfgs["multi_source"]
    assert herb["resource_types"] == ["herb"]
    assert herb["recall_top_k"] == 60 and herb["rerank_top_k"] == 6
    assert theory["resource_types"] == ["theory"]
    assert theory["rrf_k"] == 40 and theory["recall_top_k"] == 40
    # 文献策略关闭 HyDE（现有 HyDE 可被配置关闭）
    assert literature["resource_types"] == ["literature"]
    assert literature["hyde_enabled"] is False
    assert herb["hyde_enabled"] is True
    # 多来源：资源类型运行时解析 + 最大召回/精排
    assert multi["recall_top_k"] > herb["recall_top_k"]
    assert multi["rerank_top_k"] > herb["rerank_top_k"]


def test_baseline_hybrid_matches_global_settings():
    baseline = resolve_retrieval_config(get_strategy(BASELINE_STRATEGY))
    assert baseline["resource_types"] == []
    assert baseline["recall_top_k"] == settings.RECALL_TOP_K
    assert baseline["rerank_top_k"] == settings.RERANK_TOP_N
    assert baseline["rrf_k"] == settings.RRF_K
    assert baseline["hyde_enabled"] is True
    assert baseline["dense_enabled"] is True
    assert baseline["sparse_enabled"] is True
    # strategy=None 与 Baseline 完全一致（旧调用不受影响）
    assert resolve_retrieval_config(None) == baseline


def test_unknown_strategy_returns_none():
    assert get_strategy("no_such_strategy") is None
    assert get_strategy(None) is None
    assert get_strategy("") is None
    # 阶段九历史标签不在注册表中 → 按 Baseline 行为执行
    assert get_strategy(BASELINE_RETRIEVAL_STRATEGY) is None


# ── 18. 确定性 ──────────────────────────────────────────────────────────────


def test_router_is_deterministic():
    """同输入 → 同策略 → 同配置（不允许随机路由）。"""
    for q in (HERB_Q, PRESCRIPTION_Q, THEORY_Q, LITERATURE_Q, MULTI_Q, GENERAL_Q):
        analysis = analyze_query(q)
        runs = [DynamicRouter().route(analysis) for _ in range(3)]
        first = runs[0]
        for d in runs[1:]:
            assert d.strategy_name == first.strategy_name
            assert d.reason == first.reason
            assert d.resource_filter == first.resource_filter
            assert d.retrieval_config == first.retrieval_config
            assert d.to_dict() == first.to_dict()


def test_different_analysis_produces_different_strategy():
    """关键实验正确性：不同 QueryAnalysis → 不同 RetrievalStrategy。"""
    got = {
        analyze_query(q).question_type: _route(q).strategy_name
        for q in (HERB_Q, PRESCRIPTION_Q, LITERATURE_Q, THEORY_Q, MULTI_Q, GENERAL_Q)
    }
    assert got == {
        "herb": "herb_focused",
        "prescription": "prescription_focused",
        "theory": "theory_focused",
        "literature": "literature_focused",
        "multi_source": "multi_source",
        "general": BASELINE_STRATEGY,
    }
    assert len(set(got.values())) == 6


# ── 25. RouterDecision Schema 校验 ──────────────────────────────────────────


def test_router_decision_schema_validation():
    from src.api.routes.chat import RouterDecisionOut

    out = RouterDecisionOut(**_route(MULTI_Q).to_dict())
    assert set(out.model_dump()) == set(RouterDecision(
        strategy_name="x", reason="r", question_type="general"
    ).to_dict())
    assert isinstance(RouterDecisionOut, type)
    assert out.strategy_name == "multi_source"
    assert out.router_version == ROUTER_VERSION


# ── 集成：/chat/ask 与 /chat/ask-stream ─────────────────────────────────────


def _mixed_hits():
    return [
        {"id": "v1", "doc_id": "a" * 64, "kb_id": str(uuid.uuid4()),
         "content": "金银花 的证据文本", "page_num": None, "title_path": None,
         "score": 0.85, "dense_score": 0.85, "rerank_score": 0.85,
         "source_kind": "resource", "resource_type": "herb",
         "resource_id": str(uuid.uuid4()), "resource_name": "金银花",
         "doc_name": "金银花", "source_type": None, "era": None,
         "credibility_level": None},
        {"id": "v2", "doc_id": "b" * 64, "kb_id": str(uuid.uuid4()),
         "content": "本草纲目 的证据文本", "page_num": None, "title_path": None,
         "score": 0.7, "dense_score": 0.7, "rerank_score": 0.7,
         "source_kind": "resource", "resource_type": "literature",
         "resource_id": str(uuid.uuid4()), "resource_name": "本草纲目",
         "doc_name": "本草纲目", "source_type": None, "era": None,
         "credibility_level": None},
    ]


def _patch_retrieve(monkeypatch, hits):
    """固定检索命中（不改动检索算法，保证断言确定性）。"""

    async def _fake_retrieve(
        self, kb_ids, question, strategy=None, resource_types=None, analysis=None,
    ):
        await RagService._ensure_components(self)  # 仍走真实组件装配
        _fake_retrieve.strategy = strategy.name if strategy else None
        return [dict(h) for h in hits]

    _fake_retrieve.strategy = None
    monkeypatch.setattr(RagService, "_retrieve", _fake_retrieve)


class _CiteLLM:
    async def chat(self, messages):  # noqa: ANN001
        return "回答[citation: 1, 0][citation: 2, 0]"

    async def chat_stream(self, messages):  # noqa: ANN001
        yield "回答"


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
def test_ask_returns_query_analysis_and_router_decision(client, monkeypatch):
    """/chat/ask：旧字段全保留，新增 query_analysis + router_decision。"""
    import src.application.rag_service as rag_mod

    _patch_retrieve(monkeypatch, _mixed_hits())
    monkeypatch.setattr(rag_mod, "get_llm", lambda config: _CiteLLM())

    kb_id = _make_kb(client)
    resp = client.post("/api/v1/chat/ask", json={"question": MULTI_Q, "kb_ids": [kb_id]})
    assert resp.status_code == 200, resp.text
    data = resp.json()

    for key in ("answer", "citations", "evidence", "evidence_groups",
                "evidence_summary", "query_analysis", "router_decision"):
        assert key in data, f"缺失字段 {key}"

    rd = data["router_decision"]
    assert rd["strategy_name"] == "multi_source"
    assert rd["question_type"] == "multi_source"
    assert rd["resource_types"] == ["herb", "literature"]
    assert rd["router_version"] == ROUTER_VERSION
    assert rd["resource_filter"]["resource_types"] == ["herb", "literature"]
    assert rd["is_valid"] is True
    assert data["query_analysis"]["question_type"] == "multi_source"


@pytest.mark.skipif(not PG_AVAILABLE, reason="PostgreSQL 未启动")
def test_ask_stream_start_carries_router_decision(client, monkeypatch):
    """/chat/ask-stream：start 事件携带 router_decision，事件名与顺序不变。"""
    _patch_retrieve(monkeypatch, _mixed_hits())

    kb_id = _make_kb(client)
    resp = client.post(
        "/api/v1/chat/ask-stream", json={"question": MULTI_Q, "kb_ids": [kb_id]}
    )
    assert resp.status_code == 200, resp.text
    events = _parse_sse(resp.text)
    names = [e for e, _ in events]
    assert names[0] == "start"
    assert names[1] == "citations"
    assert names[-1] == "done"
    assert set(names) <= {"start", "citations", "delta", "done", "error"}

    start = events[0][1]
    assert start["conversation_id"]
    assert start["query_analysis"]["question_type"] == "multi_source"
    assert start["router_decision"]["strategy_name"] == "multi_source"
    assert start["router_decision"]["router_version"] == ROUTER_VERSION
    # citations 事件旧结构不变
    assert events[1][1]["citations"] and "evidence_groups" in events[1][1]


@pytest.mark.skipif(not PG_AVAILABLE, reason="PostgreSQL 未启动")
def test_router_does_not_change_evidence_structure(client, monkeypatch):
    """Router 不改变 Evidence / Citation 结构（阶段十结构字段全在）。"""
    import src.application.rag_service as rag_mod

    _patch_retrieve(monkeypatch, _mixed_hits())
    monkeypatch.setattr(rag_mod, "get_llm", lambda config: _CiteLLM())

    kb_id = _make_kb(client)
    data = client.post(
        "/api/v1/chat/ask", json={"question": MULTI_Q, "kb_ids": [kb_id]}
    ).json()

    legacy = ("chunk_id", "source_index", "doc_id", "doc_name", "page_num",
              "title_path", "content", "score", "source_kind", "evidence_level")
    stage10 = ("evidence_id", "source_id", "source_name", "source_label",
               "evidence_text")
    for c in data["citations"]:
        for field in legacy + stage10:
            assert field in c, f"缺失 {field}"
    assert data["evidence"] == data["citations"]
    assert data["evidence_summary"]["evidence_count"] == 2
    # 分组维度不变（herb / literature 两组）
    assert {g["group_key"] for g in data["evidence_groups"]} == {
        "resource:herb",
        "resource:literature",
    }


@pytest.mark.skipif(not PG_AVAILABLE, reason="PostgreSQL 未启动")
def test_pipeline_order_is_analyzer_router_retrieval(client, monkeypatch):
    """Analyzer → Router → Retrieval 顺序正确（Router 在检索之前执行）。"""
    import src.application.rag_service as rag_mod

    order: list[str] = []
    original_analyze = QueryAnalyzer.analyze
    original_route = DynamicRouter.route

    def spy_analyze(self, query):
        order.append("analyze")
        return original_analyze(self, query)

    def spy_route(self, analysis):
        order.append("route")
        return original_route(self, analysis)

    monkeypatch.setattr(QueryAnalyzer, "analyze", spy_analyze)
    monkeypatch.setattr(DynamicRouter, "route", spy_route)
    monkeypatch.setattr(rag_mod, "get_llm", lambda config: _CiteLLM())

    async def spy_retrieve(
        self, kb_ids, question, strategy=None, resource_types=None, analysis=None,
    ):
        await RagService._ensure_components(self)  # 仍走真实组件装配
        order.append("retrieve")
        return [dict(h) for h in _mixed_hits()]

    monkeypatch.setattr(RagService, "_retrieve", spy_retrieve)

    kb_id = _make_kb(client)
    resp = client.post("/api/v1/chat/ask", json={"question": MULTI_Q, "kb_ids": [kb_id]})
    assert resp.status_code == 200, resp.text
    assert order == ["analyze", "route", "retrieve"]


@pytest.mark.skipif(not PG_AVAILABLE, reason="PostgreSQL 未启动")
def test_analyzer_exception_falls_back_to_baseline(client, monkeypatch):
    """Analyzer 异常 → QueryAnalysis 兜底 general → Router 选 baseline_hybrid。"""
    import src.application.rag_service as rag_mod

    monkeypatch.setattr(rag_mod, "get_llm", lambda config: _CiteLLM())

    def boom(_self, _query):
        raise RuntimeError("analyzer down")

    monkeypatch.setattr(QueryAnalyzer, "analyze", boom)
    kb_id = _make_kb(client)
    _upload_doc(client, kb_id, "考勤制度.txt")

    resp = client.post(
        "/api/v1/chat/ask", json={"question": GENERAL_Q, "kb_ids": [kb_id]}
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["query_analysis"]["question_type"] == "general"
    assert body["query_analysis"]["is_valid"] is False
    assert body["router_decision"]["strategy_name"] == BASELINE_STRATEGY
    assert body["router_decision"]["retrieval_config"]["recall_top_k"] == settings.RECALL_TOP_K


@pytest.mark.skipif(not PG_AVAILABLE, reason="PostgreSQL 未启动")
def test_router_exception_falls_back_to_baseline(client, monkeypatch):
    """Router 异常 → baseline_hybrid，/chat/ask 仍正常返回。"""
    import src.application.rag_service as rag_mod

    _patch_retrieve(monkeypatch, _mixed_hits())
    monkeypatch.setattr(rag_mod, "get_llm", lambda config: _CiteLLM())

    def boom(_self, _analysis):
        raise RuntimeError("router down")

    monkeypatch.setattr(DynamicRouter, "route", boom)
    kb_id = _make_kb(client)
    resp = client.post("/api/v1/chat/ask", json={"question": MULTI_Q, "kb_ids": [kb_id]})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["router_decision"]["strategy_name"] == BASELINE_STRATEGY
    assert body["router_decision"]["is_valid"] is False
    assert "router_unavailable" in (body["router_decision"]["fallback_reason"] or "")
    # Evidence / Citation 仍正常
    assert body["citations"] and body["evidence_summary"]["evidence_count"] == 2


# ── 集成：EvaluationRun.retrieval_strategy 复用 ─────────────────────────────


@pytest.fixture
def _recorded_rag(monkeypatch):
    """记录检索调用收到的策略参数（验证策略真正驱动检索）。"""
    calls: list[dict] = []

    async def _rec(self, kb_ids, question, history=None, strategy=None,
                   resource_types=None, analysis=None, router_decision=None):
        calls.append({
            "strategy": strategy.name if strategy else None,
            "resource_types": resource_types or [],
        })
        return f"模拟答案：{question}", [
            {"content": f"模拟上下文：{question}", "score": 0.9}
        ]

    monkeypatch.setattr(RagService, "retrieve_and_answer", _rec)
    return calls


@pytest.mark.skipif(not PG_AVAILABLE, reason="PostgreSQL 未启动")
async def test_fixed_strategy_drives_retrieval(client, eval_kb, _recorded_rag):
    """固定策略运行：策略真正传给检索层，并归档到 evaluation_runs。"""
    async with _session() as db:
        svc = EvaluationService(db)
        await svc.save_test_set(
            eval_kb, [_case(f"黄芪-{uuid.uuid4().hex[:8]}", question_type="herb")],
            dataset_version="tcm-v1",
        )
        run, results = await svc.run(
            eval_kb, experiment_name="strategy_probe",
            retrieval_strategy="herb_focused",
        )

        assert run.retrieval_strategy == "herb_focused"
        assert results[0].retrieval_strategy == "herb_focused"
        assert _recorded_rag[0]["strategy"] == "herb_focused"
        # 固定策略的资源过滤来自策略静态配置
        assert run.config_snapshot["strategy"]["strategy_name"] == "herb_focused"
        assert run.config_snapshot["router"]["router_version"] == ROUTER_VERSION


@pytest.mark.skipif(not PG_AVAILABLE, reason="PostgreSQL 未启动")
async def test_legacy_strategy_label_keeps_baseline_behavior(
    client, eval_kb, _recorded_rag,
):
    """阶段九历史策略标签不在注册表 → 按 Baseline 行为执行，标签照原样归档。"""
    async with _session() as db:
        svc = EvaluationService(db)
        await svc.save_test_set(
            eval_kb, [_case(f"旧标签-{uuid.uuid4().hex[:8]}", question_type="general")],
            dataset_version="tcm-v1",
        )
        run, _ = await svc.run(
            eval_kb, retrieval_strategy=BASELINE_RETRIEVAL_STRATEGY,
        )
        assert run.retrieval_strategy == BASELINE_RETRIEVAL_STRATEGY
        # 未识别 → strategy=None → Baseline 参数（行为与阶段十一及之前一致）
        assert _recorded_rag[0]["strategy"] is None


@pytest.mark.skipif(not PG_AVAILABLE, reason="PostgreSQL 未启动")
async def test_dynamic_router_run_records_per_case_strategy(client, eval_kb, _recorded_rag):
    """use_dynamic_router：逐条 Analyzer → Router，策略按 question_type 落到结果。"""
    suffix = uuid.uuid4().hex[:8]
    async with _session() as db:
        svc = EvaluationService(db)
        await svc.save_test_set(
            eval_kb,
            [
                _case(f"金银花有什么功效？-{suffix}", question_type="herb"),
                _case(f"银翘散由哪些药物组成？-{suffix}", question_type="prescription"),
                _case(f"公司实行什么工时制度？-{suffix}", question_type="general"),
            ],
            dataset_version="tcm-v1",
        )
        run, results = await svc.run(eval_kb, use_dynamic_router=True)

        assert run.retrieval_strategy == DYNAMIC_ROUTER_STRATEGY
        assert run.config_snapshot["router"]["dynamic_router"] is True
        by_type = {r.question_type: r.retrieval_strategy for r in results}
        assert by_type == {
            "herb": "herb_focused",
            "prescription": "prescription_focused",
            "general": BASELINE_STRATEGY,
        }
        # 每条用例确实按自己的策略执行检索
        assert [c["strategy"] for c in _recorded_rag] == [
            "herb_focused", "prescription_focused", BASELINE_STRATEGY,
        ]


@pytest.mark.skipif(not PG_AVAILABLE, reason="PostgreSQL 未启动")
def test_strategies_endpoint_lists_all_strategies(client):
    """GET /evaluation/strategies：策略清单 + 生效参数（实验对比用）。"""
    resp = client.get("/api/v1/evaluation/strategies")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["default_strategy"] == BASELINE_STRATEGY
    assert set(body["names"]) == set(strategy_names())
    by_name = {s["name"]: s for s in body["strategies"]}
    assert len(by_name) == len(strategy_names())
    assert len(by_name) >= 6  # 阶段十二的六类基础策略不得被删除
    assert by_name["baseline_hybrid"]["config"]["recall_top_k"] == settings.RECALL_TOP_K
    assert by_name["literature_focused"]["config"]["hyde_enabled"] is False
    assert by_name["multi_source"]["resource_types_from_analysis"] is True
