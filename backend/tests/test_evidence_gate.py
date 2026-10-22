"""阶段十四：Evidence Gate（证据门控）专项测试。

覆盖（需求 §10）：
1   空 Evidence
2   单条高质量 Vector Evidence
3   低质量 Evidence
4   document / resource / kg 混合
5   contains KG（业务事实）
6   records KG（出处事实）
7   related_to KG（弱关系，不得当强证据）
8   多跳 KG（跳数衰减）
9   KG + Vector 互补
10  多源问题（覆盖 / 未覆盖）
11  resource_types 不匹配
12  unanswerable candidate
13  accept
14  insufficient
15  retry
16  retry 最多一次
17  retry 后仍不足
18  普通 baseline 策略行为不变
19  kg_enhanced 行为不变
20  /chat/ask
21  /chat/ask-stream
22  Evaluation 记录（含 Gate 关闭对照）
23  异常情况下安全 fallback

说明：
- 单元测试不依赖任何外部中间件（Milvus / PostgreSQL / LLM）；
- 集成测试沿用项目既有方式：TestClient + 真实 PostgreSQL（PG_AVAILABLE 开关）+
  固定检索命中（未连接真实 Milvus，不伪造 Milvus 结果）。
"""

import uuid

import pytest

from src.application.evidence import (
    EVIDENCE_LEVEL_HIGH,
    EVIDENCE_LEVEL_INSUFFICIENT,
    EVIDENCE_LEVEL_MEDIUM,
    SOURCE_KIND_KG,
    build_evidence,
    hit_to_evidence,
)
from src.application.evidence_gate import (
    DECISION_ACCEPT,
    DECISION_INSUFFICIENT,
    DECISION_RETRY,
    GATE_VERSION,
    EvidenceGate,
    GateDecision,
    classify_evidence,
    fallback_gate_decision,
    gate_config,
    select_retry_strategy,
)
from src.application.query_analyzer import analyze_query
from src.application.rag_service import GATE_REFUSAL_ANSWER, RagService
from src.application.retrieval_strategies import (
    BASELINE_STRATEGY,
    STRATEGIES,
    resolve_retrieval_config,
)
from src.domain.models import (
    KG_PROVENANCE_INGREDIENT,
    KG_PROVENANCE_SHARED_TAG,
    KG_PROVENANCE_SOURCE,
    KG_RELATION_CONTAINS,
    KG_RELATION_RELATED_TO,
    KG_RELATION_RECORDS,
)
from tests.test_dynamic_router import (  # 复用既有固定命中/LLM 桩与 SSE 解析
    _CiteLLM,
    _mixed_hits,
    _parse_sse,
    _patch_retrieve,
)
from tests.test_evaluation_experiment import _case, _session, eval_kb  # noqa: F401
from tests.test_parse import _make_kb
from tests.test_upload import PG_AVAILABLE

HERB_Q = "金银花有什么功效？"
MULTI_Q = "金银花在《本草纲目》中有什么记载？"
GENERAL_Q = "公司实行什么工时制度？"
UNANSWERABLE_Q = "明天上海的股票涨跌如何？"


# ── 证据构造助手（走真实 hit_to_evidence，保证与生产结构一致）────────────────


def _doc_ev(score: float = 0.85, doc_id: str = "doc-1", name: str = "制度文档") -> dict:
    return hit_to_evidence(
        {
            "id": f"c-{doc_id}", "doc_id": doc_id, "content": "文档证据文本",
            "page_num": 1, "title_path": "第一章", "score": score,
            "dense_score": score, "source_kind": "document", "doc_name": name,
            "resource_type": None,
        },
        1,
    )


def _res_ev(score: float = 0.8, rtype: str = "herb", name: str = "金银花",
            rid: str | None = None) -> dict:
    return hit_to_evidence(
        {
            "id": f"r-{rid or name}", "doc_id": "a" * 64, "content": f"{name}证据文本",
            "page_num": None, "title_path": None, "score": score, "dense_score": score,
            "source_kind": "resource", "resource_type": rtype,
            "resource_id": rid or str(uuid.uuid4()), "resource_name": name,
            "doc_name": name,
        },
        1,
    )


