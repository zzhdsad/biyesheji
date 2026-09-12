"""智能问答路由：RAG 问答（同步 / SSE 流式）、会话管理。

安全规范（AGENTS.md §3 / TECH_DESIGN RBAC）：
- 所有端点受 protected_router 统一鉴权（Depends(get_current_user)）
- kb_ids 必须校验：用户可访问 ∩ 请求的 kb_ids == 请求的 kb_ids
- 会话归属校验：Conversation.user_id == request.state.user.id
- 新建会话用当前登录用户（不再硬编码 DEFAULT_ADMIN_EMAIL）
"""

import json
import uuid

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse
from loguru import logger
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.application.rag_service import RagService
from src.core.deps import get_accessible_kb_ids
from src.core.exceptions import PermissionDeniedError
from src.domain.models import Conversation, Message, User
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


# ── 公共安全校验 ─────────────────────────────────────────────────────────────


async def _check_model_configured(db: AsyncSession) -> None:
    """BUSINESS_RULES §10：模型未配置时禁止提问。

    mock 模式允许（开发/测试）；非 mock 模式必须有 base_url 和 model。
    """
    from src.application.model_config_service import get_effective_config_cached
    from src.core.exceptions import AppException

    config = await get_effective_config_cached(db)
    provider = config.get("llm_provider", "mock")
    if provider == "mock":
        return  # mock 模式视为已配置（开发/测试用）
    base_url = config.get("llm_base_url", "")
    model = config.get("llm_model", "")
    if not base_url or not model:
        raise AppException(
            412,
            "模型尚未配置，请先在管理中心-系统设置中配置模型参数",
        )


async def _validate_kb_access(
    db: AsyncSession, user: User, kb_ids: list[uuid.UUID]
) -> None:
    """校验用户是否可访问请求的全部 kb_ids。

    Raises:
        PermissionDeniedError: 存在无权访问的 kb_id
    """
    accessible = await get_accessible_kb_ids(db, user)
    forbidden = set(kb_ids) - accessible
    if forbidden:
        raise PermissionDeniedError(f"无权访问以下知识库: {forbidden}")


async def _validate_conversation_owner(
    db: AsyncSession, user: User, conversation_id: uuid.UUID
) -> Conversation:
    """校验会话归属并返回会话。

    Raises:
        NotFoundError: 会话不存在
        PermissionDeniedError: 会话不属于当前用户
    """
    from src.core.exceptions import NotFoundError

    conv = await db.get(Conversation, conversation_id)
    if conv is None:
        raise NotFoundError("会话不存在")
    if conv.user_id != user.id:
        raise PermissionDeniedError("无权访问该会话")
    return conv


# ── 端点实现 ────────────────────────────────────────────────────────────────

