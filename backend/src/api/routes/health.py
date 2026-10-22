"""健康检查路由（唯一免鉴权端点）。"""

from fastapi import APIRouter
from loguru import logger
from sqlalchemy import text

from src.core.config import settings
from src.infrastructure.database import get_engine

router = APIRouter(tags=["health"])


@router.get("/health")
async def health() -> dict:
    """存活探针：返回各组件连通状态（依赖缺失不导致接口失败）。

    BUG-029 修正：
    - status 如实反映依赖状态：全部组件 ok → "ok"，任一不可用 → "degraded"
      （旧实现恒为 "ok"，组件全挂也谎报健康）。
      HTTP 状态码仍为 200：本端点表达的是"进程存活"，组件级故障由
      status / components 字段承载，便于 K8s 探针与前端分别判定。
    - Redis 客户端在 finally 中关闭（旧实现 ping 失败即跳过 aclose，泄漏连接）。
    - 组件不可用记 warning 日志并带上原因，便于排障（旧实现静默吞异常）。
    - 免鉴权端点不再暴露 version / env 等系统信息。
    """
    components: dict[str, str] = {}

    # PostgreSQL
    try:
        engine = get_engine()
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        components["postgres"] = "ok"
    except Exception as exc:
        components["postgres"] = "unavailable"
        logger.warning(f"健康检查：PostgreSQL 不可用（{exc}）")

    # Redis
    client = None
    try:
        import redis.asyncio as aioredis

        client = aioredis.from_url(settings.REDIS_URL, socket_connect_timeout=1)
        await client.ping()
        components["redis"] = "ok"
    except Exception as exc:
        components["redis"] = "unavailable"
        logger.warning(f"健康检查：Redis 不可用（{exc}）")
    finally:
        if client is not None:
            try:
                await client.aclose()
            except Exception as exc:  # noqa: BLE001  关闭失败仅记日志，不影响响应
                logger.warning(f"健康检查：Redis 连接关闭失败（{exc}）")

    all_ok = bool(components) and all(v == "ok" for v in components.values())
    return {
        "status": "ok" if all_ok else "degraded",
        "app": settings.APP_NAME,
        "components": components,
    }
