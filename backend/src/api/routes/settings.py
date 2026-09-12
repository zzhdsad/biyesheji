"""模型配置路由：前端设置页动态配置 LLM/Embedding/Rerank/HyDE。

所有接口受 protected_router 统一鉴权；已登录用户均可修改配置。
"""

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from src.application.model_config_service import (
    ModelConfigService,
    invalidate_config_cache,
)
from src.core.deps import get_current_user, get_db
from src.infrastructure.llm import OpenAICompatibleLLM

router = APIRouter(prefix="/settings", tags=["settings"])


class ModelConfigUpdate(BaseModel):
    """前端提交的模型配置（API key 可为空或脱敏值）。"""

    llm_provider: str = Field(default="mock", pattern="^(mock|deepseek|openai|qwen|ollama|custom)$")
    llm_base_url: str = Field(default="", max_length=512)
    llm_model: str = Field(default="", max_length=128)
    llm_api_key: str = Field(default="", max_length=512)
    embedding_backend: str = Field(default="mock", pattern="^(mock|flagembedding)$")
    embedding_model: str = Field(default="BAAI/bge-m3", max_length=128)
    embedding_device: str = Field(default="cpu", pattern="^(cpu|cuda)$")
    rerank_backend: str = Field(default="mock", pattern="^(mock|flagreranker)$")
    rerank_model: str = Field(default="BAAI/bge-reranker-v2-m3", max_length=128)
    rerank_device: str = Field(default="cpu", pattern="^(cpu|cuda)$")
    hyde_enabled: bool = Field(default=True)
    hyde_backend: str = Field(default="mock", pattern="^(mock|openai)$")
    hyde_model: str = Field(default="", max_length=128)
    hyde_base_url: str = Field(default="", max_length=512)


@router.get("/model")
async def get_model_config(
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """获取当前模型配置（API key 脱敏）。"""
    svc = ModelConfigService(db)
    return await svc.get_config_for_frontend()


@router.put("/model")
async def update_model_config(
    payload: ModelConfigUpdate,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """更新模型配置。已登录用户均可修改，保存后失效缓存，下次 RAG 调用加载新配置。"""
    svc = ModelConfigService(db)
    result = await svc.update_config(payload.model_dump())
    invalidate_config_cache()
    return result


@router.post("/model/test")
async def test_model_connection(
    payload: ModelConfigUpdate,
    request: Request,
) -> dict:
    """测试 LLM 连通性：用提交的配置发一条极简 chat 请求，返回成功/失败与耗时。"""
    # 测试不需要保存到 DB，直接用提交的参数构建客户端
    if payload.llm_provider == "mock":
        return {"ok": True, "message": "mock 模式无需测试连通性", "latency_ms": 0}
    if not payload.llm_base_url or not payload.llm_model:
        return {"ok": False, "message": "请填写 Base URL 和模型名称"}
    try:
        llm = OpenAICompatibleLLM(
            base_url=payload.llm_base_url,
            model=payload.llm_model,
            api_key=payload.llm_api_key or None,
        )
        import asyncio
        import time
        start = time.perf_counter()
        answer = await asyncio.wait_for(
            llm.chat([
                {"role": "user", "content": "回复一个字：好"},
            ]),
            timeout=30,
        )
        latency_ms = int((time.perf_counter() - start) * 1000)
        return {
            "ok": True,
            "message": f"连通成功，模型回复：{answer[:50]}",
            "latency_ms": latency_ms,
        }
    except asyncio.TimeoutError:
        return {"ok": False, "message": "请求超时（30s），请检查网络或 Base URL"}
    except Exception as exc:
        return {"ok": False, "message": f"连通失败：{exc}"}


# ── 系统级配置（BUSINESS_RULES §8）────────────────────────────────────────────


class SystemConfigUpdate(BaseModel):
    """系统级配置更新（回收站保留期、文件大小限制、检索参数等）。"""

    trash_retention_days: int | None = Field(default=None, ge=1, le=30)
    max_file_size_mb: int | None = Field(default=None, ge=1, le=500)
    recall_top_k: int | None = Field(default=None, ge=1, le=200)
    rerank_top_n: int | None = Field(default=None, ge=1, le=50)
    relevance_threshold: float | None = Field(default=None, ge=0.0, le=1.0)
    history_window: int | None = Field(default=None, ge=0, le=20)


@router.get("/system")
async def get_system_config(
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """获取系统级配置（fallback 到 .env 默认值）。"""
    from src.domain.models import SystemConfig
    from src.core.config import settings as cfg

    row = await db.get(SystemConfig, 1)
    if row is None:
        return {
            "trash_retention_days": cfg.TRASH_RETENTION_DAYS,
            "max_file_size_mb": cfg.MAX_FILE_SIZE_MB,
            "recall_top_k": cfg.RECALL_TOP_K,
            "rerank_top_n": cfg.RERANK_TOP_N,
            "relevance_threshold": cfg.RELEVANCE_THRESHOLD,
            "history_window": cfg.HISTORY_WINDOW,
        }
    return {
        "trash_retention_days": row.trash_retention_days,
        "max_file_size_mb": row.max_file_size_mb,
        "recall_top_k": row.recall_top_k,
        "rerank_top_n": row.rerank_top_n,
        "relevance_threshold": row.relevance_threshold,
        "history_window": row.history_window,
    }


@router.put("/system")
async def update_system_config(
    payload: SystemConfigUpdate,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """更新系统级配置（仅 admin）。"""
    from src.domain.models import SystemConfig
    from src.core.config import settings as cfg
    from src.core.exceptions import PermissionDeniedError

    user = request.state.user
    if user.role != "admin":
        raise PermissionDeniedError("仅管理员可修改系统配置")

    row = await db.get(SystemConfig, 1)
    if row is None:
        row = SystemConfig(id=1)
        db.add(row)
    changes = payload.model_dump(exclude_unset=True)
    for field, value in changes.items():
        setattr(row, field, value)
    await db.commit()
    return {"updated": True, "changes": changes}
