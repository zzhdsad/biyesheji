"""智能问答路由：RAG 问答（含 SSE 流式预留）、会话管理。"""

import uuid

from fastapi import APIRouter
from pydantic import BaseModel, Field

router = APIRouter(prefix="/chat", tags=["chat"])


class ChatAskRequest(BaseModel):
    question: str = Field(min_length=1, max_length=2000)
    kb_ids: list[str] = Field(default_factory=list, description="检索的知识库列表，空则默认当前库")
    conversation_id: str | None = None


class Citation(BaseModel):
    doc_id: str
    doc_name: str
    page_num: int | None = None
    title_path: str | None = None
    content: str


class ChatAnswerResponse(BaseModel):
    conversation_id: str
    answer: str
    citations: list[Citation]


@router.post("/ask", response_model=ChatAnswerResponse)
async def ask(payload: ChatAskRequest) -> ChatAnswerResponse:
    """RAG 问答（占位实现，返回可联调的固定结构）。

    TODO: 接入 LangGraph 流水线：
      查询改写 → (可选 HyDE) → 混合检索(向量+BM25) → RRF 融合 → Rerank → LLM 生成
    约束：
      - System Prompt 必须强制"仅根据参考资料回答，找不到就说不知道"
      - 引用标注 [citation: doc_id, page] 后处理匹配，前端渲染引用卡片
      - 后续提供 /ask/stream（SSE 流式输出）
    """
    return ChatAnswerResponse(
        conversation_id=payload.conversation_id or str(uuid.uuid4()),
        answer="平台初始化中：RAG 流水线尚未接入，暂无法回答问题。",
        citations=[],
    )


@router.get("/conversations")
async def list_conversations() -> list[dict]:
    """历史会话列表。

    TODO: 按 user_id 查询 conversations 表（含标题、时间、kb_ids）。
    """
    return []


@router.get("/conversations/{conversation_id}/messages")
async def list_messages(conversation_id: uuid.UUID) -> list[dict]:
    """会话消息记录。

    TODO: 查询 messages 表，按时间升序返回（含 citations JSON）。
    """
    return []
