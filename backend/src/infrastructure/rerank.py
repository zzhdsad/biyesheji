"""重排序（Rerank）抽象层：多路召回 + RRF 融合后对候选精排。

TECH_DESIGN §1.4 / §4.3：BGE-Reranker-v2-m3（Cross-Encoder）对 Top-K 精排 → Top-N 送 LLM。
依赖倒置：BaseRerank 抽象 + BGERerank 真实实现 + MockRerank 降级，工厂注入。

模型加载策略（用户需求：路径通过 .env 配置 + 首次加载缓存）：
- RERANK_MODEL_PATH 优先：本地预下载模型路径（离线/生产），避免运行时下载
- 否则按 RERANK_MODEL（HuggingFace ID）从远端拉取，国内默认走 HF_ENDPOINT 镜像
- 实例级缓存：_model 单例，进程内只加载一次；工厂级单例 _rerank 进一步跨调用复用
"""

from __future__ import annotations

from loguru import logger

from src.core.config import settings


class RerankError(Exception):
    """重排序失败。"""


class BaseRerank:
    """重排序抽象接口：对 query 与候选段落打分并重排。"""

    def rerank(self, query: str, candidates: list[dict], top_n: int) -> list[dict]:
        """对候选按与 query 的相关性重排，返回前 top_n 条。

        每条候选新增/更新 ``rerank_score`` 字段（越大越相关）。
        """
        raise NotImplementedError


class MockRerank(BaseRerank):
    """确定性伪重排（开发/测试）：按 query 字符与候选内容重叠率打分。

    无需下载模型即可验证 RRF→Rerank→Top-N 全流程链路。
    """

    @staticmethod
    def _overlap(query: str, content: str) -> float:
        # 字符级重叠率（中文友好）：query 中出现在 content 的字符占比
        if not query:
            return 0.0
        q_chars = set(query)
        hit = sum(1 for c in q_chars if c in content)
        return hit / len(q_chars)

    def rerank(self, query: str, candidates: list[dict], top_n: int) -> list[dict]:
        scored = [
            (self._overlap(query, c.get("content", "")), c) for c in candidates
        ]
        scored.sort(key=lambda t: t[0], reverse=True)
        return [
            {**c, "rerank_score": round(score, 6)} for score, c in scored[:top_n]
        ]


class BGERerank(BaseRerank):
    """BGE-Reranker-v2-m3（FlagEmbedding Cross-Encoder）真实实现。

    依赖：pip install FlagEmbedding（模型约 2.3GB，首次运行自动下载）。

    加载顺序（缓存优先级）：
    1. RERANK_MODEL_PATH 非空 → 本地路径直接加载（离线/生产推荐）
    2. 否则按 RERANK_MODEL（HuggingFace ID）远端拉取

    实例级缓存：self._model 首次加载后复用，进程内不再重复加载。
    """

    # 历史别名（向后兼容旧引用）
    def __init__(self, model_name: str | None = None) -> None:
        # 优先用本地路径（如配置），否则用 HuggingFace 模型 ID
        self._local_path: str | None = settings.RERANK_MODEL_PATH or None
        self._model_name: str = (
            model_name or self._local_path or settings.RERANK_MODEL
        )
        self._model = None  # 实例级缓存，首次调用 _get_model() 时加载

    def _get_model(self):
        """惰性加载并缓存 FlagReranker 实例。

        Returns:
            FlagReranker — 已加载的 Cross-Encoder 模型

        Raises:
            RerankError: 未安装 FlagEmbedding / 模型加载失败
        """
        if self._model is not None:
            return self._model

        try:
            from FlagEmbedding import FlagReranker
        except ImportError as exc:
            raise RerankError(
                "未安装 FlagEmbedding：pip install -r requirements-ai.txt"
                "，或在 .env 设置 RERANK_BACKEND=mock"
            ) from exc

        # CPU 设备禁用 fp16（半精度在 CPU 上不支持/无加速），GPU 才开启
        use_fp16 = settings.RERANK_DEVICE != "cpu"
        load_from = (
            f"本地路径={self._local_path}"
            if self._local_path
            else f"HuggingFace ID={self._model_name}"
        )
        logger.info(
            f"Reranker 首次加载中（{load_from}, device={settings.RERANK_DEVICE}, "
            f"use_fp16={use_fp16}）… 首次加载约 2.3GB，可能耗时较长…"
        )
        try:
            self._model = FlagReranker(
                self._model_name, use_fp16=use_fp16
            )
            logger.info(f"Reranker 加载完成：{self._model_name}")
        except Exception as exc:
            raise RerankError(
                f"Reranker 加载失败（{self._model_name}）：{exc}"
            ) from exc
        return self._model

    def rerank(
        self, query: str, candidates: list[dict], top_n: int
    ) -> list[dict]:
        """对 (query, doc) 对计算相关性分数 → 按分数降序 → 返回 top_n 条。

        Args:
            query: 用户原始问题
            candidates: RRF 融合后的候选段落列表，每条至少含 content 字段
            top_n: 返回的精排前 N 条数

        Returns:
            list[dict] — 每条新增 ``rerank_score`` 字段（归一化 0-1，越大越相关）
        """
        if not candidates:
            return []
        model = self._get_model()
        pairs = [[query, c.get("content", "")] for c in candidates]
        try:
            # normalize=True 输出 sigmoid 归一化分数（0-1）
            scores = model.compute_score(pairs, normalize=True)
        except Exception as exc:
            raise RerankError(f"Reranker 打分失败：{exc}") from exc
        # compute_score 单条返回 float，多条返回 list
        if isinstance(scores, float):
            scores = [scores]
        scored = sorted(
            zip(scores, candidates), key=lambda t: t[0], reverse=True
        )
        return [
            {**c, "rerank_score": round(float(s), 6)}
            for s, c in scored[:top_n]
        ]


# 向后兼容别名（如有外部代码引用旧类名）
FlagRerankerModel = BGERerank


_rerank: BaseRerank | None = None


def get_rerank() -> BaseRerank:
    """工厂单例：按 RERANK_BACKEND 注入（mock / flagreranker）。

    模块级单例 _rerank 跨调用复用，确保 BGE-Reranker 模型在进程内只加载一次。
    """
    global _rerank
    if _rerank is not None:
        return _rerank
    backend = settings.RERANK_BACKEND.lower()
    if backend == "flagreranker":
        _rerank = BGERerank()
        logger.info("Reranker 后端：flagreranker（BGE-Reranker-v2-m3 真实精排）")
    else:
        _rerank = MockRerank()
        logger.info("Reranker 后端：mock（开发模式，无模型依赖）")
    return _rerank


def set_rerank(rerank: BaseRerank | None) -> None:
    """注入重排序器（测试用）。"""
    global _rerank
    _rerank = rerank
