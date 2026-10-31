"""通用导入：Schema 分析（字段类型、唯一性、候选主键、Schema 指纹）。

职责：把「若干行记录」归纳成结构化文件的数据画像，供关系发现与人工确认使用。
只读、无 IO、无 DB 依赖，便于单元测试。

设计取舍（明确记录，避免过度承诺）：
- 唯一性基于**前 sample_size 行**统计（默认 5000 行），行数超过该值时
  候选主键会被标记「未全量验证」并降低置信度，而不是假装已验证；
- 复合主键只支持「关联表的两个外键字段」这一种确定场景，
  不做通用的多列组合枚举（避免组合爆炸与误判）。
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from typing import Any

from src.application.tabular_parser import ParsedRow

# ── 字段推断类型 ────────────────────────────────────────────────────────────
TYPE_EMPTY = "empty"
TYPE_INTEGER = "integer"
TYPE_NUMBER = "number"
TYPE_BOOLEAN = "boolean"
TYPE_DATE = "date"
TYPE_STRING = "string"
TYPE_MIXED = "mixed"

DEFAULT_SAMPLE_SIZE = 5000
# 参与唯一性/覆盖率统计的不同取值上限（与 sample_size 保持一致，保证样本内可判定唯一）
MAX_DISTINCT_VALUES = DEFAULT_SAMPLE_SIZE

_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}")
# 主键命名特征：id / herb_id / 术语id / 编号 / code 等。
# 前置字符用「非 ASCII 字母」而非「分隔符」：中文列名（药材id、术语id）前面没有分隔符，
# 若要求分隔符这些列名就拿不到命名加分，会被同表内其他唯一列挤到后面（实测踩到）。
# 同时仍排除 valid / fluid 这类以 id 结尾的普通英文词。
_PK_NAME_RE = re.compile(r"(?:^|[^A-Za-z])(id|编号|编码|代码|code|key)$", re.IGNORECASE)


def _stable_key(value: Any) -> str:
    """把取值转成稳定的比较键（用于去重与跨表匹配）。"""
    if isinstance(value, bool):
        return f"b:{value}"
    if isinstance(value, (int, float)):
        return f"n:{value}"
    return f"s:{value}"


def _value_type(value: Any) -> str:
    if isinstance(value, bool):
        return TYPE_BOOLEAN
    if isinstance(value, int):
        return TYPE_INTEGER
    if isinstance(value, float):
        return TYPE_NUMBER
    if isinstance(value, str):
        return TYPE_DATE if _ISO_DATE_RE.match(value.strip()) else TYPE_STRING
    return TYPE_STRING


def _merge_types(types: set[str]) -> str:
    if not types:
        return TYPE_EMPTY
    if types == {TYPE_INTEGER}:
        return TYPE_INTEGER
    if types <= {TYPE_INTEGER, TYPE_NUMBER}:
        return TYPE_NUMBER
    if len(types) == 1:
        return next(iter(types))
    return TYPE_MIXED


def _is_null(value: Any) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


@dataclass
class FieldProfile:
    """单个字段的数据画像。"""

    name: str
    inferred_type: str = TYPE_EMPTY
    total: int = 0
    null_count: int = 0
    # 仅统计前 sample_size 行，用于判定唯一性与覆盖率
    sample_total: int = 0
    sample_null_count: int = 0
    distinct: set[str] = field(default_factory=set)
    samples: list[Any] = field(default_factory=list)
    _types: set[str] = field(default_factory=set)

    @property
    def null_ratio(self) -> float:
        return self.null_count / self.total if self.total else 1.0

    @property
    def unique_ratio(self) -> float:
        if not self.sample_total:
            return 0.0
        return len(self.distinct) / self.sample_total

    @property
    def non_null_count(self) -> int:
        return self.sample_total - self.sample_null_count

    @property
    def is_unique(self) -> bool:
        return (
            self.sample_total > 0
            and self.sample_null_count == 0
            and len(self.distinct) == self.sample_total
        )

    @property
    def is_unique_non_null(self) -> bool:
        """非空取值是否互不相同（**允许存在少量空值**）。

        用于「该列能否被外键引用」的判定。真实数据里 ID 列常混入个别脏空值，
        若要求整列无空值才可作为父表，几行脏数据就会让整张表的关联全部丢失——
        压力测试实测：D1 混入 3 行空主键后，D3 关联表识别直接失效、
        报出无上下文的错误。主键本身仍要求严格无空值（见 primary_key_candidates）。
        """
        return self.non_null_count > 0 and len(self.distinct) == self.non_null_count

    def record(self, value: Any, analyzed: bool) -> None:
        self.total += 1
        if _is_null(value):
            self.null_count += 1
        if analyzed:
            self.sample_total += 1
            if _is_null(value):
                self.sample_null_count += 1
            else:
                if len(self.distinct) < MAX_DISTINCT_VALUES:
                    self.distinct.add(_stable_key(value))
                self._types.add(_value_type(value))
                if len(self.samples) < 5:
                    self.samples.append(value)

    def finalize(self) -> None:
        self.inferred_type = _merge_types(self._types)


@dataclass
class PrimaryKeyCandidate:
    """候选主键及其置信度与理由（前端展示 + 人工确认依据）。"""

    fields: tuple[str, ...]
    uniqueness: float
    null_ratio: float
    confidence: float
    reason: str


@dataclass
class TableProfile:
    """一张表（一个文件）的数据画像。"""

    table_name: str
    file_name: str
    row_count: int
    analyzed_rows: int
    fields: list[FieldProfile]

    @property
    def sampled(self) -> bool:
        """是否只分析了部分行（行数超过样本上限）。"""
        return self.row_count > self.analyzed_rows

    @property
    def field_names(self) -> list[str]:
        return [f.name for f in self.fields]

    def field(self, name: str) -> FieldProfile | None:
        for item in self.fields:
            if item.name == name:
                return item
        return None

    @property
    def primary_key_candidates(self) -> list[PrimaryKeyCandidate]:
        candidates: list[PrimaryKeyCandidate] = []
        for profile in self.fields:
            if profile.sample_total == 0:
                continue
            # 主键必须**真正唯一且非空**。
            # 曾经只按置信度打分（0.7*唯一比例+命名加分），结果 4 行关联表里唯一比例
            # 0.75 的「术语id」被选成主键：该表于是被当成实体表，
            # 又命中外键发现里「两表同名主键」的噪声排除规则，导致外键少一条、
            # 多对多关联表识别不出来（浏览器端到端实测：junctions 为空、D3 被建了实体）。
            # 主键不是"像不像"的问题，有重复值就绝不可能是主键。
            if not profile.is_unique or profile.null_ratio > 0:
                continue
            uniqueness = profile.unique_ratio
            score = 0.7 * uniqueness
            if _PK_NAME_RE.search(profile.name):
                score += 0.2
            if self.sampled:
                # 只看了部分行，唯一性未被全量验证
                score -= 0.15
            confidence = max(0.0, min(1.0, score))
            reasons = [f"唯一比例 {uniqueness:.0%}"]
            if profile.null_ratio > 0:
                reasons.append(f"空值 {profile.null_ratio:.0%}")
            if _PK_NAME_RE.search(profile.name):
                reasons.append("命名符合主键特征")
            if self.sampled:
                reasons.append(f"仅分析前 {self.analyzed_rows} 行，未全量验证")
            candidates.append(
                PrimaryKeyCandidate(
                    fields=(profile.name,),
                    uniqueness=uniqueness,
                    null_ratio=profile.null_ratio,
                    confidence=round(confidence, 3),
                    reason="；".join(reasons),
                )
            )
        candidates.sort(key=lambda c: (-c.confidence, c.fields))
        return candidates[:3]


def schema_fingerprint(field_names: Iterable[str]) -> str:
    """计算 Schema 指纹：按字段名排序后取哈希。

    排序是为了「列顺序变化不影响模板复用」；大小写不敏感，避免列名大小写差异。
    """
    normalized = sorted({n.strip().lower() for n in field_names if n and n != "_extra_cells"})
    return hashlib.sha256("\x1f".join(normalized).encode("utf-8")).hexdigest()


def analyze_rows(
    rows: Iterable[ParsedRow],
    table_name: str,
    file_name: str = "",
    sample_size: int = DEFAULT_SAMPLE_SIZE,
) -> TableProfile:
    """流式统计行数据，产出 TableProfile。

    row_count 统计全部行（保证真实），唯一性/类型只基于前 sample_size 行。
    """
    profiles: dict[str, FieldProfile] = {}
    order: list[str] = []
    row_count = 0

    for row in rows:
        row_count += 1
        analyzed = row_count <= sample_size
        for name, value in row.data.items():
            profile = profiles.get(name)
            if profile is None:
                profile = FieldProfile(name=name)
                profiles[name] = profile
                order.append(name)
            profile.record(value, analyzed)

    for profile in profiles.values():
        profile.finalize()

    return TableProfile(
        table_name=table_name,
        file_name=file_name,
        row_count=row_count,
        analyzed_rows=min(row_count, sample_size),
        fields=[profiles[name] for name in order],
    )


def profile_from_rows(
    rows: Iterator[ParsedRow], **kwargs: Any
) -> TableProfile:
    """analyze_rows 的别名，供调用方按语义选择。"""
    return analyze_rows(rows, **kwargs)
