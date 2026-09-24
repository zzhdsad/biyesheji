"""基础 RAG 问答用例：向量化检索 → 上下文拼接 → LLM 生成 → 引用溯源 → 持久化。

TECH_DESIGN / AGENTS.md 约束：
- System Prompt 强制"仅根据参考资料回答，找不到就说知道"（防幻觉）
- 所有检索强制 kb_id 过滤（权限隔离）
- 答案引用标注 [citation: 来源编号, 页码]，后处理解析匹配生成引用来源（文档名、页码、段落）
- 多轮对话：Redis 缓存最近 HISTORY_WINDOW 轮历史（24h TTL），未命中回源 PG 并回填

阶段十一：检索前增加 Query Analyzer（Query → QueryAnalysis → 现有 Baseline RAG），
仅做问题分析，不改变检索策略（HyDE / Dense+Sparse / RRF / Rerank / Gate 全部保持原样）。

阶段十二：Analyzer 之后增加 Dynamic Router（QueryAnalysis → RouterDecision →
RetrievalStrategy），按策略参数驱动同一套 Baseline 检索（无第二套 RAG）：
策略只调整 HyDE 开关 / Dense-Sparse 路数 / recall-rerank 条数 / RRF k /
resource_type 过滤，Baseline 策略 baseline_hybrid 参数全部沿用全局配置，行为不变。
"""

import asyncio
import re
import uuid
from collections.abc import AsyncIterator

from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.config import settings
from src.core.exceptions import NotFoundError, PermissionDeniedError
from src.domain.models import Conversation, Document, Message, User
from src.infrastructure.embedding import BaseEmbedding, EmbeddingError, get_embedding
from src.infrastructure.hyde import BaseHyDE, HyDEError, get_hyde
from src.infrastructure.llm import BaseLLM, LLMError, get_llm
from src.infrastructure.milvus_store import BaseVectorStore, VectorStoreError, get_vector_store
from src.infrastructure.rerank import BaseRerank, RerankError, get_rerank
from src.infrastructure.redis_client import BaseConversationCache, get_conversation_cache
from src.utils.retrieval import rrf_fusion
from src.application.model_config_service import get_effective_config_cached

SYSTEM_PROMPT = (
    "你是企业知识库智能问答助手，回答风格借鉴腾讯 ima：用自然、流畅的中文，"
    "像专业同事一样把资料中的信息组织成清晰易读的回答。\n\n"
    "【硬性规则（不可违反）】\n"
    "1. 仅根据参考资料回答。若参考资料中没有相关内容，直接回答"
    "「根据现有资料，我无法回答该问题」，禁止编造任何资料中没有的事实、数字、结论，"
    "也禁止用你自己的通用知识补答。\n"
    "2. 判断「相关」的标准：资料必须能直接回答用户的问题。仅出现相同词语"
    "（如都提到「产品」）但内容答非所问时，视为不相关，必须拒答；"
    "主观评价类问题（如「你觉得这款产品如何」）若资料中没有评价性内容，同样必须拒答。\n"
    "3. 引用标注：回答中凡是来自参考资料的内容，必须在对应语句末尾标注来源，"
    "格式为 [citation: 来源编号, 页码]，例如 [citation: 1, 3]。"
    "来源编号即参考资料前的方括号编号，页码取该资料标注的页码（无页码则填 0）。"
    "同一句话引用多份资料时，可连续标注如 [citation: 1, 3][citation: 2, 5]。\n\n"
    "【回答风格要求】\n"
    "4. 用完整的自然语言组织答案，不要照搬原文整段，不要只列关键词或返回原文片段。\n"
    "5. 答案结构清晰：先用 1-2 句话直接回答问题，再分点展开说明（用「1. 2. 3.」或"
    "「首先、其次、最后」等连接词），涉及多方面信息时用小段落分层。\n"
    "6. 语言简洁准确，避免口语化和废话，不使用「根据参考资料」「综上所述」等冗余开头。\n"
    "7. 若资料中存在矛盾或信息不完整，如实说明，不要自行推断。"
)

# 检索相关性不足时的固定拒答文案（不调用 LLM，确定性防幻觉）
REFUSAL_ANSWER = (
    "根据现有资料，我无法回答该问题。知识库中未找到与您问题直接相关的内容，"
    "请尝试更换提问方式，或确认知识库中已包含相关文档。"
)

# 阶段十四：Evidence Gate 判定证据不足时的拒答文案（保留证据/引用，禁止编造）。
# 与 REFUSAL_ANSWER 同前缀，保证前端与既有测试对"拒答"的识别逻辑不变。
GATE_REFUSAL_ANSWER = (
    "根据现有资料，我无法回答该问题。当前检索到的证据不足以支撑可靠回答"
    "（已换用其他检索策略重试仍未获得充分证据），请补充相关资料或调整提问方式。"
)

_CONTEXT_MARKER = "参考资料"
# [citation: 1, 3] 或 [citation: 1]；页码可选，兼容有无空格
_CITATION_PATTERN = re.compile(r"\[citation:\s*(\d+)\s*(?:,\s*(\d+)\s*)?\]", re.IGNORECASE)

# Stage 4-4 / Stage 4-5：Resource 类型标签与证据等级统一收敛到
# application.evidence（阶段十：统一 Evidence 模型），此处仅保留别名，
# 保证既有引用（Prompt 标注、旧测试 import）不受影响。
from src.application.evidence import (  # noqa: E402
    RESOURCE_TYPE_LABELS as _RESOURCE_TYPE_LABELS,
)
from src.application.evidence import (  # noqa: E402
    EVIDENCE_HIGH_THRESHOLD as _EVIDENCE_HIGH_THRESHOLD,
)
from src.application.evidence import (  # noqa: E402
    EVIDENCE_MEDIUM_THRESHOLD as _EVIDENCE_MEDIUM_THRESHOLD,
)
from src.application.evidence import evidence_level as _evidence_level  # noqa: E402
from src.application.evidence import (  # noqa: E402
    SOURCE_KIND_KG,
    build_evidence,
    displayable_hits,
    hit_to_evidence,
    package_evidence,
)
from src.application.evidence_gate import (  # noqa: E402
    DECISION_ACCEPT,
    DECISION_RETRY,
    EvidenceGate,
    GateDecision,
    fallback_gate_decision,
    with_retry,
)
from src.application.self_reflection import (  # noqa: E402
    CONSERVATIVE_ANSWER,
    DECISION_ACCEPT as REFLECTION_ACCEPT,
    DECISION_REVISE as REFLECTION_REVISE,
    DECISION_RETRY as REFLECTION_RETRY,
    SelfReflection,
    ReflectionDecision,
    build_revision_messages,
    fallback_reflection_decision,
    llm_consistency_check,
    with_counts,
    with_fallback,
    with_reflection_retry,
    with_revision,
)
from src.application.kg_retrieval import KgRetriever  # noqa: E402
from src.application.query_analyzer import (  # noqa: E402
    QueryAnalysis,
    QueryAnalyzer,
    fallback_analysis,
)
from src.application.dynamic_router import (  # noqa: E402
    DynamicRouter,
    RouterDecision,
    fallback_decision,
)
from src.application.retrieval_strategies import (  # noqa: E402
    RetrievalStrategy,
    filter_hits,
    get_strategy,
    resolve_retrieval_config,
)


