"""用户鉴权路由：登录 / 登出 / 当前用户 / 修改密码。

用户注册功能已移除：账号由管理员在「用户管理」中创建（单个添加或批量导入），
系统首次启动时内置默认管理员账号。
/login 免鉴权；/logout、/me、/change-password 需要鉴权。
"""

from datetime import datetime, timedelta
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, EmailStr, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.config import settings
from src.core.deps import get_current_user
from src.core.security import create_access_token, hash_password, verify_password
from src.domain.models import User
from src.infrastructure.database import get_db

router = APIRouter(prefix="/auth", tags=["auth"])

# ── Schemas ──────────────────────────────────────────────────────────────────


class UserOut(BaseModel):
    """对外暴露的用户信息（不含密码）。"""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    email: EmailStr
    username: str
    role: str
    name: str = ""
    department: str = ""
    must_change_password: bool = False
    created_at: datetime | None = None


class LoginRequest(BaseModel):
    """用户名或邮箱登录。"""

    username_or_email: str = Field(min_length=1, max_length=255)
    password: str


class ChangePasswordRequest(BaseModel):
    """修改密码（首次登录强制修改时使用）。"""

    old_password: str
    new_password: str = Field(min_length=6, max_length=128)


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int  # 秒
    user: UserOut


# ── Endpoints ───────────────────────────────────────────────────────────────


@router.post("/login", response_model=TokenResponse)
async def login(
    payload: LoginRequest,
    db: AsyncSession = Depends(get_db),
) -> TokenResponse:
    """用户名或邮箱登录，返回 JWT。"""
    q = select(User).where(
        ((User.email == payload.username_or_email) | (User.username == payload.username_or_email))
        & User.deleted_at.is_(None)
    )
    user = await db.scalar(q)
    if user is None or not verify_password(payload.password, user.hashed_password):
        raise HTTPException(status_code=401, detail="用户名/邮箱或密码错误")

    token = create_access_token(
        subject=user.id,
        role=user.role,
        expires_delta=timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES),
    )
    expires_in = int(timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES).total_seconds())
    return TokenResponse(
        access_token=token,
        token_type="bearer",
        expires_in=expires_in,
        user=UserOut.model_validate(user),
    )


@router.post("/logout")
async def logout(
    _current_user: Annotated[User, Depends(get_current_user)],
) -> dict:
    """登出（语义端点，前端删 token；无 session 表，不做服务端黑名单）。"""
    return {"message": "ok"}


@router.get("/me", response_model=UserOut)
async def me(
    current_user: Annotated[User, Depends(get_current_user)],
) -> User:
    """返回当前登录用户信息。"""
    return current_user


@router.post("/change-password")
async def change_password(
    payload: ChangePasswordRequest,
    current_user: Annotated[User, Depends(get_current_user)],
    db: AsyncSession = Depends(get_db),
) -> dict:
    """修改密码。首次登录（must_change_password=True）时必须调用。"""
    if not verify_password(payload.old_password, current_user.hashed_password):
        raise HTTPException(status_code=400, detail="原密码错误")
    if payload.old_password == payload.new_password:
        raise HTTPException(status_code=400, detail="新密码不能与原密码相同")

    current_user.hashed_password = hash_password(payload.new_password)
    current_user.must_change_password = False
    await db.commit()
    return {"message": "密码修改成功"}
