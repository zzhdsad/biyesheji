"""向量化抽象：BGE-M3（稠密 1024d + 稀疏 lexical weights）（依赖倒置）。

TECH_DESIGN：Embedding 使用 BAAI/bge-m3，输出稠密+稀疏双路向量供 Milvus 混合检索。
BGE-M3 稀疏向量为 lexical weights（token_id → 权重），与 Milvus SPARSE_FLOAT_VECTOR 对齐。

模型不需要用户下载：镜像构建阶段已把 BGE-M3 权重固化到 EMBEDDING_MODEL_PATH
（详见 backend/Dockerfile）；未配置该路径时再按 HF ID 加载（沿用 HF 缓存）。
"""

import hashlib
import math
import threading
from abc import ABC, abstractmethod
from pathlib import Path

from loguru import logger

from src.core.config import settings


class EmbeddingError(Exception):
    """向量化层异常。"""


class BaseEmbedding(ABC):
    """向量化接口：texts → (dense_vectors, sparse_vectors)。"""

    @abstractmethod
    def encode(self, texts: list[str]) -> tuple[list[list[float]], list[dict[int, float]]]:
        """批量编码。

        Returns:
            dense: [n][1024] 稠密向量
            sparse: [n]{token_id: weight} 稀疏向量（lexical weights）
        """


class BGE3Embedding(BaseEmbedding):
    """BGE-M3 本地推理（FlagEmbedding），稠密 1024d + 稀疏 lexical weights。

    模型由镜像内置（EMBEDDING_MODEL_PATH），无需运行时下载；
    未配置本地路径时才按 HF ID 加载（走 HF_ENDPOINT 镜像 + 本地 HF 缓存）。
    延迟导入避免未安装 FlagEmbedding 时影响启动。
    """

    def __init__(self, model_name: str | None = None, device: str | None = None) -> None:
        try:
            from FlagEmbedding import BGEM3FlagModel
        except ImportError as exc:
            # FlagEmbedding 属于镜像内置依赖；走到这里说明运行环境不是本项目镜像
            raise EmbeddingError(
                "BGE-M3 依赖缺失（未安装 FlagEmbedding）。"
                "请使用项目镜像部署，或在部署环境 pip install -r requirements-ai.txt。"
            ) from exc
        _device = device or settings.EMBEDDING_DEVICE
        _model = self._resolve_model_path(model_name)
        logger.info(f"加载 BGE-M3 模型（device={_device}, model={_model}）…")
        self._model = BGEM3FlagModel(
            _model,
            use_fp16=_device != "cpu",
            device=_device,
        )

    @staticmethod
    def _resolve_model_path(model_name: str | None) -> str:
        """本地内置路径优先 → HF ID 回落；配置了本地路径则要求其真实存在。"""
        local_path = settings.EMBEDDING_MODEL_PATH or None
        resolved = local_path or model_name or settings.EMBEDDING_MODEL
        if local_path and not Path(local_path).exists():
            raise EmbeddingError(
                f"内置 BGE-M3 模型路径不存在：{local_path}。"
                "请检查镜像模型目录或清空 EMBEDDING_MODEL_PATH 走模型 ID 加载。"
            )
        return resolved

    def encode(self, texts: list[str]) -> tuple[list[list[float]], list[dict[int, float]]]:
        if not texts:
            return [], []
        out = self._model.encode(
            texts,
            batch_size=settings.EMBEDDING_BATCH_SIZE,
            max_length=1024,
            return_dense=True,
            return_sparse=True,
            return_colbert_vecs=False,
        )
        dense = [list(map(float, v)) for v in out["dense_vecs"]]
        sparse = [{int(k): float(w) for k, w in lw.items()} for lw in out["lexical_weights"]]
        return dense, sparse


class MockEmbedding(BaseEmbedding):
    """确定性伪向量化：**仅供单元测试**（避免测试加载 2.3GB 真实模型）。

    相同文本输出恒定；不同文本向量不同。
    生产环境不可选：``get_embedding`` 不再提供 mock 分支，模型只能由 DB/env 配置
    （且默认为镜像内置的 BGE-M3）。
    """

    def __init__(self, dim: int | None = None) -> None:
        self.dim = dim or settings.MILVUS_DIM

    def encode(self, texts: list[str]) -> tuple[list[list[float]], list[dict[int, float]]]:
        dense: list[list[float]] = []
        sparse: list[dict[int, float]] = []
        for text in texts:
            seed = hashlib.sha256(text.encode("utf-8")).digest()
            # 稠密：以 hash 字节流驱动确定性正弦序列，归一化
            vec = [
                math.sin(int.from_bytes(seed[i % len(seed) :] + bytes([i % 256]), "big")) * 0.5 + 0.5
                for i in range(self.dim)
            ]
            norm = math.sqrt(sum(v * v for v in vec)) or 1.0
            dense.append([v / norm for v in vec])
            # 稀疏：按字符切词，hash 为 token_id，频率为权重
            weights: dict[int, float] = {}
            for ch in text.strip() or "empty":
                tid = int.from_bytes(hashlib.md5(ch.encode()).digest()[:4], "big") % 1_000_000
                weights[tid] = weights.get(tid, 0.0) + 1.0
            sparse.append(weights)
        return dense, sparse


# 模块级单槽缓存：(配置 key, 实例)。BGE-M3 加载 2.3GB 耗时数十秒，
# 每次提问/每个后台任务重建实例会打爆内存与 CPU；配置变更时替换旧引用。
_embedding_cache: tuple[tuple, BaseEmbedding] | None = None
_embedding_lock = threading.Lock()


def _resolve_config(config: dict | None) -> tuple[str, str, str]:
    """解析生效配置（None/空值回退 env），key 与构造参数用同一组结果。"""
    c = config or {}
    backend = (c.get("embedding_backend") or settings.EMBEDDING_BACKEND).lower()
    model = c.get("embedding_model") or settings.EMBEDDING_MODEL
    device = (c.get("embedding_device") or settings.EMBEDDING_DEVICE).lower()
    return backend, model, device


def get_embedding(config: dict | None = None) -> BaseEmbedding:
    """按 EMBEDDING_BACKEND 创建/复用向量化实现（工厂 + 单例缓存）。

    Args:
        config: 运行时配置（DB），含 embedding_backend/embedding_model/embedding_device；
                None 时回退 env。

    缓存策略：按 (backend, model, device) 单槽缓存实例，命中直接复用
    （避免重复加载 2.3GB 模型）；配置变更时重建并替换旧实例。线程安全。
    """
    global _embedding_cache
    backend, model, device = _resolve_config(config)

    if backend == "mock":
        # 仅测试/环境级逃生舱：产品参数（DB 默认值、API 校验、前端选项）
        # 均已不再提供 mock，存量 mock 配置也由 ModelConfigService 规范化掉。
        return MockEmbedding()
    if backend != "flagembedding":
        raise EmbeddingError(f"未知 EMBEDDING_BACKEND：{backend}")

    key = (backend, model, device)
    with _embedding_lock:
        if _embedding_cache is not None and _embedding_cache[0] == key:
            return _embedding_cache[1]
        inst = BGE3Embedding(model_name=model, device=device)
        _embedding_cache = (key, inst)
        return inst


def reset_embedding_cache() -> None:
    """清空实例缓存（测试/模型配置变更后重建时使用）。

    下次 get_embedding() 会按新配置重新构造实现。
    """
    global _embedding_cache
    with _embedding_lock:
        _embedding_cache = None
    logger.info("embedding 实例缓存已清空（依赖初始化后生效）")
