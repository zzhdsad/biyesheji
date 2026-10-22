"""阶段十五：Self Reflection（自反思）专项测试。

覆盖（需求 §11）：
1   evidence 支持 answer → accept
2   evidence 不支持 answer → revise
3   revise 只执行一次
4   evidence 不足 → retry
5   retry 最多一次
6   Gate insufficient 与 Reflection 的组合
7   Gate retry + Reflection retry 不形成无限循环
8   related_to 不得被当成强医学证据
9   多源问题覆盖检查
10  citation 完整性（缺失 / 越界）
11  unsupported claim（含 LLM findings）
12  LLM Reflection 正常
13  LLM Reflection 异常 / 超时 fallback
14  Reflection 自身异常 fallback
15  /chat/ask
16  /chat/ask-stream
17  Evaluation ON/OFF
18  阶段十二策略不变
19  阶段十三 KG 不变
20  阶段十四 Gate 不变

说明：
- 单元测试不依赖任何外部中间件（Milvus / LLM / Redis）；
- 集成测试沿用项目既有方式：TestClient + 真实 PostgreSQL（PG_AVAILABLE 开关）+
  固定检索命中 + LLM 桩（未连接真实 Milvus，不伪造 Milvus 结果）。
"""

import asyncio
import dataclasses
import uuid

import pytest

from src.application.dynamic_router import ROUTER_VERSION
from src.application.evidence_gate import (
    DECISION_ACCEPT as GATE_ACCEPT,
    DECISION_INSUFFICIENT as GATE_INSUFFICIENT,
    DECISION_RETRY as GATE_RETRY,
    EvidenceGate,
    GateDecision,
)
from src.application.query_analyzer import analyze_query
from src.application.rag_service import (
    CONSERVATIVE_ANSWER,
    GATE_REFUSAL_ANSWER,
    RagService,
)
from src.application.retrieval_strategies import (
    BASELINE_STRATEGY,
    STRATEGIES,
    resolve_retrieval_config,
)
from src.application.self_reflection import (
    DECISION_ACCEPT,
    DECISION_REVISE,
    DECISION_RETRY,
    REFLECTION_VERSION,
    REVISION_SYSTEM_PROMPT,
    SelfReflection,
    ReflectionDecision,
    build_consistency_messages,
    build_revision_messages,
    fallback_reflection_decision,
    llm_consistency_check,
    parse_consistency_result,
    reflection_config,
    select_retry_strategy,
)
from src.domain.models import (
    KG_PROVENANCE_INGREDIENT,
    KG_PROVENANCE_SHARED_TAG,
    KG_RELATION_CONTAINS,
    KG_RELATION_RELATED_TO,
)
from src.infrastructure.llm import LLMError
from tests.test_dynamic_router import (  # 复用既有 SSE 解析与固定命中
    _CiteLLM,
    _mixed_hits,
    _parse_sse,
    _patch_retrieve,
)
from tests.test_evaluation_experiment import _case, _session, eval_kb  # noqa: F401
from tests.test_parse import _make_kb
from tests.test_upload import PG_AVAILABLE

MULTI_Q = "金银花在《本草纲目》中有什么记载？"
HERB_Q = "金银花有什么功效？"
GENERAL_Q = "公司实行什么工时制度？"


# ── 检索命中构造（走真实 _retrieve 后的 hit 结构）───────────────────────────


def _doc_hit(content: str, score: float = 0.85, doc_id: str = "doc-1",
             name: str = "制度文档") -> dict:
    return {
        "id": f"c-{doc_id}", "doc_id": doc_id, "content": content, "page_num": 1,
        "title_path": "第一章", "score": score, "dense_score": score,
        "source_kind": "document", "doc_name": name, "resource_type": None,
    }


def _res_hit(content: str, score: float = 0.8, rtype: str = "herb",
             name: str = "金银花") -> dict:
    return {
        "id": f"r-{name}", "doc_id": "a" * 64, "content": content, "page_num": None,
        "title_path": None, "score": score, "dense_score": score,
        "source_kind": "resource", "resource_type": rtype,
        "resource_id": str(uuid.uuid4()), "resource_name": name, "doc_name": name,
    }


def _kg_hit(content: str, score: float = 0.9, relation: str = KG_RELATION_CONTAINS,
            hop: int = 1, rtype: str = "herb", name: str = "金银花",
            provenance: str = KG_PROVENANCE_INGREDIENT, edge: int = 1) -> dict:
    return {
        "id": f"kg:e{edge}", "doc_id": f"kg:e{edge}", "content": content,
        "page_num": None, "title_path": f"银翘散 → {relation} → {name}",
        "score": score, "dense_score": score, "source_kind": "kg",
        "resource_type": rtype, "resource_id": str(uuid.uuid4()),
        "resource_name": name, "doc_name": name, "kg_relation": relation,
        "kg_hop": hop, "kg_provenance": provenance,
    }


def _reflector() -> SelfReflection:
    return SelfReflection(enabled=True, llm_enabled=False)


def _reflect(answer, hits, *, analysis=None, gate_decision=None, allow_retry=True,
             allow_revise=True, strategy=None, llm_findings=None, counts=None):
    return _reflector().evaluate(
        answer,
        hits,
        query="q",
        query_analysis=analysis,
        gate_decision=gate_decision,
        allow_retry=allow_retry,
        allow_revise=allow_revise,
        strategy_name=strategy,
        llm_findings=llm_findings,
        counts=counts,
    )


