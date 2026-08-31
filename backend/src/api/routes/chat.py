"""智能问答路由：RAG 问答、会话管理（SSE 流式输出后续迭代）。"""

import uuid

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.application.rag_service import RagService
from src.domain.models import Conversation, Message
from src.infrastructure.database import get_db

router = APIRouter(prefix="/chat", tags=["chat"])


class ChatAskRequest(BaseModel):
    question: str = Field(min_length=1, max_length=2000)
    kb_ids: list[uuid.UUID] = Field(
        default_factory=list, description="检索的知识库列表（权限过滤的依据）"
    )
    conversation_id: uuid.UUID | None = None


class Citation(BaseModel):
    chunk_id: str
    source_index: int  # 来源编号（1-based，对应答案中 [citation: 编号, 页码] 的编号）
    doc_id: str
    doc_name: str
    page_num: int | None = None
    title_path: str | None = None
    content: str
    score: float


class ChatAnswerResponse(BaseModel):
    conversation_id: str
    message_id: str
    answer: str
    citations: list[Citation]


class ConversationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    title: str
    kb_ids: list[uuid.UUID]
    created_at: object


class MessageOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    role: str
    content: str
    citations: dict | None
    created_at: object


@router.post("/ask", response_model=ChatAnswerResponse)
async def ask(payload: ChatAskRequest, db: AsyncSession = Depends(get_db)) -> ChatAnswerResponse:
    """RAG 问答：向量检索 Top-K → 上下文拼接 → LLM 生成 → 引用溯源。

    - kb_ids 为空返回 422（检索必须限定知识库范围，禁止越权）
    - 答案严格基于检索资料，找不到时明确告知"无法回答"（防幻觉约束）
    - citations 为答案引用的来源（文档名、页码、段落），前端渲染引用卡片
    """
    from src.core.exceptions import AppException

    if not payload.kb_ids:
        raise AppException(422, "kb_ids 不能为空：必须指定检索的知识库范围")

    service = RagService(db)
    conv, assistant, citations = await service.ask(
        kb_ids=payload.kb_ids,
        question=payload.question.strip(),
        conversation_id=payload.conversation_id,
    )
    return ChatAnswerResponse(
        conversation_id=str(conv.id),
        message_id=str(assistant.id),
        answer=assistant.content,
        citations=[Citation(**c) for c in citations],
    )


@router.get("/conversations", response_model=list[ConversationOut])
async def list_conversations(db: AsyncSession = Depends(get_db)) -> list[Conversation]:
    """历史会话列表（按创建时间倒序）。

    TODO: 接入 JWT 鉴权后按当前用户 user_id 过滤。
    """
    rows = await db.scalars(select(Conversation).order_by(Conversation.created_at.desc()))
    return list(rows)


@router.get("/conversations/{conversation_id}/messages", response_model=list[MessageOut])
async def list_messages(
    conversation_id: uuid.UUID, db: AsyncSession = Depends(get_db)
) -> list[Message]:
    """会话消息记录（按时间升序，含 citations JSON）。"""
    rows = await db.scalars(
        select(Message)
        .where(Message.conversation_id == conversation_id)
        .order_by(Message.created_at.asc())
    )
    return list(rows)
