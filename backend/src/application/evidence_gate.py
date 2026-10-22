"""阶段十四：Evidence Gate（证据门控）。

流水线位置（需求 §1）：

    Query → Analyzer → Router → RetrievalStrategy
          → Vector Retrieval + KG Retrieval → RRF/Reranker
          → 【Evidence Gate】 → Evidence/Groups/Citation → Answer

本模块只做「整体证据质量判断」，不重写检索、不改变 Evidence/Citation 结构：

- 输入：统一的 Evidence 列表（application.evidence.build_evidence 的产物，
  已按 RELEVANCE_THRESHOLD 过滤）+ QueryAnalysis + RouterDecision
- 输出：GateDecision（decision = accept / insufficient / retry）

设计约束（需求 §7 / §AGENTS）：
1. **纯规则、确定性**：不使用 LLM、不调用向量库、不做医学判断，
   同输入必得同输出（可在评测中复现）。
2. **区分来源**：document / resource（向量证据）与 kg（图谱证据）分开判断，
   **不把 KG 的 score 当向量余弦相似度用**（KG score 实际是实体匹配分 × 跳数衰减）。
3. **非单点故障**：Gate 自身异常 → 安全兜底为 accept（is_valid=False），
   绝不因门控让 /chat/ask 失败。
4. **retry 最多一次**：retry 是否真的执行、重试几次由调用方（RagService）控制，
   本模块只给出「建议重试的策略」，且保证不与当前策略相同（不产生循环）。

阈值均为「待实验验证的假设」，本阶段不声称 Gate 提升任何指标。
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field

from loguru import logger

from src.application.evidence import (
    EVIDENCE_HIGH_THRESHOLD,
    EVIDENCE_LEVEL_HIGH,
    EVIDENCE_LEVEL_INSUFFICIENT,
    EVIDENCE_LEVEL_MEDIUM,
    EVIDENCE_MEDIUM_THRESHOLD,
    SOURCE_KIND_DOCUMENT,
    SOURCE_KIND_KG,
    SOURCE_KIND_RESOURCE,
    evidence_level,
)
from src.core.config import settings
from src.domain.models import (
    KG_RELATION_CONTAINS,
    KG_RELATION_RECORDS,
)

# ── 门控规则版本（写入 GateDecision / config_snapshot，实验可追溯）────────────
# gate-v1：阶段十四初始规则（来源分类 + KG 关系/跳数 + 聚合条件 + 单次 retry）
GATE_VERSION = "gate-v1"

# ── 三种判定结果 ────────────────────────────────────────────────────────────
DECISION_ACCEPT = "accept"
DECISION_INSUFFICIENT = "insufficient"
DECISION_RETRY = "retry"

GATE_DECISIONS: tuple[str, ...] = (
    DECISION_ACCEPT,
    DECISION_INSUFFICIENT,
    DECISION_RETRY,
)

# ── KG 关系可信度（需求 §3：业务事实 > 共享标签；跳数越多越弱）────────────────
# contains / records 来自 prescription_ingredients 与资源 source 字段，是业务事实
KG_STRONG_RELATIONS = frozenset({KG_RELATION_CONTAINS, KG_RELATION_RECORDS})
# related_to 只表示"共享人工标签"，是弱关系，不得因分数高被当成强证据
KG_WEAK_RELATIONS = frozenset({"related_to"})
# 强关系 1 跳 = 直接业务事实；2 跳 = 间接（降为 medium）；≥3 跳 = 弱证据
KG_STRONG_HOP = 1
KG_MEDIUM_HOP = 2
# KG 强关系在该分数以上视为 high（分数只作辅助，不是向量相似度）
KG_HIGH_SCORE = EVIDENCE_HIGH_THRESHOLD

# ── 聚合阈值（需求 §4：判断"整体证据"，不是单条）───────────────────────────
# 只有 medium 证据时至少需要几条才算互补（单条中等证据不足以支撑结论）
GATE_MIN_MEDIUM_EVIDENCE = 2
# 纯 KG（无 document/resource 证据）时至少需要几条强关系业务事实
GATE_MIN_KG_STRONG_ONLY = 2
# unanswerable candidate（可能超出知识库范围）需要更硬的证据
GATE_UNANSWERABLE_MIN_HIGH = 1
GATE_UNANSWERABLE_MIN_SOURCES = 2

# ── retry 策略选择表（确定性映射，需求 §7）──────────────────────────────────
# 原则：当前策略"窄"→ 换"宽"的；当前策略已是最宽 → 换能补充证据来源的。
# 保证：目标 ≠ 当前（不产生循环），且目标必须已注册。
RETRY_STRATEGY_BY_CURRENT: dict[str, str] = {
    "baseline_hybrid": "multi_source",       # 无过滤 → 放大召回 + 多来源
    "herb_focused": "baseline_hybrid",       # 聚焦过窄 → 放开资源过滤
    "prescription_focused": "baseline_hybrid",
    "theory_focused": "baseline_hybrid",
    "literature_focused": "baseline_hybrid",
    "multi_source": "kg_enhanced",           # 已经是宽召回 → 补 KG 关系证据
    "kg_enhanced": "multi_source",           # KG 不足 → 放大向量召回
}


def _as_float(value: object, default: float = 0.0) -> float:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def _as_int(value: object, default: int = 1) -> int:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def _evidence_score(evidence: dict) -> float:
    """证据分数：统一取 Evidence 的 score（build_evidence 已按 rerank 优先）。"""
    return _as_float(evidence.get("score"), 0.0)


def classify_evidence(evidence: dict) -> tuple[str, bool]:
    """单条证据 → (strength, accepted)。

    - document / resource：沿用既有 evidence_level（high ≥ 0.7 / medium ≥ 0.3）
    - kg：**先看关系与跳数，score 只作辅助**
        * contains / records + 1 跳 → strong（high/medium 由分数定）
        * contains / records + 2 跳 → medium（跳数衰减）
        * related_to（共享标签）或 ≥3 跳 → weak，**不接受**（只能作补充，
          不得单独支撑结论，更不得据此生成医疗结论）
    """
    kind = evidence.get("source_kind") or SOURCE_KIND_DOCUMENT
    score = _evidence_score(evidence)

    if kind == SOURCE_KIND_KG:
        relation = str(evidence.get("kg_relation") or "")
        hop = _as_int(evidence.get("kg_hop"), 1)
        if relation in KG_STRONG_RELATIONS and hop <= KG_STRONG_HOP:
            return (
                EVIDENCE_LEVEL_HIGH if score >= KG_HIGH_SCORE else EVIDENCE_LEVEL_MEDIUM
            ), True
        if relation in KG_STRONG_RELATIONS and hop <= KG_MEDIUM_HOP:
            return EVIDENCE_LEVEL_MEDIUM, True
        # related_to / 未知关系 / 多跳：弱证据
        return EVIDENCE_LEVEL_INSUFFICIENT, False

    # document / resource：与既有 Citation 分级完全一致
    level = evidence.get("evidence_level") or evidence_level(score)
    if level == EVIDENCE_LEVEL_HIGH:
        return EVIDENCE_LEVEL_HIGH, True
    if level == EVIDENCE_LEVEL_MEDIUM:
        return EVIDENCE_LEVEL_MEDIUM, True
    return EVIDENCE_LEVEL_INSUFFICIENT, False


def select_retry_strategy(current_strategy: str | None) -> str | None:
    """选择 retry 用的已有策略（确定性、可解释、不产生循环）。

    - 当前策略未知/为空 → 视为 baseline_hybrid
    - 映射目标必须在 Strategy Registry 中，且不能等于当前策略
    - 无可用目标 → None（调用方应转 insufficient）
    """
    from src.application.retrieval_strategies import (
        BASELINE_STRATEGY,
        STRATEGIES,
    )

    current = current_strategy or BASELINE_STRATEGY
    # 未知/历史策略标签：按 Baseline 处理（确定性、不会返回未注册策略）
    if current not in RETRY_STRATEGY_BY_CURRENT:
        current = BASELINE_STRATEGY
    target = RETRY_STRATEGY_BY_CURRENT.get(current)
    if not target or target == current:
        return None
    if target not in STRATEGIES:
        logger.warning(f"retry 策略 {target!r} 未注册，放弃 retry")
        return None
    return target


def fallback_gate_decision(reason: str, evidence_count: int = 0) -> "GateDecision":
    """安全兜底决策：Gate 异常时按 accept 放行（需求：不得成为单点故障）。"""
    return GateDecision(
        decision=DECISION_ACCEPT,
        reason=f"gate_fallback={reason}",
        evidence_count=evidence_count,
        accepted_count=evidence_count,
        is_valid=False,
        fallback_reason=reason,
    )


def gate_config(enabled: bool | None = None) -> dict:
    """Gate 配置快照（写入 EvaluationRun.config_snapshot，保证实验可复现）。"""
    if enabled is None:
        enabled = bool(getattr(settings, "EVIDENCE_GATE_ENABLED", True))
    return {
        "gate_version": GATE_VERSION,
        "enabled": enabled,
        "thresholds": {
            "evidence_high": EVIDENCE_HIGH_THRESHOLD,
            "evidence_medium": EVIDENCE_MEDIUM_THRESHOLD,
            "min_medium_evidence": GATE_MIN_MEDIUM_EVIDENCE,
            "min_kg_strong_only": GATE_MIN_KG_STRONG_ONLY,
            "unanswerable_min_high": GATE_UNANSWERABLE_MIN_HIGH,
            "unanswerable_min_sources": GATE_UNANSWERABLE_MIN_SOURCES,
            "kg_high_score": KG_HIGH_SCORE,
            "kg_strong_hop": KG_STRONG_HOP,
            "kg_medium_hop": KG_MEDIUM_HOP,
        },
        "kg_relations": {
            "strong": sorted(KG_STRONG_RELATIONS),
            "weak": sorted(KG_WEAK_RELATIONS),
        },
        "retry_strategy_by_current": dict(RETRY_STRATEGY_BY_CURRENT),
    }


@dataclass(frozen=True)
class GateDecision:
    """一次证据门控决策（可解释、可序列化、与 API/Pydantic 字段一致）。"""

    decision: str
    reason: str = ""
    gate_version: str = GATE_VERSION
    evidence_count: int = 0
    accepted_count: int = 0
    high_count: int = 0
    medium_count: int = 0
    # 未被接受的弱证据条数（低于阈值 / related_to / 多跳 KG）
    weak_count: int = 0
    # 按来源类别统计（document / resource / kg，仅统计被接受的证据）
    source_kind_counts: dict = field(default_factory=dict)
    best_score: float = 0.0
    # False = Gate 自身走了兜底（不拦截，链路继续）
    is_valid: bool = True
    fallback_reason: str | None = None
    # retry 信息（需求 §5：最多一次，retry 后仍不足 → insufficient）
    retry_reason: str | None = None
    original_strategy: str | None = None
    retry_strategy: str | None = None
    retried: bool = False
    # 判定依据明细（供调试 / 实验分析，不参与前端展示）
    details: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        """序列化为 API 响应结构（字段与 chat.GateDecisionOut 对齐）。"""
        return {
            "decision": self.decision,
            "reason": self.reason,
            "gate_version": self.gate_version,
            "evidence_count": self.evidence_count,
            "accepted_count": self.accepted_count,
            "high_count": self.high_count,
            "medium_count": self.medium_count,
            "weak_count": self.weak_count,
            "source_kind_counts": dict(self.source_kind_counts),
            "best_score": self.best_score,
            "is_valid": self.is_valid,
            "fallback_reason": self.fallback_reason,
            "retry_reason": self.retry_reason,
            "original_strategy": self.original_strategy,
            "retry_strategy": self.retry_strategy,
            "retried": self.retried,
            "details": dict(self.details),
        }


@dataclass
class _EvidenceStats:
    """证据聚合统计（内部中间结构）。"""

    total: int = 0
    accepted: int = 0
    high: int = 0
    medium: int = 0
    weak: int = 0
    best_score: float = 0.0
    kind_counts_total: dict = field(default_factory=dict)
    kind_counts_accepted: dict = field(default_factory=dict)
    vector_accepted: int = 0
    kg_accepted: int = 0
    kg_strong: int = 0
    kg_weak: int = 0
    kg_relations: dict = field(default_factory=dict)
    sources: set = field(default_factory=set)
    matched_accepted: int = 0
    resource_typed_accepted: int = 0
    covered_types: set = field(default_factory=set)


def _aggregate(evidence: list[dict], expected_types: list[str]) -> _EvidenceStats:
    """聚合证据统计（单遍扫描，确定性）。"""
    stats = _EvidenceStats(total=len(evidence))
    expected = {t for t in expected_types if t}
    for ev in evidence:
        if not isinstance(ev, dict):
            continue
        score = _evidence_score(ev)
        stats.best_score = max(stats.best_score, score)
        kind = ev.get("source_kind") or SOURCE_KIND_DOCUMENT
        stats.kind_counts_total[kind] = stats.kind_counts_total.get(kind, 0) + 1

        strength, accepted = classify_evidence(ev)
        if kind == SOURCE_KIND_KG:
            relation = str(ev.get("kg_relation") or "unknown")
            stats.kg_relations[relation] = stats.kg_relations.get(relation, 0) + 1

        if not accepted:
            stats.weak += 1
            if kind == SOURCE_KIND_KG:
                stats.kg_weak += 1
            continue

        stats.accepted += 1
        stats.kind_counts_accepted[kind] = stats.kind_counts_accepted.get(kind, 0) + 1
        if strength == EVIDENCE_LEVEL_HIGH:
            stats.high += 1
        else:
            stats.medium += 1

        if kind == SOURCE_KIND_KG:
            stats.kg_accepted += 1
            relation = str(ev.get("kg_relation") or "")
            hop = _as_int(ev.get("kg_hop"), 1)
            if relation in KG_STRONG_RELATIONS and hop <= KG_STRONG_HOP:
                stats.kg_strong += 1
        else:
            stats.vector_accepted += 1

        # 独立来源（文档 / 资源 / 图谱节点）
        sid = ev.get("source_id") or ev.get("resource_id") or ev.get("doc_id")
        stats.sources.add((kind, str(sid) if sid is not None else ev.get("source_name")))

        # 资源类型匹配（document 无 resource_type，视为通用证据）
        rtype = ev.get("resource_type")
        if rtype:
            stats.resource_typed_accepted += 1
            if rtype in expected:
                stats.covered_types.add(rtype)
            if not expected or rtype in expected:
                stats.matched_accepted += 1
        else:
            stats.matched_accepted += 1
    return stats


class EvidenceGate:
    """确定性证据门控：统一 Evidence → GateDecision。"""

    def __init__(self, enabled: bool | None = None) -> None:
        # None = 沿用全局配置（settings.EVIDENCE_GATE_ENABLED）
        self.enabled = (
            bool(getattr(settings, "EVIDENCE_GATE_ENABLED", True))
            if enabled is None
            else bool(enabled)
        )

    def evaluate(
        self,
        evidence: list[dict],
        query_analysis=None,
        router_decision=None,
        *,
        allow_retry: bool = True,
        strategy_name: str | None = None,
    ) -> GateDecision:
        """评估整体证据质量（纯规则，不调用 LLM / 向量库）。

        Args:
            evidence: 统一 Evidence 列表（build_evidence 产物）
            query_analysis: QueryAnalysis（resource_types / is_multi_source /
                is_unanswerable_candidate）
            router_decision: RouterDecision（取当前策略名，供 retry 选择）
            allow_retry: False = 已经是 retry 后的评估，不再给 retry
            strategy_name: 当前策略名（优先于 router_decision，便于 retry 后复用）

        Returns:
            GateDecision（decision = accept / insufficient / retry）
        """
        try:
            return self._evaluate(
                evidence or [],
                query_analysis,
                router_decision,
                allow_retry=allow_retry,
                strategy_name=strategy_name,
            )
        except Exception as exc:  # noqa: BLE001  Gate 不得成为单点故障
            logger.warning(f"Evidence Gate 评估失败，安全放行: {exc}")
            return fallback_gate_decision(f"gate_exception: {exc}", len(evidence or []))

    # ── 规则本体 ────────────────────────────────────────────────────────────

    def _evaluate(
        self,
        evidence: list[dict],
        analysis,
        router_decision,
        *,
        allow_retry: bool,
        strategy_name: str | None,
    ) -> GateDecision:
        current_strategy = strategy_name
        if not current_strategy and router_decision is not None:
            current_strategy = getattr(router_decision, "strategy_name", None)

        expected_types = list(getattr(analysis, "resource_types", None) or [])
        is_multi_source = bool(getattr(analysis, "is_multi_source", False))
        unanswerable = bool(getattr(analysis, "is_unanswerable_candidate", False))

        stats = _aggregate(evidence, expected_types)
        details = {
            "vector_accepted": stats.vector_accepted,
            "kg_accepted": stats.kg_accepted,
            "kg_strong": stats.kg_strong,
            "kg_weak": stats.kg_weak,
            "kg_relation_counts": dict(stats.kg_relations),
            "source_kind_counts_total": dict(stats.kind_counts_total),
            "independent_sources": len(stats.sources),
            "matched_accepted": stats.matched_accepted,
            "expected_resource_types": list(expected_types),
            "covered_resource_types": sorted(stats.covered_types),
            "resource_type_mismatch": bool(
                expected_types
                and stats.resource_typed_accepted > 0
                and stats.matched_accepted == 0
            ),
            "kg_only": stats.accepted > 0 and stats.vector_accepted == 0,
            "kg_complement": stats.vector_accepted > 0 and stats.kg_accepted > 0,
            "weak_count": stats.weak,
        }

        # 1) 空证据 → 直接不足（retry 无意义：连一条证据都没有）
        if stats.total == 0:
            return self._make(
                DECISION_INSUFFICIENT, "no_evidence", stats, details,
                allow_retry=False, current_strategy=current_strategy,
            )

        # 2) 全部为弱证据（低于阈值 / related_to / 多跳 KG）→ 可尝试换策略
        if stats.accepted == 0:
            return self._make(
                DECISION_RETRY, "no_accepted_evidence", stats, details,
                allow_retry=allow_retry, current_strategy=current_strategy,
            )

        # 3) 多来源问题未覆盖任何指定资源类型 → 换策略。
        #    仅当"确实存在资源类证据却一个都没覆盖"时才触发；
        #    若证据全是 Document（无 resource_type），属于通用证据，不拦截
        #    （避免过度严格，需求 §4）。
        if (
            is_multi_source or len(expected_types) >= 2
        ) and stats.resource_typed_accepted > 0 and not stats.covered_types:
            return self._make(
                DECISION_RETRY, "multi_source_not_covered", stats, details,
                allow_retry=allow_retry, current_strategy=current_strategy,
            )

        # 4) 资源类型完全不匹配（问题要 A 类资源，证据全是 B 类）→ 换策略
        if details["resource_type_mismatch"]:
            return self._make(
                DECISION_RETRY, "resource_type_mismatch", stats, details,
                allow_retry=allow_retry, current_strategy=current_strategy,
            )

        # 5) 纯 KG 证据：弱关系（related_to / 多跳）不得单独支撑结论，
        #    强关系业务事实（组成/出处）也要求 ≥2 条，避免凭单一关系下结论
        if details["kg_only"]:
            if stats.kg_strong < GATE_MIN_KG_STRONG_ONLY:
                return self._make(
                    DECISION_RETRY, "kg_only_insufficient_strong", stats, details,
                    allow_retry=allow_retry, current_strategy=current_strategy,
                )
            return self._make(
                DECISION_ACCEPT, "kg_only_strong_facts", stats, details,
                allow_retry=False, current_strategy=current_strategy,
            )

        # 6) unanswerable candidate：需要更硬的证据（强证据 + 多个独立来源）
        if unanswerable and (
            stats.high < GATE_UNANSWERABLE_MIN_HIGH
            or len(stats.sources) < GATE_UNANSWERABLE_MIN_SOURCES
        ):
            return self._make(
                DECISION_RETRY, "unanswerable_candidate_weak_evidence", stats, details,
                allow_retry=allow_retry, current_strategy=current_strategy,
            )

        # 7) 常规判定：已有被接受且与问题匹配的证据 → accept。
        #    分数是否"够好"由既有 RELEVANCE_THRESHOLD / evidence_level 决定（build_evidence
        #    已过滤），Gate 不再重复设一道分数线，避免把现有问答大量拦成拒答
        #    （需求 §4：避免过度严格）。high/medium 分布写入决策供实验分析。
        if stats.high >= 1:
            reason = "has_high_evidence"
        elif stats.medium >= GATE_MIN_MEDIUM_EVIDENCE:
            reason = "multiple_medium_evidence"
        elif details["kg_complement"]:
            reason = "medium_with_kg_complement"
        else:
            reason = "single_medium_evidence"
        return self._make(
            DECISION_ACCEPT, reason, stats, details,
            allow_retry=False, current_strategy=current_strategy,
        )

    # ── 构造决策 ────────────────────────────────────────────────────────────

    def _make(
        self,
        decision: str,
        reason: str,
        stats: _EvidenceStats,
        details: dict,
        *,
        allow_retry: bool,
        current_strategy: str | None,
    ) -> GateDecision:
        """统一构造 GateDecision：retry 不可用时降级为 insufficient。"""
        retry_strategy = None
        if decision == DECISION_RETRY:
            if not allow_retry:
                # 已重试过一次（或没有可换的策略）→ 判定不足，禁止继续循环
                decision = DECISION_INSUFFICIENT
                reason = f"{reason};retry_exhausted"
            else:
                retry_strategy = select_retry_strategy(current_strategy)
                if retry_strategy is None:
                    decision = DECISION_INSUFFICIENT
                    reason = f"{reason};no_retry_strategy"
        return GateDecision(
            decision=decision,
            reason=reason,
            evidence_count=stats.total,
            accepted_count=stats.accepted,
            high_count=stats.high,
            medium_count=stats.medium,
            weak_count=stats.weak,
            source_kind_counts=dict(stats.kind_counts_accepted),
            best_score=round(stats.best_score, 4),
            is_valid=True,
            retry_strategy=retry_strategy,
            original_strategy=current_strategy if retry_strategy else None,
            retry_reason=reason if retry_strategy else None,
            details=details,
        )


def with_retry(
    decision: GateDecision,
    *,
    original_strategy: str | None = None,
    retry_strategy: str | None = None,
    retry_reason: str | None = None,
) -> GateDecision:
    """把「已执行一次 retry」的信息合并进最终决策（retry 后不再给 retry）。"""
    return dataclasses.replace(
        decision,
        retried=True,
        original_strategy=original_strategy or decision.original_strategy,
        retry_strategy=retry_strategy or decision.retry_strategy,
        retry_reason=retry_reason or decision.retry_reason,
    )


_DEFAULT_GATE = EvidenceGate()


def evaluate_evidence(
    evidence: list[dict],
    query_analysis=None,
    router_decision=None,
    *,
    allow_retry: bool = True,
    strategy_name: str | None = None,
) -> GateDecision:
    """模块级便捷入口（使用默认 Gate，enabled 取自全局配置）。"""
    return _DEFAULT_GATE.evaluate(
        evidence,
        query_analysis,
        router_decision,
        allow_retry=allow_retry,
        strategy_name=strategy_name,
    )