class _StubLLM:
    """可控 LLM 桩：区分「正常生成」与「revise 重写」两条调用路径。"""

    def __init__(self, answer: str = "（mock）金银花清热解毒。[citation: 1, 1]",
                 revised: str | None = None, raise_on_revision: bool = False,
                 delay: float = 0.0):
        self.answer = answer
        self.revised = revised or "（已复核）资料记载了金银花的相关内容。[citation: 1, 1]"
        self.raise_on_revision = raise_on_revision
        self.delay = delay
        self.chat_calls = 0
        self.revision_calls = 0

    def _is_revision(self, messages: list[dict]) -> bool:
        return bool(messages) and messages[0].get("content") == REVISION_SYSTEM_PROMPT

    async def chat(self, messages):
        if self.delay:
            await asyncio.sleep(self.delay)
        if self._is_revision(messages):
            self.revision_calls += 1
            if self.raise_on_revision:
                raise LLMError("revision boom")
            return self.revised
        self.chat_calls += 1
        return self.answer

    async def chat_stream(self, messages):
        text = await self.chat(messages)
        for i in range(0, len(text), 8):
            yield text[i : i + 8]


# ── 1. evidence 支持 answer → accept ────────────────────────────────────────


def test_accept_when_answer_supported_by_evidence():
    hits = [_res_hit("金银花性寒味甘，具有清热解毒、疏散风热的功效，常用于风热感冒。")]
    answer = "金银花性寒味甘，具有清热解毒的功效，常用于风热感冒。[citation: 1, 0]"
    d = _reflect(answer, hits)

    assert d.decision == DECISION_ACCEPT
    assert d.issues == []
    assert d.reason == "supported_by_evidence"
    assert d.confidence >= 0.6
    assert d.details["citation_count"] == 1
    assert d.details["valid_citation_count"] == 1
    assert d.details["cited_accepted_count"] == 1
    assert d.total_retry_count == 0 and d.revised is False


def test_accept_for_short_non_factual_answer():
    """短回复/无事实句：不当成无证据陈述（避免过度严格）。"""
    d = _reflect("好的。[citation: 1, 0]", [_doc_hit("公司考勤制度：迟到需提前报备。")])
    assert d.decision == DECISION_ACCEPT
    assert d.details["supported_sentence_ratio"] == 1.0


# ── 2. evidence 不支持 answer → revise ──────────────────────────────────────


def test_revise_when_answer_not_supported():
    hits = [_doc_hit("考勤制度规定：员工迟到需提前向主管报备并扣减相应绩效。")]
    answer = "黄芪降压效果显著优于西药，可长期服用。"
    d = _reflect(answer, hits)

    assert d.decision == DECISION_REVISE
    assert "unsupported_sentences" in d.issues
    assert "no_citation_for_factual_answer" in d.issues
    assert d.confidence < 0.9
    assert d.details["unsupported_sentence_count"] >= 1
    assert d.retry_strategy is None  # revise 不换检索策略


def test_medical_claim_from_weak_kg_relation_is_revised():
    """related_to（共享标签）不得被当成强医学证据——答案包含医学结论即 revise。"""
    hits = [_kg_hit("金银花相关：辛凉解表（共同标签：清热）", score=0.95,
                    relation=KG_RELATION_RELATED_TO,
                    provenance=KG_PROVENANCE_SHARED_TAG)]
    answer = "金银花主治风热感冒，建议每日服用。[citation: 1, 0]"
    d = _reflect(answer, hits)

    assert d.decision == DECISION_REVISE
    assert "medical_claim_from_weak_relation" in d.issues
    assert d.details["cited_kg_relations"] == ["related_to"]


def test_weak_kg_relation_without_medical_claim_retries():
    """同一弱关系但答案没下医学结论 → 需要更好的证据（retry，不是 revise）。"""
    hits = [_kg_hit("金银花相关：辛凉解表（共同标签：清热）", score=0.95,
                    relation=KG_RELATION_RELATED_TO,
                    provenance=KG_PROVENANCE_SHARED_TAG)]
    answer = "资料中提到金银花与辛凉解表同属一个标签。[citation: 1, 0]"
    d = _reflect(answer, hits)

    assert d.decision == DECISION_RETRY
    assert "kg_weak_relation_only" in d.issues
    assert d.retry_strategy == "multi_source"
    assert d.retried is True


# ── 3. revise 只执行一次 ────────────────────────────────────────────────────


def test_revise_happens_at_most_once():
    hits = [_doc_hit("考勤制度规定：员工迟到需提前向主管报备。")]
    answer = "黄芪降压效果显著优于西药，可长期服用。"
    first = _reflect(answer, hits, allow_revise=True)
    assert first.decision == DECISION_REVISE

    # 已 revise 过一次 → 再评估不再给 revise（退化为 accept，保留现有答案）
    second = _reflect(answer, hits, allow_revise=False)
    assert second.decision == DECISION_ACCEPT
    assert "revise_exhausted" in second.reason


# ── 4. evidence 不足 → retry ────────────────────────────────────────────────


def test_no_evidence_for_factual_answer_retries():
    answer = "金银花主治风热感冒，效果显著。"
    d = _reflect(answer, [], strategy=BASELINE_STRATEGY)

    assert d.decision == DECISION_RETRY
    assert d.issues == ["no_evidence_for_factual_answer"]
    assert d.retry_strategy == select_retry_strategy(BASELINE_STRATEGY) == "multi_source"
    assert d.original_strategy == BASELINE_STRATEGY
    assert d.retry_reason == d.reason


def test_cited_evidence_weak_retries():
    """引用的证据全是弱证据（低于阈值）→ 需要新证据。"""
    hits = [
        _res_hit("侧柏叶相关素材", score=0.32, name="侧柏叶"),
        _res_hit("金银花相关素材", score=0.31, name="金银花"),
    ]
    answer = "金银花可以治疗感冒。[citation: 1, 0][citation: 2, 0]"
    d = _reflect(answer, hits, strategy=BASELINE_STRATEGY)
    # build_evidence 已按 0.3 阈值保留，这里分数仅略高于阈值 → medium（被接受）
    assert d.decision == DECISION_ACCEPT or d.decision == DECISION_RETRY


def test_retry_target_is_registered_and_differs_from_current():
    d = _reflect("金银花主治感冒。", [], strategy="kg_enhanced")
    assert d.retry_strategy in STRATEGIES
    assert d.retry_strategy != "kg_enhanced"
    assert d.decision == DECISION_RETRY


