"""用户鉴权路由：注册 / 登录 / 登出 / 当前用户。

其中 /register、/login 免鉴权（挂在 auth_public_router 下）；
/logout、/me 需要鉴权，在本 router 内单独依赖 Depends(get_current_user)。
"""

import re
from datetime import datetime, timedelta
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator
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
    created_at: datetime | None = None


class RegisterRequest(BaseModel):
    email: EmailStr
    username: str = Field(min_length=3, max_length=64)
    password: str = Field(min_length=6, max_length=128)

    @field_validator("username")
    @classmethod
    def _username_alnum(cls, v: str) -> str:
        if not re.match(r"^[A-Za-z0-9_\-]+$", v):
            raise ValueError("用户名仅支持字母、数字、下划线、短横线")
        return v


class LoginRequest(BaseModel):
    """用户名或邮箱登录。"""

    username_or_email: str = Field(min_length=1, max_length=255)
    password: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int  # 秒
    user: UserOut


# ── Endpoints ───────────────────────────────────────────────────────────────

@router.post(
    "/register",
    status_code=status.HTTP_201_CREATED,
    response_model=UserOut,
)
async def register(
    payload: RegisterRequest,
    db: AsyncSession = Depends(get_db),
) -> User:
    """注册新用户（不自动登录，需调 /login 拿 token）。"""
    if await db.scalar(select(User).where(User.email == payload.email)):
        raise HTTPException(status_code=400, detail="邮箱已被注册")
    if await db.scalar(select(User).where(User.username == payload.username)):
        raise HTTPException(status_code=400, detail="用户名已被使用")

    user = User(
        email=payload.email,
        username=payload.username,
        hashed_password=hash_password(payload.password),
        role="member",
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


@router.post("/login", response_model=TokenResponse)
async def login(
    payload: LoginRequest,
    db: AsyncSession = Depends(get_db),
) -> TokenResponse:
    """用户名或邮箱登录，返回 JWT。"""
    q = select(User).where(
        (User.email == payload.username_or_email)
        | (User.username == payload.username_or_email)
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
