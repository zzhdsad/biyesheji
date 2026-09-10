"""FastAPI 依赖注入：统一鉴权 + RBAC 权限辅助。

通过父级 APIRouter 的 dependencies=[Depends(get_current_user)] 对整组业务路由
一次性挂载鉴权，避免每个 endpoint 忘加。

get_current_user 在返回前把 User ORM 挂到 request.state.user，后续业务 handler
可直接 request.state.user.id / .role 读取当前登录用户。

RBAC 辅助：get_accessible_kb_ids 返回用户可访问的知识库 ID 集合（owner + members + public），
所有涉及 KB 数据的接口必须用此集合做数据隔离。

测试模式：设置 TEST_MODE_ENABLED = True 可跳过 JWT 校验，直接返回测试管理员用户，
无需 patch Depends.dependency（FastAPI 路由注册时已缓存函数引用）。
"""

from __future__ import annotations

import uuid
from typing import ClassVar

from fastapi import Depends, Header, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
import jwt as pyjwt

from src.core.config import settings
from src.core.security import decode_access_token
from src.domain.models import KnowledgeBase, KBMember, User
from src.infrastructure.database import get_db

# ── 测试模式开关 ──────────────────────────────────────────────────────────────
# 设置为 True 时，get_current_user 跳过 JWT 校验直接返回 TEST_USER
# 这样即使 FastAPI 已经缓存了函数引用，动态切换也能生效
TEST_MODE_ENABLED: bool = False
TEST_USER = User(
    id=uuid.UUID("4e7742e2-d751-4947-b85a-4076c054bbc4"),
    email="admin@example.com",
    username="admin",
    hashed_password="",
    role="admin",
)


async def get_current_user(
    request: Request,
    authorization: str | None = Header(default=None),
    db: AsyncSession = Depends(get_db),
) -> User:
    """解析 Authorization: Bearer xxx → 查 DB → 挂 request.state.user。

    测试模式（TEST_MODE_ENABLED=True）：跳过 JWT 校验，直接返回 TEST_USER。
    这样 FastAPI 路由注册时已经缓存了对本函数的引用，无需 patch Depends 对象。
    """
    # ── 测试模式：直接返回测试管理员 ──
    if TEST_MODE_ENABLED:
        request.state.user = TEST_USER
        return TEST_USER

    # ── 生产/开发模式：真实 JWT 鉴权 ──
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="未提供认证信息")

    token = authorization.split(" ", 1)[1].strip()
    if not token:
        raise HTTPException(status_code=401, detail="token 为空")

    try:
        payload = decode_access_token(token)
    except pyjwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="token 已过期")
    except pyjwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="无效的 token")

    user_id_str: str = payload["sub"]
    user = await db.scalar(
        select(User).where(User.id == user_id_str, User.deleted_at.is_(None))
    )
    if user is None:
        raise HTTPException(status_code=401, detail="用户不存在")

    # 挂到 request.state，后续业务 handler 可直接用
    request.state.user = user
    return user


async def get_accessible_kb_ids(db: AsyncSession, user: User) -> set[uuid.UUID]:
    """计算当前用户有权访问的知识库 ID 集合（RBAC 数据隔离核心）。

    访问规则（TECH_DESIGN §4 / AGENTS.md 安全规范）：
    - admin 角色：无条件返回全部知识库
    - 普通用户：owner_id == user.id 的知识库 ∪ kb_members 中 user_id == user.id 的知识库 ∪ visibility == 'public' 的知识库

    Returns:
        set[uuid.UUID] — 当前用户可访问的 kb_id 集合
    """
    if user.role == "admin":
        rows = (await db.scalars(select(KnowledgeBase))).all()
        return {kb.id for kb in rows}

    # 非 admin：owner + member + public 的并集
    stmt = select(KnowledgeBase.id).where(
        (KnowledgeBase.owner_id == user.id)
        | (KnowledgeBase.visibility == "public")
        | KnowledgeBase.id.in_(
            select(KBMember.kb_id).where(KBMember.user_id == user.id)
        )
    )
    rows = (await db.scalars(stmt)).all()
    return set(rows)