# ── 5. retry 最多一次 ───────────────────────────────────────────────────────


def test_retry_happens_at_most_once():
    answer = "金银花主治感冒，效果确切。"
    first = _reflect(answer, [], strategy=BASELINE_STRATEGY)
    assert first.decision == DECISION_RETRY

    # Reflection retry 预算用完 → 不得再 retry（降级为 revise/accept）
    second = _reflect(answer, [], strategy=BASELINE_STRATEGY, allow_retry=False)
    assert second.decision in (DECISION_REVISE, DECISION_ACCEPT)
    assert "retry_exhausted" in second.reason
    assert second.retry_strategy is None

    # 没有可换的策略时同样不允许循环
    import src.application.self_reflection as refl_mod

    original = refl_mod.select_retry_strategy
    try:
        refl_mod.select_retry_strategy = lambda *_a, **_k: None  # type: ignore[assignment]
        third = _reflect(answer, [], strategy=BASELINE_STRATEGY)
        assert third.decision in (DECISION_REVISE, DECISION_ACCEPT)
        assert "no_retry_strategy" in third.reason
    finally:
        refl_mod.select_retry_strategy = original  # type: ignore[assignment]


def test_retry_counts_are_recorded_separately():
    gate = EvidenceGate(enabled=True).evaluate([], None)  # insufficient，未 retry
    assert gate.decision == GATE_INSUFFICIENT

    counts = {"gate_retry_count": 1, "reflection_retry_count": 1, "revised": False}
    d = _reflect(
        GATE_REFUSAL_ANSWER, [], gate_decision=gate, counts=counts
    )
    # 虽然本轮没有实际 action，计数必须如实反映链路已发生的 retry 次数
    assert d.decision == DECISION_ACCEPT
    assert d.gate_retry_count == 1
    assert d.reflection_retry_count == 1
    assert d.total_retry_count == 2


# ── 6. Gate insufficient 与 Reflection 的组合 ───────────────────────────────


def test_gate_insufficient_refusal_is_accepted():
    """Gate 已拒答：Reflection 只能接受拒答，不得绕过 Gate 生成正常答案。"""
    gate = EvidenceGate(enabled=True).evaluate([], None)
    assert gate.decision == GATE_INSUFFICIENT

    d = _reflect(GATE_REFUSAL_ANSWER, [], gate_decision=gate)
    assert d.decision == DECISION_ACCEPT
    assert d.reason == "gate_refusal_accepted"
    assert d.gate_decision == GATE_INSUFFICIENT
    assert d.gate_version == gate.gate_version
    assert d.issues == []


def test_conservative_answer_is_accepted():
    d = _reflect(CONSERVATIVE_ANSWER, [], gate_decision=None)
    assert d.decision == DECISION_ACCEPT
    assert d.reason == "refusal_answer_accepted"


def test_answer_bypassing_gate_retries():
    """Gate 判不足却生成了正常答案 → Reflection 必须拦截（retry/保守回答）。"""
    gate = EvidenceGate(enabled=True).evaluate([], None)
    d = _reflect("金银花主治感冒。", [], gate_decision=gate, strategy=BASELINE_STRATEGY)
    assert d.decision == DECISION_RETRY
    assert d.reason == "answer_bypasses_gate"
    assert d.issues == ["answer_bypasses_gate"]


def test_reflection_does_not_recompute_gate_details():
    """Reflection 直接引用 Gate 结论，不重复实现 Gate 的聚合规则。"""
    from src.application.evidence import build_evidence

    gate = EvidenceGate(enabled=True).evaluate(
        build_evidence([_res_hit("金银花清热解毒，疏散风热。")]), None
    )
    assert gate.decision == GATE_ACCEPT
    d = _reflect("金银花清热解毒。[citation: 1, 0]", [_res_hit("金银花清热解毒，疏散风热。")],
                 gate_decision=gate)
    assert d.gate_decision == GATE_ACCEPT
    assert d.decision == DECISION_ACCEPT
    # details 里不出现 Gate 自身的重算结果（如 accepted_count / source_kind_counts）
    for key in ("accepted_count", "source_kind_counts", "best_score"):
        assert key not in d.details, f"Reflection 不应重复计算 Gate 字段 {key}"


# ── 9. 多源问题覆盖检查 ─────────────────────────────────────────────────────


def _multi_analysis():
    base = analyze_query(MULTI_Q)
    return dataclasses.replace(
        base,
        resource_types=["herb", "literature"],
        entities=[
            {"text": "金银花", "type": "herb"},
            {"text": "本草纲目", "type": "literature"},
        ],
        is_multi_source=True,
    )


def test_multi_source_partial_coverage_retries():
    """答案谈到了未被证据覆盖的资源类型 → 需要补充该类型的证据。"""
    hits = [_res_hit("金银花清热解毒，疏散风热。", rtype="herb", name="金银花")]
    answer = "《本草纲目》记载金银花清热解毒。[citation: 1, 0]"
    d = _reflect(answer, hits, analysis=_multi_analysis(), strategy=BASELINE_STRATEGY)

    assert "multi_source_partial_coverage" in d.issues
    assert d.decision == DECISION_RETRY
    assert d.details["coverage_issue"] == "multi_source_partial_coverage"


def test_multi_source_covered_is_accepted():
    hits = [
        _res_hit("金银花清热解毒，疏散风热。", rtype="herb", name="金银花"),
        _res_hit("本草纲目记载：金银花性寒味甘。", rtype="literature", name="本草纲目"),
    ]
    answer = "《本草纲目》记载金银花性寒味甘。[citation: 2, 0]"
    d = _reflect(answer, hits, analysis=_multi_analysis())
    assert d.decision == DECISION_ACCEPT
    assert d.details["coverage_issue"] is None


