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

from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.config import settings
from src.core.exceptions import NotFoundError
from src.domain.models import Conversation, Document, Message, User
from src.infrastructure.embedding import BaseEmbedding, EmbeddingError, get_embedding
from src.infrastructure.hyde import BaseHyDE, HyDEError, get_hyde
from src.infrastructure.llm import BaseLLM, LLMError, get_llm
from src.infrastructure.milvus_store import BaseVectorStore, VectorStoreError, get_vector_store
from src.infrastructure.rerank import BaseRerank, RerankError, get_rerank
from src.infrastructure.redis_client import BaseConversationCache, get_conversation_cache
from src.utils.retrieval import rrf_fusion

SYSTEM_PROMPT = (
    "你是企业知识库问答助手。你必须严格遵守以下规则：\n"
    "1. 仅根据参考资料回答问题；如果参考资料中没有相关内容，直接回答"
    "「根据现有资料，我无法回答该问题」，禁止编造任何资料中没有的信息。\n"
    "2. 回答中引用参考资料时，必须在对应语句末尾标注来源，"
    "格式为 [citation: 来源编号, 页码]，例如 [citation: 1, 3]。"
    "来源编号即参考资料前的方括号编号，页码取该资料标注的页码（无页码则填 0）。\n"
    "3. 使用简洁、准确的中文回答。"
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
        self.embedding = embedding or get_embedding()
        self.store = store or get_vector_store()
        self.llm = llm or get_llm()
        self.cache = cache or get_conversation_cache()
        self.rerank = rerank or get_rerank()
        self.hyde = hyde if hyde is not None else get_hyde()

    async def ask(
        self,
        kb_ids: list[uuid.UUID],
        question: str,
        conversation_id: uuid.UUID | None = None,
    ) -> tuple[Conversation, Message, list[dict]]:
        """执行 RAG 问答。

        Returns:
            (conversation, assistant_message, citations)

        Raises:
            NotFoundError: 会话不存在
            AppException: 422 检索/生成失败
        """
        from src.core.exceptions import AppException

        started_at = asyncio.get_event_loop().time()
        conversation = await self._ensure_conversation(kb_ids, question, conversation_id)
        history, cache_hit = await self._load_history(conversation.id)

        # 先持久化用户消息（独立事务）：保证与助手消息 created_at 不同，
        # 使历史查询 ORDER BY created_at 顺序确定；且生成失败时用户意图仍留存。
        self.db.add(Message(
            conversation_id=conversation.id, role="user", content=question,
        ))
        await self.db.commit()

        # HyDE 查询改写（TECH_DESIGN：用假设答案替换原问题做向量检索）
        # 仅改写检索查询；最终 Prompt 仍用原始问题。失败回退原问题。
        query_text = question
        if self.hyde is not None:
            try:
                hypo = await asyncio.to_thread(self.hyde.generate, question)
                if hypo and hypo.strip():
                    query_text = hypo.strip()
                    logger.info(
                        f"HyDE 改写 conv={conversation.id}: "
                        f"{question[:30]}... -> {query_text[:50]}..."
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
            sparse_hits = []
            if query_sparse and query_sparse[0]:
                sparse_hits = await asyncio.to_thread(
                    self.store.search_sparse, query_sparse[0], kb_str, recall
                )
        except (EmbeddingError, VectorStoreError) as exc:
            logger.error(f"RAG 检索失败 conv={conversation.id}: {exc}")
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
            logger.error(f"RAG 重排失败 conv={conversation.id}: {exc}")
            raise AppException(422, f"重排序失败：{exc}") from exc

        logger.info(
            f"混合检索 conv={conversation.id} dense={len(dense_hits)} "
            f"sparse={len(sparse_hits)} fused={len(fused)} reranked={len(hits)}"
        )

        # 为精排后的命中注入 doc_name（Prompt 与引用卡片均需展示文档名）
        await self._enrich_hits_with_doc_name(hits)

        # 生成
        user_prompt = self._build_user_prompt(question, hits)
        messages = [{"role": "system", "content": SYSTEM_PROMPT}, *history,
                    {"role": "user", "content": user_prompt}]
        try:
            answer = await self.llm.chat(messages)
        except LLMError as exc:
            logger.error(f"RAG 生成失败 conv={conversation.id}: {exc}")
            raise AppException(422, f"回答生成失败：{exc}") from exc
        answer = answer.strip() or "（模型未返回内容，请重试）"

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

    async def _ensure_conversation(
        self, kb_ids: list[uuid.UUID], question: str, conversation_id: uuid.UUID | None
    ) -> Conversation:
        if conversation_id is not None:
            conv = await self.db.get(Conversation, conversation_id)
            if conv is None:
                raise NotFoundError("会话不存在")
            return conv
        user = await self.db.scalar(
            select(User).where(User.email == settings.DEFAULT_ADMIN_EMAIL)
        )
        if user is None:  # 测试/异常环境下兜底
            user = User(
                email=settings.DEFAULT_ADMIN_EMAIL,
                username="admin",
                hashed_password="not-set-yet",
                role="admin",
            )
            self.db.add(user)
            await self.db.flush()
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

        citations = []
        for i in picked:
            h = hits[i]
            citations.append(
                {
                    "chunk_id": h["id"],
                    "source_index": i + 1,  # 来源编号（1-based，对应答案标记）
                    "doc_id": h["doc_id"],
                    "doc_name": h.get("doc_name", "未知文档"),
                    "page_num": h["page_num"],
                    "title_path": h["title_path"],
                    "content": h["content"],
                    # 优先用 Rerank 精排分（归一化 0-1），无则退回召回分
                    "score": h.get("rerank_score", h.get("score", 0.0)),
                }
            )
        return citations
