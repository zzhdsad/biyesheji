"""阶段十二：Retrieval Strategy 抽象与集中式 Strategy Registry。

设计目标（需求 §6 / §13）：
- 不同策略是「同一套 Baseline RAG 的不同参数配置」，不是多套 RAG 实现；
  全部策略继续复用 HyDE → BGE-M3 Dense+Sparse → RRF → Reranker → Relevance Gate。
- 新增策略只需在 STRATEGIES 注册表里加一项，不需要改 RagService 多处 if/else。
- 参数覆盖策略：字段为 None 表示「沿用全局 settings」，非 None 表示策略显式覆盖
  （绝对值，保证实验可复现，并随 config_snapshot 落盘）。

参数为 None/non-None 的含义与「为什么这样取值」写在 docstring 里，
这些差异只是「待实验验证的假设」，本阶段不声称任何策略优于 Baseline。
"""

from __future__ import annotations

from dataclasses import dataclass

from loguru import logger

from src.core.config import settings

# Baseline 策略名（默认 / fallback / 对照实验组）
BASELINE_STRATEGY = "baseline_hybrid"

# 资源类型 → 聚焦策略（供 Router 按单一资源类型直选）
FOCUSED_STRATEGY_BY_RESOURCE_TYPE: dict[str, str] = {
    "herb": "herb_focused",
    "prescription": "prescription_focused",
    "theory": "theory_focused",
    "literature": "literature_focused",
}


@dataclass(frozen=True)
class RetrievalStrategy:
    """一条检索策略 = Baseline RAG 的一组参数配置。

    Attributes:
        name: 策略标识（写入 EvaluationRun.retrieval_strategy）
        description: 策略说明（实验报告用）
        resource_types: 只允许这些 resource_type 的命中；None = 不限制
            （Document + 全部 Resource，即 Baseline 行为）
        include_document: 过滤开启时是否仍保留 Document 命中（resource_type 为空）
        resource_types_from_analysis: True = resource_types 运行时取
            QueryAnalysis.resource_types（multi_source 动态多类资源）
        recall_top_k: 每路召回条数；None = 沿用 settings.RECALL_TOP_K
        rerank_top_k: 精排后送 LLM 的条数；None = 沿用 settings.RERANK_TOP_N
        rrf_k: RRF 平滑常数；None = 沿用 settings.RRF_K
        hyde_enabled: 是否启用 HyDE 查询改写
        dense_enabled / sparse_enabled: 是否走稠密 / 稀疏召回路
        kg_enabled: 阶段十三：是否启用 KG Retrieval（KG 命中并入 Evidence，
            不替代向量检索；False 即完全回到阶段十二及之前的行为）
        kg_max_hops: KG 图遍历跳数（1 = 实体直连关系，2 = 两跳关系）
        kg_top_k: KG 证据条数上限
    """

    name: str
    description: str
    resource_types: tuple[str, ...] | None = None
    include_document: bool = True
    resource_types_from_analysis: bool = False
    recall_top_k: int | None = None
    rerank_top_k: int | None = None
    rrf_k: int | None = None
    hyde_enabled: bool = True
    dense_enabled: bool = True
    sparse_enabled: bool = True
    kg_enabled: bool = False
    kg_max_hops: int = 1
    kg_top_k: int = 5


