"""模型配置路由：前端设置页动态配置 LLM/Embedding/Rerank/HyDE。

所有接口受 protected_router 统一鉴权；**修改/连通性测试仅管理员可用**
（配置影响全站问答链路，普通用户不得修改，也不得借测试端点发起任意请求）。
"""

import uuid

from fastapi import APIRouter, Depends, Request
from loguru import logger
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.exceptions import AppException

from src.application import revectorize_service
from src.application.model_config_service import (
    ModelConfigService,
    get_effective_config_cached,
    invalidate_config_cache,
)
from src.application.vector_model import (
    key_from_config,
    stamp_legacy_documents,
    stale_doc_ids,
    stale_models,
)
from src.core.deps import get_current_user, get_db, require_admin
from src.core import runtime_config
from src.infrastructure.llm import OpenAICompatibleLLM
from src.utils.net import assert_safe_public_url

router = APIRouter(prefix="/settings", tags=["settings"])


def _require_admin(request: Request) -> None:
    """模型配置属全局配置，仅系统管理员可修改/测试。

    实现收敛到 src.core.deps.require_admin（BUG-068），业务文案保持不变。
    """
    require_admin(request, "仅管理员可修改模型配置")


class ModelConfigUpdate(BaseModel):
    """前端提交的模型配置（API key 可为空或脱敏值）。"""

    # 各后端均为真实实现（模型权重随镜像内置），不再提供 mock 降级选项
    llm_provider: str = Field(default="openai", pattern="^(deepseek|openai|qwen|ollama|custom)$")
    llm_base_url: str = Field(default="", max_length=512)
    llm_model: str = Field(default="", max_length=128)
    llm_api_key: str = Field(default="", max_length=512)
    embedding_backend: str = Field(default="flagembedding", pattern="^(flagembedding)$")
    embedding_model: str = Field(default="BAAI/bge-m3", max_length=128)
    embedding_device: str = Field(default="cpu", pattern="^(cpu|cuda)$")
    rerank_backend: str = Field(default="flagreranker", pattern="^(flagreranker)$")
    rerank_model: str = Field(default="BAAI/bge-reranker-v2-m3", max_length=128)
    rerank_device: str = Field(default="cpu", pattern="^(cpu|cuda)$")
    hyde_enabled: bool = Field(default=True)
    hyde_backend: str = Field(default="openai", pattern="^(openai)$")
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
    """更新模型配置（仅 admin）。保存后失效缓存，下次 RAG 调用加载新配置。

    embedding 模型标识发生变化时，把此前"未标注"的存量文档标注为**旧模型**，
    使其进入"需重新向量化"统计并在检索时被排除（避免新旧向量混检）。
    """
    _require_admin(request)
    svc = ModelConfigService(db)
    old_key = key_from_config(await svc.get_effective_config())
    result = await svc.update_config(payload.model_dump())
    invalidate_config_cache()
    new_key = key_from_config(await svc.get_effective_config())
    if old_key != new_key:
        stamped = await stamp_legacy_documents(db, old_key)
        logger.info(f"embedding 模型切换 {old_key} → {new_key}，标注存量文档 {stamped} 篇")
    return result


@router.post("/model/test")
async def test_model_connection(
    payload: ModelConfigUpdate,
    request: Request,
) -> dict:
    """测试 LLM 连通性（仅 admin）：用提交的配置发一条极简 chat 请求。

    安全：先校验调用者身份，再校验 Base URL 不属于内网/本机/云元数据地址
    （SSRF 防护），失败信息不回显响应体原文。
    """
    _require_admin(request)
    # 测试不需要保存到 DB，直接用提交的参数构建客户端
    if not payload.llm_base_url or not payload.llm_model:
        return {"ok": False, "message": "请填写 Base URL 和模型名称"}
    # SSRF 防护：禁止内网 / 本机 / 云元数据地址
    assert_safe_public_url(payload.llm_base_url, field="LLM Base URL")
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
        # 详细异常只写日志：避免把内部响应体/堆栈回显给调用方
        logger.warning(f"模型连通性测试失败 base_url={payload.llm_base_url}: {exc}")
        return {"ok": False, "message": f"连通失败：{type(exc).__name__}"}


# ── Embedding 依赖状态 / 初始化 / 批量重新向量化 ──────────────────────────────


class RevectorizeRequest(BaseModel):
    """发起批量重新向量化；kb_id 为空表示全库。"""

    kb_id: str | None = None
    # 小批量分批处理：只取前 N 篇（不传则处理全部存量旧向量文档）
    limit: int | None = Field(default=None, ge=1, le=5000)


class RetryRequest(BaseModel):
    """重试指定任务的失败项。"""

    job_id: str


class CancelRequest(BaseModel):
    """取消进行中的任务。"""

    job_id: str


