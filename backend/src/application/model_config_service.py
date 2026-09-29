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


def normalize_backends(cfg: dict[str, Any]) -> dict[str, Any]:
    """把历史遗留的 ``mock`` 后端替换为对应的真实后端。

    早期版本 model_configs 各后端默认值是 mock；升级后生产不再提供 mock 实现，
    存量行若直接透传会让整个系统静默退化成伪向量 / 假重排 / 假改写。
    """
    replacements = {
        "llm_provider": settings.LLM_BACKEND,
        "embedding_backend": settings.EMBEDDING_BACKEND,
        "rerank_backend": settings.RERANK_BACKEND,
        "hyde_backend": settings.HYDE_BACKEND,
    }
    for key, default in replacements.items():
        if str(cfg.get(key) or "").lower() == "mock":
            cfg[key] = default
    return cfg


class ModelConfigService:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def _get_row(self) -> ModelConfig | None:
        return await self.db.get(ModelConfig, 1)

    async def get_effective_config(self) -> dict[str, Any]:
        """合并 DB 配置与 env 默认值，供工厂函数读取（含真实 API key）。

        历史兼容：早期版本各后端的默认值是 ``mock``，存量行可能仍为 mock。
        这里统一规范化为真实后端（LLM: openai 兼容 / Embedding: BGE-M3 /
        Rerank: FlagReranker / HyDE: OpenAI 兼容复用主 LLM），
        避免升级后整个系统静默退化成伪向量与假回答。
        """
        row = await self._get_row()
        if row is None:
            # 无 DB 配置：全部回退 env
            return normalize_backends(self._env_defaults())
        return normalize_backends({
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
            # HyDE 默认复用主 LLM：未单独配置时回落 LLM 的模型与地址
            "hyde_enabled": row.hyde_enabled,
            "hyde_backend": row.hyde_backend or settings.HYDE_BACKEND,
            "hyde_model": row.hyde_model or "",
            "hyde_base_url": row.hyde_base_url or "",
        })

    async def get_config_for_frontend(self) -> dict[str, Any]:
        """返回给前端的配置（API key 脱敏，保留前4后4）。

        额外给出 ``llm_api_key_configured``：前端据此显示"已配置 / 未配置"，
        而不是让用户凭脱敏串判断"是不是又要重新填一次"。
        """
        cfg = await self.get_effective_config()
        raw_key = cfg.get("llm_api_key") or ""
        cfg["llm_api_key"] = mask_api_key(raw_key)
        cfg["llm_api_key_configured"] = bool(raw_key.strip())
        cfg["llm_api_key_masked"] = cfg["llm_api_key"]
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
            # 留空 = 复用主 LLM（具体回落由 QwenHyDE 完成）
            "hyde_model": settings.HYDE_MODEL,
            "hyde_base_url": settings.HYDE_BASE_URL or "",
        }


# 模块级缓存：工厂函数每次调用都需要 DB session，这里提供同步便捷方法
# 实际工厂通过传入的 db session 获取配置
_config_cache: dict[str, Any] | None = None
_cache_lock = asyncio.Lock()


async def get_effective_config_cached(db: AsyncSession) -> dict[str, Any]:
    """带缓存的配置读取，避免每次 RAG 调用都查库。

    进程内单槽缓存（无 TTL）：配置更新（PUT /settings/model）后由
    ``invalidate_config_cache()`` 主动失效。
    """
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