STRATEGIES: dict[str, RetrievalStrategy] = {
    # ── Baseline（对照）：阶段八～十既有的那条链路，参数全部沿用全局配置 ──
    BASELINE_STRATEGY: RetrievalStrategy(
        name=BASELINE_STRATEGY,
        description=(
            "Baseline：HyDE + Dense/Sparse 混合召回 + RRF + Rerank + Relevance Gate，"
            "不限制资源类型（Document 与四类 Resource 一并召回）"
        ),
    ),
    # ── 中药聚焦：限定 herb 资源，放大召回后略放宽精排条数 ──
    "herb_focused": RetrievalStrategy(
        name="herb_focused",
        description=(
            "中药问题：只召回 herb 资源（保留 Document 兜底），"
            "适度放大一路召回数量与精排条数，便于覆盖性味归经/功效主治等多段落"
        ),
        resource_types=("herb",),
        include_document=True,
        recall_top_k=60,
        rerank_top_k=6,
    ),
    # ── 方剂聚焦：限定 prescription 资源，放大召回 ──
    "prescription_focused": RetrievalStrategy(
        name="prescription_focused",
        description=(
            "方剂问题：只召回 prescription 资源（保留 Document 兜底），"
            "放大召回以覆盖组成/功用/主治/加减等多字段"
        ),
        resource_types=("prescription",),
        include_document=True,
        recall_top_k=60,
    ),
    # ── 理论聚焦：限定 theory 资源，收紧召回 + 更小 RRF 平滑常数（更看重头部排名）──
    "theory_focused": RetrievalStrategy(
        name="theory_focused",
        description=(
            "理论问题：只召回 theory 资源（保留 Document 兜底），"
            "回收集数收紧、RRF 平滑常数调小，使头部命中权重更高"
        ),
        resource_types=("theory",),
        include_document=True,
        recall_top_k=40,
        rrf_k=40,
    ),
    # ── 文献聚焦：限定 literature 资源，关闭 HyDE（书名/原文更依赖字面匹配）──
    "literature_focused": RetrievalStrategy(
        name="literature_focused",
        description=(
            "文献问题：只召回 literature 资源（保留 Document 兜底）。"
            "关闭 HyDE，避免假设答案改写把书名/原文表述带偏；精排条数略放宽"
        ),
        resource_types=("literature",),
        include_document=True,
        rerank_top_k=6,
        hyde_enabled=False,
    ),
    # ── 阶段十三：KG 增强 = Baseline 向量检索 + KG 关系证据（补充，不替代）──
    "kg_enhanced": RetrievalStrategy(
        name="kg_enhanced",
        description=(
            "实体关系问题：在 Baseline 向量检索之外追加 KG 关系证据"
            "（方剂-中药组成 / 文献记载 / 共享标签），两跳内关系证据并入 Evidence"
        ),
        # 不限制资源类型（KG 证据也要能覆盖文档之外的资源关系）
        resource_types=None,
        include_document=True,
        kg_enabled=True,
        kg_max_hops=2,
        kg_top_k=5,
    ),
    # ── 多来源：资源类型运行时取 QueryAnalysis.resource_types（可多类 + Document）──
    "multi_source": RetrievalStrategy(
        name="multi_source",
        description=(
            "多来源问题：同时允许 QueryAnalysis.resource_types 中的多类资源，"
            "并保留 Document；放大召回与精排条数以覆盖多类证据"
        ),
        resource_types_from_analysis=True,
        include_document=True,
        recall_top_k=80,
        rerank_top_k=8,
    ),
}


def strategy_names() -> list[str]:
    """策略注册表中的全部策略名（顺序稳定，便于实验列表展示）。"""
    return list(STRATEGIES.keys())


def get_strategy(name: str | None) -> RetrievalStrategy | None:
    """按名取策略；未知/空返回 None（调用方应回落 Baseline）。

    注意：阶段九的历史 strategy 标签（如 hybrid_rrf_rerank_hyde）不在注册表中，
    返回 None 表示「按既有 Baseline 行为执行」，保证旧数据/旧调用不受影响。
    """
    if not name:
        return None
    return STRATEGIES.get(name)


def resolve_resource_types(
    strategy: RetrievalStrategy, analysis_resource_types: list[str] | None = None
) -> tuple[str, ...] | None:
    """解析策略实际生效的资源类型过滤（None = 不过滤）。

    - 静态策略：直接用 strategy.resource_types
    - 动态策略（multi_source）：取 QueryAnalysis.resource_types
    - 动态策略但分析未给出资源类型：退化为不过滤（等价于保留 Document + 全资源）
    """
    if strategy.resource_types_from_analysis:
        dynamic = tuple(t for t in (analysis_resource_types or []) if t)
        if not dynamic:
            return None
        return dynamic
    return strategy.resource_types


