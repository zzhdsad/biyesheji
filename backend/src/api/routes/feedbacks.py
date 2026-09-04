"""反馈收集路由：用户对助手消息点赞/踩 + 文本纠错意见。

PRD §3.6 / §8 边界场景要求：
- 提供"反馈"按钮收集用户纠错，用于后续优化检索和提示词
- 用户行为日志：问答记录、反馈收集

TECH_DESIGN §3 feedbacks 表：id / message_id / rating / comment

安全规范（AGENTS.md §3）：
- 所有端点受 protected_router 统一鉴权
- 权限隔离：反馈需通过 message → conversation → user_id 校验，
  只有会话所有者才能对自己收到的助手消息反馈，禁止越权操作他人消息
- 同一用户对同一消息重复反馈时覆盖（update rather than insert），
  避免主键冲突并保留最新意见
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Request
from loguru import logger
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.domain.models import Conversation, Feedback, Message, User
from src.infrastructure.database import get_db

router = APIRouter(prefix="/feedbacks", tags=["feedbacks"])


# ── 请求 / 响应 Schema ──────────────────────────────────────────────────────
class FeedbackCreate(BaseModel):
    """创建/更新反馈请求体。rating: 1 赞 / -1 踩（与 Feedback 模型一致）。"""

    message_id: uuid.UUID
    rating: int = Field(..., description="1=赞 / -1=踩", ge=-1, le=1)
    comment: str = Field(default="", max_length=2000, description="纠错意见（可选）")


class FeedbackOut(BaseModel):
    """反馈响应体。"""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    message_id: uuid.UUID
    rating: int
    comment: str


# ── 内部辅助：权限校验 ──────────────────────────────────────────────────────
async def _load_owned_message(
    db: AsyncSession, message_id: uuid.UUID, user: User
) -> Message:
    """加载消息并校验：消息存在 + 会话归属当前用户。

    Raises:
        HTTPException 404: 消息不存在
        HTTPException 403: 当前用户无权访问该消息所在会话
    """
    msg = await db.get(Message, message_id)
    if msg is None:
        raise HTTPException(status_code=404, detail="消息不存在")

    conv = await db.get(Conversation, msg.conversation_id)
    if conv is None or conv.user_id != user.id:
        # 不暴露存在性差异，避免枚举攻击
        raise HTTPException(status_code=403, detail="无权访问该消息")

    return msg


# ── 路由 ─────────────────────────────────────────────────────────────────────
@router.post("", response_model=FeedbackOut, status_code=200)
async def create_or_update_feedback(
    payload: FeedbackCreate,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> Feedback:
    """提交反馈（点赞/踩 + 可选纠错意见）。

    - 同一用户对同一消息重复反馈时执行覆盖更新（保留最新意见）；
    - rating 必须是 1 或 -1，0 不允许（Pydantic ge=-1/le=1 已限，但语义上要求非 0）。
    """
    if payload.rating == 0:
        raise HTTPException(status_code=400, detail="rating 必须是 1（赞）或 -1（踩）")

    user: User = request.state.user
    msg = await _load_owned_message(db, payload.message_id, user)

    # 按 message_id 查现有反馈（覆盖式：同一消息只保留一条最新反馈）
    existing = await db.scalar(
        select(Feedback).where(Feedback.message_id == payload.message_id)
    )

    if existing is not None:
        existing.rating = payload.rating
        existing.comment = payload.comment
        await db.commit()
        await db.refresh(existing)
        logger.info(
            f"反馈更新 id={existing.id} message_id={msg.id} rating={payload.rating}"
        )
        return existing

    fb = Feedback(
        message_id=msg.id,
        rating=payload.rating,
        comment=payload.comment,
    )
    db.add(fb)
    await db.commit()
    await db.refresh(fb)
    logger.info(
        f"反馈创建 id={fb.id} message_id={msg.id} rating={payload.rating} "
        f"has_comment={bool(payload.comment)}"
    )
    return fb


@router.get("", response_model=list[FeedbackOut])
async def list_my_feedbacks(
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> list[Feedback]:
    """列出当前用户提交过的所有反馈（按创建时间倒序）。

    权限隔离：只返回当前用户拥有的会话下消息对应的反馈。
    """
    user: User = request.state.user
    stmt = (
        select(Feedback)
        .join(Message, Feedback.message_id == Message.id)
        .join(Conversation, Message.conversation_id == Conversation.id)
        .where(Conversation.user_id == user.id)
        .order_by(Feedback.created_at.desc())
    )
    rows = (await db.scalars(stmt)).all()
    return list(rows)


@router.get("/{message_id}", response_model=FeedbackOut | None)
async def get_feedback_by_message(
    message_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> Feedback | None:
    """查询某条消息的反馈（无反馈返回 null）。

    权限：消息必须属于当前用户的会话。
    """
    user: User = request.state.user
    await _load_owned_message(db, message_id, user)
    fb = await db.scalar(
        select(Feedback).where(Feedback.message_id == message_id)
    )
    return fb
