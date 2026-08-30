"""FastAPI 应用入口：python -m uvicorn src.main:app --reload"""

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from loguru import logger

from src.api.routes import api_router
from src.api.routes import health as health_router
from src.core.config import settings
from src.core.exceptions import register_exception_handlers
from src.core.logging import setup_logging


@asynccontextmanager
async def lifespan(app: FastAPI):
    setup_logging()
    if settings.ENV == "dev":
        from src.infrastructure.database import init_db

        try:
            await init_db()
        except Exception as exc:
            # 数据库不可用时服务仍可启动（/health 会标记 unavailable）
            logger.warning(f"数据库初始化跳过：{exc}")
    logger.info(f"{settings.APP_NAME} v{settings.VERSION} 启动完成（环境：{settings.ENV}）")
    yield
    logger.info("应用关闭")


def create_app() -> FastAPI:
    app = FastAPI(
        title=settings.APP_NAME,
        version=settings.VERSION,
        lifespan=lifespan,
        docs_url="/docs",
        openapi_url="/openapi.json",
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.CORS_ORIGINS,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    register_exception_handlers(app)
    app.include_router(api_router, prefix=settings.API_PREFIX)
    # /health 挂在根路径：存活探针，且为唯一免鉴权端点
    app.include_router(health_router.router)
    return app


app = create_app()
