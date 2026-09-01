"""RAGAS 指标（中文适配）。

TECH_DESIGN：采用 RAGAS（中文适配）作为自动化质量评估框架。本模块遵循 RAGAS 的
指标定义，使用中文 prompt 适配中文语料，复用项目自有的 LLM / Embedding 抽象
（mock 可用，可平滑切换真实模型），避免 ragas 库英文 prompt 对中文评估不友好、
以及与 MockLLM 协议耦合的脆弱性。

实现指标：
- Context Relevancy：LLM 从检索上下文中抽取对回答问题有实质帮助的句子，
  得分 = 相关句数 / 总句数（RAGAS context precision/relevancy 定义）。
- Answer Correctness：LLM 从生成答案与标准答案抽取事实陈述，按 TP/FP/FN 计算
  事实相似度 F1，再与 Embedding 余弦语义相似度加权融合（RAGAS answer correctness 定义）。

鲁棒性：LLM 抽取/解析失败时回退到基于字符 token 重叠的确定性打分，保证评估流水线
在任何后端（含 mock）下都能产出数值，便于联调与回归。
"""

import asyncio
import json
import math
import re

from loguru import logger

from src.infrastructure.embedding import BaseEmbedding
from src.infrastructure.llm import BaseLLM, LLMError

# Answer Correctness 中事实相似度权重（RAGAS 默认语义与事实各占一半，可经环境覆盖）
_FACTUAL_WEIGHT = 0.5


def _split_sentences(text: str) -> list[str]:
    """中文友好的句子切分：按句末标点/换行切分，丢弃空白句。"""
    if not text:
        return []
    parts = re.split(r"(?<=[。！？!?\n])\s*", text)
    return [s.strip() for s in parts if s and s.strip()]


def _char_tokens(text: str) -> set[str]:
    """中文友好的 token：去除空白与标点后的字符集合（unigram）。"""
    cleaned = re.sub(r"[\s，。、；：！？,.!?;:\"'()（）\[\]【】]+", "", text or "")
    return set(cleaned)


def _token_f1(prediction: str, reference: str) -> float:
    """字符级 token 重叠 F1（LLM 不可用时的确定性回退）。"""
    p, r = _char_tokens(prediction), _char_tokens(reference)
    if not p or not r:
        return 0.0
    overlap = len(p & r)
    if overlap == 0:
        return 0.0
    precision = overlap / len(p)
    recall = overlap / len(r)
    return 2 * precision * recall / (precision + recall)


def _cosine(a: list[float], b: list[float]) -> float:
    """余弦相似度（向量已归一化时等价内积，此处仍做范数兜底）。"""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a)) or 1.0
    nb = math.sqrt(sum(y * y for y in b)) or 1.0
    return max(0.0, min(1.0, dot / (na * nb)))


def _parse_json(raw: str) -> dict | None:
    """容错解析 LLM 输出的 JSON（去除 markdown 代码块与多余文本）。"""
    if not raw:
        return None
    s = raw.strip()
    # 去除 ```json ... ``` 包裹
    s = re.sub(r"^```(?:json)?\s*", "", s)
    s = re.sub(r"\s*```$", "", s)
    # 截取首个 { 到末个 } 之间内容，过滤前后噪声
    start, end = s.find("{"), s.rfind("}")
    if start != -1 and end != -1 and end > start:
        s = s[start : end + 1]
    try:
        return json.loads(s)
    except (ValueError, TypeError):
        return None