def _kg_ev(score: float = 0.9, relation: str = KG_RELATION_CONTAINS, hop: int = 1,
           rtype: str = "herb", name: str = "金银花", edge: int = 1,
           provenance: str = KG_PROVENANCE_INGREDIENT) -> dict:
    return hit_to_evidence(
        {
            "id": f"kg:e{edge}", "doc_id": f"kg:e{edge}",
            "content": "银翘散 组成 金银花", "page_num": None,
            "title_path": "银翘散 → 组成 → 金银花", "score": score,
            "dense_score": score, "source_kind": SOURCE_KIND_KG,
            "resource_type": rtype, "resource_id": str(uuid.uuid4()),
            "resource_name": name, "doc_name": name,
            "kg_relation": relation, "kg_hop": hop, "kg_provenance": provenance,
        },
        1,
    )


def _gate() -> EvidenceGate:
    return EvidenceGate(enabled=True)


def _eval(evidence, query: str | None = None, analysis=None, allow_retry: bool = True,
          strategy: str | None = None):
    """便捷评估入口：可用 query 生成真实 QueryAnalysis。"""
    ana = analysis if analysis is not None else (
        analyze_query(query) if query else None
    )
    return _gate().evaluate(
        evidence, ana, None, allow_retry=allow_retry, strategy_name=strategy
    )


# ── 1. 空 Evidence ──────────────────────────────────────────────────────────


def test_empty_evidence_is_insufficient():
    d = _eval([], HERB_Q)
    assert d.decision == DECISION_INSUFFICIENT
    assert d.reason == "no_evidence"
    assert d.evidence_count == 0 and d.accepted_count == 0
    assert d.is_valid is True and d.fallback_reason is None
    assert d.gate_version == GATE_VERSION


def test_no_analysis_still_evaluates():
    """analysis 为 None（如评估固定策略路径）时 Gate 仍可判定，不抛异常。"""
    d = _eval([_doc_ev()], None)
    assert d.decision == DECISION_ACCEPT
    assert d.details["expected_resource_types"] == []


# ── 2~3. Vector Evidence 质量 ───────────────────────────────────────────────


def test_single_high_vector_evidence_accepted():
    d = _eval(build_evidence([_doc_ev(0.85)]), HERB_Q)
    assert d.decision == DECISION_ACCEPT
    assert d.reason == "has_high_evidence"
    assert d.high_count == 1 and d.medium_count == 0
    assert d.accepted_count == 1 and d.weak_count == 0
    assert d.source_kind_counts == {"document": 1}
    assert d.best_score == pytest.approx(0.85)


def test_low_quality_evidence_is_not_accepted():
    """低质量证据（低于 RELEVANCE_THRESHOLD）：不进入 Evidence → 判不足。"""
    evidence = build_evidence([_doc_ev(0.15, doc_id="low")])
    assert evidence == []  # 与既有 Citation 过滤口径一致
    d = _eval(evidence, HERB_Q)
    assert d.decision == DECISION_INSUFFICIENT

    # 即使绕过 build_evidence 直接传入低分证据，也不被接受
    forced = [_doc_ev(0.2, doc_id="forced")]
    d2 = _eval(forced, HERB_Q, allow_retry=False)
    assert d2.decision == DECISION_INSUFFICIENT
    assert d2.accepted_count == 0 and d2.weak_count == 1
    assert "no_accepted_evidence" in d2.reason


# ── 4. document / resource / kg 混合 ────────────────────────────────────────


def test_mixed_document_resource_kg_accepted():
    evidence = build_evidence([
        _doc_ev(0.8, doc_id="d1"),
        _res_ev(0.75, rtype="herb", name="金银花"),
        _kg_ev(0.9, KG_RELATION_CONTAINS, 1, edge=1),
    ])
    d = _eval(evidence, HERB_Q)
    assert d.decision == DECISION_ACCEPT
    assert d.source_kind_counts == {"document": 1, "resource": 1, "kg": 1}
    assert d.details["kg_complement"] is True
    assert d.details["kg_only"] is False
    assert d.details["independent_sources"] == 3


# ── 5~8. KG 关系与跳数 ──────────────────────────────────────────────────────


def test_kg_contains_facts_are_business_facts():
    """contains（方剂组成）：两条业务事实 → 纯 KG 也可判定通过。"""
    evidence = build_evidence([
        _kg_ev(0.9, KG_RELATION_CONTAINS, 1, name="金银花", edge=1),
        _kg_ev(0.9, KG_RELATION_CONTAINS, 1, name="连翘", edge=2),
    ])
    d = _eval(evidence, HERB_Q)
    assert d.decision == DECISION_ACCEPT
    assert d.reason == "kg_only_strong_facts"
    assert d.details["kg_strong"] == 2
    assert d.details["kg_only"] is True


