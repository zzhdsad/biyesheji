"""关联结构化数据的纯函数层（第四阶段：多个文件之间的关系）。

职责边界：
- 输入是**第三阶段已经解析好的**文件结构与记录（字段清单 + 记录 payload），
  本模块不读文件、不重新解析、不写数据库，因此服务层可以在内存里反复调用
  （推测关系 → 校验 → 生成关联记录），失败也碰不到已有结果。
- 输出三份东西：主键/外键候选与推测关系、校验报告、关联知识记录。

三条硬约束（对应任务要求）：
1. **推测不等于确认**：suggest_relations 给出的每条关系都带 ``confirmed=False``，
   只有用户显式确认（或自己选了字段）后才参与正式生成。判定证据以「值能对上」为主，
   字段名相似只作为加分与说明，绝不单凭名字认定关联。
2. **不改原始数据**：比较走只读的规范化键（``match_key``），payload 原值原样保留；
   未匹配、空键、重复主键的行同样生成记录并带提示，不为了关联成功率丢行或改值。
3. 不调用 Embedding、不写 Milvus：产出只是文本与结构，向量化统一留到第六阶段。
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Any

from src.core.config import settings
from src.utils.structured_data import MAX_KNOWLEDGE_TEXT_CHARS, value_text

# 关联**文本**里一个关系最多展开多少个值（一对多 / 多对多的可读性上限）。
# 只限制 knowledge_text 的展开数量；parts / sources 溯源始终完整保留全部匹配，
# 超出部分在文本与提示里标注「展开 X 条、省略 Y 条，完整列表见详情」。
MAX_LINK_VALUES = 20
# 推测关系的最低值覆盖率：低于此值说明两边根本对不上，不端给用户
MIN_KEY_COVERAGE = 0.6
# 关系评分是 0–100 的规则打分（不是概率）：≥90 自动预填、70–89 供用户选择、<70 仅参考
HIGH_SCORE = 90
MEDIUM_SCORE = 70
# 对侧（主键侧）唯一率低于该值说明不能直接当主键，评分压到中置信度，绝不自动预填
MIN_PK_UNIQUE_RATIO = 0.95
# 参与比对的去重值太少时，值匹配更像巧合，评分压到低置信度
MIN_EVIDENCE_VALUES = 10
# 字段值平均长度超过该值视为长文本，不自动预填
MAX_KEY_VALUE_LENGTH = 30
# 字段名带这些特征更像标识符（只加分，不作为唯一依据）
_KEY_HINTS = ("编号", "代码", "主键", "外键", "代号", "编码", "ID", "Id", "id", "号")
# 字段名相似度里不携带语义的通用词元（避免 CPM_ID / CHP_ID 因共享 ID 被当成相似）
_NAME_STOP_TOKENS = {"id", "no", "num", "code", "group", "term", "type", "value"}
_NAME_STOP_SUFFIXES = {"id", "no", "num", "code"}
# 字段名同义词组：只作为加分与说明，绝不单凭名字认定关联
_NAME_SYNONYM_GROUPS = (
    ("编号", "编码", "代码", "代号", "序号"),
    ("名称", "名字", "姓名", "药名", "品名", "术语", "词条"),
    ("日期", "时间"),
    ("类型", "类别", "分类", "种类"),
)
# 实体展示字段优先取「名字」类（用于「学生：张三」这样的可读文本）
_LABEL_HINTS = ("姓名", "名称", "名字", "标题", "题名", "药名", "品名", "名", "title", "name")

RELATION_ONE_TO_ONE = "one_to_one"
RELATION_ONE_TO_MANY = "one_to_many"
RELATION_MANY_TO_ONE = "many_to_one"
RELATION_MANY_TO_MANY = "many_to_many"
RELATION_KINDS = (
    RELATION_ONE_TO_ONE,
    RELATION_ONE_TO_MANY,
    RELATION_MANY_TO_ONE,
    RELATION_MANY_TO_MANY,
)
RELATION_KIND_LABELS = {
    RELATION_ONE_TO_ONE: "一对一",
    RELATION_ONE_TO_MANY: "一对多",
    RELATION_MANY_TO_ONE: "多对一",
    RELATION_MANY_TO_MANY: "多对多",
}
# 声明某种关联类型 = 对两侧唯一性的承诺：哪一侧必须值唯一，另一侧的重复就是正常的
LEFT_UNIQUE_KINDS = (RELATION_ONE_TO_ONE, RELATION_ONE_TO_MANY)
RIGHT_UNIQUE_KINDS = (RELATION_ONE_TO_ONE, RELATION_MANY_TO_ONE)


@dataclass
class GroupFileSchema:
    """文件组里的一个文件：结构与记录（都来自第三阶段的解析结果）。"""

    doc_id: str
    file_name: str
    alias: str = ""
    order_no: int = 0
    fields: list[dict[str, Any]] = field(default_factory=list)
    # 每条记录：{"record_id","row_no","sheet_name","json_path","payload"}
    records: list[dict[str, Any]] = field(default_factory=list)
    # 该文件在数据库里的真实记录数。流式调用方只加载部分记录（或不加载锚点记录）时，
    # 用它给出准确行数，避免统计里出现「只数了已加载的分片」。
    total_rows: int = 0

    @property
    def record_count(self) -> int:
        return self.total_rows or len(self.records)

    @property
    def display_name(self) -> str:
        """界面与关联文本用的名字：优先用户起的别名，其次文件名。"""
        return (self.alias or "").strip() or self.file_name

    def field_names(self) -> list[str]:
        return [str(f.get("name", "")) for f in self.fields]

    def field_type(self, name: str) -> str:
        for spec in self.fields:
            if spec.get("name") == name:
                return str(spec.get("type", ""))
        return ""


def match_key(value: Any) -> str:
    """只读的比较键：文本规范化、压缩空白、忽略大小写；原始值不动。

    「1001」与 1001、" C001 " 与 "C001" 视为同一个键——这是为了尽量把关系对上，
    而规范化结果绝不写回 payload（用户看到与追溯的仍是原值）。
    """
    text = value_text(value).strip()
    if not text:
        return ""
    return " ".join(text.split()).casefold()


def _keys_of(schema: GroupFileSchema, name: str) -> list[str]:
    return [match_key(rec.get("payload", {}).get(name)) for rec in schema.records]


def _is_unique(schema: GroupFileSchema, name: str, keys: list[str] | None = None) -> bool:
    """该字段能否当主键：每行都有值且互不相同。"""
    values = keys if keys is not None else _keys_of(schema, name)
    rows = schema.record_count
    non_empty = [k for k in values if k]
    return len(non_empty) == rows and len(set(non_empty)) == rows


def _is_key_like(name: str) -> bool:
    return any(hint in name for hint in _KEY_HINTS)


def _is_label_like(name: str) -> bool:
    return any(hint in name for hint in _LABEL_HINTS)


def _key_candidate_item(
    schema: GroupFileSchema, spec: dict[str, Any], profile: dict[str, Any]
) -> dict[str, Any]:
    """由字段画像算出一条主键候选（画像可来自整份记录，也可来自分批累积）。"""
    name = str(spec.get("name", ""))
    total = schema.record_count or profile["rows"]
    non_empty = profile["non_empty"]
    distinct = profile["distinct"]
    unique = profile["unique"]
    ratio = distinct / non_empty if non_empty else 0.0
    score = 0.0
    reasons: list[str] = []
    if unique:
        score += 0.55
        reasons.append("每行都有值且互不相同")
    elif ratio >= 0.9:
        score += 0.2
        reasons.append(f"值去重率 {ratio:.0%}，接近唯一")
    if _is_key_like(name):
        score += 0.3
        reasons.append("字段名带标识符特征")
    if str(spec.get("type", "")) in ("integer", "string"):
        score += 0.05
    return {
        "field": name,
        "type": str(spec.get("type", "")),
        "unique": unique,
        "distinct": distinct,
        "record_count": total,
        "null_count": total - non_empty,
        "score": round(min(score, 1.0), 3),
        "reason": "；".join(reasons) or "无明显主键特征",
    }


def _finish_key_candidates(items: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    items.sort(key=lambda item: (-item["score"], item["field"]))
    return items[:limit]


def key_candidates(schema: GroupFileSchema, limit: int = 6) -> list[dict[str, Any]]:
    """主键候选：按「值唯一 + 无空值 + 名称像标识符」打分，全部只是候选而非结论。"""
    if schema.record_count == 0:
        return []
    out: list[dict[str, Any]] = []
    for spec in schema.fields:
        name = str(spec.get("name", ""))
        if not name:
            continue
        profile = _field_profile(schema, name)
        if not profile["non_empty"]:
            continue
        out.append(_key_candidate_item(schema, spec, profile))
    return _finish_key_candidates(out, limit)


def key_candidates_from_probe(
    schema: GroupFileSchema, probe: "FieldProfileProbe", limit: int = 6
) -> list[dict[str, Any]]:
    """分批累积版本的 key_candidates：调用方不必把整份记录留在内存里。"""
    if schema.record_count == 0:
        return []
    out: list[dict[str, Any]] = []
    for spec in schema.fields:
        name = str(spec.get("name", ""))
        if not name:
            continue
        profile = probe.profile(name)
        if not profile["non_empty"]:
            continue
        out.append(_key_candidate_item(schema, spec, profile))
    return _finish_key_candidates(out, limit)


def relation_id(from_doc: str, from_field: str, to_doc: str, to_field: str) -> str:
    return f"{from_doc}:{from_field}->{to_doc}:{to_field}"


def _kind_of(left_unique: bool, right_unique: bool) -> str:
    """关联类型由两侧的唯一性决定，不靠字段名猜。"""
    if left_unique and right_unique:
        return RELATION_ONE_TO_ONE
    if left_unique:
        return RELATION_ONE_TO_MANY
    if right_unique:
        return RELATION_MANY_TO_ONE
    return RELATION_MANY_TO_MANY


def _normalize_name(name: str) -> str:
    return "".join(ch for ch in name.strip().casefold() if ch.isalnum())


def _name_tokens(name: str) -> set[str]:
    tokens = {
        token.casefold()
        for token in re.split(r"[^0-9a-zA-Z\u4e00-\u9fff]+", name.strip())
        if token
    }
    return {token for token in tokens if token not in _NAME_STOP_TOKENS}


def _name_hits(name: str, tokens: set[str], hints: tuple[str, ...]) -> set[str]:
    hits: set[str] = set()
    for hint in hints:
        if hint.isascii():
            if hint.casefold() in tokens:
                hits.add(hint)
        elif hint in name:
            hits.add(hint)
    return hits


def _name_similarity(left_name: str, right_name: str) -> tuple[float, list[str]]:
    """字段名相似度（0–1）：只是评分中的加分项，不单独决定关联。"""
    left, right = _normalize_name(left_name), _normalize_name(right_name)
    if not left or not right:
        return 0.0, []
    if left == right:
        return 1.0, ["字段名完全一致"]
    left_tokens, right_tokens = _name_tokens(left_name), _name_tokens(right_name)
    common = left_tokens & right_tokens
    if common:
        jaccard = len(common) / len(left_tokens | right_tokens)
        return round(0.55 + 0.35 * jaccard, 2), [f"字段名共享词元：{'、'.join(sorted(common))}"]
    if left in right or right in left:
        return 0.6, ["字段名互相包含"]
    for size in (4, 3, 2):
        if (
            len(left) >= size
            and len(right) >= size
            and left[-size:] == right[-size:]
            and left[-size:] not in _NAME_STOP_SUFFIXES
        ):
            return 0.5, [f"字段名共享后缀「{left[-size:]}」"]
    for group in _NAME_SYNONYM_GROUPS:
        if _name_hits(left_name, left_tokens, group) and _name_hits(
            right_name, right_tokens, group
        ):
            return 0.45, ["字段名用词同义（如 编号/代码/ID）"]
    return 0.0, []


def _type_compatibility(left_type: str, right_type: str) -> float:
    if not left_type or not right_type:
        return 0.5
    if left_type == right_type:
        return 1.0
    if {left_type, right_type} <= {"integer", "float"}:
        return 0.8
    if {left_type, right_type} <= {"integer", "float", "string"}:
        # 数字与文本仍可按规范化文本比对，只是不如同类型可靠
        return 0.4
    return 0.2


class FieldProfileProbe:
    """按批累积字段画像：只保留去重值与计数，不需要把记录留在内存里。

    推测关系与主键候选都要看「整个字段的取值分布」。逐批喂给本类后，内存里只
    有「去重值 → 出现次数」，规模由**不同取值的个数**决定，而不是由行数决定；
    7 万行与 2 万行占用的内存是同一量级，因此不再有行数上限的必要。
    """

    def __init__(self, fields: list[str] | None = None) -> None:
        self.rows = 0
        self._counts: dict[str, Counter] = {}
        self._non_empty: dict[str, int] = {}
        self._length: dict[str, int] = {}
        # 指定 fields 时只统计这些字段：单字段画像不必顺带保留其它列的取值
        self._only: set[str] | None = None if fields is None else set(fields)
        for name in fields or []:
            self._slot(name)

    def _slot(self, name: str) -> None:
        if name not in self._counts:
            self._counts[name] = Counter()
            self._non_empty[name] = 0
            self._length[name] = 0

    def add(self, records: list[dict[str, Any]]) -> None:
        self.rows += len(records)
        for rec in records:
            payload = rec.get("payload", {})
            for name, value in payload.items():
                if self._only is not None and name not in self._only:
                    continue
                key = match_key(value)
                if not key:
                    continue
                self._slot(name)
                self._counts[name][key] += 1
                self._non_empty[name] += 1
                self._length[name] += len(key)

    def profile(self, name: str) -> dict[str, Any]:
        self._slot(name)
        counts = self._counts[name]
        non_empty = self._non_empty[name]
        distinct = len(counts)
        return {
            "rows": self.rows,
            "non_empty": non_empty,
            "distinct": distinct,
            "values": set(counts),
            "counts": counts,
            "unique": self.rows > 0 and non_empty == self.rows and distinct == self.rows,
            "unique_ratio": (distinct / non_empty) if non_empty else 0.0,
            "avg_len": (self._length[name] / non_empty) if non_empty else 0.0,
        }


def _field_profile(schema: GroupFileSchema, name: str) -> dict[str, Any]:
    """只读字段画像：去重值、行数、唯一性、非空率、平均长度。"""
    probe = FieldProfileProbe([name])
    probe.add(schema.records)
    return probe.profile(name)


def _score_relation(
    left: GroupFileSchema,
    left_name: str,
    right: GroupFileSchema,
    right_name: str,
    left_profile: dict[str, Any],
    right_profile: dict[str, Any],
) -> dict[str, Any] | None:
    """给一个「left.field → right.field」方向打分（0–100 规则打分，不是概率）。

    值匹配是主证据；字段名、类型、唯一性只做加减分与说明。任何单一因素
    （尤其字段名）都不足以把关系推到自动预填线。
    """
    if not left_profile["distinct"] or not right_profile["distinct"]:
        return None
    # 必要条件快速排除：覆盖率不可能高于两侧去重值的数量比
    if right_profile["distinct"] < MIN_KEY_COVERAGE * left_profile["distinct"]:
        return None
    hit_values = left_profile["values"] & right_profile["values"]
    coverage = len(hit_values) / left_profile["distinct"]
    if coverage < MIN_KEY_COVERAGE:
        return None
    matched_rows = sum(
        count for key, count in left_profile["counts"].items() if key in right_profile["values"]
    )
    row_coverage = matched_rows / left_profile["non_empty"] if left_profile["non_empty"] else 0.0
    non_empty_ratio = left_profile["non_empty"] / left_profile["rows"] if left_profile["rows"] else 0.0
    name_similarity, name_notes = _name_similarity(left_name, right_name)
    left_type = left.field_type(left_name)
    right_type = right.field_type(right_name)
    type_compatibility = _type_compatibility(left_type, right_type)
    pk_unique_ratio = right_profile["unique_ratio"]

    score = 100 * (
        0.40 * coverage
        + 0.15 * row_coverage
        + 0.15 * name_similarity
        + 0.10 * type_compatibility
        + 0.10 * pk_unique_ratio
        + 0.10 * non_empty_ratio
    )
    notes = [
        f"「{left.display_name}」的「{left_name}」有 {left_profile['distinct']} 个非空去重值，"
        f"其中 {len(hit_values)} 个（{coverage:.0%}）能在「{right.display_name}」的「{right_name}」中找到",
        f"按行统计：{matched_rows}/{left_profile['non_empty']} 行匹配（{row_coverage:.0%}）",
        f"「{right.display_name}」的「{right_name}」唯一率 {pk_unique_ratio:.0%}"
        + ("，可作主键" if right_profile["unique"] else "，不能直接当主键"),
        *name_notes,
        f"类型 {left_type or '未知'} / {right_type or '未知'}",
    ]
    caps: list[int] = []
    warnings: list[str] = []
    if left_profile["distinct"] < MIN_EVIDENCE_VALUES:
        caps.append(MEDIUM_SCORE - 1)
        warnings.append(f"参与比对的去重值只有 {left_profile['distinct']} 个，样本太少，评分已下调")
    if max(left_profile["avg_len"], right_profile["avg_len"]) > MAX_KEY_VALUE_LENGTH:
        caps.append(MEDIUM_SCORE - 1)
        warnings.append(
            f"字段值平均长度 {max(left_profile['avg_len'], right_profile['avg_len']):.0f} 字符，"
            "更像长文本而不是标识符，评分已下调"
        )
    if (
        name_similarity == 0
        and not _is_key_like(left_name)
        and not _is_key_like(right_name)
        and left_profile["distinct"] < 100
    ):
        caps.append(MEDIUM_SCORE - 1)
        warnings.append("字段名没有标识符特征且去重值较少，值相同可能只是枚举巧合，评分已下调")
    if pk_unique_ratio < MIN_PK_UNIQUE_RATIO:
        caps.append(HIGH_SCORE - 1)
        warnings.append("对侧字段值不唯一，不能直接当主键，不会自动预填")
    if non_empty_ratio < 1:
        notes.append(f"「{left_name}」非空率 {non_empty_ratio:.0%}，空值行无法关联（仍保留原行）")
    if caps:
        score = min(score, min(caps))
    score_value = int(round(max(0.0, min(100.0, score))))
    tier = "high" if score_value >= HIGH_SCORE else "medium" if score_value >= MEDIUM_SCORE else "low"
    return {
        "id": relation_id(left.doc_id, left_name, right.doc_id, right_name),
        "from_doc_id": left.doc_id,
        "from_field": left_name,
        "to_doc_id": right.doc_id,
        "to_field": right_name,
        "kind": _kind_of(left_profile["unique"], right_profile["unique"]),
        "origin": "suggested",
        "confirmed": False,
        # 兼容旧字段：0–1 的值就是评分/100，不是经过校准的概率
        "confidence": round(score_value / 100, 3),
        "score": score_value,
        "tier": tier,
        "coverage": round(coverage, 3),
        "row_coverage": round(row_coverage, 3),
        "factors": {
            "coverage": round(coverage, 3),
            "row_coverage": round(row_coverage, 3),
            "name": round(name_similarity, 2),
            "type": round(type_compatibility, 2),
            "pk_unique": round(pk_unique_ratio, 3),
            "non_empty": round(non_empty_ratio, 3),
        },
        "evidence": notes,
        "warnings": warnings,
        "reason": "；".join(notes + warnings),
        # 仅用于同一文件对内部的择优，不对外返回
        "_rank": (
            score_value,
            name_similarity,
            int(_is_key_like(left_name)) + int(_is_key_like(right_name)),
            left_profile["distinct"],
        ),
    }


def suggest_relations(schemas: list[GroupFileSchema]) -> list[dict[str, Any]]:
    """按数据特征推测跨文件关系（每条都 confirmed=False，等用户确认）。"""
    profiles = {
        schema.doc_id: {name: _field_profile(schema, name) for name in schema.field_names()}
        for schema in schemas
    }
    return suggest_relations_from_profiles(profiles, schemas)


def suggest_relations_from_profiles(
    profiles: dict[str, dict[str, dict[str, Any]]], schemas: list[GroupFileSchema]
) -> list[dict[str, Any]]:
    """画像版本的关系推测。

    每个文件对只保留最可信的一条（含方向）：先比「值能不能对上」，再看字段名、
    类型、唯一性。评分是规则打分而非概率，判断依据逐条写在 evidence 里。
    ``profiles`` 可以由服务层分批累积得到，因此这里不需要任何记录常驻内存。
    """
    suggestions: list[dict[str, Any]] = []
    for index, left in enumerate(schemas):
        for right in schemas[index + 1 :]:
            best: dict[str, Any] | None = None
            for source, target in ((left, right), (right, left)):
                for left_name, left_profile in profiles[source.doc_id].items():
                    if not left_profile["distinct"]:
                        continue
                    for right_name, right_profile in profiles[target.doc_id].items():
                        if not right_profile["distinct"]:
                            continue
                        item = _score_relation(
                            source,
                            left_name,
                            target,
                            right_name,
                            left_profile,
                            right_profile,
                        )
                        if item is None:
                            continue
                        if best is None or item["_rank"] > best["_rank"]:
                            best = item
            if best is not None:
                best.pop("_rank", None)
                suggestions.append(best)
    suggestions.sort(
        key=lambda item: (-item["score"], -item["coverage"], item["from_field"], item["to_field"])
    )
    return suggestions


def high_confidence_relations(suggestions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """把评分 ≥90 的推测关系转成「自动预填但未确认」的关系配置（纯函数，不写库）。

    自动预填只是替用户把推荐的字段与关联类型选好；``confirmed`` 恒为 False，
    仍然必须逐条确认后才能通过校验并参与正式生成。
    """
    prefilled: list[dict[str, Any]] = []
    for item in suggestions:
        if item.get("tier") != "high" or int(item.get("score") or 0) < HIGH_SCORE:
            continue
        prefilled.append(
            {
                "id": item["id"],
                "from_doc_id": item["from_doc_id"],
                "from_field": item["from_field"],
                "to_doc_id": item["to_doc_id"],
                "to_field": item["to_field"],
                "kind": item["kind"],
                "origin": "suggested",
                "confirmed": False,
                "confidence": item.get("confidence"),
                "score": item.get("score"),
                "tier": item.get("tier"),
                "coverage": item.get("coverage"),
                "row_coverage": item.get("row_coverage"),
                "evidence": item.get("evidence", []),
                "reason": item.get("reason", ""),
            }
        )
    return prefilled


def _index_by_key(schema: GroupFileSchema, name: str) -> dict[str, list[dict[str, Any]]]:
    index: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for rec in schema.records:
        key = match_key(rec.get("payload", {}).get(name))
        if key:
            index[key].append(rec)
    return index


def _issue(level: str, kind: str, relation_id: str, message: str) -> dict[str, Any]:
    return {"level": level, "type": kind, "relation_id": relation_id, "message": message}


def validate_group(
    schemas: list[GroupFileSchema], relations: list[dict[str, Any]]
) -> dict[str, Any]:
    """正式生成前的校验：只报告问题，绝不修改、丢弃或重写任何记录。

    检查项：文件/字段是否还存在、主键是否重复、外键是否缺失（空值）、外键能否匹配、
    孤立记录、字段类型不一致、重复关联关系、以及「尚未确认」的关系。
    """
    by_doc = {s.doc_id: s for s in schemas}
    issues: list[dict[str, Any]] = []
    stats: list[dict[str, Any]] = []
    seen: set[str] = set()

    for rel in relations:
        rid = str(
            rel.get("id")
            or relation_id(
                str(rel.get("from_doc_id", "")),
                str(rel.get("from_field", "")),
                str(rel.get("to_doc_id", "")),
                str(rel.get("to_field", "")),
            )
        )
        rel["id"] = rid
        left = by_doc.get(str(rel.get("from_doc_id", "")))
        right = by_doc.get(str(rel.get("to_doc_id", "")))
        if left is None or right is None:
            issues.append(
                _issue(
                    "error",
                    "missing_file",
                    rid,
                    f"关系指向的文件已不在文件组内（{rel.get('from_field')} → {rel.get('to_field')}），请重新配置或删除该关系",
                )
            )
            continue
        if rel.get("from_field") not in left.field_names():
            issues.append(
                _issue(
                    "error",
                    "missing_field",
                    rid,
                    f"「{left.display_name}」里没有字段「{rel.get('from_field')}」（文件可能已被替换，请重新校验）",
                )
            )
            continue
        if rel.get("to_field") not in right.field_names():
            issues.append(
                _issue(
                    "error",
                    "missing_field",
                    rid,
                    f"「{right.display_name}」里没有字段「{rel.get('to_field')}」（文件可能已被替换，请重新校验）",
                )
            )
            continue
        if rid in seen:
            issues.append(
                _issue(
                    "warning",
                    "duplicate_relation",
                    rid,
                    f"重复的关联关系：{left.display_name}.{rel['from_field']} → {right.display_name}.{rel['to_field']}",
                )
            )
            continue
        seen.add(rid)

        left_keys = _keys_of(left, str(rel["from_field"]))
        right_keys = _keys_of(right, str(rel["to_field"]))
        right_index = _index_by_key(right, str(rel["to_field"]))
        non_empty_left = [k for k in left_keys if k]
        empty_left = left.record_count - len(non_empty_left)
        referenced = {k for k in non_empty_left if k in right_index}
        matched_rows = sum(1 for k in non_empty_left if k in right_index)
        unmatched_rows = len(non_empty_left) - matched_rows
        orphans = sum(len(v) for k, v in right_index.items() if k not in referenced)
        dup_right = {k: len(v) for k, v in right_index.items() if len(v) > 1}
        left_counter = Counter(k for k in non_empty_left)
        dup_left = {k: c for k, c in left_counter.items() if c > 1}

        kind = rel.get("kind") or _kind_of(
            _is_unique(left, str(rel["from_field"]), left_keys),
            _is_unique(right, str(rel["to_field"]), right_keys),
        )
        rel["kind"] = kind
        stats.append(
            {
                "relation_id": rid,
                "from_doc_id": left.doc_id,
                "from_file": left.display_name,
                "from_field": rel["from_field"],
                "to_doc_id": right.doc_id,
                "to_file": right.display_name,
                "to_field": rel["to_field"],
                "kind": kind,
                "confirmed": bool(rel.get("confirmed")),
                "origin": str(rel.get("origin", "manual")),
                "left_rows": left.record_count,
                "right_rows": right.record_count,
                "matched": matched_rows,
                "unmatched": unmatched_rows,
                "empty_key": empty_left,
                # 「一」侧（被当作主键的字段）与「多」侧的重复分开统计，含义不同
                "left_duplicate_keys": len(dup_left),
                "left_duplicate_rows": int(sum(dup_left.values())),
                "duplicate_keys": len(dup_right),
                "duplicate_key_rows": int(sum(dup_right.values())),
                "orphan_right_rows": orphans,
            }
        )

        if empty_left:
            issues.append(
                _issue(
                    "warning",
                    "empty_foreign_key",
                    rid,
                    f"「{left.display_name}」有 {empty_left} 行的「{rel['from_field']}」为空，这些行无法关联（仍保留并提示，不会丢弃）",
                )
            )
        if unmatched_rows:
            samples = [
                value_text(rec.get("payload", {}).get(rel["from_field"]))
                for rec in left.records
                if (k := match_key(rec.get("payload", {}).get(rel["from_field"])))
                and k not in right_index
            ][:5]
            issues.append(
                _issue(
                    "error",
                    "unmatched_foreign_key",
                    rid,
                    f"「{left.display_name}」有 {unmatched_rows} 行的「{rel['from_field']}」在「{right.display_name}」里找不到对应值"
                    + ("（示例：" + "、".join(samples) + "）" if samples else ""),
                )
            )
        # 重复值分两种情形，含义完全相反，不能混为一谈：
        # - 右侧（"多"侧）重复：一对多/多对多本来就该这样，只在统计里给出条数；
        #   只有声明要求右侧唯一（一对一 / 多对一）时，重复才与声明矛盾。
        # - 左侧（"一"侧）重复：说明被当作主键的字段其实不唯一，关联类型应为多对多。
        if kind in RIGHT_UNIQUE_KINDS and dup_right:
            samples = "、".join(f"{k}×{c}" for k, c in list(dup_right.items())[:5])
            issues.append(
                _issue(
                    "warning",
                    "duplicate_primary_key",
                    rid,
                    f"声明为{RELATION_KIND_LABELS[kind]}，但「{right.display_name}」的「{rel['to_field']}」有 {len(dup_right)} 个重复值（涉及 {sum(dup_right.values())} 行）：{samples}。生成时仍会展开全部匹配，不丢记录；如确实是一对多，请调整关联类型",
                )
            )
        if dup_left and kind in LEFT_UNIQUE_KINDS:
            samples = "、".join(f"{k}×{c}" for k, c in list(dup_left.items())[:5])
            issues.append(
                _issue(
                    "warning",
                    "primary_key_not_unique",
                    rid,
                    f"「{left.display_name}」的「{rel['from_field']}」作为关联主键有 {len(dup_left)} 个重复值（涉及 {sum(dup_left.values())} 行）：{samples}。这些行都会各自生成关联记录（不合并、不丢弃），但关联类型建议改为多对多",
                )
            )
        if orphans:
            issues.append(
                _issue(
                    "warning",
                    "orphan_records",
                    rid,
                    f"「{right.display_name}」有 {orphans} 行没有被任何记录引用（孤立记录；它们仍以单文件知识记录存在）",
                )
            )
        ltype = left.field_type(str(rel["from_field"]))
        rtype = right.field_type(str(rel["to_field"]))
        if ltype and rtype and ltype != rtype and {ltype, rtype} != {"integer", "float"}:
            issues.append(
                _issue(
                    "warning",
                    "type_mismatch",
                    rid,
                    f"关联字段类型不一致：「{left.display_name}」的「{rel['from_field']}」推断为 {ltype}，「{right.display_name}」的「{rel['to_field']}」推断为 {rtype}（按文本规范化匹配，原值不改）",
                )
            )
        if not rel.get("confirmed"):
            issues.append(
                _issue(
                    "info",
                    "unconfirmed",
                    rid,
                    f"关系 {left.display_name}.{rel['from_field']} → {right.display_name}.{rel['to_field']} 尚未确认，不参与正式生成",
                )
            )

    errors = [i for i in issues if i["level"] == "error"]
    structural = [i for i in errors if i["type"] in ("missing_file", "missing_field")]
    unmatched = [i for i in errors if i["type"] == "unmatched_foreign_key"]
    unconfirmed = [r for r in relations if not r.get("confirmed")]
    return {
        "issues": issues,
        "relation_stats": stats,
        "error_count": len(errors),
        "warning_count": len([i for i in issues if i["level"] == "warning"]),
        "unconfirmed_count": len(unconfirmed),
        # 分级判定：
        # - 结构错误（文件/字段已不存在）：关系本身不成立，必须先修好才能确认；
        # - 外键对不上：是数据事实，不阻断，但要求用户明确知情（force）后仍保留原始行生成；
        # - 未确认的关系一律不参与生成。
        "structural_error_count": len(structural),
        "unmatched_error_count": len(unmatched),
        "can_confirm": bool(relations) and not structural and not unconfirmed,
        "force_required": bool(unmatched),
        "total_rows": sum(s.record_count for s in schemas),
    }


def _label_value(rec: dict[str, Any], schema: GroupFileSchema, skip: set[str]) -> str:
    """取一条记录最能代表实体的字段值（优先「姓名/名称」类），用于关联文本。"""
    payload = rec.get("payload", {})
    names = [n for n in schema.field_names() if n not in skip]
    for name in names:
        if _is_label_like(name) and value_text(payload.get(name)):
            return value_text(payload.get(name))
    for name in names:
        if value_text(payload.get(name)):
            return value_text(payload.get(name))
    return ""


def format_link_text(segments: list[tuple[str, str]]) -> str:
    """把 (标签, 值) 拼成关联知识文本；空值不参与拼接，超长才截断。"""
    parts = [f"{label}：{value}" for label, value in segments if label and value.strip()]
    text = "；".join(parts)
    if text:
        text += "。"
    if len(text) > MAX_KNOWLEDGE_TEXT_CHARS:
        text = text[:MAX_KNOWLEDGE_TEXT_CHARS] + "…"
    return text


def source_of(
    schema: GroupFileSchema,
    rec: dict[str, Any],
    *,
    join_field: str | None,
    join_from_field: str | None,
    role: str,
) -> dict[str, Any]:
    """来源追溯坐标：文件 + 记录 + 行号/JSON 路径 + 参与关联的字段。"""
    return {
        "doc_id": schema.doc_id,
        "file_name": schema.file_name,
        "alias": schema.display_name,
        "record_id": rec.get("record_id"),
        "row_no": rec.get("row_no"),
        "sheet_name": rec.get("sheet_name"),
        "json_path": rec.get("json_path"),
        "join_field": join_field,
        "join_from_field": join_from_field,
        "role": role,
    }


def locate_text(schema: GroupFileSchema, rec: dict[str, Any]) -> str:
    """人读的定位串：CSV/TSV/XLSX 给行号（含工作表），JSON 给路径。"""
    if rec.get("json_path"):
        return str(rec["json_path"])
    sheet = f"{rec['sheet_name']} · " if rec.get("sheet_name") else ""
    return f"{sheet}第 {rec.get('row_no', 0)} 行"


def pick_anchor(
    schemas: list[GroupFileSchema],
    confirmed: list[dict[str, Any]],
    anchor_doc_id: str | None,
) -> GroupFileSchema | None:
    """选锚点文件：默认取「向外关联最多」的文件（典型即选课表/关联表）。

    这样每条锚点记录都能连起两侧实体，而不是把关系表的一行孤立地转成文本。
    """
    by_doc = {s.doc_id: s for s in schemas}
    if anchor_doc_id:
        return by_doc.get(anchor_doc_id)
    outgoing = Counter(str(r["from_doc_id"]) for r in confirmed)
    candidates = [s for s in schemas if outgoing.get(s.doc_id, 0) > 0] or schemas
    if not candidates:
        return None
    return max(candidates, key=lambda s: (outgoing.get(s.doc_id, 0), -s.order_no))


class LinkBuilder:
    """按已确认关系把**一条**锚点记录展开成一条关联记录。

    拆成「一条一条 build」而不是「一次算完所有行」，是为了让服务层可以按批从
    数据库流式读取锚点记录：锚点侧有多少行都不会整体驻留内存，也没有行数上限。
    对侧（被关联方）要按键查找，仍需要完整记录，由调用方一次性准备。
    多跳：沿已确认关系从锚点继续向下展开，最多 ``max_hops`` 跳（默认取配置
    ``STRUCTURED_RELATION_MAX_HOPS``）。只有已确认的关系参与；未确认的推测关系
    一律不展开。溯源（parts / sources）完整保留每一跳匹配到的记录，文本展开数量
    受 ``MAX_LINK_VALUES`` 限制并标注省略数量。
    """

    def __init__(
        self,
        schemas: list[GroupFileSchema],
        relations: list[dict[str, Any]],
        anchor_doc_id: str | None = None,
        max_hops: int | None = None,
    ) -> None:
        self.schemas = list(schemas)
        self.by_doc = {s.doc_id: s for s in self.schemas}
        self.confirmed = [r for r in relations if r.get("confirmed")]
        self.max_hops = max(
            1,
            int(max_hops if max_hops is not None else settings.STRUCTURED_RELATION_MAX_HOPS),
        )
        self.anchor = pick_anchor(self.schemas, self.confirmed, anchor_doc_id)
        self.relations: list[dict[str, Any]] = []
        self.key_fields: set[str] = set()
        self.indexes: dict[str, dict[str, list[dict[str, Any]]]] = {}
        # 每个文件向外的已确认关系（含多跳途经文件），只保留可达且目标存在的边
        self.outgoing: dict[str, list[dict[str, Any]]] = defaultdict(list)
        # 无法从锚点沿已确认关系展开的边及原因（供统计提示，绝不擅自推测生成）
        self.unreachable: list[dict[str, Any]] = []
        self.read_count = 0
        self.record_count = 0
        self.anomaly_count = 0
        self.skipped = ""
        if not self.confirmed:
            self.skipped = "没有已确认的关系"
            return
        if self.anchor is None:
            self.skipped = "找不到锚点文件"
            return
        self.relations = [
            r for r in self.confirmed if str(r["from_doc_id"]) == self.anchor.doc_id
        ]
        if not self.relations:
            self.skipped = (
                f"「{self.anchor.display_name}」没有以它为起点的已确认关系，"
                "请换锚点或调整关系方向"
            )
            return
        self.key_fields = {str(r["from_field"]) for r in self.relations}
        self._build_graph()

    def _build_graph(self) -> None:
        """按最大跳数算出锚点可达的文件深度，只为可达边建索引。

        索引指向的是调用方已加载的对侧记录（服务层对每个 to_doc 都加载完整记录），
        这里不额外读库、不复制记录，因此不会引入新的全量加载。
        """
        by_from: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for rel in self.confirmed:
            by_from[str(rel["from_doc_id"])].append(rel)
        depth: dict[str, int] = {self.anchor.doc_id: 0}
        queue = [self.anchor.doc_id]
        while queue:
            doc_id = queue.pop(0)
            current = depth[doc_id]
            if current >= self.max_hops:
                continue
            for rel in by_from.get(doc_id, []):
                to_doc = str(rel["to_doc_id"])
                if to_doc not in self.by_doc:
                    continue
                if to_doc not in depth or depth[to_doc] > current + 1:
                    depth[to_doc] = current + 1
                    queue.append(to_doc)
        for rel in self.confirmed:
            from_doc = str(rel["from_doc_id"])
            to_doc = str(rel["to_doc_id"])
            rid = str(rel["id"])
            if from_doc not in depth:
                self.unreachable.append(
                    {"id": rid, "reason": "起点文件不在锚点的已确认关联路径内"}
                )
                continue
            if depth[from_doc] >= self.max_hops:
                self.unreachable.append(
                    {"id": rid, "reason": f"已达最大跳数 {self.max_hops}，未继续展开"}
                )
                continue
            if to_doc not in self.by_doc:
                self.unreachable.append({"id": rid, "reason": "目标文件不在文件组内"})
                continue
            self.outgoing[from_doc].append(rel)
            if rid not in self.indexes:
                self.indexes[rid] = _index_by_key(self.by_doc[to_doc], str(rel["to_field"]))

    def build(self, rec: dict[str, Any]) -> dict[str, Any]:
        """把一条锚点记录展开成关联记录行（不写入任何全局状态，除计数）。"""
        self.read_count += 1
        payload = rec.get("payload", {})
        anomalies: list[str] = []
        segments: list[tuple[str, str]] = []
        parts: list[dict[str, Any]] = [
            {
                "doc_id": self.anchor.doc_id,
                "file_name": self.anchor.file_name,
                "alias": self.anchor.display_name,
                "locate": locate_text(self.anchor, rec),
                "values": payload,
            }
        ]
        sources: list[dict[str, Any]] = [
            source_of(
                self.anchor, rec, join_field=None, join_from_field=None, role="anchor"
            )
        ]
        relation_ids: list[str] = []
        # 同一条关联记录里每个原始记录只保留一次（防菱形 / 重复路径重复生成）
        seen: set[tuple[str, str]] = {(self.anchor.doc_id, str(rec.get("record_id")))}
        # 多跳文本按关系汇总：rid → {title, labels, omitted}
        deep_texts: dict[str, dict[str, Any]] = {}
        # 多跳提示按「关系 + 问题类型」去重汇总，避免逐条记录刷屏
        deep_notes: dict[tuple[str, str], dict[str, Any]] = {}

        # 锚点自己的非关联字段先按原顺序写入（空值不生成无意义文本）
        for name, value in payload.items():
            if name in self.key_fields:
                continue
            text = value_text(value)
            if text:
                segments.append((str(name), text))

        for rel in self.relations:
            rid = str(rel["id"])
            relation_ids.append(rid)
            right = self.by_doc.get(str(rel["to_doc_id"]))
            if right is None:
                anomalies.append(
                    f"关系指向的文件不在文件组内，无法展开「{rel['from_field']}」"
                )
                continue
            raw_key = value_text(payload.get(rel["from_field"]))
            key = match_key(payload.get(rel["from_field"]))
            if not key:
                anomalies.append(
                    f"「{rel['from_field']}」为空，无法关联「{right.display_name}」（原始空值已保留）"
                )
                continue
            matches = self.indexes.get(rid, {}).get(key, [])
            if not matches:
                anomalies.append(
                    f"「{rel['from_field']}={raw_key}」在「{right.display_name}」中未找到对应记录（原始值已保留）"
                )
                segments.append((str(rel["from_field"]), raw_key))
                continue
            if len(matches) > 1 and rel.get("kind") in RIGHT_UNIQUE_KINDS:
                expanded_note = (
                    "文本已全部展开"
                    if len(matches) <= MAX_LINK_VALUES
                    else f"溯源已全部保留，文本只展开前 {MAX_LINK_VALUES} 条"
                )
                anomalies.append(
                    f"该关系声明为{RELATION_KIND_LABELS.get(str(rel.get('kind')), '一对一')}，"
                    f"但「{right.display_name}」的「{rel['to_field']}={raw_key}」有 {len(matches)} 条记录，"
                    f"{expanded_note}（若不符合预期请调整关联类型）"
                )
            # 溯源完整保留全部匹配；文本只展开前 MAX_LINK_VALUES 条并标注省略数量
            text_count = min(len(matches), MAX_LINK_VALUES)
            omitted = len(matches) - text_count
            if omitted:
                anomalies.append(
                    f"「{rel['from_field']}={raw_key}」匹配到 {len(matches)} 条，溯源已全部保留，"
                    f"文本只展开前 {text_count} 条、省略 {omitted} 条（完整列表见详情）"
                )
            texts: list[str] = []
            for index, m in enumerate(matches):
                m_key = (right.doc_id, str(m.get("record_id")))
                is_new = m_key not in seen
                if is_new:
                    seen.add(m_key)
                    parts.append(
                        {
                            "doc_id": right.doc_id,
                            "file_name": right.file_name,
                            "alias": right.display_name,
                            "locate": locate_text(right, m),
                            "values": m.get("payload", {}),
                        }
                    )
                    sources.append(
                        source_of(
                            right,
                            m,
                            join_field=str(rel["to_field"]),
                            join_from_field=str(rel["from_field"]),
                            role="linked",
                        )
                    )
                label_value = _label_value(
                    m, right, {str(rel["to_field"])}
                ) or value_text(m.get("payload", {}).get(rel["to_field"]))
                if is_new and index < text_count and label_value:
                    texts.append(label_value)
                if self.max_hops > 1 and is_new:
                    self._expand_deeper(
                        m,
                        right,
                        depth=1,
                        path_docs={self.anchor.doc_id, right.doc_id},
                        seen=seen,
                        parts=parts,
                        sources=sources,
                        relation_ids=relation_ids,
                        deep_texts=deep_texts,
                        deep_notes=deep_notes,
                        text_allowed=index < text_count,
                    )
            title = (
                right.display_name
                if len(matches) == 1
                else f"{right.display_name}（匹配 {len(matches)} 条）"
            )
            value = "、".join(t for t in texts if t)
            if omitted:
                note = (
                    f"共匹配 {len(matches)} 条，文本展开 {text_count} 条，"
                    f"省略 {omitted} 条，完整列表见详情"
                )
                value = f"{value}（{note}）" if value else note
            segments.append((title, value))

        # 多跳文本：每个 deeper 关系汇总成一段，体现「锚点 → 中间 → 目标」完整上下文
        for entry in deep_texts.values():
            labels = entry["labels"]
            if not labels and not entry["omitted"]:
                continue
            value = "、".join(labels)
            if entry["omitted"]:
                note = f"文本展开 {len(labels)} 条，省略 {entry['omitted']} 条，完整列表见详情"
                value = f"{value}（{note}）" if value else note
            segments.append((entry["title"], value))

        for note in deep_notes.values():
            anomalies.append(note["message"])

        text = format_link_text(segments)
        if not text:
            anomalies.append("该行所有字段均为空，未生成关联文本")
        self.record_count += 1
        if anomalies:
            self.anomaly_count += 1
        return {
            "anchor_doc_id": self.anchor.doc_id,
            "anchor_record_id": rec.get("record_id"),
            "anchor_locate": locate_text(self.anchor, rec),
            "knowledge_text": text,
            "parts": parts,
            "sources": sources,
            "relation_ids": relation_ids,
            "anomalies": anomalies,
        }

    def _expand_deeper(
        self,
        rec: dict[str, Any],
        schema: GroupFileSchema,
        *,
        depth: int,
        path_docs: set[str],
        seen: set[tuple[str, str]],
        parts: list[dict[str, Any]],
        sources: list[dict[str, Any]],
        relation_ids: list[str],
        deep_texts: dict[str, dict[str, Any]],
        deep_notes: dict[tuple[str, str], dict[str, Any]],
        text_allowed: bool,
    ) -> None:
        """从一条已匹配记录沿其文件的已确认出边继续展开（depth=当前记录跳数）。

        循环防护：目标文件已在当前路径里就停止（A→B→A 不会无限展开）。
        重复防护：seen 保证同一原始记录在整条关联记录里只出现一次（菱形路径不重复）。
        溯源完整：每一跳匹配到的新记录都进 parts / sources（带 hop 与 via_relation_id）；
        文本只取前 MAX_LINK_VALUES 个标签并按关系汇总，省略数量如实标注。
        """
        if depth >= self.max_hops:
            return
        payload = rec.get("payload", {})
        for rel in self.outgoing.get(schema.doc_id, []):
            rid = str(rel["id"])
            target = self.by_doc.get(str(rel["to_doc_id"]))
            if target is None:
                continue
            if target.doc_id in path_docs:
                self._note(
                    deep_notes,
                    rid,
                    "cycle",
                    f"「{schema.display_name}→{target.display_name}」与已有路径形成循环，"
                    "已停止展开（原始记录已保留）",
                )
                continue
            if rid not in relation_ids:
                relation_ids.append(rid)
            raw_key = value_text(payload.get(rel["from_field"]))
            key = match_key(payload.get(rel["from_field"]))
            if not key:
                self._note(
                    deep_notes,
                    rid,
                    "empty",
                    f"「{schema.display_name}」的「{rel['from_field']}」存在空值，"
                    f"无法继续关联「{target.display_name}」（原始空值已保留）",
                )
                continue
            matches = self.indexes.get(rid, {}).get(key, [])
            if not matches:
                self._note(
                    deep_notes,
                    rid,
                    "unmatched",
                    f"「{schema.display_name}」的「{rel['from_field']}={raw_key}」"
                    f"在「{target.display_name}」中未找到对应记录（原始值已保留）",
                )
                continue
            if len(matches) > 1 and rel.get("kind") in RIGHT_UNIQUE_KINDS:
                self._note(
                    deep_notes,
                    rid,
                    "kind",
                    f"「{schema.display_name}→{target.display_name}」声明为"
                    f"{RELATION_KIND_LABELS.get(str(rel.get('kind')), '一对一')}，但存在一键多值，"
                    "溯源已全部保留（若不符合预期请调整关联类型）",
                )
            entry = deep_texts.get(rid)
            if entry is None:
                entry = {
                    "title": f"{target.display_name}（经「{schema.display_name}」）",
                    "labels": [],
                    "omitted": 0,
                }
                deep_texts[rid] = entry
            for index, m in enumerate(matches):
                m_key = (target.doc_id, str(m.get("record_id")))
                if m_key in seen:
                    continue
                seen.add(m_key)
                parts.append(
                    {
                        "doc_id": target.doc_id,
                        "file_name": target.file_name,
                        "alias": target.display_name,
                        "locate": locate_text(target, m),
                        "values": m.get("payload", {}),
                        "hop": depth + 1,
                        "via_relation_id": rid,
                    }
                )
                src = source_of(
                    target,
                    m,
                    join_field=str(rel["to_field"]),
                    join_from_field=str(rel["from_field"]),
                    role="linked",
                )
                src["hop"] = depth + 1
                src["via_relation_id"] = rid
                sources.append(src)
                label_value = _label_value(
                    m, target, {str(rel["to_field"])}
                ) or value_text(m.get("payload", {}).get(rel["to_field"]))
                child_text_allowed = text_allowed and index < MAX_LINK_VALUES
                if label_value:
                    if child_text_allowed and len(entry["labels"]) < MAX_LINK_VALUES:
                        entry["labels"].append(label_value)
                    elif text_allowed:
                        entry["omitted"] += 1
                self._expand_deeper(
                    m,
                    target,
                    depth=depth + 1,
                    path_docs=path_docs | {target.doc_id},
                    seen=seen,
                    parts=parts,
                    sources=sources,
                    relation_ids=relation_ids,
                    deep_texts=deep_texts,
                    deep_notes=deep_notes,
                    text_allowed=child_text_allowed,
                )

    @staticmethod
    def _note(
        deep_notes: dict[tuple[str, str], dict[str, Any]],
        rid: str,
        category: str,
        message: str,
    ) -> None:
        """多跳提示按「关系 + 问题类型」只记首条示例，避免逐行刷屏。"""
        key = (rid, category)
        if key not in deep_notes:
            deep_notes[key] = {"message": message}

    def build_many(self, records: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """整批展开（供服务层放进线程执行，避免 CPU 工作阻塞事件循环）。"""
        return [self.build(rec) for rec in records]

    def stats(self) -> dict[str, Any]:
        anchor = self.anchor
        if anchor is None:
            return {"record_count": 0, "skipped": self.skipped}
        out: dict[str, Any] = {
            "anchor_doc_id": anchor.doc_id,
            "anchor_file": anchor.display_name,
            "record_count": self.record_count,
            "read_count": self.read_count,
            "anomaly_count": self.anomaly_count,
            "anchor_rows": anchor.record_count,
            "relation_count": len(self.relations),
            "max_hops": self.max_hops,
        }
        if self.unreachable:
            out["unused_relations"] = self.unreachable
        return out


def build_linked_records(
    schemas: list[GroupFileSchema],
    relations: list[dict[str, Any]],
    anchor_doc_id: str | None = None,
    max_hops: int | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """按已确认关系生成关联记录（纯内存）：返回记录行 + 统计。

    一对多 / 多对多：同一关系匹配到多条时把它们合并展开（「课程A、课程B」），
    溯源（parts / sources）完整保留全部匹配，文本超出 MAX_LINK_VALUES 的部分标注
    展开 / 省略数量；多跳时沿已确认关系继续展开（默认 max_hops 取配置），保留每一跳
    上下文，循环与重复路径自动停止。仍然不改任何原始行。
    服务层的正式生成走 ``LinkBuilder`` 分批读取，这里保留给测试与小数据场景。
    """
    builder = LinkBuilder(schemas, relations, anchor_doc_id, max_hops=max_hops)
    if builder.skipped:
        if builder.anchor is None:
            return [], {"record_count": 0, "skipped": builder.skipped}
        return [], {
            "record_count": 0,
            "anchor_doc_id": builder.anchor.doc_id,
            "skipped": builder.skipped,
        }
    rows = [builder.build(rec) for rec in builder.anchor.records]
    return rows, builder.stats()