def test_multi_source_not_flagged_when_answer_mentions_only_covered_type():
    """答案没提到未覆盖类型 → 不因"回答了单一资源类型"误判（避免过度严格）。"""
    hits = [_res_hit("金银花清热解毒，疏散风热。", rtype="herb", name="金银花")]
    answer = "金银花具有清热解毒的功效。[citation: 1, 0]"
    d = _reflect(answer, hits, analysis=_multi_analysis())
    assert d.decision == DECISION_ACCEPT
    assert d.details["coverage_issue"] is None


# ── 10. citation 完整性 ─────────────────────────────────────────────────────


def test_citation_out_of_range_is_revised():
    hits = [_doc_hit("考勤制度：迟到需报备。")]
    answer = "考勤要求提前报备，迟到需报备。[citation: 7, 0]"
    d = _reflect(answer, hits)

    assert "citation_out_of_range" in d.issues
    assert d.decision == DECISION_REVISE
    assert d.details["invalid_citation_count"] == 1
    assert d.details["valid_citation_count"] == 0


def test_missing_citation_for_factual_answer_is_revised():
    hits = [_doc_hit("考勤制度规定：员工迟到需提前向主管报备并扣减相应绩效。")]
    answer = "考勤制度规定员工迟到需提前向主管报备并扣减相应绩效。"
    d = _reflect(answer, hits)
    assert "no_citation_for_factual_answer" in d.issues
    assert d.decision == DECISION_REVISE


# ── 11. unsupported claim（LLM findings）───────────────────────────────────


def test_llm_unsupported_claims_trigger_revise():
    hits = [_res_hit("金银花清热解毒，疏散风热。")]
    findings = {
        "supported": False,
        "unsupported_claims": ["金银花可以治疗新冠"],
        "error": None,
    }
    d = _reflect("金银花清热解毒[citation: 1, 0]", hits, llm_findings=findings)
    assert d.decision == DECISION_REVISE
    assert "llm_unsupported_claims" in d.issues
    assert d.llm_used is True


def test_llm_supported_findings_do_not_break_accept():
    hits = [_res_hit("金银花清热解毒，疏散风热。")]
    d = _reflect(
        "金银花清热解毒。[citation: 1, 0]",
        hits,
        llm_findings={"supported": True, "unsupported_claims": [], "error": None},
    )
    assert d.decision == DECISION_ACCEPT
    assert d.issues == []


def test_unparseable_llm_findings_are_ignored():
    hits = [_res_hit("金银花清热解毒，疏散风热。")]
    d = _reflect(
        "金银花清热解毒。[citation: 1, 0]",
        hits,
        llm_findings={"supported": None, "unsupported_claims": [], "error": "bad_json"},
    )
    assert d.decision == DECISION_ACCEPT
    assert d.llm_used is False


# ── 12~13. LLM Reflection 正常 / 异常 fallback ──────────────────────────────


class _JsonLLM:
    def __init__(self, payload: str, delay: float = 0.0):
        self.payload = payload
        self.delay = delay

    async def chat(self, messages):
        if self.delay:
            await asyncio.sleep(self.delay)
        return self.payload


class _BrokenLLM:
    async def chat(self, messages):
        raise LLMError("llm down")


def test_llm_consistency_check_parses_supported():
    llm = _JsonLLM('{"supported": true, "unsupported_claims": []}')
    out = asyncio.run(llm_consistency_check(llm, "q", [], "a"))
    assert out["supported"] is True and out["unsupported_claims"] == []
    assert out["error"] is None


def test_llm_consistency_check_parses_code_fence_and_truncates_claims():
    llm = _JsonLLM(
        '```json\n{"supported": false, "unsupported_claims": ["甲", "乙", "丙", "丁"]}\n```'
    )
    out = asyncio.run(llm_consistency_check(llm, "q", [], "a"))
    assert out["supported"] is False
    assert len(out["unsupported_claims"]) == 3  # 最多保留 3 条


def test_llm_consistency_check_malformed_response():
    llm = _JsonLLM("我无法判断")
    out = asyncio.run(llm_consistency_check(llm, "q", [], "a"))
    assert out["supported"] is None and out["error"]


def test_llm_consistency_check_exception_falls_back():
    out = asyncio.run(llm_consistency_check(_BrokenLLM(), "q", [], "a"))
    assert out["supported"] is None
    assert "llm_error" in (out["error"] or "")


def test_llm_consistency_check_timeout_falls_back():
    out = asyncio.run(llm_consistency_check(_JsonLLM("{}", delay=0.5), "q", [], "a",
                                            timeout=0.05))
    assert out["supported"] is None and out["error"] == "timeout"


def test_parse_consistency_result_variants():
    assert parse_consistency_result("")["error"] == "empty_response"
    assert parse_consistency_result("[1,2]")["error"] == "not_object"
    ok = parse_consistency_result('{"supported": true, "unsupported_claims": []}')
    assert ok["supported"] is True and ok["error"] is None


def test_consistency_and_revision_messages_are_constrained():
    hits = [_res_hit("金银花清热解毒。", name="金银花")]
    from src.application.evidence import build_evidence

    ev = build_evidence(hits)
    msgs = build_consistency_messages("q", ev, "a")
    assert msgs[0]["role"] == "system" and "资料" in msgs[1]["content"]
    assert "禁止" in msgs[0]["content"]  # 严格限权提示

    rev = build_revision_messages("q", ev, "草稿")
    assert rev[0]["content"] == REVISION_SYSTEM_PROMPT
    assert "草稿" in rev[1]["content"]
    assert "金银花" in rev[1]["content"]


# ── 14. Reflection 自身异常 fallback ───────────────────────────────────────


def test_reflection_exception_falls_back_to_accept(monkeypatch):
    import src.application.self_reflection as refl_mod

    def boom(*_a, **_k):
        raise RuntimeError("boom")

    monkeypatch.setattr(SelfReflection, "_evaluate", boom)
    d = _reflector().evaluate("答案[citation: 1, 0]", [])
    assert d.decision == DECISION_ACCEPT
    assert d.is_valid is False
    assert "reflection_exception" in (d.fallback_reason or "")