def test_kg_records_facts_are_business_facts():
    """records（文献记载）：同为强关系业务事实。"""
    evidence = build_evidence([
        _kg_ev(0.8, KG_RELATION_RECORDS, 1, name="本草纲目", edge=1,
               provenance=KG_PROVENANCE_SOURCE),
        _kg_ev(0.8, KG_RELATION_RECORDS, 1, name="本草纲目2", edge=2,
               provenance=KG_PROVENANCE_SOURCE),
    ])
    d = _eval(evidence, HERB_Q)
    assert d.decision == DECISION_ACCEPT
    assert d.details["kg_relation_counts"][KG_RELATION_RECORDS] == 2


def test_kg_related_to_is_weak_evidence():
    """related_to（共享标签）是弱关系：分数再高也不得单独支撑结论。"""
    evidence = build_evidence([
        _kg_ev(1.0, KG_RELATION_RELATED_TO, 1, name="金银花", edge=1,
               provenance=KG_PROVENANCE_SHARED_TAG),
        _kg_ev(1.0, KG_RELATION_RELATED_TO, 1, name="连翘", edge=2,
               provenance=KG_PROVENANCE_SHARED_TAG),
    ])
    # 证据分数是满分，但仍被判为弱证据（不因分数高变成强证据）
    d = _eval(evidence, HERB_Q, allow_retry=False)
    assert d.decision == DECISION_INSUFFICIENT
    assert d.accepted_count == 0 and d.weak_count == 2
    assert d.best_score == pytest.approx(1.0)
    assert d.details["kg_weak"] == 2


def test_kg_hop_decays_credibility():
    """跳数越多可信度越低：1 跳 strong → 2 跳 medium → 3 跳 weak。"""
    hop1, _ = classify_evidence(_kg_ev(0.9, KG_RELATION_CONTAINS, 1))
    hop2, _ = classify_evidence(_kg_ev(0.9, KG_RELATION_CONTAINS, 2))
    hop3, accepted3 = classify_evidence(_kg_ev(0.9, KG_RELATION_CONTAINS, 3))
    assert hop1 == EVIDENCE_LEVEL_HIGH
    assert hop2 == EVIDENCE_LEVEL_MEDIUM
    assert hop3 == EVIDENCE_LEVEL_INSUFFICIENT and accepted3 is False

    # 两跳证据不足以单独支撑纯 KG 结论（kg_strong 只统计 1 跳业务事实）
    evidence = build_evidence([
        _kg_ev(0.85, KG_RELATION_CONTAINS, 2, name="金银花", edge=1),
        _kg_ev(0.85, KG_RELATION_CONTAINS, 2, name="连翘", edge=2),
    ])
    d = _eval(evidence, HERB_Q, allow_retry=False)
    assert d.decision == DECISION_INSUFFICIENT
    assert d.details["kg_strong"] == 0
    assert d.medium_count == 2  # 仍被接受为中等证据，只是不足以单独支撑


def test_kg_plus_vector_complement_accepted():
    """KG + Vector 互补：单条 KG 事实 + 向量证据即可通过（不必凑够两条 KG）。"""
    evidence = build_evidence([
        _doc_ev(0.75, doc_id="d1"),
        _kg_ev(0.9, KG_RELATION_CONTAINS, 1, edge=1),
    ])
    d = _eval(evidence, HERB_Q)
    assert d.decision == DECISION_ACCEPT
    assert d.details["kg_complement"] is True
    assert d.details["kg_only"] is False


def test_kg_only_single_strong_fact_retries():
    """纯 KG 且只有一条强关系事实 → retry（不得凭单一关系下结论）。"""
    evidence = build_evidence([_kg_ev(0.9, KG_RELATION_CONTAINS, 1, edge=1)])
    d = _eval(evidence, HERB_Q, strategy=BASELINE_STRATEGY)
    assert d.decision == DECISION_RETRY
    assert d.reason == "kg_only_insufficient_strong"
    assert d.retry_strategy == "multi_source"
    assert d.original_strategy == BASELINE_STRATEGY
    assert d.is_valid is True


# ── 10~12. 聚合维度：多源 / 资源类型匹配 / unanswerable ─────────────────────


def test_multi_source_covered_accepted():
    evidence = build_evidence([
        _res_ev(0.8, rtype="herb", name="金银花"),
        _res_ev(0.8, rtype="literature", name="本草纲目"),
    ])
    d = _eval(evidence, MULTI_Q)
    assert d.decision == DECISION_ACCEPT
    assert d.details["covered_resource_types"] == ["herb", "literature"]


