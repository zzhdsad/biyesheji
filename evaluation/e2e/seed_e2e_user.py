"""在隔离库里创建免强制改密的 E2E 管理员（供浏览器端到端登录使用）。"""

import asyncio
import sys

sys.path.insert(0, "/app")

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from src.core.config import settings
from src.core.security import hash_password
from src.domain.models import User

# 注意：不能用 .local/.test/.example 等保留域名，
# UserOut 的邮箱校验会拒绝，登录接口会 500（这里踩过一次）。
EMAIL = "e2e@biyesheji.dev"
USERNAME = "e2e_admin"
PASSWORD = "E2ePass123!"


async def main() -> None:
    engine = create_async_engine(settings.DATABASE_URL)
    try:
        async with async_sessionmaker(engine)() as session:
            user = await session.scalar(
                select(User).where(or_(User.email == EMAIL, User.username == USERNAME))
            )
            if user is None:
                session.add(
                    User(
                        email=EMAIL,
                        username=USERNAME,
                        hashed_password=hash_password(PASSWORD),
                        role="admin",
                        name="E2E 管理员",
                        department="测试部",
                        must_change_password=False,
                    )
                )
            else:
                user.hashed_password = hash_password(PASSWORD)
                user.must_change_password = False
                user.email = EMAIL
                user.is_active = True
            await session.commit()
            print(f"OK {settings.DATABASE_URL} {EMAIL}")
    finally:
        await engine.dispose()


asyncio.run(main())