class RagService:
    """RAG 问答编排（检索 Top-K → Prompt → LLM → 引用后处理 → 入库）。"""

    def __init__(
        self,
        db: AsyncSession,
        embedding: BaseEmbedding | None = None,
        store: BaseVectorStore | None = None,
        llm: BaseLLM | None = None,
        cache: BaseConversationCache | None = None,
        rerank: BaseRerank | None = None,
        hyde: BaseHyDE | None = None,
        analyzer: QueryAnalyzer | None = None,
        router: DynamicRouter | None = None,
        kg: KgRetriever | None = None,
        gate: EvidenceGate | None = None,
        reflector: SelfReflection | None = None,
    ) -> None:
        self.db = db
        # 运行时配置（DB 优先，env 兜底），供工厂选择后端
        self._config: dict | None = None
        self.embedding = embedding
        self.store = store or get_vector_store()
        self.llm = llm
        self.cache = cache or get_conversation_cache()
        self.rerank = rerank
        self.hyde = hyde
        # 阶段十一：Query Analyzer（检索前的问题分析；不参与/不改变检索策略）
        self.analyzer = analyzer or QueryAnalyzer()
        # 阶段十二：Dynamic Router（Analyzer 之后、Retrieval 之前选择检索策略）
        self.router = router or DynamicRouter()
        # 阶段十三：KG 检索（可选来源，仅在策略开启时执行；失败不影响向量检索）
        self.kg = kg or KgRetriever(db)
        # 阶段十四：Evidence Gate（Vector + KG 证据合并后的统一质量判断）
        self.gate = gate or EvidenceGate()
        # 本次问答的 Gate 决策（供 /chat/ask 输出；旧调用方不感知，默认 None）
        self.last_gate_decision: GateDecision | None = None
        # 阶段十五：Self Reflection（生成之后的答案忠实性检查）
        self.reflector = reflector or SelfReflection()
        # 本次问答的 Reflection 决策（供 /chat/ask 输出；默认 None）
        self.last_reflection_decision: ReflectionDecision | None = None

    async def _ensure_components(self) -> None:
        """惰性加载运行时配置并初始化 embedding/llm/rerank/hyde。

        在首次检索/生成时调用，确保读到最新的 DB 配置。
        """
        if self._config is not None:
            return
        self._config = await get_effective_config_cached(self.db)
        if self.embedding is None:
            self.embedding = get_embedding(self._config)
        if self.llm is None:
            self.llm = get_llm(self._config)
        if self.rerank is None:
            self.rerank = get_rerank(self._config)
        if self.hyde is None:
            self.hyde = get_hyde(self._config)

    async def retrieve_and_answer(
        self,
        kb_ids: list[uuid.UUID],
        question: str,
        history: list[dict] | None = None,
        strategy: RetrievalStrategy | None = None,
        resource_types: list[str] | None = None,
        analysis: QueryAnalysis | None = None,
        router_decision: RouterDecision | None = None,
    ) -> tuple[str, list[dict]]:
        """RAG 检索+生成核心（不持久化，供评估与编排复用）。

        流程：HyDE 改写 → 混合检索 → RRF 融合 → Rerank → 上下文拼接 → LLM 生成。
        不写会话/消息/缓存，避免评估批量调用污染聊天记录。

        阶段十四：Rerank 之后、生成之前插入 Evidence Gate（Vector + KG 证据合并
        后的统一质量判断）；Gate 判为 insufficient 时返回拒答文案并保留 hits
        （证据/引用仍可展示），retry 时最多换一次已有策略。
        阶段十五：生成之后插入 Self Reflection（accept / revise / retry）。

        阶段十五：生成之后插入 Self Reflection（答案是否忠实使用了这些证据）：
        revise = 基于同一份证据重写一次（最多一次），retry = 换策略重检索一次
        （最多一次，与 Gate retry 分别计数）。Reflection 自身异常不影响原答案。

        Args:
            kb_ids: 检索范围（权限隔离，禁止越权）
            question: 原始问题（Prompt 用之；检索查询可能被 HyDE 改写）
            history: 多轮历史（chat 路径传入；评估为 None，单轮）
            strategy: 阶段十二 RetrievalStrategy；None = Baseline（沿用全局配置，
                行为与阶段十一及之前完全一致；评估可显式指定策略做对比实验）
            resource_types: 动态策略（multi_source）运行时解析出的资源类型过滤
            analysis: 阶段十三 QueryAnalysis（KG 策略需要实体做图遍历；未提供时
                由本方法内部调用 Analyzer 补算，保证 KG 永远由结构化分析驱动）
            router_decision: 阶段十四 RouterDecision（Gate 需要当前策略名以选择
                retry 策略；未提供时回退 strategy.name）

        Returns:
            (answer, hits) — hits 含 doc_name/content/page_num/title_path/score，
            供引用构建与评估（retrieved_contexts）复用。
            检索相关性不足（Top 候选稠密相似度均低于 RELEVANCE_THRESHOLD）时
            返回固定拒答文案，hits 为空，不调用 LLM。

        Raises:
            AppException: 422 检索/生成失败
        """
        from src.core.exceptions import AppException

        # 阶段十三：KG 策略需要 QueryAnalysis；调用方未传时内部补算（不重复分析）
        if strategy is not None and strategy.kg_enabled and analysis is None:
            analysis = self.analyze_query(question)

        # 检索+重排核心（HyDE → 混合检索 → RRF → Rerank → 注入文档名 → 可选 KG 证据）
        hits = await self._retrieve(
            kb_ids, question, strategy, resource_types, analysis=analysis
        )

        # 阶段十四：Evidence Gate（Vector + KG 证据合并后的统一质量判断）
        # - accept：继续生成（行为与阶段十三完全一致）
        # - retry：换一个已注册的检索策略重试一次（最多一次）
        # - insufficient：返回拒答文案 + 保留 hits（证据/引用仍展示），不调用 LLM
        hits, gate_decision = await self._apply_evidence_gate(
            kb_ids, question, hits, analysis, router_decision, strategy
        )
        self.last_gate_decision = gate_decision
        if gate_decision is not None and gate_decision.decision != DECISION_ACCEPT:
            logger.info(
                f"Evidence Gate 判定 {gate_decision.decision}（{gate_decision.reason}）"
                f" evidence={gate_decision.evidence_count} "
                f"accepted={gate_decision.accepted_count} q={question[:30]!r}"
            )
            return GATE_REFUSAL_ANSWER, hits

        # 相关性门槛（PRD §8.3 幻觉兜底）：Top 候选稠密相似度均低于阈值 →
        # 仅词语重叠、答非所问（如问"你觉得产品如何"仅命中含"产品"的 PRD），
        # 直接拒答且不调用 LLM，杜绝模型用通用知识补答
        if not self._is_relevant(hits):
            return REFUSAL_ANSWER, []

        # 生成
        user_prompt = self._build_user_prompt(question, hits)
        messages = [{"role": "system", "content": SYSTEM_PROMPT}, *(history or []),
                    {"role": "user", "content": user_prompt}]
        try:
            answer = await self.llm.chat(messages)
        except LLMError as exc:
            logger.error(f"RAG 生成失败: {exc}")
            raise AppException(422, f"回答生成失败：{exc}") from exc
        answer = answer.strip() or "（模型未返回内容，请重试）"

        # 阶段十五：Self Reflection（答案忠实性检查；异常不影响原答案）
        answer, hits, gate_decision, _reflection = await self._reflect_and_finalize(
            kb_ids=kb_ids,
            question=question,
            history=history,
            analysis=analysis,
            hits=hits,
            gate_decision=gate_decision,
            answer=answer,
            strategy_name=(
                router_decision.strategy_name
                if router_decision is not None
                else (strategy.name if strategy is not None else None)
            ),
            allow_retry=True,
        )
        return answer, hits

    async def _retrieve(
        self,
        kb_ids: list[uuid.UUID],
        question: str,
        strategy: RetrievalStrategy | None = None,
        resource_types: list[str] | None = None,
        analysis: QueryAnalysis | None = None,
    ) -> list[dict]:
        """检索+重排核心：HyDE → 混合检索 → RRF → Rerank → 注入文档名（+ 可选 KG）。

        抽取自 retrieve_and_answer，供非流式与流式路径共用，避免逻辑漂移。

        阶段十二：strategy 决定本次检索参数（HyDE 开关、Dense/Sparse 路数、
        recall/rerank 条数、RRF k、resource_type 过滤）。strategy=None 时全部参数
        回落全局 settings，行为与 Baseline（阶段十一及之前）完全一致。

        阶段十三：kg_enabled 策略在向量命中之外追加 KG 关系证据（补充证据），
        KG 异常一律吞掉并记日志，不影响向量检索结果与本次问答。

        Raises:
            AppException: 422 检索/重排失败
        """
        from src.core.exceptions import AppException

        # 加载运行时配置并初始化 embedding/llm/rerank/hyde
        await self._ensure_components()

        # 策略 → 生效参数（None 覆盖项自动回落到全局 settings）
        cfg = resolve_retrieval_config(strategy, resource_types)
        recall = cfg["recall_top_k"]
        # 资源过滤：None 表示与 Baseline 一样不限制来源
        rtypes = tuple(cfg["resource_types"]) or None
        include_document = cfg["include_document"]

        # HyDE 查询改写（TECH_DESIGN：用假设答案替换原问题做向量检索）
        # 仅改写检索查询；最终 Prompt 仍用原始问题。失败回退原问题。
        query_text = question
        if cfg["hyde_enabled"] and self.hyde is not None:
            try:
                hypo = await asyncio.to_thread(self.hyde.generate, question)
                if hypo and hypo.strip():
                    query_text = hypo.strip()
                    logger.info(
                        f"HyDE 改写: {question[:30]}... -> {query_text[:50]}..."
                    )
            except HyDEError as exc:
                logger.warning(f"HyDE 改写失败，回退原问题: {exc}")

        # 混合检索（CPU/IO 密集放线程池）：稠密语义召回 + 稀疏关键词召回
        try:
            query_dense, query_sparse = await asyncio.to_thread(
                self.embedding.encode, [query_text]
            )
            kb_str = [str(k) for k in kb_ids]
            dense_hits = []
            if cfg["dense_enabled"]:
                dense_hits = await asyncio.to_thread(
                    self.store.search,
                    query_dense[0],
                    kb_str,
                    recall,
                    list(rtypes) if rtypes else None,
                    include_document,
                )
            # 保留稠密语义相似度：RRF 融合会用排名分覆盖 score 字段，
            # 相关性门槛（_is_relevant）需要原始 COSINE 相似度
            dense_hits = [{**h, "dense_score": h.get("score", 0.0)} for h in dense_hits]
            sparse_hits = []
            if cfg["sparse_enabled"] and query_sparse and query_sparse[0]:
                sparse_hits = await asyncio.to_thread(
                    self.store.search_sparse,
                    query_sparse[0],
                    kb_str,
                    recall,
                    list(rtypes) if rtypes else None,
                    include_document,
                )
        except (EmbeddingError, VectorStoreError) as exc:
            logger.error(f"RAG 检索失败: {exc}")
            raise AppException(422, f"知识检索失败：{exc}") from exc

        # RRF 融合 → Top-recall 候选
        fused = rrf_fusion([dense_hits, sparse_hits], k=cfg["rrf_k"], top_n=recall)

        # Rerank 精排 → Top-N 送 LLM
        try:
            hits = await asyncio.to_thread(
                self.rerank.rerank, question, fused, cfg["rerank_top_k"]
            )
        except RerankError as exc:
            # BUG-017：Reranker 是增强环节，不得成为问答的单点故障。
            # 与 KG 检索一致的降级语义：模型/服务不可用时退回 RRF 融合顺序，
            # 召回质量下降但仍可作答（此时拒绝式静默优于 422 中断）。
            logger.warning(f"RAG 重排不可用，本次使用 RRF 融合顺序: {exc}")
            hits = fused[: cfg["rerank_top_k"]]

        # 资源过滤（后置权威过滤）：向量库侧过滤生效时此处为 no-op；
        # 老集合不支持动态字段时由此保证策略语义一致。
        hits = filter_hits(hits, rtypes, include_document)

        # 阶段十三：KG 关系证据（补充，不替代向量命中；失败不影响本次问答）
        kg_hits: list[dict] = []
        if cfg["kg_enabled"]:
            try:
                kg_hits = await self.kg.retrieve(
                    list(kb_ids),
                    analysis,
                    max_hops=cfg["kg_max_hops"],
                    top_k=cfg["kg_top_k"],
                )
                # 与向量命中同一套资源过滤口径
                kg_hits = filter_hits(kg_hits, rtypes, include_document)
                # BUG-064（已知限制，本批不修改行为）：KG 命中拼在末尾，
                # 未经 Reranker 与向量命中统一排序，因此 KG 证据编号总是最大的一批；
                # 而 group_evidence() 按组内最高分重排，会导致前端分层展示顺序与
                # source_index 编号顺序不一致。**刻意不在此处重排**——那会改变
                # Prompt 编号与既有引用对应关系（属于检索业务规则变更），
                # 需要带评测数据的专门实验，不由质量收尾批次顺手改动。
                # 当前一致性由 test_batch6_quality.py 的锁定用例守护。
                hits = hits + kg_hits
            except Exception as exc:  # noqa: BLE001  KG 不得成为问答的单点故障
                logger.warning(f"KG 检索不可用，本次仅用向量检索: {exc}")
                kg_hits = []

        logger.info(
            f"混合检索 strategy={cfg['strategy_name']} dense={len(dense_hits)} "
            f"sparse={len(sparse_hits)} fused={len(fused)} "
            f"reranked={len(hits) - len(kg_hits)} kg={len(kg_hits)}"
        )

        # 为精排后的命中注入 doc_name（Prompt 与引用卡片均需展示文档名）
        await self._enrich_hits_with_doc_name(hits)
        return hits

    def _is_relevant(self, hits: list[dict]) -> bool:
        """相关性门槛（PRD §8.3 幻觉兜底）：Top 候选的稠密语义相似度均低于
        RELEVANCE_THRESHOLD 时判定为"仅词语重叠、答非所问"，应拒答。

        - 稠密 COSINE 相似度反映语义相关性；纯稀疏/关键词命中不参与豁免
          （无 dense_score 按 0 计）——词语重叠不代表能回答，正是本门槛要拦的场景。
        - 阈值经 .env RELEVANCE_THRESHOLD 调整：真实 BGE-M3 下无关文本通常
          0.3~0.5、相关文本 0.6+；mock 向量化得分普遍 ~0.75（不拦截，仅开发用）。
        """
        if not hits:
            return False
        best = max(h.get("dense_score", 0.0) for h in hits)
        if best < settings.RELEVANCE_THRESHOLD:
            logger.info(
                f"相关性不足（best_dense_score={best:.4f} < "
                f"{settings.RELEVANCE_THRESHOLD}）"
            )
            return False
        return True

    def evaluate_evidence_gate(
        self,
        hits: list[dict],
        analysis: QueryAnalysis | None = None,
        router_decision: RouterDecision | None = None,
        *,
        allow_retry: bool = True,
        strategy_name: str | None = None,
    ) -> GateDecision | None:
        """阶段十四：检索命中 → 统一 Evidence → Gate 决策。

        - Gate 关闭（settings.EVIDENCE_GATE_ENABLED=False）返回 None，
          调用方按阶段十三及之前的行为继续（不拦截）。
        - Gate 自身异常由 EvidenceGate 内部兜底为 accept（is_valid=False）；
          此处再兜一层，保证门控永远不会让问答失败（非单点故障）。
        """
        if not getattr(self.gate, "enabled", False):
            return None
        try:
            evidence = build_evidence(hits)
            return self.gate.evaluate(
                evidence,
                analysis,
                router_decision,
                allow_retry=allow_retry,
                strategy_name=strategy_name,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"Evidence Gate 不可用，安全放行: {exc}")
            return fallback_gate_decision(
                f"gate_unavailable: {exc}", len(hits or [])
            )

    async def _apply_evidence_gate(
        self,
        kb_ids: list[uuid.UUID],
        question: str,
        hits: list[dict],
        analysis: QueryAnalysis | None,
        router_decision: RouterDecision | None,
        strategy: RetrievalStrategy | None,
    ) -> tuple[list[dict], GateDecision | None]:
        """阶段十四：执行 Gate，并在 retry 判定时换策略重试（**最多一次**）。

        返回 (最终 hits, GateDecision)：
        - retry 后重新评估一次（allow_retry=False），仍不足即 insufficient；
        - retry 未改善（被接受证据数变少）时保留原 hits，决策按最终评估给出；
        - retry 检索异常 → 保留原 hits 与原决策（不因重试失败丢证据）。
        """
        if not getattr(self.gate, "enabled", False):
            return hits, None

        strategy_name = (
            router_decision.strategy_name
            if router_decision is not None
            else (strategy.name if strategy is not None else None)
        )
        decision = self.evaluate_evidence_gate(
            hits, analysis, router_decision, strategy_name=strategy_name
        )
        if decision is None or decision.decision != DECISION_RETRY:
            return hits, decision
        if not decision.retry_strategy:
            return hits, decision

        retry_strategy = get_strategy(decision.retry_strategy)
        if retry_strategy is None:
            return hits, decision

        original = decision.original_strategy or strategy_name
        retry_reason = decision.reason
        logger.info(
            f"Evidence Gate 建议重试（{retry_reason}）: "
            f"{original} → {decision.retry_strategy}"
        )
        # retry 策略可能需要 QueryAnalysis（multi_source 运行时过滤 / KG 图遍历）
        ana = analysis if analysis is not None else self.analyze_query(question)
        retry_types: list[str] | None = None
        if retry_strategy.resource_types_from_analysis:
            retry_types = list(ana.resource_types or [])

        try:
            retry_hits = await self._retrieve(
                kb_ids, question, retry_strategy, retry_types, analysis=ana
            )
        except Exception as exc:  # noqa: BLE001  重试失败不应丢掉原结果
            logger.warning(f"Evidence Gate retry 检索失败，保留原结果: {exc}")
            return hits, decision

        final = self.evaluate_evidence_gate(
            retry_hits,
            ana,
            router_decision,
            allow_retry=False,
            strategy_name=decision.retry_strategy,
        )
        merged = with_retry(
            final if final is not None else decision,
            original_strategy=original,
            retry_strategy=decision.retry_strategy,
            retry_reason=retry_reason,
        )
        # retry 未改善 → 保留原证据（引用仍可展示），决策按最终评估给出
        if final is not None and final.accepted_count < decision.accepted_count:
            return hits, merged
        return retry_hits, merged

    # ── 阶段十五：Self Reflection（生成之后）───────────────────────────────

    def evaluate_reflection(
        self,
        answer: str,
        hits: list[dict],
        *,
        query: str = "",
        analysis: QueryAnalysis | None = None,
        gate_decision: GateDecision | None = None,
        allow_retry: bool = True,
        allow_revise: bool = True,
        strategy_name: str | None = None,
        llm_findings: dict | None = None,
        counts: dict | None = None,
    ) -> ReflectionDecision | None:
        """阶段十五：Answer + Evidence + GateDecision → ReflectionDecision。

        - Reflection 关闭（settings.SELF_REFLECTION_ENABLED=False）返回 None，
          调用方按阶段十四行为继续（不反思）。
        - Reflection 自身异常由 SelfReflection 内部兜底为 accept（is_valid=False）；
          此处再兜一层，保证反思永远不会让问答失败（非单点故障）。
        """
        if not getattr(self.reflector, "enabled", False):
            return None
        try:
            return self.reflector.evaluate(
                answer,
                hits,
                query=query,
                query_analysis=analysis,
                gate_decision=gate_decision,
                allow_retry=allow_retry,
                allow_revise=allow_revise,
                strategy_name=strategy_name,
                llm_findings=llm_findings,
                counts=counts,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"Self Reflection 不可用，保留原答案: {exc}")
            return fallback_reflection_decision(
                f"reflection_unavailable: {exc}", len(hits or [])
            )

    async def _llm_consistency_check(
        self, question: str, hits: list[dict], answer: str
    ) -> dict | None:
        """可选 LLM 一致性检查（单次调用，超时/异常都不影响链路）。

        关闭或不可用返回 None（调用方按"不采纳 LLM 结论"处理）。
        """
        reflector = getattr(self, "reflector", None)
        if reflector is None or not getattr(reflector, "enabled", False):
            return None
        if not getattr(reflector, "llm_enabled", False):
            return None
        if self.llm is None:
            await self._ensure_components()
        if self.llm is None:
            return None
        try:
            findings = await llm_consistency_check(
                self.llm, question, build_evidence(hits), answer
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"LLM Reflection 不可用，回落到规则反思: {exc}")
            return None
        if findings.get("error"):
            logger.info(f"LLM Reflection 结论不可用: {findings.get('error')}")
        logger.info(
            f"LLM Reflection supported={findings.get('supported')} "
            f"claims={len(findings.get('unsupported_claims') or [])} q={question[:30]!r}"
        )
        return findings

    async def _revise_answer(
        self, question: str, hits: list[dict], answer: str
    ) -> tuple[str, bool]:
        """revise：基于**现有证据**重写一次更保守的答案（不重新检索，最多一次）。

        Returns:
            (revised_answer, ok) — ok=False 表示 LLM 调用失败，保留原答案。
        """
        try:
            await self._ensure_components()
            messages = build_revision_messages(question, build_evidence(hits), answer)
            revised = await self.llm.chat(messages)  # type: ignore[union-attr]
            revised = (revised or "").strip()
            if not revised:
                return answer, False
            return revised, True
        except Exception as exc:  # noqa: BLE001  revise 失败不得影响原答案
            logger.warning(f"Reflection revise 失败，保留原答案: {exc}")
            return answer, False

    async def _generate_answer(
        self, question: str, hits: list[dict], history: list[dict] | None = None
    ) -> str:
        """按既有 Prompt 结构（System + 历史 + 带编号资料）生成一次答案。"""
        await self._ensure_components()
        user_prompt = self._build_user_prompt(question, hits)
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            *(history or []),
            {"role": "user", "content": user_prompt},
        ]
        answer = await self.llm.chat(messages)  # type: ignore[union-attr]
        return (answer or "").strip() or "（模型未返回内容，请重试）"

    async def _reflect_and_finalize(
        self,
        *,
        kb_ids: list[uuid.UUID],
        question: str,
        history: list[dict] | None,
        analysis: QueryAnalysis | None,
        hits: list[dict],
        gate_decision: GateDecision | None,
        answer: str,
        strategy_name: str | None,
        allow_retry: bool,
    ) -> tuple[str, list[dict], GateDecision | None, ReflectionDecision | None]:
        """阶段十五：生成之后执行 Self Reflection 并产出最终答案。

        流程（次数上限严格受限）：
        1. accept → 原答案返回
        2. revise → 基于同一份证据重写一次（最多一次），重写后不再 revise/retry
        3. retry  → 换策略重检索一次（最多一次）→ Gate（不再给 Gate retry）
                    → 重新生成 → 再反思一次；仍失败 → 保守回答
        计数：gate_retry_count / reflection_retry_count / total_retry_count
        分别记录，保证总数有界（Gate ≤1 + Reflection ≤1）。

        Returns:
            (final_answer, final_hits, gate_decision, reflection_decision)
        """
        if not getattr(self.reflector, "enabled", False):
            self.last_reflection_decision = None
            return answer, hits, gate_decision, None

        # Gate retry 次数直接引用 GateDecision（不重复计算 Gate）
        counts: dict = {
            "gate_retry_count": 1 if getattr(gate_decision, "retried", False) else 0,
            "reflection_retry_count": 0,
            "revised": False,
        }
        ana = analysis if analysis is not None else self.analyze_query(question)
        findings = await self._llm_consistency_check(question, hits, answer)
        decision = self.evaluate_reflection(
            answer,
            hits,
            query=question,
            analysis=ana,
            gate_decision=gate_decision,
            allow_retry=allow_retry,
            allow_revise=True,
            strategy_name=strategy_name,
            llm_findings=findings,
            counts=counts,
        )
        if decision is None:
            self.last_reflection_decision = None
            return answer, hits, gate_decision, None

        if decision.decision == REFLECTION_ACCEPT:
            self.last_reflection_decision = decision
            return answer, hits, gate_decision, decision

        if decision.decision == REFLECTION_REVISE:
            logger.info(f"Self Reflection revise: {decision.reason} q={question[:30]!r}")
            revised, ok = await self._revise_answer(question, hits, answer)
            counts["revised"] = ok
            final = self.evaluate_reflection(
                revised,
                hits,
                query=question,
                analysis=ana,
                gate_decision=gate_decision,
                allow_retry=False,  # revise 之后不再 retry（预算有界）
                allow_revise=False,  # revise 最多一次
                strategy_name=strategy_name,
                llm_findings=None,
                counts=counts,
            )
            if final is None:
                final = decision
            final = with_counts(with_revision(final), counts)
            if not ok:
                final = with_fallback(final, "revision_failed")
            self.last_reflection_decision = final
            return (revised if ok else answer), hits, gate_decision, final

        # ── DECISION_RETRY：换策略重检索一次（最多一次）─────────────────────
        retry_strategy = get_strategy(decision.retry_strategy or "")
        if not allow_retry or retry_strategy is None:
            # 无可用 retry 预算/策略：沿用降级后的决策，保留原答案与证据
            final = with_counts(decision, counts)
            self.last_reflection_decision = final
            return answer, hits, gate_decision, final

        logger.info(
            f"Self Reflection retry: {decision.reason} → "
            f"{decision.retry_strategy} q={question[:30]!r}"
        )
        counts["reflection_retry_count"] += 1
        retry_types: list[str] | None = None
        if retry_strategy.resource_types_from_analysis:
            retry_types = list(ana.resource_types or [])
        try:
            retry_hits = await self._retrieve(
                kb_ids, question, retry_strategy, retry_types, analysis=ana
            )
        except Exception as exc:  # noqa: BLE001  重试失败不得丢掉原结果
            logger.warning(f"Self Reflection retry 检索失败，保留原结果: {exc}")
            final = with_fallback(with_counts(decision, counts), "reflection_retry_failed")
            self.last_reflection_decision = final
            return answer, hits, gate_decision, final

        # retry 之后重新执行 Gate（不再给 Gate retry，避免与 Reflection retry 叠加）
        retry_gate = self.evaluate_evidence_gate(
            retry_hits,
            ana,
            None,
            allow_retry=False,
            strategy_name=retry_strategy.name,
        )
        if retry_gate is not None and retry_gate.decision != DECISION_ACCEPT:
            # 仍然拿不到足够的证据 → 进入最终保守回答（禁止模型编造）
            logger.info("Self Reflection retry 后 Gate 仍判定不足，进入保守回答")
            final = with_reflection_retry(
                with_counts(decision, counts),
                original_strategy=strategy_name,
                retry_strategy=retry_strategy.name,
                retry_reason=decision.reason,
            )
            self.last_reflection_decision = final
            return CONSERVATIVE_ANSWER, retry_hits, retry_gate, final

        try:
            retry_answer = await self._generate_answer(question, retry_hits, history)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"Self Reflection retry 生成失败，保留原答案: {exc}")
            final = with_fallback(with_counts(decision, counts), "reflection_retry_generate_failed")
            self.last_reflection_decision = final
            return answer, hits, gate_decision, final

        retry_findings = await self._llm_consistency_check(question, retry_hits, retry_answer)
        final = self.evaluate_reflection(
            retry_answer,
            retry_hits,
            query=question,
            analysis=ana,
            gate_decision=retry_gate,
            allow_retry=False,  # Reflection retry 最多一次
            allow_revise=True,
            strategy_name=retry_strategy.name,
            llm_findings=retry_findings,
            counts=counts,
        )
        if final is not None and final.decision == REFLECTION_REVISE:
            revised, ok = await self._revise_answer(question, retry_hits, retry_answer)
            counts["revised"] = ok
            after = self.evaluate_reflection(
                revised,
                retry_hits,
                query=question,
                analysis=ana,
                gate_decision=retry_gate,
                allow_retry=False,
                allow_revise=False,
                strategy_name=retry_strategy.name,
                llm_findings=None,
                counts=counts,
            )
            final = with_counts(with_revision(after or final), counts)
            if not ok:
                final = with_fallback(final, "revision_failed")
            retry_answer = revised if ok else retry_answer

        # retry 未改善（比如反思仍认为答案超出证据）→ 保留 retry 后的证据，原样返回
        final = with_reflection_retry(
            with_counts(final if final is not None else decision, counts),
            original_strategy=strategy_name,
            retry_strategy=retry_strategy.name,
            retry_reason=decision.reason,
        )
        self.last_reflection_decision = final
        return retry_answer, retry_hits, retry_gate, final

    def _hits_to_citations(self, hits: list[dict]) -> list[dict]:
        """将检索命中转为引用来源（BUSINESS_RULES §6：只展示相关度 ≥ 0.3 的引用）。

        流式路径用：citations 在生成前推送，实现引用卡片实时展示。
        DB 持久化用同一份，保证历史回看与流式一致。

        Stage 4-5：区分 document / resource 来源。Resource Citation 携带
        resource_type/resource_id/resource_name；Document Citation 保持原结构
        兼容。source_kind 字段标识来源类别，evidence_level 按分数分级。

        阶段十：Citation 由统一 Evidence 模型（application.evidence）生成——
        Evidence 是 Citation 的超集，旧字段不变，新增 evidence_id/source_id/
        source_name/source_label/evidence_text，使 Document 与 Resource 命中
        进入同一结构，可直接交给多来源分组展示。
        """
        return build_evidence(hits)

    @staticmethod
    def _evidence_payload(citations: list[dict], gate: dict | None = None) -> dict:
        """构造 SSE citations 事件 data：兼容旧 citations，并附加阶段十证据结构。

        返回 {"citations", "evidence", "evidence_groups", "evidence_summary"}：
        - citations：旧字段不变（前端旧解析逻辑继续可用）
        - evidence：统一 Evidence 列表（与 citations 同内容，语义更明确）
        - evidence_groups：多来源分组（document / herb / prescription / ...）
        - evidence_summary：证据数、来源数、分组数、各等级数量
        - evidence_gate（阶段十四，可选）：Gate 决策 dict；Gate 关闭时为 None
        """
        groups, summary = package_evidence(citations)
        return {
            "citations": citations,
            "evidence": citations,
            "evidence_groups": groups,
            "evidence_summary": summary,
            # 阶段十三：KG 证据切片（citations/evidence 结构不变，仅新增可选视图）
            "kg_evidence": [
                c for c in citations if c.get("source_kind") == SOURCE_KIND_KG
            ],
            # 阶段十四：证据门控决策（事件名与顺序不变，旧客户端忽略新增字段即可）
            "evidence_gate": gate,
        }

    def analyze_query(self, question: str) -> QueryAnalysis:
        """阶段十一：检索前的问题分析（Query → QueryAnalysis）。

        与检索解耦：纯本地规则，不调用 LLM / 不访问向量库，且不读取或修改任何
        检索参数（top_k / rerank / HyDE / Dense-Sparse 权重均保持 Baseline）。
        分析失败由 QueryAnalyzer 内部兜底为 general；此处再兜一层，
        确保 Analyzer 完全不可用时也不会让 /chat/ask 失败（非单点故障）。
        """
        try:
            return self.analyzer.analyze(question)
        except Exception as exc:
            logger.warning(f"Query 分析不可用，回落 general: {exc}")
            return fallback_analysis(question, f"analyzer_unavailable: {exc}")

    def route_query(self, analysis: QueryAnalysis) -> RouterDecision:
        """阶段十二：QueryAnalysis → RouterDecision（Analyzer 之后、Retrieval 之前）。

        Router 只消费 QueryAnalysis 的结构化字段，不重新解析原始 query。
        Router 自身异常同样兜底为 baseline_hybrid（非单点故障）。
        """
        try:
            return self.router.route(analysis)
        except Exception as exc:
            logger.warning(f"路由不可用，回落 Baseline: {exc}")
            return fallback_decision(analysis, f"router_unavailable: {exc}")

    def plan_retrieval(self, question: str) -> tuple[QueryAnalysis, RouterDecision]:
        """阶段十二：Query → Analyzer → Router（检索前的完整决策链，均带兜底）。

        Returns:
            (query_analysis, router_decision)
        """
        analysis = self.analyze_query(question)
        decision = self.route_query(analysis)
        return analysis, decision

    @staticmethod
    def _strategy_of(decision: RouterDecision) -> tuple[RetrievalStrategy | None, list[str]]:
        """从 RouterDecision 取策略对象与已解析的资源类型（未知策略返回 None）。"""
        strategy = get_strategy(decision.strategy_name)
        resource_types = list(decision.resource_filter.get("resource_types") or [])
        return strategy, resource_types

    async def ask(
        self,
        user: User,
        kb_ids: list[uuid.UUID],
        question: str,
        conversation_id: uuid.UUID | None = None,
    ) -> tuple[Conversation, Message, list[dict], QueryAnalysis, RouterDecision]:
        """执行 RAG 问答（编排：会话管理 + 持久化 + 缓存）。

        检索/生成核心委派 retrieve_and_answer，本方法负责会话创建、消息持久化、
        引用后处理与 Redis 缓存同步。多轮历史透传至核心以保持上下文。

        阶段十二：Query → Analyzer → Router → RetrievalStrategy → 现有 Baseline 检索，
        分析结果与路由决策随结果一起返回，供 /chat/ask 输出。

        Args:
            user: 当前登录用户（用于归属校验 + 新建会话绑定）
            kb_ids: 检索范围（调用方已校验权限）
            question: 原始问题
            conversation_id: 续用已有会话；None 则新建

        Returns:
            (conversation, assistant_message, citations, query_analysis, router_decision)

        Raises:
            NotFoundError: 会话不存在
            PermissionDeniedError: 会话归属不匹配
            AppException: 422 检索/生成失败
        """
        started_at = asyncio.get_event_loop().time()
        # 阶段十二：Query → Analyzer → Router → Strategy（两者均带兜底）
        query_analysis, router_decision = self.plan_retrieval(question)
        strategy, resource_types = self._strategy_of(router_decision)
        conversation = await self._ensure_conversation(user, kb_ids, question, conversation_id)
        history, cache_hit = await self._load_history(conversation.id)

        # 先持久化用户消息（独立事务）：保证与助手消息 created_at 不同，
        # 使历史查询 ORDER BY created_at 顺序确定。
        # BUG-036：后续失败时由 _discard_user_message 补偿删除，不留孤儿提问。
        user_msg = Message(
            conversation_id=conversation.id, role="user", content=question,
        )
        self.db.add(user_msg)
        await self.db.commit()

        # 检索+生成核心（不持久化；阶段十二按路由选择的策略执行；
        # 阶段十三透传 QueryAnalysis，供 KG 策略做图遍历）
        try:
            answer, hits = await self.retrieve_and_answer(
                kb_ids, question, history,
                strategy=strategy, resource_types=resource_types,
                analysis=query_analysis, router_decision=router_decision,
            )
        except Exception:
            await self._discard_user_message(user_msg)
            raise

        # 引用后处理：解析答案中的 [citation: 编号, 页码] → 引用来源；无标记时兜底全部来源
        citations = await self._build_citations(answer, hits)
        elapsed_ms = int((asyncio.get_event_loop().time() - started_at) * 1000)
        logger.info(
            f"RAG 问答完成 conv={conversation.id} hits={len(hits)} "
            f"citations={len(citations)} cache={'hit' if cache_hit else 'miss'} "
            f"question_type={query_analysis.question_type} "
            f"strategy={router_decision.strategy_name} 耗时={elapsed_ms}ms"
        )

        # 持久化助手消息（独立事务，引用 JSON）→ PostgreSQL（事实源）
        assistant = Message(
            conversation_id=conversation.id, role="assistant",
            content=answer, citations={"sources": citations},
        )
        self.db.add(assistant)
        await self.db.commit()
        await self.db.refresh(assistant)
        # 同步追加到 Redis 缓存（24h TTL，自动裁剪至最近 N 轮）
        await self.cache.append_message(conversation.id, "user", question)
        await self.cache.append_message(conversation.id, "assistant", answer)
        return conversation, assistant, citations, query_analysis, router_decision

    async def ask_stream(
        self,
        user: User,
        kb_ids: list[uuid.UUID],
        question: str,
        conversation_id: uuid.UUID | None = None,
    ) -> AsyncIterator[dict]:
        """流式 RAG 问答：yield SSE 事件 dict（{"event": ..., "data": ...}）。

        编排同 ask()，但生成阶段流式推送 chunk；持久化在生成完成后一次性完成。
        事件序列：start → citations → delta×N → done（出错时 error 替代 done）。
        阶段十：citations 事件在原 citations 之外附带 evidence / evidence_groups /
        evidence_summary（事件名与顺序不变，旧客户端解析不受影响）。
        阶段十一：start 事件在原 conversation_id 之外附带 query_analysis
        （事件名与顺序不变，旧客户端解析不受影响）。
        阶段十二：start 事件同时附带 router_decision（事件名与顺序不变）。

        Args:
            user: 当前登录用户（用于归属校验 + 新建会话绑定）
            kb_ids: 检索范围（调用方已校验权限）
            question: 原始问题
            conversation_id: 续用已有会话；None 则新建

        Raises:
            NotFoundError: 会话不存在
            PermissionDeniedError: 会话归属不匹配（由调用方先校验，此处兜底）
            AppException: 检索失败（端点层异常处理器返回 422，不进入流）；
                生成失败转 error 事件。BUG-036：以上失败路径均未落库助手消息，
                先落库的用户消息会被补偿删除，不留下无回答的孤儿提问。
        """
        started_at = asyncio.get_event_loop().time()
        # 阶段十二：Query → Analyzer → Router → Strategy（两者均带兜底）
        query_analysis, router_decision = self.plan_retrieval(question)
        strategy, resource_types = self._strategy_of(router_decision)
        conversation = await self._ensure_conversation(user, kb_ids, question, conversation_id)
        yield {
            "event": "start",
            "data": {
                "conversation_id": str(conversation.id),
                "query_analysis": query_analysis.to_dict(),
                "router_decision": router_decision.to_dict(),
            },
        }

        history, cache_hit = await self._load_history(conversation.id)

        # 先持久化用户消息（独立事务）：保证与助手消息 created_at 顺序确定。
        # BUG-036：后续失败时由 _discard_user_message 补偿删除，不留孤儿提问。
        user_msg = Message(
            conversation_id=conversation.id, role="user", content=question,
        )
        self.db.add(user_msg)
        await self.db.commit()

        # 检索+重排核心（不持久化；阶段十二按路由选择的策略执行；
        # 阶段十三透传 QueryAnalysis，供 KG 策略做图遍历）
        # 阶段十五：拒绝/保守分支不进入生成，不产出 Reflection
        self.last_reflection_decision = None
        try:
            hits = await self._retrieve(
                kb_ids, question, strategy=strategy, resource_types=resource_types,
                analysis=query_analysis,
            )

            # 阶段十四：Evidence Gate（含最多一次 retry）；Gate 关闭时返回 None
            hits, gate_decision = await self._apply_evidence_gate(
                kb_ids, question, hits, query_analysis, router_decision, strategy
            )
        except Exception:
            # 检索/Gate 失败：尚未产出任何助手内容 → 删除用户消息后向上抛出
            # （端点层转为 error 事件），避免会话里留下无回答的提问。
            await self._discard_user_message(user_msg)
            raise
        self.last_gate_decision = gate_decision
        gate_payload = gate_decision.to_dict() if gate_decision is not None else None

        if not self._is_relevant(hits):
            # 相关性门槛（PRD §8.3 幻觉兜底）：答非所问时直接拒答，不送 LLM，
            # 也不展示引用卡片（引用与问题无关的片段会误导用户）
            logger.info(f"检索相关性不足，拒答: question={question[:50]!r}")
            citations: list[dict] = []
            answer = REFUSAL_ANSWER
            yield {
                "event": "citations",
                "data": self._evidence_payload(citations, gate_payload),
            }
            yield {"event": "delta", "data": {"content": answer}}
        elif gate_decision is not None and gate_decision.decision != DECISION_ACCEPT:
            # 阶段十四：证据不足 → 拒答 + 保留证据/引用（禁止模型编造）
            logger.info(
                f"Evidence Gate 判定 {gate_decision.decision}"
                f"（{gate_decision.reason}），拒答: question={question[:50]!r}"
            )
            citations = self._hits_to_citations(hits)
            answer = GATE_REFUSAL_ANSWER
            yield {
                "event": "citations",
                "data": self._evidence_payload(citations, gate_payload),
            }
            yield {"event": "delta", "data": {"content": answer}}
        else:
            citations = self._hits_to_citations(hits)
            yield {
                "event": "citations",
                "data": self._evidence_payload(citations, gate_payload),
            }

            # 构造 Prompt 并流式生成
            user_prompt = self._build_user_prompt(question, hits)
            messages = [{"role": "system", "content": SYSTEM_PROMPT}, *(history or []),
                        {"role": "user", "content": user_prompt}]
            answer_parts: list[str] = []
            try:
                async for chunk in self.llm.chat_stream(messages):
                    answer_parts.append(chunk)
                    # BUG-066（已知限制，本批不修改 SSE 协议）：这里的 delta 携带的是
                    # **Reflection 之前**的文本——citations 必须在生成前下发，
                    # Self Reflection 又只能在完整答案产生后才能判定，二者天然有先后顺序。
                    # 因此可能出现"打字机显示了一段最终被改写的话"。
                    # 约定由 done 事件收口：**done.answer 才是最终权威答案**，
                    # 客户端应以 done.answer 为准替换正文（历史消息也是用它落库的）。
                    # 这里刻意不新增 corrected 事件、也不取消流式输出（保持协议稳定），
                    # 由 test_batch6_quality.py 锁定"事件名集合不变"这一契约。
                    yield {"event": "delta", "data": {"content": chunk}}
            except LLMError as exc:
                logger.error(f"RAG 流式生成失败: {exc}")
                # BUG-036：生成失败不会落库助手消息 → 同步删除用户消息，
                # 否则重试会在同一会话留下两条相同提问（且无回答）。
                await self._discard_user_message(user_msg)
                yield {"event": "error", "data": {"message": f"回答生成失败：{exc}"}}
                return
            answer = "".join(answer_parts).strip() or "（模型未返回内容，请重试）"

            # 阶段十五：Self Reflection（流式路径 Citations 事件已先发出，
            # 因此不做 Reflection retry——换检索策略会导致证据与已下发 citations
            # 不一致；此处只允许 accept / revise，且 revise 不改变 Evidence）
            answer, _hits, _gate, _reflection = await self._reflect_and_finalize(
                kb_ids=kb_ids,
                question=question,
                history=history,
                analysis=query_analysis,
                hits=hits,
                gate_decision=gate_decision,
                answer=answer,
                strategy_name=router_decision.strategy_name,
                allow_retry=False,
            )

        elapsed_ms = int((asyncio.get_event_loop().time() - started_at) * 1000)
        logger.info(
            f"RAG 流式问答完成 conv={conversation.id} hits={len(hits)} "
            f"citations={len(citations)} cache={'hit' if cache_hit else 'miss'} "
            f"question_type={query_analysis.question_type} "
            f"strategy={router_decision.strategy_name} 耗时={elapsed_ms}ms"
        )

        # 持久化助手消息（独立事务，引用 JSON）→ PostgreSQL（事实源）
        assistant = Message(
            conversation_id=conversation.id, role="assistant",
            content=answer, citations={"sources": citations},
        )
        self.db.add(assistant)
        await self.db.commit()
        await self.db.refresh(assistant)
        # 同步追加到 Redis 缓存（24h TTL，自动裁剪至最近 N 轮）
        await self.cache.append_message(conversation.id, "user", question)
        await self.cache.append_message(conversation.id, "assistant", answer)
        reflection_decision = self.last_reflection_decision
        yield {
            "event": "done",
            "data": {
                "conversation_id": str(conversation.id),
                "message_id": str(assistant.id),
                # 阶段十五：Reflection 可能把流式答案改写为更保守的版本，
                # done 事件下发最终权威文本（事件名与顺序不变，旧客户端忽略即可）
                "answer": answer,
                "reflection": (
                    reflection_decision.to_dict()
                    if reflection_decision is not None
                    else None
                ),
            },
        }

    async def _ensure_conversation(
        self,
        user: User,
        kb_ids: list[uuid.UUID],
        question: str,
        conversation_id: uuid.UUID | None,
    ) -> Conversation:
        """确保会话存在：有 conversation_id 则校验归属，无则创建新会话绑定当前用户。

        安全（AGENTS.md）：
        - 新会话一律绑定调用方 user.id（不再硬编码 DEFAULT_ADMIN_EMAIL）
        - 续用会话时校验归属（双保险，调用方已先校验过）

        Raises:
            NotFoundError: 会话不存在
            PermissionDeniedError: 会话不属于当前用户
        """
        if conversation_id is not None:
            conv = await self.db.get(Conversation, conversation_id)
            if conv is None:
                raise NotFoundError("会话不存在")
            if conv.user_id != user.id:
                raise PermissionDeniedError("无权访问该会话")
            return conv

        # 新建会话：绑定当前登录用户
        conv = Conversation(
            user_id=user.id,
            title=question[:20],
            kb_ids=list(kb_ids),
        )
        self.db.add(conv)
        await self.db.flush()
        return conv

    async def _load_history(self, conversation_id: uuid.UUID) -> tuple[list[dict], bool]:
        """加载最近 HISTORY_WINDOW 轮历史（按时间升序）。

        读路径（TECH_DESIGN：Redis 缓存 + PG 事实源）：
        1. 先查 Redis 缓存（命中 → 直接用）
        2. 未命中回源 PostgreSQL，并回填缓存（warm）

        Returns:
            (history, cache_hit) — cache_hit 表示本次是否命中缓存。
        """
        cached = await self.cache.get_history(conversation_id)
        if cached:
            return cached, True
        limit = max(settings.HISTORY_WINDOW, 0) * 2
        if limit == 0:
            return [], False
        stmt = (
            select(Message)
            .where(
                Message.conversation_id == conversation_id,
                Message.role.in_(["user", "assistant"]),
            )
            .order_by(Message.created_at.desc())
            .limit(limit)
        )
        rows = list((await self.db.scalars(stmt)).all())
        history = [{"role": m.role, "content": m.content} for m in reversed(rows)]
        await self.cache.warm(conversation_id, history)  # 回填缓存
        return history, False

    async def _discard_user_message(self, message: Message) -> None:
        """删除已落库但本次问答未能产出助手回复的用户消息（BUG-036 补偿清理）。

        用户消息先于检索/生成持久化（保证与助手消息 created_at 顺序确定），
        一旦后续环节失败就会留下"有提问无回答"的孤儿消息：用户重试同一会话会
        出现两条相同提问，且历史上下文被重复问题污染。故在失败路径上做补偿删除。

        失败仅记日志：补偿失败最多留一条孤儿消息，不得掩盖原始异常。
        """
        try:
            await self.db.delete(message)
            await self.db.commit()
        except Exception as exc:  # noqa: BLE001  补偿动作不得改变主流程的失败语义
            logger.error(f"清理孤儿用户消息失败 message_id={message.id}: {exc}")
            await self.db.rollback()  # 复位会话，避免污染后续错误处理

    async def _enrich_hits_with_doc_name(self, hits: list[dict]) -> None:
        """为检索命中注入文档名与来源可信度信息（就地修改）。

        Stage 4-4：区分 Document 命中与 Resource 命中：
        - Document 命中：doc_id 为文档 UUID → 以 documents 表为事实源，
          PG 缺失时由 Milvus 动态字段兜底（老集合无动态字段时由此处兜底）。
        - Resource 命中：doc_id 为 SHA256（非 UUID）→ 不查 documents 表；
          resource_name / resource_type / era 由 Milvus 动态字段带回，
          直接填充，doc_name = resource_name。

        BUG-021：判定依据是 **doc_id 是否为 UUID**，而不是"resource_type 是否为空"。
        老集合（未开启动态字段）取不到 resource_type，旧判定会把 SHA256 doc_id
        送进 Document.id（UUID 列）查询 → PG `invalid input syntax for type uuid`
        （500），且把资源证据误标成 source_kind='document'。按 doc_id 分类在
        两种集合上结论一致：KG 行 doc_id = "kg:<edge_id>" 同样非 UUID，归入
        Resource 分支且保留自带 source_kind=kg。
        """
        if not hits:
            return

        def _is_doc_row(hit: dict) -> bool:
            """Document 行 doc_id 为 UUID；Resource 行为 SHA256，KG 行为 kg:<id>。"""
            raw = hit.get("doc_id")
            if not raw:
                return False
            try:
                uuid.UUID(str(raw))
            except (ValueError, TypeError):
                return False
            return True

        doc_hits = [h for h in hits if _is_doc_row(h)]
        res_hits = [h for h in hits if not _is_doc_row(h)]

        # Document 命中：按 doc_id（UUID）批量查 documents 表
        doc_ids = {h["doc_id"] for h in doc_hits if h.get("doc_id")}
        docs = {
            str(d.id): d
            for d in (
                await self.db.scalars(select(Document).where(Document.id.in_(doc_ids)))
            ).all()
        } if doc_ids else {}
        for h in doc_hits:
            doc = docs.get(h["doc_id"])
            h["doc_name"] = doc.file_name if doc is not None else "未知文档"
            h["source_kind"] = "document"
            # 优先取 PG 事实源；PG 缺失（如老数据）时保留 Milvus 带回的值
            h["source_type"] = (
                doc.source_type if doc is not None and doc.source_type else h.get("source_type")
            )
            h["era"] = doc.era if doc is not None else None
            h["credibility_level"] = (
                doc.credibility_level
                if doc is not None and doc.credibility_level is not None
                else h.get("credibility_level")
            )

        # Resource 命中：元数据由 Milvus 动态字段带回，直接填充
        # 阶段十三：KG 命中同样带 resource_type（指回具体资源），但来源类别必须
        # 保持为 kg，否则会被误标成 resource 证据、丢掉图谱归属
        for h in res_hits:
            h["doc_name"] = h.get("resource_name") or h.get("resource_type") or "资源"
            if h.get("source_kind") != SOURCE_KIND_KG:
                h["source_kind"] = "resource"
            # source_type / credibility_level 在 Stage 4-3 写入时为 None（资源表无此字段）
            # 保留 Milvus 带回的值（可能为 None）
            h.setdefault("source_type", None)
            h.setdefault("era", None)
            h.setdefault("credibility_level", None)

    def _build_user_prompt(self, question: str, hits: list[dict]) -> str:
        """构造用户 Prompt：问题 + 编号参考资料（含文档名/资源名、页码、标题、原文）。

        编号即引用标识，模型按 System Prompt 输出 [citation: 编号, 页码]。
        Stage 4-4：Resource 命中标注为"资源：{类型} {名称}"，
        Document 命中保持"文档：{file_name}"，二者可在同一上下文共存。

        BUG-016：只对**可展示命中**（相关度 ≥ 阈值）编号 —— 编号空间与
        Evidence / Citation 完全一致，模型不可能引用到没有卡片的编号。
        """
        shown = displayable_hits(hits)
        if not shown:
            return question  # 无资料：模型应按 System Prompt 回答不知道
        blocks = []
        for i, h in enumerate(shown, start=1):
            source = h.get("title_path") or "未命名段落"
            page = h.get("page_num")
            page_label = f"页码：{page}" if page else "页码：0"
            provenance = self._format_provenance(h)
            if h.get("source_kind") == SOURCE_KIND_KG:
                # 阶段十三：KG 关系证据（标题即"银翘散 → 组成 → 金银花"）
                rtype_label = _RESOURCE_TYPE_LABELS.get(
                    h.get("resource_type"), h.get("resource_type") or "资源"
                )
                blocks.append(
                    f"[{i}] 图谱：{rtype_label} {h.get('doc_name', '未知')}{provenance} | "
                    f"{page_label} | 标题：{source}\n{h['content']}"
                )
            elif h.get("source_kind") == "resource":
                # Resource 命中：标注资源类型 + 名称
                rtype_label = _RESOURCE_TYPE_LABELS.get(
                    h.get("resource_type"), h.get("resource_type") or "资源"
                )
                blocks.append(
                    f"[{i}] 资源：{rtype_label} {h.get('doc_name', '未知')}{provenance} | "
                    f"{page_label} | 标题：{source}\n{h['content']}"
                )
            else:
                blocks.append(
                    f"[{i}] 文档：{h.get('doc_name', '未知')}{provenance} | "
                    f"{page_label} | 标题：{source}\n{h['content']}"
                )
        header = f"{_CONTEXT_MARKER}（编号即引用标识，引用时标注 [citation: 编号, 页码]）："
        return f"{question}\n\n{header}\n" + "\n\n".join(blocks)

    @staticmethod
    def _format_provenance(h: dict) -> str:
        """格式化参考资料的来源标注片段：｜来源类型·年代·可信度LvN。"""
        parts = []
        if h.get("source_type"):
            parts.append(h["source_type"])
        if h.get("era"):
            parts.append(h["era"])
        if h.get("credibility_level") is not None:
            parts.append(f"可信度Lv{h['credibility_level']}")
        return f"（{'·'.join(parts)}）" if parts else ""

    async def _build_citations(self, answer: str, hits: list[dict]) -> list[dict]:
        """解析答案中的 [citation: 编号, 页码] 标记生成引用来源。

        - 编号对应 _build_user_prompt 中的资料编号（1-based）。
        - 页码以检索命中的权威 page_num 为准（模型标注仅作提示，不信任）。
        - 无标记但有资料时兜底返回全部来源（前端仍可展示引用卡片）。
        - BUSINESS_RULES §6：只展示相关度 ≥ 阈值的引用。

        Stage 4-5：区分 document / resource 来源。Resource Citation 携带
        resource_type/resource_id/resource_name；evidence_level 按分数分级。

        阶段十：与 _hits_to_citations 一样走统一 Evidence 模型，保证
        非流式 / 流式两条路径的 Evidence 结构完全一致。
        """
        if not hits:
            return []
        # BUG-016：编号空间统一为"可展示命中"，与 _build_user_prompt 一致
        shown = displayable_hits(hits)
        # 按首次出现顺序去重提取编号
        idxs: list[int] = []
        for m in _CITATION_PATTERN.finditer(answer):
            n = int(m.group(1))
            if 1 <= n <= len(shown) and n - 1 not in idxs:
                idxs.append(n - 1)
        picked = idxs or list(range(len(shown)))

        citations = []
        for i in picked:
            # 统一 Evidence（Citation 超集）
            # BUSINESS_RULES §6 的阈值过滤已由 displayable_hits 统一执行，
            # 此处不再二次过滤，保证"编号 → 卡片"一一对应（BUG-016）
            citations.append(hit_to_evidence(shown[i], i + 1))
        return citations