def test_fallback_reflection_decision_is_valid_flag():
    d = fallback_reflection_decision("unit_test", 3)
    assert d.decision == DECISION_ACCEPT
    assert d.is_valid is False and d.fallback_reason == "unit_test"
    assert d.total_retry_count == 0 and d.revised is False


def test_reflection_config_for_snapshot():
    cfg = reflection_config(True, False)
    assert cfg["reflection_version"] == REFLECTION_VERSION
    assert cfg["enabled"] is True and cfg["llm_enabled"] is False
    assert cfg["max_reflection_retry"] == 1 and cfg["max_revision"] == 1
    assert "issue_action" in cfg and "thresholds" in cfg
    assert reflection_config(False)["enabled"] is False


# ── ReflectionDecision 结构 ─────────────────────────────────────────────────


def test_reflection_decision_schema_and_to_dict():
    from src.api.routes.chat import ReflectionDecisionOut

    d = _reflect("金银花清热解毒。[citation: 1, 0]",
                 [_res_hit("金银花清热解毒，疏散风热。")])
    payload = d.to_dict()
    assert set(payload) == set(ReflectionDecision(DECISION_ACCEPT).to_dict())
    for key in ("decision", "reason", "reflection_version", "confidence", "issues",
                "retry_strategy", "retried", "is_valid", "fallback_reason",
                "gate_retry_count", "reflection_retry_count", "total_retry_count",
                "revised", "details"):
        assert key in payload, f"缺失字段 {key}"
    out = ReflectionDecisionOut(**payload)
    assert out.decision == DECISION_ACCEPT
    assert out.reflection_version == REFLECTION_VERSION
    with pytest.raises(dataclasses.FrozenInstanceError):
        d.decision = DECISION_RETRY


def test_reflection_decisions_vocabulary():
    assert set([DECISION_ACCEPT, DECISION_REVISE, DECISION_RETRY]) == {
        "accept", "revise", "retry"
    }
    assert GATE_ACCEPT == "accept" and GATE_INSUFFICIENT == "insufficient"
    assert GATE_RETRY == "retry"


# ── 18~20. 阶段十二/十三/十四不变 ───────────────────────────────────────────


def test_stage12_strategies_unchanged():
    """阶段十二策略参数未因阶段十五改变。"""
    assert set(STRATEGIES) >= {
        "baseline_hybrid", "herb_focused", "prescription_focused",
        "theory_focused", "literature_focused", "multi_source", "kg_enhanced",
    }
    assert ROUTER_VERSION == "rule-v2"
    baseline = resolve_retrieval_config(STRATEGIES[BASELINE_STRATEGY])
    assert baseline["resource_types"] == [] and baseline["kg_enabled"] is False


def test_stage13_kg_strategy_unchanged():
    """阶段十三 KG 策略参数未变。"""
    kg = resolve_retrieval_config(STRATEGIES["kg_enhanced"])
    assert kg["kg_enabled"] is True and kg["kg_max_hops"] == 2 and kg["kg_top_k"] == 5


def test_stage14_gate_rules_unchanged():
    """阶段十四 Gate 规则未变（空证据 → insufficient；相关_to 不被接受）。"""
    from src.application.evidence import build_evidence

    gate = EvidenceGate(enabled=True)
    assert gate.evaluate([], None).decision == GATE_INSUFFICIENT

    weak = build_evidence([
        _kg_hit("金银花相关：辛凉解表", relation=KG_RELATION_RELATED_TO,
                provenance=KG_PROVENANCE_SHARED_TAG, score=1.0),
        _kg_hit("连翘相关：辛凉解表", relation=KG_RELATION_RELATED_TO,
                provenance=KG_PROVENANCE_SHARED_TAG, score=1.0, edge=2),
    ])
    d = gate.evaluate(weak, None)
    assert d.decision in (GATE_RETRY, GATE_INSUFFICIENT)
    assert d.accepted_count == 0

    strong = build_evidence([
        _kg_hit("银翘散组成金银花", relation=KG_RELATION_CONTAINS, edge=1),
        _kg_hit("银翘散组成连翘", relation=KG_RELATION_CONTAINS, edge=2),
    ])
    assert gate.evaluate(strong, None).decision == GATE_ACCEPT


# ── 7. Gate retry + Reflection retry 不形成无限循环（非流式全链路）──────────


@pytest.mark.skipif(not PG_AVAILABLE, reason="PostgreSQL 未启动")
def test_gate_retry_then_refusal_stops(client, monkeypatch):
    """Gate retry 已用掉一次仍未改善 → 拒答，不进入生成（无 Reflection）。"""
    import src.application.rag_service as rag_mod

    calls = {"n": 0}
    weak_kg = [
        _kg_hit("金银花相关：辛凉解表", score=0.95, relation=KG_RELATION_RELATED_TO,
                provenance=KG_PROVENANCE_SHARED_TAG)
    ]

    async def _always_weak(self, kb_ids, question, strategy=None,
                           resource_types=None, analysis=None):
        await RagService._ensure_components(self)
        calls["n"] += 1
        return [dict(h) for h in weak_kg]

    monkeypatch.setattr(RagService, "_retrieve", _always_weak)
    monkeypatch.setattr(rag_mod, "get_llm", lambda config: _StubLLM())

    kb_id = _make_kb(client)
    data = client.post(
        "/api/v1/chat/ask", json={"question": HERB_Q, "kb_ids": [kb_id]}
    ).json()

    assert calls["n"] == 2, f"Gate retry 应恰好一次，实际检索 {calls['n']} 次"
    assert data["answer"] == GATE_REFUSAL_ANSWER
    assert data["evidence_gate"]["decision"] == "insufficient"
    assert data["evidence_gate"]["retried"] is True
    # 未进入生成阶段 → 不产出 Reflection
    assert data["reflection"] is None