@router.post("/ask", response_model=ChatAnswerResponse)
async def ask(
    payload: ChatAskRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> ChatAnswerResponse:
    """RAG 问答（同步）。

    安全：校验 kb_ids 权限 + 校验 conversation_id 归属 + RagService 使用当前用户。
    """
    from src.core.exceptions import AppException

    if not payload.kb_ids:
        raise AppException(422, "kb_ids 不能为空：必须指定检索的知识库范围")

    user: User = request.state.user
    await _check_model_configured(db)
    await _validate_kb_access(db, user, payload.kb_ids)

    if payload.conversation_id is not None:
        await _validate_conversation_owner(db, user, payload.conversation_id)

    service = RagService(db)
    conv, assistant, citations = await service.ask(
        user=user,
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


@router.post("/ask-stream")
async def ask_stream(
    payload: ChatAskRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """RAG 问答（SSE 流式）。

    安全：校验 kb_ids 权限 + 校验 conversation_id 归属 + RagService 使用当前用户。
    事件协议见原注释，不变。
    """
    from src.core.exceptions import AppException

    if not payload.kb_ids:
        raise AppException(422, "kb_ids 不能为空：必须指定检索的知识库范围")

    user: User = request.state.user
    await _check_model_configured(db)
    await _validate_kb_access(db, user, payload.kb_ids)

    if payload.conversation_id is not None:
        await _validate_conversation_owner(db, user, payload.conversation_id)

    service = RagService(db)

    async def event_stream():
        try:
            async for evt in service.ask_stream(
                user=user,
                kb_ids=payload.kb_ids,
                question=payload.question.strip(),
                conversation_id=payload.conversation_id,
            ):
                data = json.dumps(evt["data"], ensure_ascii=False)
                yield f"event: {evt['event']}\ndata: {data}\n\n"
        except Exception as exc:  # 兜底，避免流中断无提示
            logger.exception(f"流式问答异常: {exc}")
            err = json.dumps({"message": str(exc)}, ensure_ascii=False)
            yield f"event: error\ndata: {err}\n\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",  # Nginx 不缓冲，保证实时推送
        },
    )


@router.get("/conversations", response_model=list[ConversationOut])
async def list_conversations(
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> list[Conversation]:
    """历史会话列表（按创建时间倒序）。

    安全隔离：只返回当前用户自己创建的会话（Conversation.user_id == current_user.id）。
    """
    user: User = request.state.user
    rows = await db.scalars(
        select(Conversation)
        .where(Conversation.user_id == user.id)
        .order_by(Conversation.created_at.desc())
    )
    return list(rows)


@router.get("/conversations/{conversation_id}/messages", response_model=list[MessageOut])
async def list_messages(
    conversation_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> list[Message]:
    """会话消息记录（按时间升序，含 citations JSON）。

    安全隔离：校验会话归属（Conversation.user_id == current_user.id），防止越权读取他人会话消息。
    """
    user: User = request.state.user
    await _validate_conversation_owner(db, user, conversation_id)

    rows = await db.scalars(
        select(Message)
        .where(Message.conversation_id == conversation_id)
        .order_by(Message.created_at.asc())
    )
    return list(rows)


@router.delete("/conversations/{conversation_id}")
async def delete_conversation(
    conversation_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """删除历史会话（级联删除消息 + 清理 Redis 缓存）。

    安全隔离：校验会话归属（Conversation.user_id == current_user.id），防止越权删除他人会话。
    Message 通过 ORM relationship cascade="all, delete-orphan" 级联删除，
    无需手动逐条删除消息。
    """
    from src.core.exceptions import NotFoundError
    from src.infrastructure.redis_client import get_conversation_cache

    user: User = request.state.user
    conv = await db.get(Conversation, conversation_id)
    if conv is None:
        raise NotFoundError("会话不存在")
    if conv.user_id != user.id:
        raise PermissionDeniedError("无权删除该会话")

    await db.delete(conv)
    await db.commit()

    # 清理 Redis 对话缓存（失败仅告警，不阻断删除主流程）
    try:
        cache = get_conversation_cache()
        await cache.delete(conversation_id)
    except Exception as exc:
        logger.warning(f"清理会话缓存失败（不影响删除）: {exc}")

    logger.info(f"用户 {user.id} 删除会话 {conversation_id}")
    return {"id": str(conversation_id), "deleted": True}


@router.delete("/conversations")
async def delete_all_conversations(
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """删除当前用户的全部历史会话（级联删除消息 + 清理 Redis 缓存）。

    安全隔离：只删除当前用户创建的会话（Conversation.user_id == current_user.id）。
    """
    from sqlalchemy import delete as sa_delete

    from src.infrastructure.redis_client import get_conversation_cache

    user: User = request.state.user

    # 先查出所有会话 ID（供清理 Redis 缓存用）
    rows = await db.scalars(
        select(Conversation.id).where(Conversation.user_id == user.id)
    )
    conv_ids = list(rows)
    if not conv_ids:
        return {"deleted_count": 0}

    # 批量删除会话 → Message 通过 DB 级 ondelete=CASCADE 自动级联删除
    await db.execute(
        sa_delete(Conversation).where(Conversation.id.in_(conv_ids))
    )
    await db.commit()

    # 清理 Redis 对话缓存（逐个删除，失败仅告警）
    try:
        cache = get_conversation_cache()
        for cid in conv_ids:
            await cache.delete(cid)
    except Exception as exc:
        logger.warning(f"批量清理会话缓存失败（不影响删除）: {exc}")

    logger.info(f"用户 {user.id} 删除全部会话，共 {len(conv_ids)} 条")
    return {"deleted_count": len(conv_ids)}
