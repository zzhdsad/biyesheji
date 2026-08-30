"""向量化抽象：BGE-M3（稠密 1024d + 稀疏 lexical weights）/ mock 可切换（依赖倒置）。

TECH_DESIGN：Embedding 使用 BAAI/bge-m3，输出稠密+稀疏双路向量供 Milvus 混合检索。
BGE-M3 稀疏向量为 lexical weights（token_id → 权重），与 Milvus SPARSE_FLOAT_VECTOR 对齐。
"""

import hashlib
import math
from abc import ABC, abstractmethod

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

    模型约 2.3GB，首次使用自动下载；延迟导入避免未安装 FlagEmbedding 时影响启动。
    """

    def __init__(self) -> None:
        try:
            from FlagEmbedding import BGEM3FlagModel
        except ImportError as exc:
            raise EmbeddingError(
                "未安装 FlagEmbedding，无法使用 BGE-M3 向量化。"
                "请安装：pip install FlagEmbedding，或在 .env 设置 EMBEDDING_BACKEND=mock"
            ) from exc
        logger.info(f"加载 BGE-M3 模型（device={settings.EMBEDDING_DEVICE}）…")
        self._model = BGEM3FlagModel(
            settings.EMBEDDING_MODEL,
            use_fp16=settings.EMBEDDING_DEVICE != "cpu",
            device=settings.EMBEDDING_DEVICE,
        )

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
    """确定性伪向量化（开发/测试）：文本 hash 为种子生成稠密+稀疏向量。

    相同文本输出恒定；不同文本向量不同，可满足开发环境联调与单元测试。
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


def get_embedding() -> BaseEmbedding:
    """按 EMBEDDING_BACKEND 创建向量化实现（工厂）。"""
    backend = settings.EMBEDDING_BACKEND
    if backend == "flagembedding":
        return BGE3Embedding()
    if backend == "mock":
        return MockEmbedding()
    raise EmbeddingError(f"未知 EMBEDDING_BACKEND：{backend}")
