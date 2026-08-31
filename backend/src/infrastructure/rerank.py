"""重排序（Rerank）抽象层：多路召回 + RRF 融合后对候选精排。

TECH_DESIGN：bge-reranker-v2-m3（Cross-Encoder）对 Top-K 精排 → Top-N 送 LLM。
依赖倒置：BaseRerank 抽象 + FlagReranker 真实实现 + MockRerank 降级，工厂注入。
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


class FlagRerankerModel(BaseRerank):
    """BGE-Reranker-v2-m3（FlagEmbedding Cross-Encoder）真实实现。

    依赖：pip install FlagEmbedding（模型约 2.3GB，首次运行自动下载）。
    """

    def __init__(self, model_name: str | None = None) -> None:
        self._model_name = model_name or settings.RERANK_MODEL
        self._model = None

    def _get_model(self):
        if self._model is None:
            try:
                from FlagEmbedding import FlagReranker
            except ImportError as exc:
                raise RerankError(
                    "未安装 FlagEmbedding：pip install FlagEmbedding"
                ) from exc
            try:
                self._model = FlagReranker(self._model_name, use_fp16=True)
                logger.info(f"Reranker 已加载：{self._model_name}")
            except Exception as exc:
                raise RerankError(f"Reranker 加载失败（{self._model_name}）：{exc}") from exc
        return self._model

    def rerank(self, query: str, candidates: list[dict], top_n: int) -> list[dict]:
        if not candidates:
            return []
        model = self._get_model()
        pairs = [[query, c.get("content", "")] for c in candidates]
        try:
            scores = model.compute_score(pairs, normalize=True)
        except Exception as exc:
            raise RerankError(f"Reranker 打分失败：{exc}") from exc
        # compute_score 单条返回 float，多条返回 list
        if isinstance(scores, float):
            scores = [scores]
        scored = sorted(zip(scores, candidates), key=lambda t: t[0], reverse=True)
        return [
            {**c, "rerank_score": round(float(s), 6)} for s, c in scored[:top_n]
        ]


_rerank: BaseRerank | None = None


def get_rerank() -> BaseRerank:
    """工厂单例：按 RERANK_BACKEND 注入（mock / flagreranker）。"""
    global _rerank
    if _rerank is not None:
        return _rerank
    backend = settings.RERANK_BACKEND.lower()
    if backend == "flagreranker":
        _rerank = FlagRerankerModel()
    else:
        _rerank = MockRerank()
        logger.info("Reranker 使用 mock 实现（开发模式）")
    return _rerank


def set_rerank(rerank: BaseRerank | None) -> None:
    """注入重排序器（测试用）。"""
    global _rerank
    _rerank = rerank