def test_multi_source_not_covered_retries():
    """多来源问题但资源类证据未覆盖任何指定类型 → retry。"""
    evidence = build_evidence([_res_ev(0.8, rtype="theory", name="阴阳五行")])
    d = _eval(evidence, MULTI_Q, strategy=BASELINE_STRATEGY)
    assert d.decision == DECISION_RETRY
    assert d.reason == "multi_source_not_covered"
    assert d.details["covered_resource_types"] == []


def test_multi_source_answered_by_documents_only_is_accepted():
    """多来源问题仅由 Document 证据回答：通用证据，不因"未覆盖资源类型"拒答。"""
    evidence = build_evidence([
        _doc_ev(0.8, doc_id="d1"), _doc_ev(0.75, doc_id="d2", name="文档B"),
    ])
    d = _eval(evidence, MULTI_Q)
    assert d.decision == DECISION_ACCEPT
    assert d.details["covered_resource_types"] == []


def test_resource_type_mismatch_retries():
    """问题要 herb，证据全是 prescription → 类型不匹配 → retry。"""
    evidence = build_evidence([_res_ev(0.85, rtype="prescription", name="银翘散")])
    d = _eval(evidence, HERB_Q, strategy=BASELINE_STRATEGY)
    assert d.decision == DECISION_RETRY
    assert d.reason == "resource_type_mismatch"
    assert d.details["resource_type_mismatch"] is True


def test_unanswerable_candidate_needs_stronger_evidence():
    analysis = analyze_query(UNANSWERABLE_Q)
    assert analysis.is_unanswerable_candidate is True

    # 单条强证据 + 单一来源：不足以支撑"可能超出范围"的问题
    weak = build_evidence([_doc_ev(0.9, doc_id="only-one")])
    d1 = _eval(weak, None, analysis=analysis, strategy=BASELINE_STRATEGY)
    assert d1.decision == DECISION_RETRY
    assert d1.reason == "unanswerable_candidate_weak_evidence"

    # 强证据 + 两个独立来源：通过
    strong = build_evidence([
        _doc_ev(0.9, doc_id="d1"), _doc_ev(0.85, doc_id="d2", name="文档B"),
    ])
    d2 = _eval(strong, None, analysis=analysis)
    assert d2.decision == DECISION_ACCEPT


# ── 15~17. retry 规则 ───────────────────────────────────────────────────────


def test_retry_strategy_selection_is_deterministic():
    """retry 策略选择：确定性映射、目标必在注册表中、且不等于当前策略。"""
    assert select_retry_strategy("baseline_hybrid") == "multi_source"
    assert select_retry_strategy("herb_focused") == "baseline_hybrid"
    assert select_retry_strategy("prescription_focused") == "baseline_hybrid"
    assert select_retry_strategy("multi_source") == "kg_enhanced"
    assert select_retry_strategy("kg_enhanced") == "multi_source"
    # 未知策略 → 视为 Baseline
    assert select_retry_strategy(None) == "multi_source"
    assert select_retry_strategy("no_such_strategy") == "multi_source"

    for current, target in (
        ("baseline_hybrid", "multi_source"),
        ("herb_focused", "baseline_hybrid"),
        ("multi_source", "kg_enhanced"),
    ):
        assert target != current
        assert target in STRATEGIES


def test_retry_at_most_once():
    """retry 只允许一次：allow_retry=False 时直接判不足（不产生循环）。"""
    evidence = build_evidence([_kg_ev(0.9, KG_RELATION_CONTAINS, 1, edge=1)])
    first = _eval(evidence, HERB_Q, strategy=BASELINE_STRATEGY)
    assert first.decision == DECISION_RETRY

    second = _eval(
        evidence, HERB_Q, allow_retry=False, strategy=first.retry_strategy
    )
    assert second.decision == DECISION_INSUFFICIENT
    assert "retry_exhausted" in second.reason
    assert second.retry_strategy is None


def test_retry_after_still_insufficient_is_insufficient():
    """retry 后证据仍不足 → insufficient（不是无限重试）。"""
    weak = build_evidence([
        _kg_ev(0.95, KG_RELATION_RELATED_TO, 1, edge=1,
               provenance=KG_PROVENANCE_SHARED_TAG),
    ])
    d = _eval(weak, HERB_Q, allow_retry=False, strategy="multi_source")
    assert d.decision == DECISION_INSUFFICIENT
    assert d.accepted_count == 0


# ── 18~19. 阶段十二 / 十三策略不受影响 ──────────────────────────────────────


