"""鉴权安全工具：密码哈希（bcrypt 原生）+ JWT（HS256 验签）。

- bcrypt 5.x + passlib 1.7.4 不兼容，直接用 bcrypt.hashpw / bcrypt.checkpw
- JWT 必须验签（HS256 + SECRET_KEY），防止伪造 token 绕过鉴权
"""

import secrets
import string
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID

import bcrypt
import jwt
from loguru import logger

from src.core.config import settings


def generate_random_password(length: int = 8) -> str:
    """生成随机初始密码（大小写字母 + 数字，不含易混淆字符）。"""
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


def hash_password(plain: str) -> str:
    """bcrypt 哈希明文密码。"""
    return bcrypt.hashpw(plain.encode("utf-8"), bcrypt.gensalt(rounds=10)).decode("utf-8")


def verify_password(plain: str, hashed: str) -> bool:
    """校验明文与哈希是否匹配。"""
    if not hashed or hashed == "not-set-yet":
        logger.warning("用户 hashed_password 为占位值，verify 拒绝通过")
        return False
    try:
        return bcrypt.checkpw(plain.encode("utf-8"), hashed.encode("utf-8"))
    except ValueError:
        # 哈希格式不对
        logger.warning("bcrypt.checkpw 失败（哈希格式异常）")
        return False


# ── JWT ──────────────────────────────────────────────────────────────────────

def create_access_token(
    subject: str | UUID,
    role: str,
    expires_delta: timedelta | None = None,
) -> str:
    """签发 access JWT。

    payload:
        sub:   用户主键（str）
        role:  admin / member
        type:  access
        iat:   签发时间
        exp:   过期时间
    """
    exp_delta = expires_delta or timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)
    now = datetime.now(timezone.utc)
    payload: dict[str, Any] = {
        "sub": str(subject),
        "role": role,
        "type": "access",
        "iat": int(now.timestamp()),
        "exp": int((now + exp_delta).timestamp()),
    }
    token = jwt.encode(
        payload,
        settings.SECRET_KEY,
        algorithm=settings.JWT_ALGORITHM,
    )
    return token


def decode_access_token(token: str) -> dict[str, Any]:
    """验签并解析 JWT payload。

    Raises:
        jwt.ExpiredSignatureError: 令牌过期
        jwt.InvalidTokenError:      非法令牌（签名不对 / payload 缺字段）
    """
    payload = jwt.decode(
        token,
        settings.SECRET_KEY,
        algorithms=[settings.JWT_ALGORITHM],
    )
    if payload.get("type") != "access" or "sub" not in payload:
        raise jwt.InvalidTokenError("invalid token structure")
    return payload