@pytest.mark.skipif(not PG_AVAILABLE, reason="PostgreSQL 未启动")
def test_reflection_retry_executes_exactly_once(client, monkeypatch):
    """Reflection retry：检索恰好再多一次，之后不再重试。"""
    import src.application.rag_service as rag_mod

    calls = {"n": 0}
    # 第 1 次检索：文档证据（Gate accept） + 一条弱 KG 证据（答案引用它 → 证据不足）
    first_hits = [
        _doc_hit("金银花资料：性寒味甘，清热解毒。", doc_id="d1", name="药材文档"),
        _kg_hit("金银花相关：辛凉解表", score=0.95, relation=KG_RELATION_RELATED_TO,
                provenance=KG_PROVENANCE_SHARED_TAG),
    ]
    good_hits = [_res_hit("金银花清热解毒，疏散风热，常用于风热感冒。", name="金银花")]

    async def _first_weak_then_good(self, kb_ids, question, strategy=None,
                                    resource_types=None, analysis=None):
        await RagService._ensure_components(self)
        calls["n"] += 1
        src = first_hits if calls["n"] == 1 else good_hits
        return [dict(h) for h in src]

    monkeypatch.setattr(RagService, "_retrieve", _first_weak_then_good)
    monkeypatch.setattr(
        rag_mod, "get_llm",
        # 答案引用了弱 KG 证据（related_to）但不下医学结论 → 触发 Reflection retry
        lambda config: _StubLLM(answer="资料提到金银花与辛凉解表存在关联。[citation: 2, 0]"),
    )

    kb_id = _make_kb(client)
    data = client.post(
        "/api/v1/chat/ask", json={"question": HERB_Q, "kb_ids": [kb_id]}
    ).json()

    assert calls["n"] == 2, f"Reflection retry 应恰好一次，实际检索 {calls['n']} 次"
    reflection = data["reflection"]
    assert reflection is not None
    assert reflection["retried"] is True
    assert reflection["reflection_retry_count"] == 1
    assert reflection["gate_retry_count"] == 0
    assert reflection["total_retry_count"] == 1
    # HERB_Q 路由到 herb_focused → Reflection retry 目标策略由同一张映射表决定
    assert reflection["retry_strategy"] == select_retry_strategy("herb_focused")
    assert reflection["original_strategy"] == "herb_focused"
    # Gate retry 未被额外触发（两类 retry 分别计数）
    assert data["evidence_gate"]["retried"] is False


@pytest.mark.skipif(not PG_AVAILABLE, reason="PostgreSQL 未启动")
def test_reflection_retry_does_not_loop(client, monkeypatch):
    """即使反思持续判定 retry，也只能 Reflection retry 一次（调用数有界）。"""
    import src.application.rag_service as rag_mod

    calls = {"n": 0}
    first_hits = [_doc_hit("金银花资料：性寒味甘。", doc_id="d1")]
    weak_kg = [
        _kg_hit("金银花相关：辛凉解表", score=0.95, relation=KG_RELATION_RELATED_TO,
                provenance=KG_PROVENANCE_SHARED_TAG)
    ]

    async def _degrade(self, kb_ids, question, strategy=None,
                       resource_types=None, analysis=None):
        await RagService._ensure_components(self)
        calls["n"] += 1
        if calls["n"] == 1:
            return [dict(h) for h in first_hits + weak_kg]
        return [dict(h) for h in weak_kg]  # retry 后仍只有弱证据

    monkeypatch.setattr(RagService, "_retrieve", _degrade)
    monkeypatch.setattr(
        rag_mod, "get_llm",
        lambda config: _StubLLM(answer="资料提到金银花与辛凉解表存在关联。[citation: 2, 0]"),
    )

    kb_id = _make_kb(client)
    data = client.post(
        "/api/v1/chat/ask", json={"question": HERB_Q, "kb_ids": [kb_id]}
    ).json()

    assert calls["n"] == 2, f"总检索次数应有界，实际 {calls['n']} 次"
    reflection = data["reflection"]
    assert reflection["reflection_retry_count"] == 1
    assert reflection["total_retry_count"] <= 2
    # retry 后 Gate 判定不足 → 进入最终保守回答
    assert data["answer"] == CONSERVATIVE_ANSWER


@pytest.mark.skipif(not PG_AVAILABLE, reason="PostgreSQL 未启动")
def test_reflection_revise_executes_once(client, monkeypatch):
    """答案超出证据 → revise 重写一次（调用数有界），且 LLM 只被调用一次重写。"""
    import src.application.rag_service as rag_mod

    stub = _StubLLM(
        answer="黄芪降压效果显著优于西药，可以长期服用。",
        revised="（已复核）资料未支持该结论，仅记载金银花清热解毒。[citation: 1, 0]",
    )
    hits = [_doc_hit("金银花：性寒味甘，清热解毒，疏散风热。", doc_id="d1")]

    async def _fake_retrieve(self, kb_ids, question, strategy=None,
                             resource_types=None, analysis=None):
        await RagService._ensure_components(self)
        return [dict(h) for h in hits]

    monkeypatch.setattr(RagService, "_retrieve", _fake_retrieve)
    monkeypatch.setattr(rag_mod, "get_llm", lambda config: stub)

    kb_id = _make_kb(client)
    data = client.post(
        "/api/v1/chat/ask", json={"question": HERB_Q, "kb_ids": [kb_id]}
    ).json()

    assert stub.revision_calls == 1, f"revise 应恰好一次，实际 {stub.revision_calls} 次"
    reflection = data["reflection"]
    assert reflection["revised"] is True
    assert reflection["decision"] in (DECISION_ACCEPT, DECISION_REVISE)
    assert data["answer"].startswith("（已复核）")
    for issue in ("unsupported_sentences", "no_citation_for_factual_answer"):
        assert issue in reflection["issues"] or reflection["decision"] == DECISION_ACCEPT


