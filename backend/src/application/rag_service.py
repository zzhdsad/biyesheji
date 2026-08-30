"""基础 RAG 问答用例：向量化检索 → 上下文拼接 → LLM 生成 → 引用溯源 → 持久化。

TECH_DESIGN / AGENTS.md 约束：
- System Prompt 强制"仅根据参考资料回答，找不到就说知道"（防幻觉）
- 所有检索强制 kb_id 过滤（权限隔离）
- 答案引用 [n] 标注，后处理匹配生成引用来源（文档名、页码、段落）
- 多轮对话：携带最近 HISTORY_WINDOW 轮历史（MVP 直接透传，查询改写后续迭代）
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
from src.infrastructure.llm import BaseLLM, LLMError, get_llm
from src.infrastructure.milvus_store import BaseVectorStore, VectorStoreError, get_vector_store

SYSTEM_PROMPT = (
    "你是企业知识库问答助手。你必须严格遵守以下规则：\n"
    "1. 仅根据参考资料回答问题；如果参考资料中没有相关内容，直接回答"
    "「根据现有资料，我无法回答该问题」，禁止编造任何资料中没有的信息。\n"
    "2. 回答中引用参考资料时，必须在对应语句末尾标注来源编号，格式如 [1]、[2]。\n"
    "3. 使用简洁、准确的中文回答。"
)

_CONTEXT_MARKER = "参考资料："
_CITATION_PATTERN = re.compile(r"\[(\d+)\]")


class RagService:
    """RAG 问答编排（检索 Top-K → Prompt → LLM → 引用后处理 → 入库）。"""

    def __init__(
        self,
        db: AsyncSession,
        embedding: BaseEmbedding | None = None,
        store: BaseVectorStore | None = None,
        llm: BaseLLM | None = None,
    ) -> None:
        self.db = db
        self.embedding = embedding or get_embedding()
        self.store = store or get_vector_store()
        self.llm = llm or get_llm()

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
        history = await self._load_history(conversation.id)

        # 检索（CPU/IO 密集放线程池）
        try:
            query_vec, _ = await asyncio.to_thread(self.embedding.encode, [question])
            hits = await asyncio.to_thread(
                self.store.search,
                query_vec[0],
                [str(k) for k in kb_ids],
                settings.RETRIEVAL_TOP_K,
            )
        except (EmbeddingError, VectorStoreError) as exc:
            logger.error(f"RAG 检索失败 conv={conversation.id}: {exc}")
            raise AppException(422, f"知识检索失败：{exc}") from exc

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

        # 引用后处理：解析答案中的 [n] 标记 → 引用来源；无标记时兜底全部来源
        citations = await self._build_citations(answer, hits)
        elapsed_ms = int((asyncio.get_event_loop().time() - started_at) * 1000)
        logger.info(
            f"RAG 问答完成 conv={conversation.id} hits={len(hits)} "
            f"citations={len(citations)} 耗时={elapsed_ms}ms"
        )

        # 持久化：用户消息 + 助手消息（引用 JSON）
        self.db.add(Message(
            conversation_id=conversation.id, role="user", content=question,
        ))
        assistant = Message(
            conversation_id=conversation.id, role="assistant",
            content=answer, citations={"sources": citations},
        )
        self.db.add(assistant)
        await self.db.commit()
        await self.db.refresh(assistant)
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

    async def _load_history(self, conversation_id: uuid.UUID) -> list[dict]:
        """加载最近 HISTORY_WINDOW 轮（2N 条）消息，按时间升序返回。"""
        limit = max(settings.HISTORY_WINDOW, 0) * 2
        if limit == 0:
            return []
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
        return [{"role": m.role, "content": m.content} for m in reversed(rows)]

    def _build_user_prompt(self, question: str, hits: list[dict]) -> str:
        if not hits:
            return question  # 无资料：模型应按 System Prompt 回答不知道
        blocks = []
        for i, h in enumerate(hits, start=1):
            source = h.get("title_path") or "未命名段落"
            blocks.append(f"[{i}] （来源：{source}）\n{h['content']}")
        return f"{question}\n\n{_CONTEXT_MARKER}\n" + "\n\n".join(blocks)

    async def _build_citations(self, answer: str, hits: list[dict]) -> list[dict]:
        """解析答案中的 [n] 标记生成引用；无标记但有资料时兜底返回全部来源。"""
        if not hits:
            return []
        idxs = [int(m) - 1 for m in _CITATION_PATTERN.findall(answer)]
        idxs = [i for i in idxs if 0 <= i < len(hits)]
        picked = list(dict.fromkeys(idxs)) or list(range(len(hits)))

        doc_ids = {hits[i]["doc_id"] for i in picked if hits[i]["doc_id"]}
        docs = {
            str(d.id): d.file_name
            for d in (
                await self.db.scalars(select(Document).where(Document.id.in_(doc_ids)))
            ).all()
        } if doc_ids else {}

        citations = []
        for i in picked:
            h = hits[i]
            citations.append(
                {
                    "chunk_id": h["id"],
                    "doc_id": h["doc_id"],
                    "doc_name": docs.get(h["doc_id"], "未知文档"),
                    "page_num": h["page_num"],
                    "title_path": h["title_path"],
                    "content": h["content"],
                    "score": h["score"],
                }
            )
        return citations
