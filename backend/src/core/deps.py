"""FastAPI 依赖注入：统一鉴权 + RBAC 权限辅助。

通过父级 APIRouter 的 dependencies=[Depends(get_current_user)] 对整组业务路由
一次性挂载鉴权，避免每个 endpoint 忘加。

get_current_user 在返回前把 User ORM 挂到 request.state.user，后续业务 handler
可直接 request.state.user.id / .role 读取当前登录用户。

RBAC 辅助：get_accessible_kb_ids 返回用户可访问的知识库 ID 集合（owner + members + public），
所有涉及 KB 数据的接口必须用此集合做数据隔离。

测试模式：设置 TEST_MODE_ENABLED = True 可跳过 JWT 校验，直接返回测试管理员用户，
无需 patch Depends.dependency（FastAPI 路由注册时已缓存函数引用）。
仅 dev / test 环境生效：生产环境（ENV=prod）强制忽略该开关，避免鉴权被绕过。

RBAC 角色：get_kb_role 返回用户在指定知识库中的有效角色
（owner / admin / editor / viewer），写操作必须落在 KB_WRITE_ROLES 内。
"""

from __future__ import annotations

import uuid
from typing import ClassVar

from fastapi import Depends, Header, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
import jwt as pyjwt

from src.core.config import settings
from src.core.exceptions import PermissionDeniedError
from src.core.security import decode_access_token
from src.domain.models import KnowledgeBase, KBMember, User
from src.infrastructure.database import get_db

# ── 测试模式开关 ──────────────────────────────────────────────────────────────
# 设置为 True 时，get_current_user 跳过 JWT 校验直接返回 TEST_USER
# 这样即使 FastAPI 已经缓存了函数引用，动态切换也能生效
TEST_MODE_ENABLED: bool = False
# 非生产环境才允许测试模式生效（生产误开也不至于直接绕过鉴权）
TEST_MODE_ALLOWED_ENVS = ("dev", "test", "testing")


def test_mode_active() -> bool:
    """测试模式是否真正生效（生产环境恒为 False）。"""
    return TEST_MODE_ENABLED and settings.ENV in TEST_MODE_ALLOWED_ENVS


# ── 知识库角色（BUSINESS_RULES §3：Owner/Admin/Editor/Viewer）─────────────────
KB_ROLE_OWNER = "owner"
KB_ROLE_ADMIN = "admin"
KB_ROLE_EDITOR = "editor"
KB_ROLE_VIEWER = "viewer"
# 可执行写操作的角色（viewer 只读）
KB_WRITE_ROLES = frozenset({KB_ROLE_OWNER, KB_ROLE_ADMIN, KB_ROLE_EDITOR})
VALID_KB_ROLES = frozenset({KB_ROLE_OWNER, KB_ROLE_ADMIN, KB_ROLE_EDITOR, KB_ROLE_VIEWER})

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

    测试模式（TEST_MODE_ENABLED=True 且 ENV 非 prod）：跳过 JWT 校验，
    直接返回 TEST_USER。这样 FastAPI 路由注册时已经缓存了对本函数的引用，
    无需 patch Depends 对象。生产环境该开关一律不生效。
    """
    # ── 测试模式：直接返回测试管理员（生产环境不生效）──
    if test_mode_active():
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
    if not user.is_active:
        raise HTTPException(status_code=403, detail="账号已被禁用，请联系管理员")

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
        rows = (await db.scalars(select(KnowledgeBase).where(KnowledgeBase.deleted_at.is_(None)))).all()
        return {kb.id for kb in rows}

    # 非 admin：owner + member + public 的并集（排除回收站）
    stmt = select(KnowledgeBase.id).where(
        KnowledgeBase.deleted_at.is_(None),
        (KnowledgeBase.owner_id == user.id)
        | (KnowledgeBase.visibility == "public")
        | KnowledgeBase.id.in_(
            select(KBMember.kb_id).where(KBMember.user_id == user.id)
        ),
    )
    rows = (await db.scalars(stmt)).all()
    return set(rows)


async def get_kb_role(db: AsyncSession, user: User, kb_id: uuid.UUID) -> str | None:
    """返回当前用户在指定知识库中的**有效角色**；无权访问返回 None。

    规则（与 get_accessible_kb_ids 的"可读"口径保持一致，并补上写权限语义）：
    - 系统 admin：视为 admin（可读写全部 KB）
    - KB owner：owner
    - KB 成员：membership 角色（非法值按 viewer 处理，防止脏数据提权）
    - public 知识库：登录用户按 viewer（只读）
    - 其余（含不存在/已软删的 KB）：None
    """
    if user.role == "admin":
        return KB_ROLE_ADMIN

    kb = await db.get(KnowledgeBase, kb_id)
    if kb is None or kb.deleted_at is not None:
        return None
    if kb.owner_id == user.id:
        return KB_ROLE_OWNER

    role = await db.scalar(
        select(KBMember.role).where(
            KBMember.kb_id == kb_id, KBMember.user_id == user.id
        )
    )
    if role:
        return role if role in VALID_KB_ROLES else KB_ROLE_VIEWER
    if kb.visibility == "public":
        return KB_ROLE_VIEWER
    return None


async def require_kb_read(db: AsyncSession, user: User, kb_id: uuid.UUID) -> str:
    """校验可读权限，返回有效角色；无权访问抛 403。"""
    role = await get_kb_role(db, user, kb_id)
    if role is None:
        raise PermissionDeniedError("无权访问该知识库")
    return role


async def require_kb_write(db: AsyncSession, user: User, kb_id: uuid.UUID) -> str:
    """校验可写权限（owner/admin/editor），viewer 与公开库只读用户抛 403。"""
    role = await require_kb_read(db, user, kb_id)
    if role not in KB_WRITE_ROLES:
        raise PermissionDeniedError("当前角色为只读，无权执行该操作")
    return role


async def require_password_changed(
    user: User = Depends(get_current_user),
) -> None:
    """强制修改初始密码（服务端强制点，BUG-005）。

    管理员创建/重置的用户 must_change_password=True，在完成修改密码前
    不得调用任何业务接口（/auth/me、/auth/logout、/auth/change-password
    不在 protected_router 下，不受此限制）。
    """
    if getattr(user, "must_change_password", False):
        raise HTTPException(
            status_code=403, detail="请先修改初始密码，再使用系统功能"
        )
