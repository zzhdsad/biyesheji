"""阶段十四：Evidence Gate（证据门控）专项测试。

覆盖（需求 §10）：
1   空 Evidence
2   单条高质量 Vector Evidence
3   低质量 Evidence
10  多源问题（覆盖 / 未覆盖）
11  resource_types 不匹配
12  unanswerable candidate
13  accept
14  insufficient
15  retry
16  retry 最多一次
17  retry 后仍不足
18  普通 baseline 策略行为不变
20  /chat/ask
21  /chat/ask-stream
22  Evaluation 记录（含 Gate 关闭对照）
23  异常情况下安全 fallback

注：原 4~9 节（KG 混合 / contains / records / related_to / 跳数 / 互补）
与 19 节（kg_enhanced 策略）随知识图谱功能下线一并移除。

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
from tests.test_dynamic_router import (  # 复用既有固定命中/LLM 桩与 SSE 解析
    _CiteLLM,
    _mixed_hits,
    _parse_sse,
    _patch_retrieve,
)
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
    assert select_retry_strategy("multi_source") == "baseline_hybrid"
    # 未知策略 → 视为 Baseline
    assert select_retry_strategy(None) == "multi_source"
    assert select_retry_strategy("no_such_strategy") == "multi_source"

    for current, target in (
        ("baseline_hybrid", "multi_source"),
        ("herb_focused", "baseline_hybrid"),
        ("multi_source", "baseline_hybrid"),
    ):
        assert target != current
        assert target in STRATEGIES


def test_retry_at_most_once():
    """retry 只允许一次：allow_retry=False 时直接判不足（不产生循环）。

    原 KG 强事实场景随 KG 下线移除；改为 unanswerable 问题 + 单来源
    文档证据（independent_sources < 2）触发 retry，机制验证不变。
    """
    evidence = build_evidence([_doc_ev(0.85)])
    first = _eval(evidence, UNANSWERABLE_Q, strategy=BASELINE_STRATEGY)
    assert first.decision == DECISION_RETRY

    second = _eval(
        evidence, UNANSWERABLE_Q, allow_retry=False, strategy=first.retry_strategy
    )
    assert second.decision == DECISION_INSUFFICIENT
    assert "retry_exhausted" in second.reason
    assert second.retry_strategy is None


def test_retry_after_still_insufficient_is_insufficient():
    """retry 后证据仍不足 → insufficient（不是无限重试）。"""
    # 原 KG 弱证据场景随 KG 下线移除；低分文档证据低于展示阈值，效果等价
    weak = build_evidence([_doc_ev(0.1)])
    d = _eval(weak, HERB_Q, allow_retry=False, strategy="multi_source")
    assert d.decision == DECISION_INSUFFICIENT
    assert d.accepted_count == 0


# ── 18. 阶段十二策略不受影响 ────────────────────────────────────────────────


def test_baseline_strategy_unchanged():
    """阶段十二的策略注册与 Baseline 默认值不变（Gate 不改策略参数）。

    原 kg_enhanced 策略断言随知识图谱功能下线移除。
    """
    assert set(STRATEGIES) >= {
        "baseline_hybrid", "herb_focused", "prescription_focused",
        "theory_focused", "literature_focused", "multi_source",
    }
    baseline = resolve_retrieval_config(STRATEGIES[BASELINE_STRATEGY])
    # Baseline 不做资源过滤（与阶段十二一致）
    assert baseline["resource_types"] == []


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
                "evidence_gate"):
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
    assert citations_payload["evidence_gate"]["decision"] == DECISION_ACCEPT


@pytest.mark.skipif(not PG_AVAILABLE, reason="PostgreSQL 未启动")
def test_insufficient_gate_refuses_without_citations(client, monkeypatch):
    """insufficient：拒答 + 不展示引用（禁止模型编造，也不展示无关证据）。

    原 KG 弱证据场景随 KG 下线移除；改为空证据（检索无命中）触发拒答。
    """
    _patch_retrieve(monkeypatch, [])

    kb_id = _make_kb(client)
    data = client.post(
        "/api/v1/chat/ask", json={"question": HERB_Q, "kb_ids": [kb_id]}
    ).json()

    assert data["answer"] == GATE_REFUSAL_ANSWER
    assert "根据现有资料，我无法回答该问题" in data["answer"]
    gate = data["evidence_gate"]
    assert gate["decision"] == DECISION_INSUFFICIENT
    assert gate["evidence_count"] == 0
    assert gate["accepted_count"] == 0
    # 拒答不展示引用（无证据可展示，引用只会误导用户）
    assert data["citations"] == [] and data["evidence"] == []


@pytest.mark.skipif(not PG_AVAILABLE, reason="PostgreSQL 未启动")
def test_retry_executes_exactly_once(client, monkeypatch):
    """retry 最多一次：第 2 次检索拿到好证据即通过，不再继续重试。

    原 KG 弱证据场景随 KG 下线移除；改为 unanswerable 问题 + 单来源文档
    证据（independent_sources < 2）触发 retry，机制验证不变。
    """
    import src.application.rag_service as rag_mod

    calls = {"n": 0}
    thin_doc = [{
        "id": "c-thin", "doc_id": "doc-thin", "content": "文档证据文本",
        "page_num": 1, "title_path": "第一章", "score": 0.85,
        "dense_score": 0.85, "source_kind": "document",
        "resource_type": None, "doc_name": "单薄文档",
    }]

    async def _retry_then_good(self, kb_ids, question, strategy=None,
                               resource_types=None, analysis=None):
        await RagService._ensure_components(self)
        calls["n"] += 1
        if calls["n"] == 1:
            return [dict(h) for h in thin_doc]
        return _mixed_hits()

    monkeypatch.setattr(RagService, "_retrieve", _retry_then_good)
    monkeypatch.setattr(rag_mod, "get_llm", lambda config: _CiteLLM())

    kb_id = _make_kb(client)
    data = client.post(
        "/api/v1/chat/ask", json={"question": UNANSWERABLE_Q, "kb_ids": [kb_id]}
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
    """retry 后仍不足 → insufficient，且不再继续重试（检索共 2 次）。

    原 KG 弱证据场景随 KG 下线移除；改为 unanswerable 问题 + 每次检索
    均只有单来源文档证据（independent_sources < 2），机制验证不变。
    """
    calls = {"n": 0}
    thin_doc = [{
        "id": "c-thin", "doc_id": "doc-thin", "content": "文档证据文本",
        "page_num": 1, "title_path": "第一章", "score": 0.85,
        "dense_score": 0.85, "source_kind": "document",
        "resource_type": None, "doc_name": "单薄文档",
    }]

    async def _always_thin(self, kb_ids, question, strategy=None,
                           resource_types=None, analysis=None):
        await RagService._ensure_components(self)
        calls["n"] += 1
        return [dict(h) for h in thin_doc]

    monkeypatch.setattr(RagService, "_retrieve", _always_thin)

    kb_id = _make_kb(client)
    data = client.post(
        "/api/v1/chat/ask", json={"question": UNANSWERABLE_Q, "kb_ids": [kb_id]}
    ).json()

    assert calls["n"] == 2
    gate = data["evidence_gate"]
    assert gate["decision"] == DECISION_INSUFFICIENT
    assert gate["retried"] is True
    assert data["answer"] == GATE_REFUSAL_ANSWER


@pytest.mark.skipif(not PG_AVAILABLE, reason="PostgreSQL 未启动")
def test_gate_disabled_keeps_stage13_behavior(client, monkeypatch):
    """Gate 关闭（EVIDENCE_GATE_ENABLED=False）：不拦截，检索结果照常返回。"""
    import src.application.rag_service as rag_mod

    _patch_retrieve(monkeypatch, _mixed_hits())
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


# 原「22. Evaluation 记录」2 个 EvaluationService 直连用例
# （test_evaluation_records_gate_decision / test_evaluation_gate_off_records_none）
# 随评估模块彻底删除下线；Evidence Gate 机制用例保留于上文各节。