def build_resource_filter(
    strategy: RetrievalStrategy, analysis_resource_types: list[str] | None = None
) -> dict:
    """生成可读的资源过滤结构（RouterDecision / config_snapshot / 前端调试用）。"""
    resource_types = resolve_resource_types(strategy, analysis_resource_types)
    return {
        "resource_types": list(resource_types) if resource_types else [],
        "include_document": strategy.include_document,
        # 是否真的会过滤（False = 与 Baseline 一样不限制来源）
        "enabled": resource_types is not None,
    }


def matches_resource(hit: dict, resource_types: tuple[str, ...] | None, include_document: bool) -> bool:
    """判断命中是否满足资源过滤（后置 authoritative 过滤）。

    resource_types 为 None → 全部通过（Baseline）。
    Document 命中（resource_type 为空）由 include_document 决定，
    因此聚焦策略仍保留 Document 兜底，不会完全丢掉文档证据。
    """
    if not resource_types:
        return True
    rtype = hit.get("resource_type")
    if rtype:
        return rtype in resource_types
    return include_document


def filter_hits(
    hits: list[dict], resource_types: tuple[str, ...] | None, include_document: bool
) -> list[dict]:
    """按资源类型过滤命中列表（保持原顺序与字段不变）。"""
    if not resource_types:
        return hits
    kept = [h for h in hits if matches_resource(h, resource_types, include_document)]
    dropped = len(hits) - len(kept)
    if dropped:
        logger.info(
            f"策略资源过滤：保留 {len(kept)} 条，过滤 {dropped} 条"
            f"（resource_types={list(resource_types)}, include_document={include_document}）"
        )
    return kept


def resolve_retrieval_config(
    strategy: RetrievalStrategy | None,
    analysis_resource_types: list[str] | None = None,
) -> dict:
    """把策略解析为本次检索的生效参数（None 覆盖项自动回落到全局 settings）。

    返回 dict 与 RagService._retrieve 的入参一一对应，并可整体写入
    EvaluationRun.config_snapshot 保证实验可复现。
    """
    if strategy is None:
        strategy = STRATEGIES[BASELINE_STRATEGY]
    resource_types = resolve_resource_types(strategy, analysis_resource_types)
    return {
        "strategy_name": strategy.name,
        "resource_types": list(resource_types) if resource_types else [],
        "include_document": strategy.include_document,
        "recall_top_k": (
            strategy.recall_top_k
            if strategy.recall_top_k is not None
            else settings.RECALL_TOP_K
        ),
        "rerank_top_k": (
            strategy.rerank_top_k
            if strategy.rerank_top_k is not None
            else settings.RERANK_TOP_N
        ),
        "rrf_k": strategy.rrf_k if strategy.rrf_k is not None else settings.RRF_K,
        "hyde_enabled": strategy.hyde_enabled,
        "dense_enabled": strategy.dense_enabled,
        "sparse_enabled": strategy.sparse_enabled,
        # 阶段十三：KG 检索参数（Baseline 全为关闭/默认，行为不变）
        "kg_enabled": strategy.kg_enabled,
        "kg_max_hops": strategy.kg_max_hops,
        "kg_top_k": strategy.kg_top_k,
    }


def strategy_out(name: str) -> dict:
    """策略描述（API 展示 / 实验配置说明）。"""
    s = STRATEGIES[name]
    cfg = resolve_retrieval_config(s)
    return {
        "name": s.name,
        "description": s.description,
        "resource_types": list(s.resource_types) if s.resource_types else [],
        "resource_types_from_analysis": s.resource_types_from_analysis,
        "include_document": s.include_document,
        "config": cfg,
    }
