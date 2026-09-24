"""API 路由聚合 + 统一鉴权保护。

路由分层：
- auth_public_router：免鉴权（仅 /auth/login；无开放注册，账号由管理员创建）
- protected_router：业务路由，父级统一挂：
  · Depends(get_current_user)          —— 已登录
  · Depends(require_password_changed)  —— 初始密码已修改（BUG-005）
  → 所有子路由自动受保护，避免每个 endpoint 忘加
- api_router：聚合上述两个
"""

from fastapi import APIRouter, Depends

from src.api.routes import (
    admin,
    audit,
    auth,
    chat,
    documents,
    evaluation,
    feedbacks,
    herbs,
    kg,  # 阶段十三：知识图谱（构建 / 统计 / 检索调试）
    knowledge_bases,
    literatures,
    prescriptions,
    settings,
    taxonomy,
    theories,
    users,
)
from src.core.deps import get_current_user, require_password_changed

# ── 免鉴权路由 ───────────────────────────────────────────────────────────────
auth_public_router = APIRouter()
auth_public_router.include_router(
    auth.router,
    # auth.router 内 /logout、/me、/change-password 已单独依赖 Depends(get_current_user)
    # 只有 /login 真正免鉴权（无 /auth/register：注册入口已下线，BUG-014）
)

# ── 业务路由（统一鉴权 + 强制改密）────────────────────────────────────────────
protected_router = APIRouter(
    dependencies=[Depends(get_current_user), Depends(require_password_changed)]
)
protected_router.include_router(documents.router)
protected_router.include_router(chat.router)
protected_router.include_router(knowledge_bases.router)
protected_router.include_router(taxonomy.router)  # TASK-002：分类与标签
protected_router.include_router(herbs.router)  # TASK-003：中药管理
protected_router.include_router(prescriptions.router)  # TASK-004：方剂管理
protected_router.include_router(theories.router)  # TASK-005：中医理论管理
protected_router.include_router(literatures.router)  # TASK-006：中医文献管理
protected_router.include_router(evaluation.router)
protected_router.include_router(kg.router)  # TASK-013：知识图谱
protected_router.include_router(feedbacks.router)  # PRD §3.6：用户反馈收集
protected_router.include_router(admin.router)  # PRD §5.2：系统仪表盘统计（仅 admin）
protected_router.include_router(settings.router)  # 模型配置（LLM/Embedding/Rerank/HyDE）
protected_router.include_router(users.router)  # 用户管理（仅 admin）
protected_router.include_router(audit.router)  # 审计日志（仅 admin）

# ── 聚合 ─────────────────────────────────────────────────────────────────────
api_router = APIRouter()
api_router.include_router(auth_public_router)
api_router.include_router(protected_router)
