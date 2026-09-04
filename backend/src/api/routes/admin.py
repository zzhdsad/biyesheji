"""系统仪表盘统计路由（PRD §5.2：系统仪表盘）。

GET /admin/stats 返回平台全局统计指标：
- total_docs：文档总数（documents 全量计数）
- total_kbs：知识库总数（knowledge_bases 全量计数）
- total_qa：累计问答数（role='user' 的消息数 = 用户提问次数）
- avg_latency_ms：平均问答响应耗时（毫秒）

平均延迟口径（单一数据源，避免与其他页面不一致）：
一次问答在 rag_service 中先落库 user 消息（检索前），生成完成后落库 assistant 消息，
因此 单轮耗时 = assistant.created_at - 同会话内紧邻前一条 user.created_at，
用窗口函数 lag() 在 DB 侧聚合，覆盖"检索 + Rerank + LLM 生成"全链路。
无数据时 coalesce 为 0。

权限：仅 admin 角色可访问（RBAC，AGENTS.md §3）。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.exceptions import PermissionDeniedError
from src.domain.models import Document, KnowledgeBase, Message, User
from src.infrastructure.database import get_db

router = APIRouter(prefix="/admin", tags=["admin"])


class AdminStatsOut(BaseModel):
    """系统仪表盘统计指标（PRD §5.2 四个卡片）。"""

    total_docs: int
    total_kbs: int
    total_qa: int
    avg_latency_ms: int  # 平均响应耗时（毫秒），无数据为 0


@router.get("/stats", response_model=AdminStatsOut)
async def get_system_stats(
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> AdminStatsOut:
    """系统仪表盘全局统计（仅 admin）。"""
    user: User = request.state.user
    if user.role != "admin":
        raise PermissionDeniedError("仅管理员可查看系统统计")

    # ── 三个计数指标（全表 COUNT，走索引/顺序扫描，数据量下足够快）────────────
    total_docs = await db.scalar(select(func.count()).select_from(Document))
    total_kbs = await db.scalar(select(func.count()).select_from(KnowledgeBase))
    total_qa = await db.scalar(
        select(func.count()).select_from(Message).where(Message.role == "user")
    )

    # ── 平均响应延迟（窗口函数：assistant 与同会话紧邻前一条 user 配对）────────
    # lag() 取同一会话内按时间排序的上一条消息的 created_at 与 role，
    # 仅保留 (user → assistant) 配对，间隔即单轮全链路耗时。
    prev_created = func.lag(Message.created_at).over(
        partition_by=Message.conversation_id, order_by=Message.created_at
    )
    prev_role = func.lag(Message.role).over(
        partition_by=Message.conversation_id, order_by=Message.created_at
    )
    sub = select(
        Message.role.label("role"),
        Message.created_at.label("created_at"),
        prev_created.label("prev_created_at"),
        prev_role.label("prev_role"),
    ).subquery()

    avg_latency = await db.scalar(
        select(
            func.coalesce(
                func.round(
                    func.avg(
                        func.extract("epoch", sub.c.created_at - sub.c.prev_created_at)
                        * 1000
                    )
                ),
                0,
            )
        ).where(sub.c.role == "assistant", sub.c.prev_role == "user")
    )

    return AdminStatsOut(
        total_docs=int(total_docs or 0),
        total_kbs=int(total_kbs or 0),
        total_qa=int(total_qa or 0),
        avg_latency_ms=int(avg_latency or 0),
    )
