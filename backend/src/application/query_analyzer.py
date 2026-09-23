"""阶段十一：Query Analyzer（查询分析器）。

流水线位置（只"分析"，不"决策"）：

    Query
      ↓
    【QueryAnalyzer】  ← 本模块
      ↓
    QueryAnalysis（结构化）
      ↓
    现有 Baseline RAG（HyDE → BGE-M3 Dense+Sparse → RRF → Reranker → Gate）
      ↓
    Evidence / Citation → Answer

职责：
1. 判断 question_type（复用阶段九 QUESTION_TYPES 受控词表，不新建词表）
2. 提取 keywords / entities（轻量规则候选提取，不引入 NLP 依赖）
3. 判断问题可能涉及的知识资源类型 resource_types
4. 输出结构化 QueryAnalysis（阶段十二 Dynamic Router 可直接消费字段）
5. 任何异常 / 空 query / 校验失败 → fallback_analysis()，继续走 Baseline RAG

边界（阶段十一）：
- 不根据分析结果改变检索策略（不改 top_k / rerank / HyDE / Dense-Sparse 权重）
- 不实现 Dynamic Router、KG Retrieval、Evidence Gate、Self Reflection
- 不做医学判断、不声称某问题"医学上一定无法回答"
- is_unanswerable_candidate 只是候选标记，最终是否拒答由既有 Relevance Gate 决定

是否使用 LLM：
- 不使用。分析为确定性规则输出（analyzer_version=rule-v1），
  避免新增失败点/成本/mock 干扰，且保证评测可复现。
  项目已有 LLM client（infrastructure/llm.py），阶段十二如需接入，
  只需让 QueryAnalyzer 产出同样的 QueryAnalysis 结构并复用 validate_query_analysis()
  做结构化校验即可，RAG 侧无需改动。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from loguru import logger

from src.application.evidence import RESOURCE_TYPE_LABELS
from src.application.question_types import (
    DEFAULT_QUESTION_TYPE,
    QUESTION_TYPE_LABELS,
    QUESTION_TYPES,
)

# 分析器版本：规则版本变更时递增，便于评测结果对齐（阶段十二对比实验用）
ANALYZER_VERSION = "rule-v1"

# 知识资源类型：复用阶段十 Evidence 的资源类型词表（evidence.RESOURCE_TYPE_LABELS），
# 不新建第二套资源类型字符串
RESOURCE_TYPES: tuple[str, ...] = tuple(RESOURCE_TYPE_LABELS.keys())

# 资源类型判定阈值：信号强度（强信号 2 分 / 弱信号 1 分 / 实体命中 2 分）达到该值
# 才认为问题"确实涉及"该资源类型，避免仅出现一个普通关键词就判定 multi_source
RESOURCE_MIN_SCORE = 2

# ── 信号词表（仅作"问题属于哪类知识资源"的文本线索，不含任何医学判断）──────
_STRONG_SIGNALS: dict[str, tuple[str, ...]] = {
    "herb": (
        "性味", "归经", "功效", "主治", "中药", "药材", "本草", "炮制", "用量",
        "用法用量", "配伍禁忌", "禁忌", "四气五味", "药性", "煎服", "毒性", "饮片",
    ),
    "prescription": (
        "方剂", "处方", "组成", "功用", "君臣佐使", "加减", "方解", "汤头", "配伍", "用法",
    ),
    "theory": (
        "理论", "学说", "阴阳", "五行", "藏象", "气血津液", "经络", "病因病机", "病机",
        "治则", "治法", "辨证", "八纲", "卫气营血", "三焦", "六淫", "七情", "升降浮沉",
        "相生相克", "望闻问切", "四诊", "正气", "邪气", "扶正祛邪", "同病异治",
        "异病同治", "标本",
    ),
    "literature": (
        "文献", "古籍", "典籍", "出处", "原文", "记载", "成书", "作者", "版本", "引文",
        "校注", "卷", "篇",
    ),
}

_WEAK_SIGNALS: dict[str, tuple[str, ...]] = {
    "herb": ("单味", "药对", "采收", "草药", "主治"),
    "prescription": ("主治", "煎法", "服法", "汤剂"),
    "theory": ("概念", "原理", "机制", "关系", "基础"),
    "literature": ("经典", "著作", "年代"),
}

# 明显超出中医知识库范围的话题线索（仅用于标记 candidate，不据此拒答）
_OUT_OF_DOMAIN_SIGNALS: tuple[str, ...] = (
    "西医", "现代医学", "股票", "基金", "房价", "天气", "编程", "代码", "足球", "篮球",
    "电影", "游戏", "明星", "娱乐", "汇率", "外卖", "快递", "旅游", "星座", "彩票",
    "手机", "电脑", "汽车", "保险", "贷款",
)

# ── 实体候选词表（轻量规则：仅按"名称"匹配，不解释医学含义）────────────────
_KNOWN_BOOKS: tuple[str, ...] = (
    "黄帝内经", "伤寒论", "金匮要略", "神农本草经", "本草纲目", "温病条辨", "难经",
    "千金方", "千金翼方", "脾胃论", "景岳全书", "医宗金鉴", "诸病源候论", "脉经",
    "针灸甲乙经", "太平惠民和剂局方",
)

_KNOWN_HERBS: tuple[str, ...] = (
    "金银花", "连翘", "薄荷", "荆芥", "防风", "桔梗", "甘草", "桂枝", "麻黄", "杏仁",
    "石膏", "知母", "黄芩", "黄连", "黄柏", "大黄", "芒硝", "柴胡", "当归", "白芍",
    "川芎", "地黄", "人参", "党参", "黄芪", "白术", "茯苓", "半夏", "陈皮", "麦冬",
    "天冬", "枸杞", "菊花", "桑叶", "葛根", "升麻", "蝉蜕", "牛蒡子", "淡竹叶", "芦根",
    "天花粉", "栀子", "夏枯草", "决明子", "蔓荆子", "紫苏", "生姜", "大枣", "附子",
    "干姜", "肉桂", "吴茱萸", "木香", "香附", "乌药", "沉香", "枳实", "青皮", "山楂",
    "神曲", "麦芽", "莱菔子", "鸡内金",
)

# 说明：刻意不收录"归经 / 四气五味 / 虚实 / 寒热 / 表里"等既是中药属性又是基础概念
# 的短词，避免"金银花的性味归经"被误判为 multi_source（中药 + 理论）。
_KNOWN_THEORY_TERMS: tuple[str, ...] = (
    "阴阳", "五行", "藏象", "气血津液", "经络", "病因病机", "病机", "治则", "治法",
    "辨证", "八纲", "卫气营血", "三焦", "六淫", "七情", "君臣佐使", "升降浮沉",
    "相生相克", "相乘相侮", "阴平阳秘", "扶正祛邪", "同病异治", "异病同治",
    "望闻问切", "四诊", "精气神", "命门", "三因制宜",
)

# 书名号《》/【】内文本 → 文献实体
_BOOK_PATTERN = re.compile(r"[《【]([^》】]{1,40})[》】]")
# 方剂名：2~6 字 + 剂型后缀
_PRESCRIPTION_PATTERN = re.compile(r"[\u4e00-\u9fa5]{1,5}(?:汤|散|丸|饮|膏|丹|煎|方|剂)")
# 方剂后缀的通用词（不是具体方剂，排除误报）
_PRESCRIPTION_STOPWORDS = frozenset({"地方", "方面", "方式", "方向", "方法", "官方", "配方", "秘方"})
# 中药名兜底：2~4 字 + 常见药用部位/药名后缀
_HERB_SUFFIX_PATTERN = re.compile(
    r"[\u4e00-\u9fa5]{1,3}(?:花|草|叶|根|皮|子|仁|藤|果|参|芪|术|苓|连|翘|菊|荷|芥|胡|香|归|芎|芍|地|黄|麻|桂|枝|壳|砂|枣|姜|夏|冬|柏|杷|蒌|薤)"
)

_STOPWORDS: tuple[str, ...] = (
    "请问", "什么", "哪些", "怎么", "如何", "为什么", "多少", "分别", "及其", "还有",
    "对于", "关于", "以及", "一下", "可以", "能否", "是否", "的", "了", "是", "在",
    "和", "与", "有", "吗", "呢", "请", "为", "之", "其", "该", "这", "那", "我", "你", "它",
)

_MAX_KEYWORDS = 12


@dataclass(frozen=True)
class QueryAnalysis:
    """结构化查询分析结果（阶段十二 Dynamic Router 可直接消费）。"""

    query: str
    # 受控词表内的问题类型（QUESTION_TYPES 之一）
    question_type: str
    question_type_label: str
    # 可能涉及的知识资源类型（herb / prescription / theory / literature 子集）
    resource_types: list[str] = field(default_factory=list)
    # 是否明显需要多类资源共同回答
    is_multi_source: bool = False
    # 是否"可能无法由知识库可靠回答"的候选（仅标记，不据此拒答）
    is_unanswerable_candidate: bool = False
    # 查询关键词（实体优先，其余为去停用词后的候选词）
    keywords: list[str] = field(default_factory=list)
    # 候选实体：[{"text": "金银花", "type": "herb"}, ...]
    entities: list[dict] = field(default_factory=list)
    # 查询特征（长度/信号强度/实体数，供调试与阶段十二实验分析）
    features: dict = field(default_factory=dict)
    analyzer_version: str = ANALYZER_VERSION
    # 分析结果是否有效；False 表示走了 fallback（仍继续 Baseline RAG）
    is_valid: bool = True
    fallback_reason: str | None = None

    def to_dict(self) -> dict:
        """序列化为 API 响应结构（字段与 chat.QueryAnalysisOut 对齐）。"""
        return {
            "query": self.query,
            "question_type": self.question_type,
            "question_type_label": self.question_type_label,
            "resource_types": list(self.resource_types),
            "is_multi_source": self.is_multi_source,
            "is_unanswerable_candidate": self.is_unanswerable_candidate,
            "keywords": list(self.keywords),
            "entities": [dict(e) for e in self.entities],
            "features": dict(self.features),
            "analyzer_version": self.analyzer_version,
            "is_valid": self.is_valid,
            "fallback_reason": self.fallback_reason,
        }


def fallback_analysis(query: str, reason: str) -> QueryAnalysis:
    """安全兜底分析（规范 §13）：不抛异常、不阻断 RAG。

    question_type=general / resource_types=[] / is_multi_source=False /
    is_unanswerable_candidate=False，is_valid=False + fallback_reason 便于排查。
    """
    return QueryAnalysis(
        query=query,
        question_type=DEFAULT_QUESTION_TYPE,
        question_type_label=QUESTION_TYPE_LABELS[DEFAULT_QUESTION_TYPE],
        resource_types=[],
        is_multi_source=False,
        is_unanswerable_candidate=False,
        keywords=[],
        entities=[],
        features={"fallback": True},
        analyzer_version=ANALYZER_VERSION,
        is_valid=False,
        fallback_reason=reason,
    )


def validate_query_analysis(raw: dict) -> QueryAnalysis | None:
    """校验结构化分析结果（供未来 LLM 分析路径复用）。

    校验失败返回 None（调用方应转 fallback_analysis），绝不抛异常。
    """
    if not isinstance(raw, dict):
        return None
    question_type = raw.get("question_type")
    if question_type not in QUESTION_TYPES:
        return None
    resource_types = raw.get("resource_types") or []
    if not isinstance(resource_types, list) or not all(
        isinstance(t, str) and t in RESOURCE_TYPES for t in resource_types
    ):
        return None
    entities = raw.get("entities") or []
    if not isinstance(entities, list):
        return None
    for e in entities:
        if not isinstance(e, dict) or "text" not in e:
            return None
    keywords = raw.get("keywords") or []
    if not isinstance(keywords, list) or not all(isinstance(k, str) for k in keywords):
        return None
    version = raw.get("analyzer_version")
    if not isinstance(version, str) or not version.strip():
        return None
    return QueryAnalysis(
        query=str(raw.get("query", "")),
        question_type=str(question_type),
        question_type_label=QUESTION_TYPE_LABELS.get(
            str(question_type), str(question_type)
        ),
        resource_types=list(resource_types),
        is_multi_source=bool(raw.get("is_multi_source", False)),
        is_unanswerable_candidate=bool(raw.get("is_unanswerable_candidate", False)),
        keywords=list(keywords),
        entities=[dict(e) for e in entities],
        features=dict(raw.get("features") or {}),
        analyzer_version=version,
        is_valid=bool(raw.get("is_valid", True)),
        fallback_reason=raw.get("fallback_reason"),
    )


def analysis_from_dict(raw: dict, query: str = "") -> QueryAnalysis:
    """从结构化 dict 还原 QueryAnalysis；校验失败转 fallback。"""
    analysis = validate_query_analysis(raw)
    if analysis is not None:
        return analysis
    return fallback_analysis(
        query=str(raw.get("query", query)) if isinstance(raw, dict) else query,
        reason="invalid_analysis_output",
    )


# ── 规则分析内部实现 ────────────────────────────────────────────────────────


def _all_known_terms() -> tuple[str, ...]:
    """实体词表 + 信号词表（按长度降序，避免短词先匹配导致长词被切碎）。"""
    terms = set(_KNOWN_BOOKS) | set(_KNOWN_HERBS) | set(_KNOWN_THEORY_TERMS)
    for signals in _STRONG_SIGNALS.values():
        terms.update(signals)
    for signals in _WEAK_SIGNALS.values():
        terms.update(signals)
    return tuple(sorted(terms, key=len, reverse=True))


_KNOWN_TERMS: tuple[str, ...] = _all_known_terms()


def _extract_entities(query: str) -> list[dict]:
    """候选实体提取（轻量规则）：书名 / 方剂 / 中药 / 理论术语。

    同一文本可能被多个规则命中，按 文献 > 方剂 > 理论 > 中药 去重保留最具体类型。
    """
    entities: list[dict] = []
    seen: set[str] = set()

    def add(text: str, etype: str) -> None:
        text = text.strip()
        if not text or text in seen:
            return
        seen.add(text)
        entities.append({"text": text, "type": etype})

    # 1) 书名号内文本 + 已知典籍名
    for m in _BOOK_PATTERN.finditer(query):
        add(m.group(1), "literature")
    for book in _KNOWN_BOOKS:
        if book in query:
            add(book, "literature")

    # 2) 方剂：剂型后缀（排除通用词）
    for m in _PRESCRIPTION_PATTERN.finditer(query):
        text = m.group(0)
        if text in _PRESCRIPTION_STOPWORDS or len(text) < 2:
            continue
        add(text, "prescription")

    # 3) 理论术语
    for term in _KNOWN_THEORY_TERMS:
        if term in query:
            add(term, "theory")

    # 4) 中药：已知药名（主）→ 药名后缀兜底（辅，见下方过滤）
    for herb in _KNOWN_HERBS:
        if herb in query:
            add(herb, "herb")
    suffix_candidates = [m.group(0) for m in _HERB_SUFFIX_PATTERN.finditer(query)]
    for text in suffix_candidates:
        add(text, "herb")

    # 后缀兜底误报过滤（"银翘" ⊂ "银翘散"、"的性味归" 过长等）：
    # 仅保留 ≤3 字且不被其他已识别实体包含的候选
    if suffix_candidates:
        others = [e["text"] for e in entities if e["text"] not in suffix_candidates]
        kept = {
            t
            for t in suffix_candidates
            if len(t) <= 3 and not any(t != o and t in o for o in others)
        }
        entities = [
            e for e in entities
            if e["type"] != "herb" or e["text"] not in suffix_candidates or e["text"] in kept
        ]

    # 稳定顺序：保持原始 query 中出现顺序
    entities.sort(key=lambda e: query.find(e["text"]))
    return entities


def _signal_scores(query: str, entities: list[dict]) -> dict[str, int]:
    """各资源类型的信号强度：强信号 2 分、弱信号 1 分、实体命中 2 分。"""
    scores: dict[str, int] = {t: 0 for t in RESOURCE_TYPES}
    for rtype in RESOURCE_TYPES:
        for term in _STRONG_SIGNALS.get(rtype, ()):
            if term in query:
                scores[rtype] += 2
        for term in _WEAK_SIGNALS.get(rtype, ()):
            if term in query:
                scores[rtype] += 1
    for e in entities:
        rtype = e.get("type")
        if rtype in scores:
            scores[rtype] += 2
    return scores


def _extract_keywords(query: str, entities: list[dict]) -> list[str]:
    """关键词提取：已识别实体 + 已知词表命中 + 去停用词后的剩余片段。"""
    keywords: list[str] = []
    seen: set[str] = set()

    def add(word: str) -> None:
        word = word.strip()
        if not word or word in seen or word in _STOPWORDS:
            return
        seen.add(word)
        keywords.append(word)

    for e in entities:
        add(e["text"])

    # 先移除已识别实体（含正则提取的方剂名等），再移除已知词，剩下的才是残余候选词
    text = query
    for e in entities:
        text = text.replace(e["text"], " ")
    for term in _KNOWN_TERMS:
        if term in text:
            add(term)
            text = text.replace(term, " ")

    # 剩余片段按标点/空白切分，再用停用词切分，保留长度 ≥ 2 的片段
    stop_pattern = "|".join(sorted(_STOPWORDS, key=len, reverse=True))
    for part in re.split(r"[^\u4e00-\u9fa5A-Za-z0-9]+", text):
        if not part:
            continue
        for seg in re.split(stop_pattern, part):
            if len(seg) >= 2:
                add(seg)

    return keywords[:_MAX_KEYWORDS]


def _decide(
    query: str,
    scores: dict[str, int],
    entities: list[dict],
    keywords: list[str],
) -> QueryAnalysis:
    """按信号强度判定问题类型 / 资源类型 / multi_source / unanswerable 候选。"""
    active = [t for t in RESOURCE_TYPES if scores.get(t, 0) >= RESOURCE_MIN_SCORE]
    out_of_domain = [s for s in _OUT_OF_DOMAIN_SIGNALS if s in query]

    if len(active) >= 2:
        question_type = "multi_source"
    elif len(active) == 1:
        question_type = active[0]
    elif out_of_domain:
        # 无任何中医资源线索 + 明显非知识库范围 → 标记为 unanswerable 候选
        question_type = "unanswerable"
    else:
        question_type = DEFAULT_QUESTION_TYPE

    features = {
        "query_length": len(query.strip()),
        "has_book_marker": bool(_BOOK_PATTERN.search(query)),
        "signal_scores": {t: scores.get(t, 0) for t in RESOURCE_TYPES},
        "entity_count": len(entities),
        "out_of_domain_hits": out_of_domain,
    }

    return QueryAnalysis(
        query=query,
        question_type=question_type,
        question_type_label=QUESTION_TYPE_LABELS.get(question_type, question_type),
        resource_types=active,
        is_multi_source=len(active) >= 2,
        # 仅标记：最终是否拒答由既有 Relevance Gate 决定（阶段十一不新增 Gate）
        is_unanswerable_candidate=bool(out_of_domain),
        keywords=keywords,
        entities=entities,
        features=features,
        analyzer_version=ANALYZER_VERSION,
        is_valid=True,
        fallback_reason=None,
    )


class QueryAnalyzer:
    """查询分析器：Query → QueryAnalysis（纯本地规则，无 IO、无 LLM）。"""

    def analyze(self, query: str) -> QueryAnalysis:
        """分析用户问题。

        任何异常都转 fallback_analysis()，保证调用方（RAG）永不因分析失败中断。
        """
        try:
            if not isinstance(query, str) or not query.strip():
                return fallback_analysis(query if isinstance(query, str) else "", "empty_query")

            normalized = query.strip()
            entities = _extract_entities(normalized)
            scores = _signal_scores(normalized, entities)
            keywords = _extract_keywords(normalized, entities)
            analysis = _decide(normalized, scores, entities, keywords)
            logger.info(
                f"Query 分析 type={analysis.question_type} "
                f"resources={analysis.resource_types} "
                f"multi_source={analysis.is_multi_source} "
                f"unanswerable_candidate={analysis.is_unanswerable_candidate}"
            )
            return analysis
        except Exception as exc:  # 兜底：分析器绝不能成为单点故障
            logger.warning(f"Query 分析失败，回落 general: {exc}")
            return fallback_analysis(
                query if isinstance(query, str) else "", f"analyzer_exception: {exc}"
            )


_DEFAULT_ANALYZER = QueryAnalyzer()


def analyze_query(query: str) -> QueryAnalysis:
    """模块级便捷入口（使用默认分析器）。"""
    return _DEFAULT_ANALYZER.analyze(query)
