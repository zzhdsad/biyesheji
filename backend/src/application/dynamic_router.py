"""阶段十二：Dynamic Router（确定性规则路由）。

流水线位置（需求 §14）：

    Query → QueryAnalyzer → QueryAnalysis → 【DynamicRouter】→ RetrievalStrategy
          → Retrieval → Reranker → Relevance Gate → Evidence → Answer

设计约束：
- Router 只消费 QueryAnalysis 的结构化字段，**不重新解析原始 query**
  （禁止"看到金银花就 herb"这类二次判断，避免与 Analyzer 规则互相冲突）。
- 路由结果必须可解释：RouterDecision 同时给出 strategy_name / reason /
  question_type / resource_types / router_version。
- unanswerable candidate 不触发拒答：仍按资源类型或 Baseline 路由，
  是否拒答交给既有 Relevance Gate（阶段十二不实现 Evidence Gate）。
- 任何异常 / 未知策略 / 非法输入一律 fallback 到 baseline_hybrid，
  不允许 /chat/ask 500。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from loguru import logger

from src.application.query_analyzer import QueryAnalysis
from src.application.retrieval_strategies import (
    BASELINE_STRATEGY,
    FOCUSED_STRATEGY_BY_RESOURCE_TYPE,
    STRATEGIES,
    build_resource_filter,
    resolve_retrieval_config,
)
from src.application.question_types import QUESTION_TYPES

# 路由规则版本：规则变更时递增，写入 RouterDecision 与配置快照（实验可追溯）
# rule-v1：阶段十二（question_type / resource_types → 六类策略）
# rule-v2：阶段十三新增 kg_enhanced（明显实体关系查询），其余映射保持不变
ROUTER_VERSION = "rule-v2"

# 阶段十三：走 KG 增强策略的问题类型。
# 依据：现有 KG 最可靠的关系是「方剂 → contains → 中药」与「文献 → records → 资源」，
# 因此只对 herb / prescription 类、且识别出多个实体的"关系型/比较型"查询启用 KG；
# theory / literature 类仍走原有聚焦策略（其 KG 关系仅来自共享标签，证据性弱）。
KG_RELATION_QUESTION_TYPES = frozenset({"herb", "prescription"})
# 关系型查询的实体数下限（单实体提问通常是"某药功效"这类属性查询，KG 无增量）
KG_MIN_ENTITIES = 2

# 动态路由实验的 run 级标签（写入 EvaluationRun.retrieval_strategy）
DYNAMIC_ROUTER_STRATEGY = "dynamic_router"

# question_type → 策略映射（兜底 unknown → Baseline）
_STRATEGY_BY_QUESTION_TYPE: dict[str, str] = {
    "herb": "herb_focused",
    "prescription": "prescription_focused",
    "theory": "theory_focused",
    "literature": "literature_focused",
    "multi_source": "multi_source",
    "general": BASELINE_STRATEGY,
    # 候选无法回答：不拒答（Gate 负责），按 Baseline 正常检索
    "unanswerable": BASELINE_STRATEGY,
}


@dataclass(frozen=True)
class RouterDecision:
    """一次路由决策（用于解释"为什么这个问题用这个策略"）。"""

    strategy_name: str
    reason: str
    question_type: str
    resource_types: list[str] = field(default_factory=list)
    router_version: str = ROUTER_VERSION
    strategy_description: str = ""
    # 生效的资源过滤与检索参数（前端调试 / 实验快照）
    resource_filter: dict = field(default_factory=dict)
    retrieval_config: dict = field(default_factory=dict)
    # False 表示走了 fallback（strategy_name 仍为 baseline_hybrid，链路继续可用）
    is_valid: bool = True
    fallback_reason: str | None = None

    def to_dict(self) -> dict:
        """序列化为 API 响应结构（字段与 chat.RouterDecisionOut 对齐）。"""
        return {
            "strategy_name": self.strategy_name,
            "reason": self.reason,
            "question_type": self.question_type,
            "resource_types": list(self.resource_types),
            "router_version": self.router_version,
            "strategy_description": self.strategy_description,
            "resource_filter": dict(self.resource_filter),
            "retrieval_config": dict(self.retrieval_config),
            "is_valid": self.is_valid,
            "fallback_reason": self.fallback_reason,
        }


def fallback_decision(analysis: QueryAnalysis | None, reason: str) -> RouterDecision:
    """安全兜底决策（需求 §12）：回落到 baseline_hybrid，链路继续。

    Args:
        analysis: 可能为空（Analyzer 完全不可用时的兜底调用）
        reason: 兜底原因（写入 fallback_reason，便于排查与实验记录）
    """
    strategy = STRATEGIES[BASELINE_STRATEGY]
    # analysis 可能是不完整对象（甚至属性访问就抛异常）：兜底路径自身必须零失败
    try:
        question_type = analysis.question_type if analysis else "general"
        resource_types = list(analysis.resource_types) if analysis else []
    except Exception:  # noqa: BLE001  兜底不能再兜底，直接降级为空
        question_type = "general"
        resource_types = []
    # 派生结构同理：失败时退化为"不做资源过滤"，strategy_name 仍为 Baseline，
    # 检索由 RagService 直接按 Baseline 参数执行（不受此处影响）
    try:
        resource_filter = build_resource_filter(strategy)
        retrieval_config = resolve_retrieval_config(strategy)
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"兜底决策派生结构失败，退化为无过滤 Baseline: {exc}")
        resource_filter = {"resource_types": [], "include_document": True,
                           "enabled": False}
        retrieval_config = {}
    return RouterDecision(
        strategy_name=BASELINE_STRATEGY,
        reason=f"fallback={reason}; question_type={question_type}",
        question_type=question_type,
        resource_types=resource_types,
        router_version=ROUTER_VERSION,
        strategy_description=strategy.description,
        resource_filter=resource_filter,
        retrieval_config=retrieval_config,
        is_valid=False,
        fallback_reason=reason,
    )


def _is_kg_relation_query(analysis: QueryAnalysis) -> bool:
    """阶段十三：判断是否为"明显实体关系查询"（→ kg_enhanced）。

    规则（简单、确定、可测试）：分析结果有效 + question_type 属于
    KG_RELATION_QUESTION_TYPES + Query Analyzer 识别出 ≥ KG_MIN_ENTITIES 个实体。
    不重新解析原始 query，只消费 QueryAnalysis 的既有字段。
    """
    if not getattr(analysis, "is_valid", False):
        return False
    if getattr(analysis, "question_type", None) not in KG_RELATION_QUESTION_TYPES:
        return False
    entities = [
        e
        for e in (getattr(analysis, "entities", None) or [])
        if isinstance(e, dict) and e.get("text")
    ]
    return len(entities) >= KG_MIN_ENTITIES


def _select(analysis: QueryAnalysis) -> tuple[str, str]:
    """路由规则本体（纯函数）：返回 (strategy_name, reason)。

    优先级（需求 §10）：
    1. 明确 multi_source（is_multi_source / question_type=multi_source / ≥2 类资源）
    2. 阶段十三：明显实体关系查询 → kg_enhanced
       （multi_source 保持阶段十二既有映射，不被抢占）
    3. 明确单一资源类型 → 对应聚焦策略
    4. question_type 映射
    5. general → Baseline
    6. 兜底 → Baseline
    """
    rtypes = [t for t in (analysis.resource_types or []) if t]
    suffix = f"question_type={analysis.question_type}; resource_types={rtypes}"
    if analysis.fallback_reason:
        suffix = f"{suffix}; analysis_fallback={analysis.fallback_reason}"

    # 1) 多资源类型
    if analysis.is_multi_source or len(rtypes) >= 2 or analysis.question_type == "multi_source":
        return "multi_source", suffix

    # 2) 阶段十三：明显实体关系查询 → KG 增强
    if _is_kg_relation_query(analysis):
        entities = [e.get("text") for e in (analysis.entities or []) if e.get("text")]
        return "kg_enhanced", f"{suffix}; entities={entities}"

    # 3) 单一资源类型
    if len(rtypes) == 1:
        focused = FOCUSED_STRATEGY_BY_RESOURCE_TYPE.get(rtypes[0])
        if focused:
            return focused, suffix
        logger.warning(f"未知资源类型 {rtypes[0]!r}，按 question_type 兜底路由")

    # 4) question_type 映射（含 general / unanswerable → Baseline）
    mapped = _STRATEGY_BY_QUESTION_TYPE.get(analysis.question_type)
    if mapped:
        return mapped, suffix

    # 5) 兜底
    return BASELINE_STRATEGY, f"{suffix}; unknown_question_type"


class DynamicRouter:
    """确定性规则路由：QueryAnalysis → RouterDecision。"""

    def route(self, analysis: QueryAnalysis) -> RouterDecision:
        """根据 QueryAnalysis 选择 RetrievalStrategy。

        异常、未知策略、非法类型均兜底为 baseline_hybrid（不抛异常）。
        """
        try:
            if analysis is None or getattr(analysis, "question_type", None) not in QUESTION_TYPES:
                return fallback_decision(
                    analysis, "invalid_query_analysis"
                )

            name, reason = _select(analysis)
            strategy = STRATEGIES.get(name)
            if strategy is None:
                logger.warning(f"策略 {name!r} 不在注册表中，回落 Baseline")
                return fallback_decision(analysis, f"unknown_strategy:{name}")

            return RouterDecision(
                strategy_name=strategy.name,
                reason=reason,
                question_type=analysis.question_type,
                resource_types=list(analysis.resource_types or []),
                router_version=ROUTER_VERSION,
                strategy_description=strategy.description,
                resource_filter=build_resource_filter(
                    strategy, analysis.resource_types
                ),
                retrieval_config=resolve_retrieval_config(
                    strategy, analysis.resource_types
                ),
                is_valid=True,
                fallback_reason=None,
            )
        except Exception as exc:  # 兜底：Router 不能成为单点故障
            logger.warning(f"路由失败，回落 Baseline: {exc}")
            return fallback_decision(analysis, f"router_exception: {exc}")


_DEFAULT_ROUTER = DynamicRouter()


def route_query_analysis(analysis: QueryAnalysis) -> RouterDecision:
    """模块级便捷入口（使用默认 Router）。"""
    return _DEFAULT_ROUTER.route(analysis)
