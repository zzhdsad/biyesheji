"""模型配置服务：前端设置页动态配置 LLM/Embedding/Rerank/HyDE。

运行时配置存储在 model_configs 表（单行 id=1），优先级高于 .env 环境变量。
- get_effective_config()：合并 DB + env，供工厂 get_llm() 等读取
- get_config_for_frontend()：返回给前端，API key 脱敏
- update_config()：保存前端提交的配置
"""

import asyncio
from typing import Any

from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.config import settings
from src.domain.models import ModelConfig


def mask_api_key(key: str) -> str:
    """脱敏：sk-abcdefghijklmnopqrstuvwxyz123456 → sk-****1234"""
    if not key:
        return ""
    if len(key) <= 8:
        return "*" * len(key)
    return f"{key[:4]}****{key[-4:]}"


class ModelConfigService:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def _get_row(self) -> ModelConfig | None:
        return await self.db.get(ModelConfig, 1)

    async def get_effective_config(self) -> dict[str, Any]:
        """合并 DB 配置与 env 默认值，供工厂函数读取（含真实 API key）。"""
        row = await self._get_row()
        if row is None:
            # 无 DB 配置：全部回退 env
            return self._env_defaults()
        return {
            "llm_provider": row.llm_provider or settings.LLM_BACKEND,
            "llm_base_url": row.llm_base_url or settings.LLM_BASE_URL,
            "llm_model": row.llm_model or settings.LLM_MODEL,
            "llm_api_key": row.llm_api_key or settings.LLM_API_KEY,
            "embedding_backend": row.embedding_backend or settings.EMBEDDING_BACKEND,
            "embedding_model": row.embedding_model or settings.EMBEDDING_MODEL,
            "embedding_device": row.embedding_device or settings.EMBEDDING_DEVICE,
            "rerank_backend": row.rerank_backend or settings.RERANK_BACKEND,
            "rerank_model": row.rerank_model or settings.RERANK_MODEL,
            "rerank_device": row.rerank_device or settings.RERANK_DEVICE,
            "hyde_enabled": row.hyde_enabled,
            "hyde_backend": row.hyde_backend or settings.HYDE_BACKEND,
            "hyde_model": row.hyde_model or settings.HYDE_MODEL,
            "hyde_base_url": row.hyde_base_url or settings.HYDE_BASE_URL or settings.LLM_BASE_URL,
        }

    async def get_config_for_frontend(self) -> dict[str, Any]:
        """返回给前端的配置（API key 脱敏，保留前4后4）。"""
        cfg = await self.get_effective_config()
        cfg["llm_api_key"] = mask_api_key(cfg["llm_api_key"])
        return cfg

    async def update_config(self, data: dict[str, Any]) -> dict[str, Any]:
        """保存配置。若 API key 是脱敏格式（含 ****）则保留原值。"""
        row = await self._get_row()
        if row is None:
            row = ModelConfig(id=1)
            self.db.add(row)

        # API key 处理：脱敏值不覆盖
        submitted_key = data.get("llm_api_key", "")
        if submitted_key and "****" not in submitted_key:
            row.llm_api_key = submitted_key
        elif not submitted_key:
            row.llm_api_key = ""
        # 若提交的是脱敏值，保留数据库原值（不更新）

        row.llm_provider = data.get("llm_provider", row.llm_provider)
        row.llm_base_url = data.get("llm_base_url", row.llm_base_url)
        row.llm_model = data.get("llm_model", row.llm_model)
        row.embedding_backend = data.get("embedding_backend", row.embedding_backend)
        row.embedding_model = data.get("embedding_model", row.embedding_model)
        row.embedding_device = data.get("embedding_device", row.embedding_device)
        row.rerank_backend = data.get("rerank_backend", row.rerank_backend)
        row.rerank_model = data.get("rerank_model", row.rerank_model)
        row.rerank_device = data.get("rerank_device", row.rerank_device)
        row.hyde_enabled = data.get("hyde_enabled", row.hyde_enabled)
        row.hyde_backend = data.get("hyde_backend", row.hyde_backend)
        row.hyde_model = data.get("hyde_model", row.hyde_model)
        row.hyde_base_url = data.get("hyde_base_url", row.hyde_base_url)

        await self.db.commit()
        await self.db.refresh(row)
        logger.info(f"模型配置已更新: provider={row.llm_provider} model={row.llm_model}")
        return await self.get_config_for_frontend()

    @staticmethod
    def _env_defaults() -> dict[str, Any]:
        return {
            "llm_provider": settings.LLM_BACKEND,
            "llm_base_url": settings.LLM_BASE_URL,
            "llm_model": settings.LLM_MODEL,
            "llm_api_key": settings.LLM_API_KEY,
            "embedding_backend": settings.EMBEDDING_BACKEND,
            "embedding_model": settings.EMBEDDING_MODEL,
            "embedding_device": settings.EMBEDDING_DEVICE,
            "rerank_backend": settings.RERANK_BACKEND,
            "rerank_model": settings.RERANK_MODEL,
            "rerank_device": settings.RERANK_DEVICE,
            "hyde_enabled": settings.HYDE_ENABLED,
            "hyde_backend": settings.HYDE_BACKEND,
            "hyde_model": settings.HYDE_MODEL,
            "hyde_base_url": settings.HYDE_BASE_URL or settings.LLM_BASE_URL,
        }


# 模块级缓存：工厂函数每次调用都需要 DB session，这里提供同步便捷方法
# 实际工厂通过传入的 db session 获取配置
_config_cache: dict[str, Any] | None = None
_cache_lock = asyncio.Lock()


async def get_effective_config_cached(db: AsyncSession) -> dict[str, Any]:
    """带缓存的配置读取（30s TTL），避免每次 RAG 调用都查库。"""
    global _config_cache
    if _config_cache is not None:
        return _config_cache
    async with _cache_lock:
        if _config_cache is not None:
            return _config_cache
        svc = ModelConfigService(db)
        _config_cache = await svc.get_effective_config()
        return _config_cache


def invalidate_config_cache() -> None:
    """配置更新后调用，使缓存失效。"""
    global _config_cache
    _config_cache = None
