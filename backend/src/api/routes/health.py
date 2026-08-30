"""健康检查路由（唯一免鉴权端点）。"""

from fastapi import APIRouter
from sqlalchemy import text

from src.core.config import settings
from src.infrastructure.database import get_engine

router = APIRouter(tags=["health"])


@router.get("/health")
async def health() -> dict:
    """存活探针：返回各组件连通状态（依赖缺失不导致接口失败）。"""
    components: dict[str, str] = {}

    # PostgreSQL
    try:
        engine = get_engine()
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        components["postgres"] = "ok"
    except Exception:
        components["postgres"] = "unavailable"

    # Redis
    try:
        import redis.asyncio as aioredis

        client = aioredis.from_url(settings.REDIS_URL, socket_connect_timeout=1)
        await client.ping()
        await client.aclose()
        components["redis"] = "ok"
    except Exception:
        components["redis"] = "unavailable"

    return {
        "status": "ok",
        "app": settings.APP_NAME,
        "version": settings.VERSION,
        "env": settings.ENV,
        "components": components,
    }