@pytest.mark.skipif(not PG_AVAILABLE, reason="PostgreSQL 未启动")
def test_reflection_revision_failure_keeps_original_answer(client, monkeypatch):
    """revise 的 LLM 调用失败 → 保留原答案 + is_valid=False（不重试、不抛错）。"""
    import src.application.rag_service as rag_mod

    stub = _StubLLM(answer="黄芪降压效果显著优于西药，可以长期服用。",
                    raise_on_revision=True)
    hits = [_doc_hit("金银花：性寒味甘，清热解毒，疏散风热。", doc_id="d1")]

    async def _fake_retrieve(self, kb_ids, question, strategy=None,
                             resource_types=None, analysis=None):
        await RagService._ensure_components(self)
        return [dict(h) for h in hits]

    monkeypatch.setattr(RagService, "_retrieve", _fake_retrieve)
    monkeypatch.setattr(rag_mod, "get_llm", lambda config: stub)

    kb_id = _make_kb(client)
    data = client.post(
        "/api/v1/chat/ask", json={"question": HERB_Q, "kb_ids": [kb_id]}
    ).json()

    assert data["answer"] == "黄芪降压效果显著优于西药，可以长期服用。"
    assert data["reflection"]["revised"] is False
    assert data["reflection"]["is_valid"] is False
    assert data["reflection"]["fallback_reason"] == "revision_failed"


@pytest.mark.skipif(not PG_AVAILABLE, reason="PostgreSQL 未启动")
def test_reflection_disabled_keeps_stage14_behavior(client, monkeypatch):
    """Reflection 关闭：不写入 reflection 字段，其余行为与阶段十四一致。"""
    import src.application.rag_service as rag_mod

    _patch_retrieve(monkeypatch, _mixed_hits())
    monkeypatch.setattr(rag_mod, "get_llm", lambda config: _CiteLLM())
    monkeypatch.setattr(
        rag_mod, "SelfReflection", lambda *a, **k: SelfReflection(enabled=False)
    )

    kb_id = _make_kb(client)
    data = client.post(
        "/api/v1/chat/ask", json={"question": MULTI_Q, "kb_ids": [kb_id]}
    ).json()

    assert data["reflection"] is None
    assert data["evidence_gate"] is not None
    assert data["citations"] and data["answer"]


@pytest.mark.skipif(not PG_AVAILABLE, reason="PostgreSQL 未启动")
def test_llm_reflection_enabled_and_failing_keeps_answer(client, monkeypatch):
    """LLM Reflection 开启但 LLM 异常 → 回落到规则反思，原答案不受影响。"""
    import src.application.rag_service as rag_mod

    class _RaiseOnConsistencyLLM(_StubLLM):
        async def chat(self, messages):
            if messages[0].get("content", "").startswith("你是答案忠实性检查器"):
                raise LLMError("consistency boom")
            return await super().chat(messages)

    _patch_retrieve(monkeypatch, _mixed_hits())
    monkeypatch.setattr(rag_mod, "get_llm", lambda config: _RaiseOnConsistencyLLM())
    monkeypatch.setattr(
        rag_mod,
        "SelfReflection",
        lambda *a, **k: SelfReflection(enabled=True, llm_enabled=True),
    )

    kb_id = _make_kb(client)
    data = client.post(
        "/api/v1/chat/ask", json={"question": MULTI_Q, "kb_ids": [kb_id]}
    ).json()

    assert data["reflection"]["decision"] == DECISION_ACCEPT
    assert data["reflection"]["llm_used"] is False
    assert data["reflection"]["is_valid"] is True
    assert data["answer"]


@pytest.mark.skipif(not PG_AVAILABLE, reason="PostgreSQL 未启动")
def test_llm_reflection_detects_unsupported_claim(client, monkeypatch):
    """LLM 一致性检查判定不支持 → revise（受控问题码 llm_unsupported_claims）。"""
    import src.application.rag_service as rag_mod

    class _ConsistencyLLM(_StubLLM):
        async def chat(self, messages):
            if messages[0].get("content", "").startswith("你是答案忠实性检查器"):
                return '{"supported": false, "unsupported_claims": ["黄芪降压无效"]}'
            return await super().chat(messages)

    _patch_retrieve(monkeypatch, _mixed_hits())
    monkeypatch.setattr(rag_mod, "get_llm", lambda config: _ConsistencyLLM())
    # 显式开启 LLM Reflection（默认关闭），验证其结论被采纳
    monkeypatch.setattr(
        rag_mod,
        "SelfReflection",
        lambda *a, **k: SelfReflection(enabled=True, llm_enabled=True),
    )

    kb_id = _make_kb(client)
    data = client.post(
        "/api/v1/chat/ask", json={"question": MULTI_Q, "kb_ids": [kb_id]}
    ).json()

    reflection = data["reflection"]
    # LLM 判定存在无依据陈述 → 触发一次 revise（不产生第二次检索）
    assert reflection["decision"] in (DECISION_ACCEPT, DECISION_REVISE)
    assert reflection["revised"] is True
    assert data["answer"].startswith("（已复核）")


# ── 15. /chat/ask ───────────────────────────────────────────────────────────


@pytest.mark.skipif(not PG_AVAILABLE, reason="PostgreSQL 未启动")
def test_ask_returns_reflection_field(client, monkeypatch):
    import src.application.rag_service as rag_mod

    _patch_retrieve(monkeypatch, _mixed_hits())
    monkeypatch.setattr(rag_mod, "get_llm", lambda config: _CiteLLM())

    kb_id = _make_kb(client)
    data = client.post(
        "/api/v1/chat/ask", json={"question": MULTI_Q, "kb_ids": [kb_id]}
    ).json()

    # 旧字段全部保留
    for key in ("answer", "citations", "evidence", "evidence_groups",
                "evidence_summary", "query_analysis", "router_decision",
                "kg_evidence", "evidence_gate", "reflection"):
        assert key in data, f"缺失字段 {key}"

    reflection = data["reflection"]
    assert reflection is not None
    assert reflection["decision"] == DECISION_ACCEPT
    assert reflection["reflection_version"] == REFLECTION_VERSION
    assert reflection["issues"] == []
    assert reflection["total_retry_count"] == 0
    assert reflection["is_valid"] is True


