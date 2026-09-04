"""API 路由聚合 + 统一鉴权保护。

路由分层：
- auth_public_router：免鉴权（/auth/register、/auth/login）
- protected_router：业务路由，父级统一挂 Depends(get_current_user)
  → 所有子路由自动受保护，避免每个 endpoint 忘加
- api_router：聚合上述两个
"""

from fastapi import APIRouter, Depends

from src.api.routes import (
    admin,
    auth,
    chat,
    documents,
    evaluation,
    feedbacks,
    knowledge_bases,
)
from src.core.deps import get_current_user

# ── 免鉴权路由 ───────────────────────────────────────────────────────────────
auth_public_router = APIRouter()
auth_public_router.include_router(
    auth.router,
    # auth.router 内 /logout、/me 已单独依赖 Depends(get_current_user)
    # 只有 /register、/login 真正免鉴权
)

# ── 业务路由（统一鉴权） ──────────────────────────────────────────────────────
protected_router = APIRouter(dependencies=[Depends(get_current_user)])
protected_router.include_router(documents.router)
protected_router.include_router(chat.router)
protected_router.include_router(knowledge_bases.router)
protected_router.include_router(evaluation.router)
protected_router.include_router(feedbacks.router)  # PRD §3.6：用户反馈收集
protected_router.include_router(admin.router)  # PRD §5.2：系统仪表盘统计（仅 admin）

# ── 聚合 ─────────────────────────────────────────────────────────────────────
api_router = APIRouter()
api_router.include_router(auth_public_router)
api_router.include_router(protected_router)