def test_baseline_and_stage13_strategies_unchanged():
    """阶段十二/十三的七类策略默认值不变（Gate 不改策略参数）。"""
    assert set(STRATEGIES) >= {
        "baseline_hybrid", "herb_focused", "prescription_focused",
        "theory_focused", "literature_focused", "multi_source", "kg_enhanced",
    }
    baseline = resolve_retrieval_config(STRATEGIES[BASELINE_STRATEGY])
    # Baseline 不开启 KG、不做资源过滤（与阶段十二一致）
    assert baseline["kg_enabled"] is False
    assert baseline["resource_types"] == []
    # kg_enhanced 参数仍是阶段十三的值
    kg = resolve_retrieval_config(STRATEGIES["kg_enhanced"])
    assert kg["kg_enabled"] is True and kg["kg_max_hops"] == 2 and kg["kg_top_k"] == 5


# ── GateDecision 结构与安全兜底 ─────────────────────────────────────────────


def test_gate_decision_schema_and_to_dict():
    from src.api.routes.chat import GateDecisionOut

    d = _eval(build_evidence([_doc_ev(0.8)]), HERB_Q)
    payload = d.to_dict()
    assert set(payload) == set(
        GateDecision(
            decision=DECISION_ACCEPT, reason="r"
        ).to_dict()
    )
    for key in ("decision", "reason", "gate_version", "evidence_count",
                "accepted_count", "high_count", "medium_count",
                "source_kind_counts", "best_score", "is_valid",
                "fallback_reason"):
        assert key in payload, f"缺失字段 {key}"
    out = GateDecisionOut(**payload)
    assert out.decision == DECISION_ACCEPT
    assert out.gate_version == GATE_VERSION
    # 冻结语义：frozen dataclass 不可原地修改
    with pytest.raises(Exception):
        d.decision = DECISION_INSUFFICIENT


def test_gate_exception_falls_back_to_accept(monkeypatch):
    """Gate 自身异常 → 安全放行（accept + is_valid=False），不成为单点故障。"""
    import src.application.evidence_gate as gate_mod

    def boom(*_args, **_kwargs):
        raise RuntimeError("gate boom")

    monkeypatch.setattr(gate_mod, "_aggregate", boom)
    d = _eval(build_evidence([_doc_ev(0.8)]), HERB_Q)
    assert d.decision == DECISION_ACCEPT
    assert d.is_valid is False
    assert "gate_exception" in (d.fallback_reason or "")


def test_fallback_gate_decision_accepts():
    d = fallback_gate_decision("unit_test", 2)
    assert d.decision == DECISION_ACCEPT
    assert d.is_valid is False and d.fallback_reason == "unit_test"
    assert d.evidence_count == 2


def test_gate_config_for_snapshot():
    cfg = gate_config(True)
    assert cfg["gate_version"] == GATE_VERSION
    assert cfg["enabled"] is True
    assert "thresholds" in cfg and "retry_strategy_by_current" in cfg
    assert cfg["kg_relations"]["strong"] == [
        KG_RELATION_CONTAINS, KG_RELATION_RECORDS,
    ] or set(cfg["kg_relations"]["strong"]) == {
        KG_RELATION_CONTAINS, KG_RELATION_RECORDS,
    }
    assert gate_config(False)["enabled"] is False


# ── 20~21. /chat/ask 与 /chat/ask-stream 集成 ───────────────────────────────


@pytest.mark.skipif(not PG_AVAILABLE, reason="PostgreSQL 未启动")
def test_ask_returns_evidence_gate(client, monkeypatch):
    """/chat/ask：旧字段全在，新增 evidence_gate（accept）。"""
    import src.application.rag_service as rag_mod

    _patch_retrieve(monkeypatch, _mixed_hits())
    monkeypatch.setattr(rag_mod, "get_llm", lambda config: _CiteLLM())

    kb_id = _make_kb(client)
    data = client.post(
        "/api/v1/chat/ask", json={"question": MULTI_Q, "kb_ids": [kb_id]}
    ).json()

    for key in ("answer", "citations", "evidence", "evidence_groups",
                "evidence_summary", "query_analysis", "router_decision",
                "kg_evidence", "evidence_gate"):
        assert key in data, f"缺失字段 {key}"

    gate = data["evidence_gate"]
    assert gate is not None and gate["decision"] == DECISION_ACCEPT
    assert gate["gate_version"] == GATE_VERSION
    assert gate["evidence_count"] >= 1 and gate["accepted_count"] >= 1
    assert gate["is_valid"] is True and gate["retried"] is False
    # 混合证据（herb + literature）被正确计数
    assert set(gate["source_kind_counts"]) == {"resource"}
    assert data["answer"] and data["citations"]


