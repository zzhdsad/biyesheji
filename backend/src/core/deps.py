"""FastAPI 依赖注入：统一鉴权。

通过父级 APIRouter 的 dependencies=[Depends(get_current_user)] 对整组业务路由
一次性挂载鉴权，避免每个 endpoint 忘加。

get_current_user 在返回前把 User ORM 挂到 request.state.user，后续业务 handler
可直接 request.state.user.id / .role 读取当前登录用户。
"""

from fastapi import Depends, Header, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
import jwt as pyjwt

from src.core.security import decode_access_token
from src.domain.models import User
from src.infrastructure.database import get_db


async def get_current_user(
    request: Request,
    authorization: str | None = Header(default=None),
    db: AsyncSession = Depends(get_db),
) -> User:
    """解析 Authorization: Bearer xxx → 查 DB → 挂 request.state.user。"""

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
    user = await db.scalar(select(User).where(User.id == user_id_str))
    if user is None:
        raise HTTPException(status_code=401, detail="用户不存在")

    # 挂到 request.state，后续业务 handler 可直接用
    request.state.user = user
    return user
