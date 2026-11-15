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
from src.core import runtime_config


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
    # 系统级配置（回收站保留期 / 上传上限 / 检索参数等）预热：
    # DB 配置优先于 .env，取不到时静默沿用默认值，不影响启动
    try:
        cfg = await runtime_config.load_system_config(force=True)
        logger.info(f"运行时系统配置已预热：{cfg}")
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"运行时系统配置预热跳过：{exc}")
    # 后台线程随进程结束消失，遗留的 indexing 会永久悬空；启动时在线程里修复状态
    # （有完整向量→completed，否则→pending）。**不再自动续跑 pending**：向量化改为
    # 用户在页面点「开始向量化」手动触发，避免重启即把海量记录排进长时间队列。
    try:
        import threading

        from src.application.structured_runner import recover_stale_vector_jobs

        threading.Thread(
            target=recover_stale_vector_jobs,
            name="structured-vector-recover",
            daemon=True,
        ).start()

        # 后台预热 RAG 模型：BGE/Reranker 首次加载 + int8 量化约 20–30 秒，
        # 不应让第一个提问承担这段固定成本。失败不影响服务启动。
        def _warm_rag_models() -> None:
            try:
                from src.infrastructure.embedding import get_embedding
                from src.infrastructure.rerank import get_rerank

                get_embedding()
                reranker = get_rerank()
                # get_rerank() 只构造实例，Cross-Encoder 仍在首次打分时懒加载；
                # 预热必须显式触发，否则第一个提问仍承担模型加载 + int8 量化时间。
                getter = getattr(reranker, "_get_model", None)
                if callable(getter):
                    getter()
                logger.info("RAG 模型预热完成")
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"RAG 模型预热失败：{exc}")

        threading.Thread(
            target=_warm_rag_models,
            name="rag-model-warmup",
            daemon=True,
        ).start()
    except Exception as exc:  # noqa: BLE001 - 恢复失败不能影响服务启动
        logger.warning(f"结构化向量遗留状态修复未启动：{exc}")
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
        expose_headers=["X-Total-Count"],
    )

    @app.middleware("http")
    async def _refresh_runtime_config(request, call_next):
        """请求级刷新：系统配置按 TTL 重查 DB（改动不必重启，多副本也能收敛）。"""
        try:
            await runtime_config.refresh_if_stale()
        except Exception:  # noqa: BLE001 — 配置刷新失败不阻断请求
            pass
        return await call_next(request)

    register_exception_handlers(app)
    app.include_router(api_router, prefix=settings.API_PREFIX)
    # /health 挂在根路径：存活探针，且为唯一免鉴权端点
    app.include_router(health_router.router)
    return app


app = create_app()
