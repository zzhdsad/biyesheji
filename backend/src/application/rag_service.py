"""基础 RAG 问答用例：向量化检索 → 上下文拼接 → LLM 生成 → 引用溯源 → 持久化。

TECH_DESIGN / AGENTS.md 约束：
- System Prompt 强制"仅根据参考资料回答，找不到就说知道"（防幻觉）
- 所有检索强制 kb_id 过滤（权限隔离）
- 答案引用标注 [citation: 来源编号, 页码]，后处理解析匹配生成引用来源（文档名、页码、段落）
- 多轮对话：Redis 缓存最近 HISTORY_WINDOW 轮历史（24h TTL），未命中回源 PG 并回填
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

_CONTEXT_MARKER = "参考资料"
# [citation: 1, 3] 或 [citation: 1]；页码可选，兼容有无空格
_CITATION_PATTERN = re.compile(r"\[citation:\s*(\d+)\s*(?:,\s*(\d+)\s*)?\]", re.IGNORECASE)


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
    ) -> tuple[str, list[dict]]:
        """RAG 检索+生成核心（不持久化，供评估与编排复用）。

        流程：HyDE 改写 → 混合检索 → RRF 融合 → Rerank → 上下文拼接 → LLM 生成。
        不写会话/消息/缓存，避免评估批量调用污染聊天记录。

        Args:
            kb_ids: 检索范围（权限隔离，禁止越权）
            question: 原始问题（Prompt 用之；检索查询可能被 HyDE 改写）
            history: 多轮历史（chat 路径传入；评估为 None，单轮）

        Returns:
            (answer, hits) — hits 含 doc_name/content/page_num/title_path/score，
            供引用构建与评估（retrieved_contexts）复用。
            检索相关性不足（Top 候选稠密相似度均低于 RELEVANCE_THRESHOLD）时
            返回固定拒答文案，hits 为空，不调用 LLM。

        Raises:
            AppException: 422 检索/生成失败
        """
        from src.core.exceptions import AppException

        # 检索+重排核心（HyDE → 混合检索 → RRF → Rerank → 注入文档名）
        hits = await self._retrieve(kb_ids, question)

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
        return answer, hits

    async def _retrieve(
        self, kb_ids: list[uuid.UUID], question: str
    ) -> list[dict]:
        """检索+重排核心：HyDE → 混合检索 → RRF → Rerank → 注入文档名。

        抽取自 retrieve_and_answer，供非流式与流式路径共用，避免逻辑漂移。

        Raises:
            AppException: 422 检索/重排失败
        """
        from src.core.exceptions import AppException

        # 加载运行时配置并初始化 embedding/llm/rerank/hyde
        await self._ensure_components()

        # HyDE 查询改写（TECH_DESIGN：用假设答案替换原问题做向量检索）
        # 仅改写检索查询；最终 Prompt 仍用原始问题。失败回退原问题。
        query_text = question
        if self.hyde is not None:
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
            recall = settings.RECALL_TOP_K
            dense_hits = await asyncio.to_thread(
                self.store.search, query_dense[0], kb_str, recall
            )
            # 保留稠密语义相似度：RRF 融合会用排名分覆盖 score 字段，
            # 相关性门槛（_is_relevant）需要原始 COSINE 相似度
            dense_hits = [{**h, "dense_score": h.get("score", 0.0)} for h in dense_hits]
            sparse_hits = []
            if query_sparse and query_sparse[0]:
                sparse_hits = await asyncio.to_thread(
                    self.store.search_sparse, query_sparse[0], kb_str, recall
                )
        except (EmbeddingError, VectorStoreError) as exc:
            logger.error(f"RAG 检索失败: {exc}")
            raise AppException(422, f"知识检索失败：{exc}") from exc

        # RRF 融合 → Top-RECALL_TOP_K 候选
        fused = rrf_fusion(
            [dense_hits, sparse_hits], k=settings.RRF_K, top_n=settings.RECALL_TOP_K
        )

        # Rerank 精排 → Top-N 送 LLM
        try:
            hits = await asyncio.to_thread(
                self.rerank.rerank, question, fused, settings.RERANK_TOP_N
            )
        except RerankError as exc:
            logger.error(f"RAG 重排失败: {exc}")
            raise AppException(422, f"重排序失败：{exc}") from exc

        logger.info(
            f"混合检索 dense={len(dense_hits)} sparse={len(sparse_hits)} "
            f"fused={len(fused)} reranked={len(hits)}"
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

    def _hits_to_citations(self, hits: list[dict]) -> list[dict]:
        """将检索命中转为引用来源（BUSINESS_RULES §6：只展示相关度 ≥ 0.3 的引用）。

        流式路径用：citations 在生成前推送，实现引用卡片实时展示。
        DB 持久化用同一份，保证历史回看与流式一致。
        """
        threshold = settings.RELEVANCE_THRESHOLD
        citations = []
        for i, h in enumerate(hits, start=1):
            score = h.get("rerank_score", h.get("score", 0.0))
            # BUSINESS_RULES §6 引用过滤：只展示相关度 ≥ 阈值的引用
            if score < threshold:
                continue
            citations.append(
                {
                    "chunk_id": h["id"],
                    "source_index": i,
                    "doc_id": h["doc_id"],
                    "doc_name": h.get("doc_name", "未知文档"),
                    "page_num": h["page_num"],
                    "title_path": h["title_path"],
                    "content": h["content"],
                    "score": score,
                }
            )
        return citations

    async def ask(
        self,
        user: User,
        kb_ids: list[uuid.UUID],
        question: str,
        conversation_id: uuid.UUID | None = None,
    ) -> tuple[Conversation, Message, list[dict]]:
        """执行 RAG 问答（编排：会话管理 + 持久化 + 缓存）。

        检索/生成核心委派 retrieve_and_answer，本方法负责会话创建、消息持久化、
        引用后处理与 Redis 缓存同步。多轮历史透传至核心以保持上下文。

        Args:
            user: 当前登录用户（用于归属校验 + 新建会话绑定）
            kb_ids: 检索范围（调用方已校验权限）
            question: 原始问题
            conversation_id: 续用已有会话；None 则新建

        Returns:
            (conversation, assistant_message, citations)

        Raises:
            NotFoundError: 会话不存在
            PermissionDeniedError: 会话归属不匹配
            AppException: 422 检索/生成失败
        """
        started_at = asyncio.get_event_loop().time()
        conversation = await self._ensure_conversation(user, kb_ids, question, conversation_id)
        history, cache_hit = await self._load_history(conversation.id)

        # 先持久化用户消息（独立事务）：保证与助手消息 created_at 不同，
        # 使历史查询 ORDER BY created_at 顺序确定；且生成失败时用户意图仍留存。
        self.db.add(Message(
            conversation_id=conversation.id, role="user", content=question,
        ))
        await self.db.commit()

        # 检索+生成核心（不持久化）
        answer, hits = await self.retrieve_and_answer(kb_ids, question, history)

        # 引用后处理：解析答案中的 [citation: 编号, 页码] → 引用来源；无标记时兜底全部来源
        citations = await self._build_citations(answer, hits)
        elapsed_ms = int((asyncio.get_event_loop().time() - started_at) * 1000)
        logger.info(
            f"RAG 问答完成 conv={conversation.id} hits={len(hits)} "
            f"citations={len(citations)} cache={'hit' if cache_hit else 'miss'} "
            f"耗时={elapsed_ms}ms"
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
        return conversation, assistant, citations

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

        Args:
            user: 当前登录用户（用于归属校验 + 新建会话绑定）
            kb_ids: 检索范围（调用方已校验权限）
            question: 原始问题
            conversation_id: 续用已有会话；None 则新建

        Raises:
            NotFoundError: 会话不存在
            PermissionDeniedError: 会话归属不匹配（由调用方先校验，此处兜底）
            AppException: 检索失败（端点层异常处理器返回 422，不进入流）；
                生成失败转 error 事件，已持久化的用户消息保留。
        """
        started_at = asyncio.get_event_loop().time()
        conversation = await self._ensure_conversation(user, kb_ids, question, conversation_id)
        yield {"event": "start", "data": {"conversation_id": str(conversation.id)}}

        history, cache_hit = await self._load_history(conversation.id)

        # 先持久化用户消息（独立事务）：保证与助手消息 created_at 顺序确定；
        # 生成失败时用户意图仍留存。
        self.db.add(Message(
            conversation_id=conversation.id, role="user", content=question,
        ))
        await self.db.commit()

        # 检索+重排核心（不持久化）
        hits = await self._retrieve(kb_ids, question)

        if not self._is_relevant(hits):
            # 相关性门槛（PRD §8.3 幻觉兜底）：答非所问时直接拒答，不送 LLM，
            # 也不展示引用卡片（引用与问题无关的片段会误导用户）
            logger.info(f"检索相关性不足，拒答: question={question[:50]!r}")
            citations: list[dict] = []
            answer = REFUSAL_ANSWER
            yield {"event": "citations", "data": {"citations": citations}}
            yield {"event": "delta", "data": {"content": answer}}
        else:
            citations = self._hits_to_citations(hits)
            yield {"event": "citations", "data": {"citations": citations}}

            # 构造 Prompt 并流式生成
            user_prompt = self._build_user_prompt(question, hits)
            messages = [{"role": "system", "content": SYSTEM_PROMPT}, *(history or []),
                        {"role": "user", "content": user_prompt}]
            answer_parts: list[str] = []
            try:
                async for chunk in self.llm.chat_stream(messages):
                    answer_parts.append(chunk)
                    yield {"event": "delta", "data": {"content": chunk}}
            except LLMError as exc:
                logger.error(f"RAG 流式生成失败: {exc}")
                yield {"event": "error", "data": {"message": f"回答生成失败：{exc}"}}
                return
            answer = "".join(answer_parts).strip() or "（模型未返回内容，请重试）"

        elapsed_ms = int((asyncio.get_event_loop().time() - started_at) * 1000)
        logger.info(
            f"RAG 流式问答完成 conv={conversation.id} hits={len(hits)} "
            f"citations={len(citations)} cache={'hit' if cache_hit else 'miss'} "
            f"耗时={elapsed_ms}ms"
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
        yield {
            "event": "done",
            "data": {
                "conversation_id": str(conversation.id),
                "message_id": str(assistant.id),
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

    async def _enrich_hits_with_doc_name(self, hits: list[dict]) -> None:
        """为检索命中注入 doc_name（就地修改），供 Prompt 与引用卡片展示。"""
        if not hits:
            return
        doc_ids = {h["doc_id"] for h in hits if h.get("doc_id")}
        docs = {
            str(d.id): d.file_name
            for d in (
                await self.db.scalars(select(Document).where(Document.id.in_(doc_ids)))
            ).all()
        } if doc_ids else {}
        for h in hits:
            h["doc_name"] = docs.get(h["doc_id"], "未知文档")

    def _build_user_prompt(self, question: str, hits: list[dict]) -> str:
        """构造用户 Prompt：问题 + 编号参考资料（含文档名、页码、标题、原文）。

        编号即引用标识，模型按 System Prompt 输出 [citation: 编号, 页码]。
        """
        if not hits:
            return question  # 无资料：模型应按 System Prompt 回答不知道
        blocks = []
        for i, h in enumerate(hits, start=1):
            source = h.get("title_path") or "未命名段落"
            page = h.get("page_num")
            page_label = f"页码：{page}" if page else "页码：0"
            blocks.append(
                f"[{i}] 文档：{h.get('doc_name', '未知')} | {page_label} | 标题：{source}\n{h['content']}"
            )
        header = f"{_CONTEXT_MARKER}（编号即引用标识，引用时标注 [citation: 编号, 页码]）："
        return f"{question}\n\n{header}\n" + "\n\n".join(blocks)

    async def _build_citations(self, answer: str, hits: list[dict]) -> list[dict]:
        """解析答案中的 [citation: 编号, 页码] 标记生成引用来源。

        - 编号对应 _build_user_prompt 中的资料编号（1-based）。
        - 页码以检索命中的权威 page_num 为准（模型标注仅作提示，不信任）。
        - 无标记但有资料时兜底返回全部来源（前端仍可展示引用卡片）。
        - BUSINESS_RULES §6：只展示相关度 ≥ 阈值的引用。
        """
        if not hits:
            return []
        # 按首次出现顺序去重提取编号
        idxs: list[int] = []
        for m in _CITATION_PATTERN.finditer(answer):
            n = int(m.group(1))
            if 1 <= n <= len(hits) and n - 1 not in idxs:
                idxs.append(n - 1)
        picked = idxs or list(range(len(hits)))

        threshold = settings.RELEVANCE_THRESHOLD

        citations = []
        for i in picked:
            h = hits[i]
            score = h.get("rerank_score", h.get("score", 0.0))
            # BUSINESS_RULES §6：只展示相关度 ≥ 阈值的引用
            if score < threshold:
                continue
            citations.append(
                {
                    "chunk_id": h["id"],
                    "source_index": i + 1,  # 来源编号（1-based，对应答案标记）
                    "doc_id": h["doc_id"],
                    "doc_name": h.get("doc_name", "未知文档"),
                    "page_num": h["page_num"],
                    "title_path": h["title_path"],
                    "content": h["content"],
                    "score": score,
                }
            )
        return citations
