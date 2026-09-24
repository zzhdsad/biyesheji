"""阶段十五：Self Reflection（自反思）。

流水线位置（需求 §8）：

    retrieve → evidence → EvidenceGate → answer → 【SelfReflection】
        ├─ accept → 返回答案
        ├─ revise → 基于现有证据修正答案（不重新检索，最多一次）
        └─ retry  → 重新检索一次后重新生成（最多一次）
    → final answer

与 Evidence Gate 的职责边界（需求 §7，必须严格保持）：

- **Evidence Gate**：判断「证据够不够」（检索后、生成前，管证据质量）。
- **Self Reflection**：判断「答案是否忠实使用了这些证据」（生成后，管答案忠实性）。
- Reflection **不重新实现 Gate**：不重复计算 Gate 的聚合统计，直接引用
  GateDecision（decision / version / details / retried）；
  Gate 判 insufficient 并已生成拒答时，Reflection 只能接受拒答，不得绕过。

设计约束：
1. **确定性规则优先**：覆盖检查 / 引用完整性 / KG 弱关系扩展医学结论等均为
   本地规则，不调用 LLM，同输入必得同输出（评测可复现）。
2. **LLM 严格限权**（可选，默认关闭）：只允许判断答案是否被资料支持、
   以及在 revise 模式下基于已有证据重写更保守的答案；禁止搜索外部知识、
   禁止产生新的医学事实、禁止绕过 Gate、禁止改写 QueryAnalysis、禁止无限调用。
3. **次数上限**（需求 §6）：Reflection retry 最多 1 次、revise 最多 1 次，
   并单独记录 gate_retry_count / reflection_retry_count / total_retry_count
   （实际是否执行由调用方 RagService 控制）。
4. **失败安全**：Reflection / LLM 自身异常 → accept + is_valid=False +
   fallback_reason，保留原 Answer/Evidence，不让链路失败。

阈值与规则均为「待实验验证的假设」，本阶段不声称 Reflection 提升任何指标。
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import re
from dataclasses import dataclass, field

from loguru import logger

from src.application.evidence import SOURCE_KIND_KG, build_evidence
from src.application.evidence_gate import (
    DECISION_ACCEPT as GATE_ACCEPT,
    KG_STRONG_RELATIONS,
    classify_evidence,
    select_retry_strategy,
)
from src.core.config import settings

# ── 反思规则版本（写入 ReflectionDecision / config_snapshot，实验可追溯）────
# reflection-v1：阶段十五初始规则（引用完整性 + 证据支持 + KG 弱关系 + 多源覆盖
#                + 可选 LLM 一致性检查 + 单次 revise / retry）
REFLECTION_VERSION = "reflection-v1"

# ── 三种反思结果（需求 §6）──────────────────────────────────────────────────
DECISION_ACCEPT = "accept"
DECISION_REVISE = "revise"
DECISION_RETRY = "retry"

REFLECTION_DECISIONS: tuple[str, ...] = (
    DECISION_ACCEPT,
    DECISION_REVISE,
    DECISION_RETRY,
)

# ── 拒答 / 保守回答识别（复用既有文案，禁止另立一套拒答口径）────────────────
REFUSAL_PREFIX = "根据现有资料，我无法回答该问题"
CONSERVATIVE_PREFIX = "根据现有资料，我无法对该问题给出充分可靠的回答"

# Reflection retry 后仍未获得可用证据时的最终保守回答（进入最终拒答/保守回答）
CONSERVATIVE_ANSWER = (
    "根据现有资料，我无法对该问题给出充分可靠的回答。"
    "已尝试换用其他检索策略重试，仍未获得可直接支持结论的证据，"
    "请补充相关资料或调整提问方式。"
)

# ── 句子级“答案是否落在证据上”的启发式阈值 ──────────────────────────────────
# 低于该长度的句子不视为事实句（避免把"好的/以下是"等寒暄与标题当成无证据陈述）
SENTENCE_MIN_LEN = 8
# 句级支持度阈值：字符二元组在证据文本中的覆盖率 ≥ 该值视为"落在证据上"
SUPPORT_MIN_OVERLAP = 0.25
# 最多保留多少条无依据句到 details（日志/实验分析用，不参与前端展示）
MAX_UNSUPPORTED_SAMPLES = 3
SAMPLE_MAX_LEN = 80

# ── 医学结论类关键词（出现在答案里，而支撑证据只有弱 KG 关系 → 不得接受）────
MEDICAL_CLAIM_KEYWORDS: tuple[str, ...] = (
    "主治", "功效", "疗效", "治疗", "治愈", "防治", "预防",
    "用药", "剂量", "用量", "禁忌", "适应症", "适用于", "医嘱", "处方",
)

# ── 问题 → 动作映射（确定性；retry 优先于 revise）───────────────────────────
ISSUE_ACTION: dict[str, str] = {
    # 需要新证据（现有证据不足以支撑答案）
    "answer_bypasses_gate": DECISION_RETRY,
    "no_evidence_for_factual_answer": DECISION_RETRY,
    "cited_evidence_weak": DECISION_RETRY,
    "kg_weak_relation_only": DECISION_RETRY,
    "multi_source_partial_coverage": DECISION_RETRY,
    # 现有证据足够，但答案表达超出了证据 → 基于同一份证据重写
    "no_citation_for_factual_answer": DECISION_REVISE,
    "citation_out_of_range": DECISION_REVISE,
    "unsupported_sentences": DECISION_REVISE,
    "medical_claim_from_weak_relation": DECISION_REVISE,
    "llm_unsupported_claims": DECISION_REVISE,
}

# ── LLM Reflection（可选，职责严格受限）────────────────────────────────────
CONSISTENCY_SYSTEM_PROMPT = (
    "你是答案忠实性检查器。你只做一件事：判断【答案】中的事实性陈述是否能由【资料】直接支持。\n"
    "【禁止】\n"
    "1. 禁止使用资料之外的任何知识，禁止补充或推测新事实，禁止提出新的结论或建议。\n"
    "2. 禁止评价答案是否流畅/是否有礼貌，禁止修改或翻译答案，禁止改写资料内容。\n"
    "3. 禁止输出超出 JSON 的任何文字（不要代码块，不要解释）。\n"
    "【输出】严格输出如下 JSON：\n"
    '{"supported": true 或 false, "unsupported_claims": ["资料不支持的陈述", ...]}\n'
    "规则：任何一条无资料依据的陈述存在时 supported 必须为 false；"
    "unsupported_claims 最多列 3 条，没有则为空数组。"
)

REVISION_SYSTEM_PROMPT = (
    "你是回答复核助手。任务：只依据给定的资料，重写一段更严谨、更保守的中文答案。\n"
    "【硬性规则】\n"
    "1. 不得引入资料之外的任何事实、数字、结论或医疗建议；不得用自己的知识补答。\n"
    "2. 资料没有直接支持的内容必须删除；宁可少答，不可多答。\n"
    "3. 不得把图谱中的“共享标签/相关”关系扩展为功效、主治、用药等医学结论。\n"
    "4. 每个来自资料的句子末尾必须标注来源 [citation: 编号, 页码]，编号即资料前的方括号编号。\n"
    "5. 若资料不足以支撑任何结论，直接回答：根据现有资料，我无法回答该问题。\n"
    "6. 只输出重写后的答案正文，不要说明你的修改过程。"
)

_JSON_BLOCK = re.compile(r"\{.*\}", re.DOTALL)
_CITATION_PATTERN = re.compile(r"\[citation:\s*(\d+)\s*(?:,\s*(\d+)\s*)?\]", re.IGNORECASE)
_SENTENCE_SPLIT = re.compile(r"[。！？!?\n]+")
_PUNCT = re.compile(r"[\s，,。！？；;：:、“”‘’（）()\[\]【】《》\-—…·\"'0-9]+")


def _as_float(value: object, default: float = 0.0) -> float:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def _as_int(value: object, default: int = 1) -> int:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def _is_refusal_answer(answer: str) -> bool:
    """是否已经是拒答/保守回答（既有文案，禁止另立拒答口径）。"""
    text = (answer or "").strip()
    return text.startswith(REFUSAL_PREFIX) or text.startswith(CONSERVATIVE_PREFIX)


def extract_citation_indices(answer: str) -> list[int]:
    """提取答案中的 [citation: 编号, 页码] 编号（保持出现顺序，不去重）。"""
    return [int(m.group(1)) for m in _CITATION_PATTERN.finditer(answer or "")]


def strip_citations(text: str) -> str:
    """去掉引用标记后的句子正文（用于句级支持度计算）。"""
    return _CITATION_PATTERN.sub("", text or "").strip()


def split_sentences(text: str) -> list[str]:
    """句子切分（保留引用标记，以便逐句判断"该句是否标注了来源"）。

    计算支持度时用 strip_citations 去掉标记后的正文，避免 [citation: n, p]
    这类噪声抬高/拉低字符覆盖率。
    """
    out: list[str] = []
    for part in _SENTENCE_SPLIT.split(text or ""):
        s = part.strip()
        if s:
            out.append(s)
    return out


def _char_bigrams(text: str) -> set[str]:
    """字符二元组集合（中文无空格分词，用二元组做确定性覆盖率计算）。"""
    cleaned = _PUNCT.sub("", text or "")
    if not cleaned:
        return set()
    if len(cleaned) < 2:
        return {cleaned}
    return {cleaned[i : i + 2] for i in range(len(cleaned) - 1)}


def sentence_support_ratio(sentence: str, evidence_texts: list[str]) -> float:
    """句子在给定证据文本上的字符二元组覆盖率（确定性，0~1）。"""
    grams = _char_bigrams(strip_citations(sentence))
    if not grams:
        return 0.0
    pool: set[str] = set()
    for t in evidence_texts:
        pool |= _char_bigrams(t)
    if not pool:
        return 0.0
    return len(grams & pool) / len(grams)


def is_content_sentence(sentence: str) -> bool:
    """是否视为"事实句"（过短、纯引用标记、纯引导句都不算）。"""
    stripped = strip_citations(sentence).strip()
    if len(stripped) < SENTENCE_MIN_LEN:
        return False
    # 纯引导句（"主要包括以下几点："）不是事实陈述
    if stripped.endswith("：") or stripped.endswith(":"):
        return False
    return True


def has_medical_claim(text: str) -> bool:
    """答案是否包含医学结论类表述（功效/主治/用量/禁忌……）。"""
    body = strip_citations(text)
    return any(k in body for k in MEDICAL_CLAIM_KEYWORDS)


def _is_weak_kg(evidence: dict) -> bool:
    """该 KG 证据是否为弱证据（related_to / 多跳 / 未知关系）。

    与 EvidenceGate.classify_evidence 口径一致：只有 1 跳的 contains/records
    业务事实算强事实，其余（弱关系、多跳）都不得单独支撑结论。
    """
    relation = str(evidence.get("kg_relation") or "")
    hop = _as_int(evidence.get("kg_hop"), 1)
    if relation in KG_STRONG_RELATIONS and hop <= 1:
        return False
    return True


def fallback_reflection_decision(reason: str, evidence_count: int = 0) -> "ReflectionDecision":
    """安全兜底：Reflection 自身异常 → accept + is_valid=False（不影响原问答）。"""
    return ReflectionDecision(
        decision=DECISION_ACCEPT,
        reason=f"reflection_fallback={reason}",
        confidence=0.0,
        is_valid=False,
        fallback_reason=reason,
        details={"evidence_count": evidence_count},
    )


def reflection_config(enabled: bool | None = None, llm_enabled: bool | None = None) -> dict:
    """Reflection 配置快照（写入 EvaluationRun.config_snapshot，实验可复现）。"""
    if enabled is None:
        enabled = bool(getattr(settings, "SELF_REFLECTION_ENABLED", True))
    if llm_enabled is None:
        llm_enabled = bool(getattr(settings, "SELF_REFLECTION_LLM_ENABLED", False))
    return {
        "reflection_version": REFLECTION_VERSION,
        "enabled": enabled,
        "llm_enabled": llm_enabled,
        "llm_timeout_seconds": getattr(
            settings, "SELF_REFLECTION_LLM_TIMEOUT_SECONDS", 20.0
        ),
        "thresholds": {
            "sentence_min_len": SENTENCE_MIN_LEN,
            "support_min_overlap": SUPPORT_MIN_OVERLAP,
        },
        "max_reflection_retry": 1,
        "max_revision": 1,
        "issue_action": dict(ISSUE_ACTION),
        "medical_claim_keywords": list(MEDICAL_CLAIM_KEYWORDS),
    }


def evidence_number(ev: dict, position: int) -> int:
    """证据在 Prompt / Citation 中的编号（BUG-015）。

    evidence 是**阈值过滤后**的列表，而答案里的 [citation:N] 与 Prompt 里的
    [N] 都基于**未过滤的 hits** 编号。因此编号必须取证据自带的 source_index
    （命中位序），只有在缺失时才回退到过滤后的位置，避免两套编号空间错位。
    """
    raw = ev.get("source_index")
    if isinstance(raw, bool):
        return position
    if isinstance(raw, int) and raw > 0:
        return raw
    if isinstance(raw, str) and raw.isdigit() and int(raw) > 0:
        return int(raw)
    return position


def build_consistency_messages(question: str, evidence: list[dict], answer: str) -> list[dict]:
    """构造 LLM 一致性检查的 messages（严格限权提示 + 编号资料 + 答案）。

    资料沿用 _build_user_prompt 的编号口径（[i] 编号），使 LLM 指出的编号可追溯。
    BUG-015：编号取 evidence_number（source_index），与答案中的引用编号一致。
    """
    blocks: list[str] = []
    for i, ev in enumerate(evidence, start=1):
        n = evidence_number(ev, i)
        blocks.append(f"[{n}] {ev.get('source_label') or ev.get('source_kind') or ''} "
                      f"{ev.get('source_name') or ''}\n{ev.get('evidence_text') or ev.get('content') or ''}")
    user = (
        f"问题：{question}\n\n资料（编号即来源标识）：\n"
        + ("\n\n".join(blocks) if blocks else "（无）")
        + f"\n\n答案：\n{answer}\n\n请严格按系统提示输出 JSON。"
    )
    return [
        {"role": "system", "content": CONSISTENCY_SYSTEM_PROMPT},
        {"role": "user", "content": user},
    ]


def parse_consistency_result(raw: str) -> dict:
    """解析 LLM 一致性检查结果（容错：取第一个 JSON 块）。

    Returns:
        {"supported": bool | None, "unsupported_claims": list[str], "error": str | None}
        - supported=None 表示解析失败/不可用（调用方按"不采纳"处理）
    """
    text = (raw or "").strip()
    if not text:
        return {"supported": None, "unsupported_claims": [], "error": "empty_response"}
    candidate = None
    try:
        candidate = json.loads(text)
    except json.JSONDecodeError:
        block = _JSON_BLOCK.search(text)
        if not block:
            return {"supported": None, "unsupported_claims": [], "error": "not_json"}
        try:
            candidate = json.loads(block.group(0))
        except json.JSONDecodeError as exc:
            return {"supported": None, "unsupported_claims": [], "error": f"bad_json: {exc}"}
    if not isinstance(candidate, dict):
        return {"supported": None, "unsupported_claims": [], "error": "not_object"}
    supported = candidate.get("supported")
    if not isinstance(supported, bool):
        supported = None
    claims = candidate.get("unsupported_claims")
    if not isinstance(claims, list):
        claims = []
    claims = [str(c)[:200] for c in claims[:3] if isinstance(c, (str, int, float))]
    return {"supported": supported, "unsupported_claims": claims, "error": None}


async def llm_consistency_check(llm, question: str, evidence: list[dict], answer: str,
                                timeout: float | None = None) -> dict:
    """可选 LLM 一致性检查（单次调用，不得重试；异常由调用方兜底）。

    职责严格受限：只判断答案是否被资料支持。超时/异常/解析失败均返回
    "不可用"（supported=None），调用方据此忽略 LLM 结论、回到确定性规则。
    """
    wait = _as_float(
        timeout,
        _as_float(getattr(settings, "SELF_REFLECTION_LLM_TIMEOUT_SECONDS", 20.0), 20.0),
    )
    messages = build_consistency_messages(question, evidence, answer)

    async def _call() -> str:
        return await llm.chat(messages)

    try:
        raw = await asyncio.wait_for(_call(), timeout=wait if wait > 0 else None)
        result = parse_consistency_result(raw)
        result["raw"] = (raw or "")[:200]
        return result
    except (asyncio.TimeoutError, TimeoutError):
        return {"supported": None, "unsupported_claims": [], "error": "timeout", "raw": ""}
    except Exception as exc:  # noqa: BLE001  LLM 不可用不得影响问答链路
        return {"supported": None, "unsupported_claims": [], "error": f"llm_error: {exc}", "raw": ""}


def build_revision_messages(question: str, evidence: list[dict], answer: str) -> list[dict]:
    """构造 revise 模式的 messages：基于同一份证据重写更保守的答案。

    不重新检索、不引入外部知识；evidence 是已经通过 Gate 的现有证据。
    """
    blocks: list[str] = []
    for i, ev in enumerate(evidence, start=1):
        # BUG-015：编号必须用 source_index，与原文答案的 [citation:N] 同空间
        n = evidence_number(ev, i)
        blocks.append(f"[{n}] {ev.get('source_label') or ev.get('source_kind') or ''} "
                      f"{ev.get('source_name') or ''}\n{ev.get('evidence_text') or ev.get('content') or ''}")
    user = (
        f"问题：{question}\n\n资料（编号即来源标识，引用时标注 [citation: 编号, 页码]）：\n"
        + ("\n\n".join(blocks) if blocks else "（无）")
        + f"\n\n待复核的草稿答案：\n{answer}\n\n"
        "请只依据上述资料重写一段更保守的答案（不得新增资料之外的信息）。"
    )
    return [
        {"role": "system", "content": REVISION_SYSTEM_PROMPT},
        {"role": "user", "content": user},
    ]


@dataclass(frozen=True)
class ReflectionDecision:
    """一次自反思决策（可解释、可序列化，与 API/Pydantic 字段一致）。"""

    decision: str
    reason: str = ""
    reflection_version: str = REFLECTION_VERSION
    # 0~1 的规则置信度（越高表示"答案与证据越一致"）
    confidence: float = 0.0
    # 发现的问题清单（受控词表，见 ISSUE_ACTION）
    issues: list[str] = field(default_factory=list)
    # retry 目标策略（复用 retrieval_strategies 注册表；None = 未重试）
    retry_strategy: str | None = None
    retried: bool = False
    # False = Reflection 自身走了兜底（保留原答案）
    is_valid: bool = True
    fallback_reason: str | None = None
    details: dict = field(default_factory=dict)
    # ── 需求 §6：retry 上限与计数（Gate retry 与 Reflection retry 分别记录）──
    gate_retry_count: int = 0
    reflection_retry_count: int = 0
    total_retry_count: int = 0
    # 是否已执行过一次 revise（最多一次）
    revised: bool = False
    # ── 需求 §7：直接引用 Gate 结论，不重复计算 ────────────────────────────
    gate_decision: str | None = None
    gate_version: str | None = None
    original_strategy: str | None = None
    retry_reason: str | None = None
    # 是否采用了 LLM Reflection 结论（默认方案为纯规则；llm_enabled 时才可能为 True）
    llm_used: bool = False

    def to_dict(self) -> dict:
        """序列化为 API 响应结构（字段与 chat.ReflectionDecisionOut 对齐）。"""
        return {
            "decision": self.decision,
            "reason": self.reason,
            "reflection_version": self.reflection_version,
            "confidence": self.confidence,
            "issues": list(self.issues),
            "retry_strategy": self.retry_strategy,
            "retried": self.retried,
            "is_valid": self.is_valid,
            "fallback_reason": self.fallback_reason,
            "gate_retry_count": self.gate_retry_count,
            "reflection_retry_count": self.reflection_retry_count,
            "total_retry_count": self.total_retry_count,
            "revised": self.revised,
            "gate_decision": self.gate_decision,
            "gate_version": self.gate_version,
            "original_strategy": self.original_strategy,
            "retry_reason": self.retry_reason,
            "llm_used": self.llm_used,
            "details": dict(self.details),
        }


class SelfReflection:
    """确定性自反思器：Answer + Evidence + GateDecision → ReflectionDecision。"""

    def __init__(
        self,
        enabled: bool | None = None,
        llm_enabled: bool | None = None,
    ) -> None:
        # None = 沿用全局配置
        self.enabled = (
            bool(getattr(settings, "SELF_REFLECTION_ENABLED", True))
            if enabled is None
            else bool(enabled)
        )
        # LLM Reflection 默认关闭：默认方案是纯规则（确定性、零额外延迟）
        self.llm_enabled = (
            bool(getattr(settings, "SELF_REFLECTION_LLM_ENABLED", False))
            if llm_enabled is None
            else bool(llm_enabled)
        )

    def evaluate(
        self,
        answer: str,
        hits: list[dict],
        *,
        query: str = "",
        query_analysis=None,
        gate_decision=None,
        allow_retry: bool = True,
        allow_revise: bool = True,
        strategy_name: str | None = None,
        llm_findings: dict | None = None,
        counts: dict | None = None,
    ) -> ReflectionDecision:
        """评估答案是否忠实使用了证据（纯规则 + 可选 LLM 结论）。

        Args:
            answer: 本轮生成的答案原文
            hits: 本次检索命中（内部转统一的 Evidence）
            query: 原始问题（仅用于 details/日志，不重新分析）
            query_analysis: QueryAnalysis（resource_types / entities / multi_source）
            gate_decision: GateDecision（直接引用其结论，不重复计算）
            allow_retry: False = Reflection retry 预算已用完
            allow_revise: False = revise 预算已用完
            strategy_name: 当前策略名（Reflection retry 选择目标策略用）
            llm_findings: 可选 LLM 一致性检查结论（parse_consistency_result 结构）
            counts: {"gate_retry_count", "reflection_retry_count", "revised"}

        Returns:
            ReflectionDecision（decision = accept / revise / retry）
        """
        try:
            return self._evaluate(
                answer or "",
                hits or [],
                query=query,
                analysis=query_analysis,
                gate_decision=gate_decision,
                allow_retry=allow_retry,
                allow_revise=allow_revise,
                strategy_name=strategy_name,
                llm_findings=llm_findings,
                counts=dict(counts or {}),
            )
        except Exception as exc:  # noqa: BLE001  Reflection 不得成为单点故障
            logger.warning(f"Self Reflection 评估失败，保留原答案: {exc}")
            return fallback_reflection_decision(f"reflection_exception: {exc}", len(hits or []))

    # ── 规则本体 ────────────────────────────────────────────────────────────

    def _evaluate(
        self,
        answer: str,
        hits: list[dict],
        *,
        query: str,
        analysis,
        gate_decision,
        allow_retry: bool,
        allow_revise: bool,
        strategy_name: str | None,
        llm_findings: dict | None,
        counts: dict,
    ) -> ReflectionDecision:
        evidence = build_evidence(hits)
        accepted = [e for e in evidence if classify_evidence(e)[1]]
        gate_name = getattr(gate_decision, "decision", None)
        gate_version = getattr(gate_decision, "gate_version", None)
        gate_retry = 1 if getattr(gate_decision, "retried", False) else 0
        counts.setdefault("gate_retry_count", gate_retry)
        counts.setdefault("reflection_retry_count", 0)
        counts.setdefault("revised", False)

        base_details = {
            "query": query[:120],
            "evidence_count": len(evidence),
            "accepted_evidence_count": len(accepted),
            "answer_len": len(answer or ""),
            "gate_decision": gate_name,
            "gate_version": gate_version,
        }

        # 1) Gate 已判不足 → 只允许拒答/保守回答，不得绕过 Gate 生成正常答案
        if gate_name is not None and gate_name != GATE_ACCEPT:
            if _is_refusal_answer(answer) or not answer.strip():
                return self._make(
                    DECISION_ACCEPT, "gate_refusal_accepted", [], 1.0,
                    {**base_details, "note": "Gate 已拒答，Reflection 接受该拒答（不绕过 Gate）"},
                    counts, allow_retry=False, allow_revise=False,
                    current_strategy=strategy_name, llm_used=False,
                    gate_decision=gate_decision,
                )
            return self._make(
                DECISION_RETRY, "answer_bypasses_gate", ["answer_bypasses_gate"], 0.2,
                base_details, counts, allow_retry=allow_retry, allow_revise=allow_revise,
                current_strategy=strategy_name, llm_used=False, gate_decision=gate_decision,
            )

        # 2) 拒答 / 保守回答：无需反思（既有拒答口径，不重复改写成别的文案）
        if _is_refusal_answer(answer) or not answer.strip():
            return self._make(
                DECISION_ACCEPT, "refusal_answer_accepted", [], 1.0,
                {**base_details, "note": "答案为拒答/保守回答，Reflection 不改写拒答文案"},
                counts, allow_retry=False, allow_revise=False,
                current_strategy=strategy_name, llm_used=False,
                gate_decision=gate_decision,
            )

        # ── 引用完整性 + 证据支持检查 ──────────────────────────────────────
        # BUG-015：evidence 是过滤后的列表，答案编号基于未过滤的 hits。
        # 因此必须按 source_index 建映射，不能再用 evidence[i-1] 取位置，
        # 否则会误判越界（多余 revise/retry）或取到错位证据做 Cited 判定。
        by_index: dict[int, dict] = {}
        for pos, ev in enumerate(evidence, start=1):
            by_index.setdefault(evidence_number(ev, pos), ev)

        indices = extract_citation_indices(answer)
        valid = [i for i in indices if i in by_index]
        invalid_count = len(indices) - len(valid)
        cited = [by_index[i] for i in valid]
        cited_accepted = [e for e in cited if classify_evidence(e)[1]]

        issues: list[str] = []
        # 3) 完全没有证据却给出了事实性回答 → 需要新证据
        if not evidence:
            issues.append("no_evidence_for_factual_answer")
        # 4) 有事实句但整篇没有任何引用标注 → 答案脱离了证据
        elif not indices and cited_free_claim_exists(answer):
            issues.append("no_citation_for_factual_answer")
        # 5) 引用编号越界：指向不存在的证据
        if invalid_count:
            issues.append("citation_out_of_range")
        # 6) 引用的证据全是弱证据（低分 / related_to / 多跳 KG）→ 不足以支撑答案
        if cited and not cited_accepted:
            issues.append("cited_evidence_weak")

        # 7) KG 弱关系检查：不得把 related_to / 多跳关系当作强医学事实
        kg_cited = [e for e in cited if e.get("source_kind") == SOURCE_KIND_KG]
        weak_kg_cited = [e for e in kg_cited if _is_weak_kg(e)]
        if kg_cited and len(weak_kg_cited) == len(kg_cited):
            if has_medical_claim(answer):
                issues.append("medical_claim_from_weak_relation")
            else:
                issues.append("kg_weak_relation_only")

        # 8) 句级支持度：找出"没有引用且不在证据文本上"的陈述
        #    （无证据时由 no_evidence_for_factual_answer 统一表达，不重复记问题）
        unsupported = self._unsupported_sentences(answer, evidence) if evidence else []
        if unsupported:
            issues.append("unsupported_sentences")

        # 9) 多来源覆盖：答案提到了某类资源，但该类资源没有任何参与本次回答的证据
        #    （覆盖按"被引证据 ∪ 已接受证据"计算：Gate 已接受的证据也算进来源，
        #     避免把"检索到了但答案没引用"正常场景误判成覆盖不足）
        used_evidence = cited + [e for e in accepted if e not in cited]
        coverage_issue = self._coverage_issue(answer, used_evidence, analysis)
        if coverage_issue:
            issues.append("multi_source_partial_coverage")

        # 10) 可选 LLM 一致性检查结论（只采纳"不支持"，不支持即 revise）
        llm_used = False
        if isinstance(llm_findings, dict) and llm_findings.get("supported") is False:
            issues.append("llm_unsupported_claims")
            llm_used = True

        issues = _dedupe(issues)
        supported_ratio = self._supported_ratio(answer, evidence)

        details = {
            **base_details,
            "citation_count": len(indices),
            "valid_citation_count": len(valid),
            "invalid_citation_count": invalid_count,
            "cited_evidence_count": len(cited),
            "cited_accepted_count": len(cited_accepted),
            "cited_source_kinds": sorted({str(e.get("source_kind")) for e in cited}),
            "cited_kg_relations": sorted({str(e.get("kg_relation")) for e in kg_cited}),
            "unsupported_sentence_count": len(unsupported),
            "unsupported_sentences": [
                u[:SAMPLE_MAX_LEN] for u in unsupported[:MAX_UNSUPPORTED_SAMPLES]
            ],
            "supported_sentence_ratio": round(supported_ratio, 4),
            "expected_resource_types": list(
                getattr(analysis, "resource_types", None) or []
            ),
            "covered_resource_types": sorted(
                {
                    str(e.get("resource_type"))
                    for e in used_evidence
                    if e.get("resource_type")
                }
            ),
            "coverage_issue": coverage_issue,
            "llm_findings": llm_findings if isinstance(llm_findings, dict) else None,
        }

        if not issues:
            return self._make(
                DECISION_ACCEPT, "supported_by_evidence", [], supported_ratio,
                details, counts, allow_retry=False, allow_revise=False,
                current_strategy=strategy_name, llm_used=llm_used,
                gate_decision=gate_decision,
            )

        # 安全优先：答案把弱关系扩展为医学结论时，必须先改写（revise），
        # 而不是再去检索更多证据试图"补强"这条弱关系（AGENTS.md §10 医疗安全）
        if "medical_claim_from_weak_relation" in issues:
            decision = DECISION_REVISE
        elif any(ISSUE_ACTION.get(i) == DECISION_RETRY for i in issues):
            decision = DECISION_RETRY
        else:
            decision = DECISION_REVISE
        reason = "supported" if decision == DECISION_ACCEPT else "+".join(issues)
        return self._make(
            decision, reason, issues, supported_ratio, details, counts,
            allow_retry=allow_retry, allow_revise=allow_revise,
            current_strategy=strategy_name, llm_used=llm_used,
            gate_decision=gate_decision,
        )

    # ── 子检查 ──────────────────────────────────────────────────────────────

    @staticmethod
    def _unsupported_sentences(answer: str, evidence: list[dict]) -> list[str]:
        """找出"没有引用标记、且不在证据文本上"的事实句。"""
        if not answer:
            return []
        texts = [e.get("evidence_text") or e.get("content") or "" for e in evidence]
        texts = [t for t in texts if t]
        if not texts:
            # 无证据可用：所有事实句都视为无依据
            return [s for s in split_sentences(answer) if is_content_sentence(s)]
        out: list[str] = []
        for sentence in split_sentences(answer):
            if not is_content_sentence(sentence):
                continue
            if _CITATION_PATTERN.search(sentence):
                continue  # 带引用标记的句子已标注来源（引用合法性另行检查）
            if sentence_support_ratio(sentence, texts) < SUPPORT_MIN_OVERLAP:
                out.append(sentence)
        return out

    @staticmethod
    def _supported_ratio(answer: str, evidence: list[dict]) -> float:
        """事实句的证据支持比例（0~1，无事实句时按 1.0 计）。"""
        sentences = [s for s in split_sentences(answer) if is_content_sentence(s)]
        if not sentences:
            return 1.0
        texts = [e.get("evidence_text") or e.get("content") or "" for e in evidence]
        texts = [t for t in texts if t]
        if not texts:
            return 0.0
        ok = sum(
            1
            for s in sentences
            if _CITATION_PATTERN.search(s)
            or sentence_support_ratio(s, texts) >= SUPPORT_MIN_OVERLAP
        )
        return ok / len(sentences)

    @staticmethod
    def _coverage_issue(answer: str, evidence_used: list[dict], analysis) -> str | None:
        """多来源问题的资源类型覆盖检查。

        仅当"答案里确实提到了未被覆盖资源类型的实体，却没有任何该类型的被引证据"
        时返回问题码——避免把正常的单类资源答案误判成覆盖不足（避免过度严格）。
        """
        expected = [t for t in (getattr(analysis, "resource_types", None) or []) if t]
        if len(expected) < 2:
            return None
        covered = {str(e.get("resource_type")) for e in evidence_used if e.get("resource_type")}
        uncovered = [t for t in expected if t not in covered]
        if not uncovered:
            return None
        entities = getattr(analysis, "entities", None) or []
        body = strip_citations(answer)
        for ent in entities:
            text = ent.get("text") if isinstance(ent, dict) else None
            etype = ent.get("type") if isinstance(ent, dict) else None
            if text and etype in uncovered and str(text) in body:
                return "multi_source_partial_coverage"
        return None

    # ── 构造决策 ────────────────────────────────────────────────────────────

    def _make(
        self,
        decision: str,
        reason: str,
        issues: list[str],
        supported_ratio: float,
        details: dict,
        counts: dict,
        *,
        allow_retry: bool,
        allow_revise: bool,
        current_strategy: str | None,
        llm_used: bool,
        gate_decision,
    ) -> ReflectionDecision:
        """统一构造 ReflectionDecision：预算用完时降级，保证不无限循环。"""
        retry_strategy = None
        original_strategy = None
        retry_reason = None
        if decision == DECISION_RETRY:
            target = select_retry_strategy(current_strategy)
            if target is None:
                # 没有可换的已注册策略 → 不能再重试
                decision = DECISION_REVISE if allow_revise else DECISION_ACCEPT
                reason = f"{reason};no_retry_strategy"
            elif not allow_retry:
                # Reflection retry 预算已用完（最多一次）→ 降级为 revise/accept
                decision = DECISION_REVISE if allow_revise else DECISION_ACCEPT
                reason = f"{reason};retry_exhausted"
            else:
                retry_strategy = target
                original_strategy = current_strategy
                retry_reason = reason
        if decision == DECISION_REVISE and not allow_revise:
            decision = DECISION_ACCEPT
            reason = f"{reason};revise_exhausted"

        gate_count = int(counts.get("gate_retry_count") or 0)
        reflect_count = int(counts.get("reflection_retry_count") or 0)
        confidence = _confidence(decision, issues, supported_ratio)
        return ReflectionDecision(
            decision=decision,
            reason=reason,
            confidence=confidence,
            issues=list(issues),
            retry_strategy=retry_strategy,
            retried=retry_strategy is not None,
            details=details,
            gate_retry_count=gate_count,
            reflection_retry_count=reflect_count,
            total_retry_count=gate_count + reflect_count,
            revised=bool(counts.get("revised")),
            gate_decision=getattr(gate_decision, "decision", None),
            gate_version=getattr(gate_decision, "gate_version", None),
            original_strategy=original_strategy,
            retry_reason=retry_reason,
            llm_used=llm_used,
        )


def cited_free_claim_exists(answer: str) -> bool:
    """答案是否存在未带引用标记的事实句（用于"整篇无引用"判断）。"""
    return any(is_content_sentence(s) for s in split_sentences(answer))


def _dedupe(issues: list[str]) -> list[str]:
    out: list[str] = []
    for i in issues:
        if i not in out:
            out.append(i)
    return out


def _confidence(decision: str, issues: list[str], supported_ratio: float) -> float:
    """规则置信度（0~1）：accept 看句级支持比例，revise/retry 按问题数衰减。"""
    if decision == DECISION_ACCEPT:
        return round(min(1.0, 0.6 + 0.4 * max(0.0, supported_ratio)), 3)
    penalty = 0.0
    for issue in issues:
        penalty += 0.3 if ISSUE_ACTION.get(issue) == DECISION_RETRY else 0.2
    return round(max(0.05, 0.9 - penalty), 3)


def with_counts(decision: ReflectionDecision, counts: dict) -> ReflectionDecision:
    """把最新的 retry / revise 计数合并进决策（供 RagService 执行后回填）。"""
    gate_count = int(counts.get("gate_retry_count", decision.gate_retry_count) or 0)
    reflect_count = int(
        counts.get("reflection_retry_count", decision.reflection_retry_count) or 0
    )
    return dataclasses.replace(
        decision,
        gate_retry_count=gate_count,
        reflection_retry_count=reflect_count,
        total_retry_count=gate_count + reflect_count,
        revised=bool(counts.get("revised", decision.revised)),
    )


def with_revision(decision: ReflectionDecision) -> ReflectionDecision:
    """标记「已执行一次 revise」（最多一次）。"""
    return dataclasses.replace(decision, revised=True)


def with_reflection_retry(
    decision: ReflectionDecision,
    *,
    original_strategy: str | None = None,
    retry_strategy: str | None = None,
    retry_reason: str | None = None,
) -> ReflectionDecision:
    """标记「已执行一次 Reflection retry」（最多一次）。"""
    return dataclasses.replace(
        decision,
        retried=True,
        original_strategy=original_strategy or decision.original_strategy,
        retry_strategy=retry_strategy or decision.retry_strategy,
        retry_reason=retry_reason or decision.retry_reason,
    )


def with_fallback(decision: ReflectionDecision, reason: str) -> ReflectionDecision:
    """链路内某步失败（如 revise/retry 的 LLM 调用失败）→ 标记不可用但保留答案。"""
    return dataclasses.replace(decision, is_valid=False, fallback_reason=reason)


_DEFAULT_REFLECTION = SelfReflection()


def evaluate_answer(
    answer: str,
    hits: list[dict],
    query: str = "",
    query_analysis=None,
    gate_decision=None,
    *,
    allow_retry: bool = True,
    allow_revise: bool = True,
    strategy_name: str | None = None,
) -> ReflectionDecision:
    """模块级便捷入口（使用默认 Reflection，enabled 取自全局配置）。"""
    return _DEFAULT_REFLECTION.evaluate(
        answer,
        hits,
        query=query,
        query_analysis=query_analysis,
        gate_decision=gate_decision,
        allow_retry=allow_retry,
        allow_revise=allow_revise,
        strategy_name=strategy_name,
    )