@pytest.mark.skipif(not PG_AVAILABLE, reason="PostgreSQL 未启动")
def test_ask_stream_keeps_event_order_and_carries_gate(client, monkeypatch):
    """/chat/ask-stream：事件名与顺序不变，citations 事件携带 evidence_gate。"""
    _patch_retrieve(monkeypatch, _mixed_hits())

    kb_id = _make_kb(client)
    resp = client.post(
        "/api/v1/chat/ask-stream", json={"question": MULTI_Q, "kb_ids": [kb_id]}
    )
    assert resp.status_code == 200, resp.text
    events = _parse_sse(resp.text)
    names = [e for e, _ in events]
    assert names[0] == "start" and names[1] == "citations" and names[-1] == "done"

    citations_payload = events[1][1]
    assert citations_payload["citations"]
    assert "evidence_groups" in citations_payload
    assert "kg_evidence" in citations_payload
    assert citations_payload["evidence_gate"]["decision"] == DECISION_ACCEPT


@pytest.mark.skipif(not PG_AVAILABLE, reason="PostgreSQL 未启动")
def test_insufficient_gate_refuses_but_keeps_evidence(client, monkeypatch):
    """insufficient：拒答 + 保留证据/引用（禁止模型编造）。"""
    weak_kg = [
        {
            "id": "kg:e1", "doc_id": "kg:e1", "kb_id": None,
            "content": "金银花 相关 辛凉解表（共同标签：清热）", "page_num": None,
            "title_path": "金银花 → 相关 → 辛凉解表", "score": 0.9,
            "dense_score": 0.9, "source_kind": SOURCE_KIND_KG,
            "resource_type": "herb", "resource_id": str(uuid.uuid4()),
            "resource_name": "金银花", "doc_name": "金银花",
            "kg_relation": KG_RELATION_RELATED_TO, "kg_hop": 1,
            "kg_provenance": KG_PROVENANCE_SHARED_TAG,
        }
    ]
    _patch_retrieve(monkeypatch, weak_kg)

    kb_id = _make_kb(client)
    data = client.post(
        "/api/v1/chat/ask", json={"question": HERB_Q, "kb_ids": [kb_id]}
    ).json()

    assert data["answer"] == GATE_REFUSAL_ANSWER
    assert "根据现有资料，我无法回答该问题" in data["answer"]
    gate = data["evidence_gate"]
    assert gate["decision"] == DECISION_INSUFFICIENT
    assert gate["retried"] is True          # 已换策略重试一次
    assert gate["original_strategy"]        # 记录了原策略
    assert gate["retry_strategy"] and gate["retry_strategy"] != gate["original_strategy"]
    assert gate["accepted_count"] == 0 and gate["weak_count"] == 1
    # 证据/引用保留（用户仍能看到检索到的弱证据）
    assert data["citations"] and data["evidence"] == data["citations"]


@pytest.mark.skipif(not PG_AVAILABLE, reason="PostgreSQL 未启动")
def test_retry_executes_exactly_once(client, monkeypatch):
    """retry 最多一次：第 2 次检索拿到好证据即通过，不再继续重试。"""
    import src.application.rag_service as rag_mod

    calls = {"n": 0}
    weak_kg = [{
        "id": "kg:e1", "doc_id": "kg:e1", "content": "金银花 相关 辛凉解表",
        "page_num": None, "title_path": "金银花 → 相关 → 辛凉解表", "score": 0.9,
        "dense_score": 0.9, "source_kind": SOURCE_KIND_KG, "resource_type": "herb",
        "resource_id": str(uuid.uuid4()), "resource_name": "金银花",
        "doc_name": "金银花", "kg_relation": KG_RELATION_RELATED_TO, "kg_hop": 1,
        "kg_provenance": KG_PROVENANCE_SHARED_TAG,
    }]

    async def _retry_then_good(self, kb_ids, question, strategy=None,
                               resource_types=None, analysis=None):
        await RagService._ensure_components(self)
        calls["n"] += 1
        if calls["n"] == 1:
            return [dict(h) for h in weak_kg]
        return _mixed_hits()

    monkeypatch.setattr(RagService, "_retrieve", _retry_then_good)
    monkeypatch.setattr(rag_mod, "get_llm", lambda config: _CiteLLM())

    kb_id = _make_kb(client)
    data = client.post(
        "/api/v1/chat/ask", json={"question": GENERAL_Q, "kb_ids": [kb_id]}
    ).json()

    assert calls["n"] == 2, f"retry 应恰好执行一次，实际检索 {calls['n']} 次"
    gate = data["evidence_gate"]
    assert gate["decision"] == DECISION_ACCEPT
    assert gate["retried"] is True
    assert gate["original_strategy"] == BASELINE_STRATEGY
    assert gate["retry_strategy"] == "multi_source"
    assert data["answer"] != GATE_REFUSAL_ANSWER