@router.get("/model/embedding/status")
async def embedding_status(
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Embedding 运行状态总览（依赖 / 存量向量模型 / 待重建数量 / 任务进度）。

    只读接口，登录用户可查看（不含任何密钥）。
    """
    # 与检索/向量化同源：读缓存后的生效配置，保证"当前模型"判定一致
    cfg = await get_effective_config_cached(db)
    current_key = key_from_config(cfg)

    stale_ids = await stale_doc_ids(db, current_key)
    models = await stale_models(db, current_key)
    job = await revectorize_service.latest_job(db)

    # 依赖（FlagEmbedding + 模型权重）随镜像内置，不再有"待安装/初始化中"状态：
    # 状态只由重新向量化任务决定（running → revectorizing，否则 ready）。
    dep_state = "revectorizing" if job is not None and job.status in ("pending", "running") else "ready"
    return {
        "config": {
            "backend": cfg.get("embedding_backend"),
            "model": cfg.get("embedding_model"),
            "device": cfg.get("embedding_device"),
            "model_key": current_key,
        },
        "vectors": {
            "current_model_key": current_key,
            "stale_documents": len(stale_ids),
            "stale_models": models,
        },
        "job": revectorize_service.job_to_dict(job),
        "overall_status": dep_state,
    }


@router.post("/model/revectorize")
async def start_revectorize(
    payload: RevectorizeRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """批量重新向量化（仅 admin）：后台异步重建旧模型的存量向量。"""
    _require_admin(request)
    # 目标模型 = 向量化实际会使用的模型（与 IndexingService 同源）
    cfg = await get_effective_config_cached(db)
    kb_id = uuid.UUID(payload.kb_id) if payload.kb_id else None
    job = await revectorize_service.create_job(
        db,
        target_model=key_from_config(cfg),
        kb_id=kb_id,
        user_id=getattr(request.state.user, "id", None),
        limit=payload.limit,
    )
    snapshot = revectorize_service.job_to_dict(job)
    revectorize_service.start_job(job.id)
    return snapshot


@router.get("/model/revectorize/status")
async def revectorize_status(
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> dict | None:
    """最近一次批量重新向量化任务的进度（前端轮询）。"""
    job = await revectorize_service.latest_job(db)
    return revectorize_service.job_to_dict(job)


@router.post("/model/revectorize/retry")
async def retry_revectorize(
    payload: RetryRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """重试指定任务的失败项（仅 admin）：只重建失败文档，不动已成功的文档。"""
    _require_admin(request)
    try:
        job_id = uuid.UUID(payload.job_id)
    except ValueError as exc:
        raise AppException(400, "job_id 格式非法") from exc
    job = await revectorize_service.retry_failed(
        db, job_id, getattr(request.state.user, "id", None)
    )
    snapshot = revectorize_service.job_to_dict(job)
    revectorize_service.start_job(job.id)
    return snapshot


@router.post("/model/revectorize/cancel")
async def cancel_revectorize(
    payload: CancelRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """取消进行中的批量重新向量化（仅 admin）。

    协作式取消：已完成的文档保留新向量，未处理的文档保持旧向量，可再次发起。
    """
    _require_admin(request)
    try:
        job_id = uuid.UUID(payload.job_id)
    except ValueError as exc:
        raise AppException(400, "job_id 格式非法") from exc
    job = await revectorize_service.cancel_job(db, job_id)
    return revectorize_service.job_to_dict(job)


# ── 系统级配置（BUSINESS_RULES §8）────────────────────────────────────────────


# 系统配置字段与其默认值来源统一定义在 src/core/runtime_config.py：
# 字段名大写即 core/config.py 中的 Settings 属性名，二者一一对应，
# 避免"默认值 / 字段清单"在两处各写一份导致漂移。
SYSTEM_CONFIG_FIELDS = runtime_config.SYSTEM_CONFIG_FIELDS


def _system_defaults() -> dict[str, object]:
    """系统配置默认值（单一来源：Settings）。"""
    from src.core.runtime_config import system_config_defaults

    return system_config_defaults()


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
    """获取系统级配置与默认值。

    - 无配置行 / 某个字段为 NULL → 回落到 Settings 默认值，界面永不出现空值
    - 返回值额外带 `defaults`，供前端展示各字段的默认值并支持"恢复默认"
    """
    from src.domain.models import SystemConfig

    defaults = _system_defaults()
    row = await db.get(SystemConfig, 1)
    values: dict[str, object] = {}
    for name in SYSTEM_CONFIG_FIELDS:
        current = getattr(row, name) if row is not None else None
        values[name] = defaults[name] if current is None else current
    return {**values, "defaults": defaults}


@router.put("/system")
async def update_system_config(
    payload: SystemConfigUpdate,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """更新系统级配置（仅 admin）。

    提交后立即让运行时快照失效，保证改动当次请求起就在全进程生效。
    """
    from src.domain.models import SystemConfig

    require_admin(request, "仅管理员可修改系统配置")

    row = await db.get(SystemConfig, 1)
    if row is None:
        row = SystemConfig(id=1)
        db.add(row)
    changes = payload.model_dump(exclude_unset=True)
    for field, value in changes.items():
        setattr(row, field, value)
    await db.commit()
    runtime_config.invalidate_system_config()
    logger.info(f"系统配置已更新并对运行时生效：{changes}")
    return {"updated": True, "changes": changes}


@router.post("/system/reset")
async def reset_system_config(
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """恢复系统配置为默认值（仅 admin）。

    把配置行各字段写回 Settings 默认值并返回，等价于"一键恢复默认"。
    """
    from src.domain.models import SystemConfig

    require_admin(request, "仅管理员可修改系统配置")

    defaults = _system_defaults()
    row = await db.get(SystemConfig, 1)
    if row is None:
        row = SystemConfig(id=1)
        db.add(row)
    for name, value in defaults.items():
        setattr(row, name, value)
    await db.commit()
    runtime_config.invalidate_system_config()
    logger.info(f"系统配置已恢复默认值并对运行时生效：{defaults}")
    return {**defaults, "defaults": defaults}