class RAGASMetrics:
    """RAGAS 指标计算器（中文适配）。

    依赖项目 LLM（陈述抽取/相关性判定）与 Embedding（语义相似度）。两者均可为 mock，
    抽取失败时回退到 token 重叠，确保流水线始终产出数值。
    """

    def __init__(self, llm: BaseLLM, embedding: BaseEmbedding) -> None:
        self.llm = llm
        self.embedding = embedding

    async def context_relevancy(self, question: str, contexts: list[str]) -> float:
        """上下文相关度 = 相关句数 / 总句数（RAGAS context relevancy）。

        将检索上下文按句切分并编号，让 LLM 选出对回答问题有实质帮助的句子编号。
        LLM 失败时回退：问题 token 在上下文中的命中率。
        """
        if not contexts:
            return 0.0
        sentences = _split_sentences("\n".join(contexts))[:200]
        if not sentences:
            return 0.0

        try:
            numbered = "\n".join(f"{i}. {s}" for i, s in enumerate(sentences, 1))
            prompt = (
                "你是评估助手。判断以下参考资料中哪些句子对回答问题有实质帮助（提供答案所需事实）。\n"
                f"问题：{question}\n参考资料（编号即句子编号）：\n{numbered}\n"
                '请仅输出 JSON：{"relevant_indices": [1, 3, ...]}，'
                "编号为有帮助的句子（1-based）；若无任何有帮助的句子，返回空数组 []。"
            )
            raw = await self.llm.chat([{"role": "user", "content": prompt}])
            data = _parse_json(raw)
            if data is None:
                # LLM 输出无法解析为 JSON（如 mock 或偶发噪声）→ 回退 token 命中
                logger.warning("context_relevancy 输出非 JSON，回退 token 命中")
                return self._ctx_relevancy_fallback(question, contexts)
            idxs = data.get("relevant_indices", []) or []
            relevant = {int(i) for i in idxs if str(i).isdigit() or isinstance(i, int)}
            score = len(relevant) / len(sentences)
            return max(0.0, min(1.0, score))
        except (LLMError, ValueError, TypeError) as exc:
            logger.warning(f"context_relevancy LLM 抽取失败，回退 token 命中: {exc}")
            return self._ctx_relevancy_fallback(question, contexts)

    def _ctx_relevancy_fallback(self, question: str, contexts: list[str]) -> float:
        """回退：问题 token 在上下文中的命中率（粗粒度相关度）。"""
        q = _char_tokens(question)
        if not q:
            return 0.0
        merged = _char_tokens(" ".join(contexts))
        hit = len(q & merged)
        return min(1.0, hit / max(1, len(q)))

    async def answer_correctness(
        self, question: str, answer: str, golden_answer: str
    ) -> float:
        """答案正确度 = w * 事实相似度F1 + (1-w) * 语义相似度（RAGAS answer correctness）。

        事实相似度：LLM 抽取答案与标准答案的事实陈述，按 TP/FP/FN 计算 F1。
        语义相似度：答案与标准答案 Embedding 余弦。
        LLM 抽取失败时事实相似度回退为字符 token F1。
        """
        factual = await self._factual_similarity(answer, golden_answer)
        semantic = await self._semantic_similarity(answer, golden_answer)
        w = _FACTUAL_WEIGHT
        return max(0.0, min(1.0, w * factual + (1 - w) * semantic))

    async def _factual_similarity(self, answer: str, golden_answer: str) -> float:
        """事实相似度 F1：TP / (TP + 0.5*(FP+FN))。

        - TP：答案陈述被标准答案支持
        - FP：答案陈述未被标准答案支持（编造/错误）
        - FN：标准答案陈述未被答案覆盖
        """
        stmts_ans, stmts_gold = await self._extract_statements(answer, golden_answer)
        if not stmts_ans and not stmts_gold:
            # 两边都无陈述：要么都为空（完全正确，1.0），要么抽取失败
            if not answer.strip() and not golden_answer.strip():
                return 1.0
            return _token_f1(answer, golden_answer)

        supported = await self._classify_support(stmts_ans, stmts_gold)
        tp = sum(1 for s in supported if s)
        fp = len(stmts_ans) - tp
        # FN：标准答案陈述未被任一答案陈述覆盖（近似：用支持判定反向）
        covered = await self._classify_support(stmts_gold, stmts_ans)
        fn = sum(1 for s in covered if not s)
        denom = tp + 0.5 * (fp + fn)
        return tp / denom if denom > 0 else 0.0

    async def _extract_statements(self, answer: str, golden: str) -> tuple[list[str], list[str]]:
        """LLM 从答案与标准答案抽取事实陈述（一次调用）。失败回退 None 驱动上层回退。"""
        prompt = (
            "你是评估助手。从下面的「生成答案」与「标准答案」中分别提取可独立成立的事实陈述（命题），"
            "用于正确性比对。仅提取陈述事实的句子，忽略纯客套/无信息量的话。\n"
            f"生成答案：{answer}\n标准答案：{golden}\n"
            '请仅输出 JSON：{"answer_statements": ["...", "..."], "golden_statements": ["...", "..."]}。'
        )
        try:
            raw = await self.llm.chat([{"role": "user", "content": prompt}])
            data = _parse_json(raw)
            if not data:
                return [], []
            ans = [str(s).strip() for s in data.get("answer_statements", []) if str(s).strip()]
            gold = [str(s).strip() for s in data.get("golden_statements", []) if str(s).strip()]
            return ans, gold
        except (LLMError, ValueError, TypeError) as exc:
            logger.warning(f"陈述抽取失败，回退 token F1: {exc}")
            return [], []

    async def _classify_support(self, predicted: list[str], reference: list[str]) -> list[bool]:
        """LLM 判定 predicted 中每条陈述是否被 reference 集合支持。失败回退 token 重叠。"""
        if not predicted:
            return []
        if not reference:
            return [False] * len(predicted)
        prompt = (
            "你是评估助手。判断每条「待评陈述」是否被「参考陈述」集合中的事实所支持"
            "（语义等价或可由参考推出）。\n"
            f"待评陈述：\n{self._numbered(predicted)}\n参考陈述：\n{self._numbered(reference)}\n"
            '请仅输出 JSON：{"supported": [true, false, ...]}，'
            "顺序与待评陈述编号一一对应。"
        )
        try:
            raw = await self.llm.chat([{"role": "user", "content": prompt}])
            data = _parse_json(raw)
            if not data or not isinstance(data.get("supported"), list):
                return self._token_support_fallback(predicted, reference)
            flags = [bool(x) for x in data["supported"]]
            # 长度对齐
            if len(flags) < len(predicted):
                flags += [False] * (len(predicted) - len(flags))
            return flags[: len(predicted)]
        except (LLMError, ValueError, TypeError) as exc:
            logger.warning(f"支持判定失败，回退 token 重叠: {exc}")
            return self._token_support_fallback(predicted, reference)

    @staticmethod
    def _numbered(items: list[str]) -> str:
        return "\n".join(f"{i}. {s}" for i, s in enumerate(items, 1))

    @staticmethod
    def _token_support_fallback(predicted: list[str], reference: list[str]) -> list[bool]:
        """回退：预测陈述与参考陈述集合的 token 重叠是否超过阈值。"""
        ref_tokens = _char_tokens(" ".join(reference))
        out = []
        for p in predicted:
            pt = _char_tokens(p)
            if not pt or not ref_tokens:
                out.append(False)
                continue
            overlap = len(pt & ref_tokens) / max(1, len(pt))
            out.append(overlap >= 0.5)
        return out

    async def _semantic_similarity(self, a: str, b: str) -> float:
        """Embedding 余弦语义相似度。"""
        if not a.strip() or not b.strip():
            return 0.0
        try:
            dense, _ = await asyncio.to_thread(self.embedding.encode, [a, b])
            return _cosine(dense[0], dense[1]) if len(dense) == 2 else 0.0
        except Exception as exc:  # noqa: BLE001 — Embedding 失败不应中断评估
            logger.warning(f"语义相似度计算失败，回退 token F1: {exc}")
            return _token_f1(a, b)


def get_metrics() -> RAGASMetrics:
    """工厂：复用项目 LLM / Embedding 工厂，与 RagService 同源。"""
    from src.infrastructure.embedding import get_embedding
    from src.infrastructure.llm import get_llm

    return RAGASMetrics(get_llm(), get_embedding())