@pytest.mark.skipif(not PG_AVAILABLE, reason="PostgreSQL 未启动")
def test_retry_still_insufficient_stops(client, monkeypatch):
    """retry 后仍不足 → insufficient，且不再继续重试（检索共 2 次）。"""
    calls = {"n": 0}
    weak_kg = [{
        "id": "kg:e1", "doc_id": "kg:e1", "content": "金银花 相关 辛凉解表",
        "page_num": None, "title_path": "金银花 → 相关 → 辛凉解表", "score": 0.9,
        "dense_score": 0.9, "source_kind": SOURCE_KIND_KG, "resource_type": "herb",
        "resource_id": str(uuid.uuid4()), "resource_name": "金银花",
        "doc_name": "金银花", "kg_relation": KG_RELATION_RELATED_TO, "kg_hop": 1,
        "kg_provenance": KG_PROVENANCE_SHARED_TAG,
    }]

    async def _always_weak(self, kb_ids, question, strategy=None,
                           resource_types=None, analysis=None):
        await RagService._ensure_components(self)
        calls["n"] += 1
        return [dict(h) for h in weak_kg]

    monkeypatch.setattr(RagService, "_retrieve", _always_weak)

    kb_id = _make_kb(client)
    data = client.post(
        "/api/v1/chat/ask", json={"question": GENERAL_Q, "kb_ids": [kb_id]}
    ).json()

    assert calls["n"] == 2
    gate = data["evidence_gate"]
    assert gate["decision"] == DECISION_INSUFFICIENT
    assert gate["retried"] is True
    assert data["answer"] == GATE_REFUSAL_ANSWER


@pytest.mark.skipif(not PG_AVAILABLE, reason="PostgreSQL 未启动")
def test_gate_disabled_keeps_stage13_behavior(client, monkeypatch):
    """Gate 关闭（EVIDENCE_GATE_ENABLED=False）：不拦截，回到阶段十三行为。"""
    import src.application.rag_service as rag_mod

    weak_kg = [{
        "id": "kg:e1", "doc_id": "kg:e1", "content": "金银花 相关 辛凉解表",
        "page_num": None, "title_path": "金银花 → 相关 → 辛凉解表", "score": 0.9,
        "dense_score": 0.9, "source_kind": SOURCE_KIND_KG, "resource_type": "herb",
        "resource_id": str(uuid.uuid4()), "resource_name": "金银花",
        "doc_name": "金银花", "kg_relation": KG_RELATION_RELATED_TO, "kg_hop": 1,
        "kg_provenance": KG_PROVENANCE_SHARED_TAG,
    }]
    _patch_retrieve(monkeypatch, weak_kg)
    monkeypatch.setattr(rag_mod, "get_llm", lambda config: _CiteLLM())
    # 注入关闭状态的 Gate（不改动任何策略参数）
    monkeypatch.setattr(
        rag_mod, "EvidenceGate", lambda *a, **k: EvidenceGate(enabled=False)
    )

    kb_id = _make_kb(client)
    data = client.post(
        "/api/v1/chat/ask", json={"question": HERB_Q, "kb_ids": [kb_id]}
    ).json()

    assert data["evidence_gate"] is None      # 未启用 → 不下发门控字段
    assert data["answer"] != GATE_REFUSAL_ANSWER
    assert data["citations"]                  # 弱证据照常进入引用（阶段十三行为）


# ── 22. Evaluation 记录 ─────────────────────────────────────────────────────