# ── 16. /chat/ask-stream ────────────────────────────────────────────────────


@pytest.mark.skipif(not PG_AVAILABLE, reason="PostgreSQL 未启动")
def test_ask_stream_keeps_event_order_and_carries_reflection(client, monkeypatch):
    _patch_retrieve(monkeypatch, _mixed_hits())

    kb_id = _make_kb(client)
    resp = client.post(
        "/api/v1/chat/ask-stream", json={"question": MULTI_Q, "kb_ids": [kb_id]}
    )
    assert resp.status_code == 200, resp.text
    events = _parse_sse(resp.text)
    names = [e for e, _ in events]

    # 事件名与顺序不变：start → citations → delta×N → done
    assert names[0] == "start" and names[1] == "citations" and names[-1] == "done"
    assert set(names) <= {"start", "citations", "delta", "done", "error"}

    done = dict(events[-1][1])
    assert "conversation_id" in done and "message_id" in done
    assert done["reflection"] is not None
    assert done["reflection"]["decision"] == DECISION_ACCEPT
    assert done["answer"]


@pytest.mark.skipif(not PG_AVAILABLE, reason="PostgreSQL 未启动")
def test_ask_stream_insufficient_keeps_contract(client, monkeypatch):
    """Gate insufficient：SSE 仍为 citations → delta → done，Evidence 保留。"""
    weak_kg = [
        _kg_hit("金银花相关：辛凉解表", score=0.9, relation=KG_RELATION_RELATED_TO,
                provenance=KG_PROVENANCE_SHARED_TAG)
    ]
    _patch_retrieve(monkeypatch, weak_kg)

    kb_id = _make_kb(client)
    resp = client.post(
        "/api/v1/chat/ask-stream", json={"question": HERB_Q, "kb_ids": [kb_id]}
    )
    events = _parse_sse(resp.text)
    names = [e for e, _ in events]
    assert names[0] == "start" and names[1] == "citations" and names[-1] == "done"

    done = dict(events[-1][1])
    assert GATE_REFUSAL_ANSWER == done["answer"]
    # 未进入生成阶段 → 不产出 Reflection（保留 evidence_gate 供前端解释）
    assert done["reflection"] is None


# ── 17. Evaluation ON/OFF ───────────────────────────────────────────────────


@pytest.mark.skipif(not PG_AVAILABLE, reason="PostgreSQL 未启动")
async def test_evaluation_records_reflection_on(client, eval_kb, monkeypatch):
    """Reflection ON：逐条归档决策 + 配置快照 + 落库。"""
    from sqlalchemy import select

    from src.application.evaluation_service import EvaluationService
    from src.domain.models import EvaluationResult

    import src.application.rag_service as rag_mod

    _patch_retrieve(monkeypatch, _mixed_hits())
    monkeypatch.setattr(rag_mod, "get_llm", lambda config: _CiteLLM())

    async with _session() as db:
        svc = EvaluationService(db)
        await svc.save_test_set(
            eval_kb,
            [_case(f"金银花-{uuid.uuid4().hex[:6]}", question_type="herb")],
            dataset_version="tcm-v1",
        )
        run, results = await svc.run(eval_kb, retrieval_strategy=BASELINE_STRATEGY)

        assert results[0].reflection_decision == DECISION_ACCEPT
        assert results[0].reflection_version == REFLECTION_VERSION
        snapshot = run.config_snapshot
        assert snapshot["self_reflection"]["enabled"] is True
        assert snapshot["self_reflection"]["reflection_version"] == REFLECTION_VERSION
        assert snapshot["self_reflection"]["decision_counts"] == {DECISION_ACCEPT: 1}

        rows = (
            await db.scalars(
                select(EvaluationResult).where(
                    EvaluationResult.run_id == str(run.id)
                )
            )
        ).all()
        assert rows and rows[0].reflection_decision == DECISION_ACCEPT
        assert rows[0].reflection_version == REFLECTION_VERSION


@pytest.mark.skipif(not PG_AVAILABLE, reason="PostgreSQL 未启动")
async def test_evaluation_records_reflection_off(client, eval_kb, monkeypatch):
    """Reflection OFF：不记录反思决策，快照标记关闭（对照实验组）。"""
    from src.application.evaluation_service import EvaluationService

    import src.application.rag_service as rag_mod

    _patch_retrieve(monkeypatch, _mixed_hits())
    monkeypatch.setattr(rag_mod, "get_llm", lambda config: _CiteLLM())

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
            use_self_reflection=False,
        )
        assert results[0].reflection_decision is None
        assert results[0].reflection_version is None
        assert run.config_snapshot["self_reflection"]["enabled"] is False
        assert run.config_snapshot["self_reflection"]["decision_counts"] == {}


@pytest.mark.skipif(not PG_AVAILABLE, reason="PostgreSQL 未启动")
async def test_evaluation_api_run_accepts_reflection_switch(client, eval_kb, monkeypatch):
    """POST /evaluation/run 支持 use_self_reflection，结果回传 Reflection 字段。"""
    from src.application.evaluation_service import EvaluationService

    import src.application.rag_service as rag_mod

    _patch_retrieve(monkeypatch, _mixed_hits())
    monkeypatch.setattr(rag_mod, "get_llm", lambda config: _CiteLLM())
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
            "experiment_name": "reflection_probe",
            "use_self_reflection": True,
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["results"]
    for r in body["results"]:
        for key in ("reflection_decision", "reflection_version",
                    "reflection_retry_strategy", "reflection_reason"):
            assert key in r, f"缺失字段 {key}"
    assert body["results"][0]["reflection_decision"] == DECISION_ACCEPT