@pytest.mark.skipif(not PG_AVAILABLE, reason="PostgreSQL 未启动")
async def test_evaluation_records_gate_decision(client, eval_kb, monkeypatch):
    """EvaluationResult 归档 gate_decision / gate_version / retry_strategy。"""
    from sqlalchemy import select

    from src.application.evaluation_service import EvaluationService
    from src.domain.models import EvaluationResult

    weak_kg = [{
        "id": "kg:e1", "doc_id": "kg:e1", "content": "金银花 相关 辛凉解表",
        "page_num": None, "title_path": "金银花 → 相关 → 辛凉解表", "score": 0.9,
        "dense_score": 0.9, "source_kind": SOURCE_KIND_KG, "resource_type": "herb",
        "resource_id": str(uuid.uuid4()), "resource_name": "金银花",
        "doc_name": "金银花", "kg_relation": KG_RELATION_RELATED_TO, "kg_hop": 1,
        "kg_provenance": KG_PROVENANCE_SHARED_TAG,
    }]
    _patch_retrieve(monkeypatch, weak_kg)

    async with _session() as db:
        svc = EvaluationService(db)
        await svc.save_test_set(
            eval_kb,
            [_case(f"金银花-{uuid.uuid4().hex[:6]}", question_type="herb")],
            dataset_version="tcm-v1",
        )
        run, results = await svc.run(eval_kb, retrieval_strategy=BASELINE_STRATEGY)

        assert results[0].gate_decision == DECISION_INSUFFICIENT
        assert results[0].gate_version == GATE_VERSION
        assert results[0].retry_strategy == "multi_source"

        snapshot = run.config_snapshot
        assert snapshot["evidence_gate"]["enabled"] is True
        assert snapshot["evidence_gate"]["gate_version"] == GATE_VERSION
        assert snapshot["evidence_gate"]["decision_counts"] == {
            DECISION_INSUFFICIENT: 1
        }

        rows = (
            await db.scalars(
                select(EvaluationResult).where(
                    EvaluationResult.run_id == str(run.id)
                )
            )
        ).all()
        assert rows and rows[0].gate_decision == DECISION_INSUFFICIENT
        assert rows[0].gate_version == GATE_VERSION
        assert rows[0].retry_strategy == "multi_source"


@pytest.mark.skipif(not PG_AVAILABLE, reason="PostgreSQL 未启动")
async def test_evaluation_gate_off_records_none(client, eval_kb, monkeypatch):
    """use_evidence_gate=False：Gate 关闭，不记录决策（对照实验组）。"""
    from src.application.evaluation_service import EvaluationService

    weak_kg = [{
        "id": "kg:e1", "doc_id": "kg:e1", "content": "金银花 相关 辛凉解表",
        "page_num": None, "title_path": "金银花 → 相关 → 辛凉解表", "score": 0.9,
        "dense_score": 0.9, "source_kind": SOURCE_KIND_KG, "resource_type": "herb",
        "resource_id": str(uuid.uuid4()), "resource_name": "金银花",
        "doc_name": "金银花", "kg_relation": KG_RELATION_RELATED_TO, "kg_hop": 1,
        "kg_provenance": KG_PROVENANCE_SHARED_TAG,
    }]
    _patch_retrieve(monkeypatch, weak_kg)

    async with _session() as db:
        svc = EvaluationService(db)
        await svc.save_test_set(
            eval_kb,
            [_case(f"连翘-{uuid.uuid4().hex[:6]}", question_type="herb")],
            dataset_version="tcm-v1",
        )
        run, results = await svc.run(
            eval_kb,
            retrieval_strategy=BASELINE_STRATEGY,
            use_evidence_gate=False,
        )
        assert results[0].gate_decision is None
        assert results[0].gate_version is None
        assert run.config_snapshot["evidence_gate"]["enabled"] is False
        assert run.config_snapshot["evidence_gate"]["decision_counts"] == {}


@pytest.mark.skipif(not PG_AVAILABLE, reason="PostgreSQL 未启动")
async def test_evaluation_api_run_accepts_gate_switch(client, eval_kb, monkeypatch):
    """POST /evaluation/run 支持 use_evidence_gate，结果回传 Gate 字段。"""
    from src.application.evaluation_service import EvaluationService

    _patch_retrieve(monkeypatch, _mixed_hits())
    async with _session() as db:
        await EvaluationService(db).save_test_set(
            eval_kb,
            [_case(f"桂枝汤-{uuid.uuid4().hex[:6]}", question_type="prescription")],
            dataset_version="tcm-v1",
        )

    resp = client.post(
        "/api/v1/evaluation/run",
        json={
            "kb_id": str(eval_kb),
            "retrieval_strategy": BASELINE_STRATEGY,
            "experiment_name": "gate_probe",
            "use_evidence_gate": True,
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["results"]
    for r in body["results"]:
        assert "gate_decision" in r and "gate_version" in r
        assert "retry_strategy" in r
    assert body["results"][0]["gate_decision"] == DECISION_ACCEPT
