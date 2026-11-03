"""通用导入：主外键候选发现（含关联表/多对多识别）。

依据《要求》使用四类证据辅助判断，全部记录到候选对象里供前端展示与人工确认：
1. 字段名称相似度（术语id ↔ 术语id、术语id ↔ 术语）
2. 数据类型一致（integer ↔ integer 才加分，string ↔ integer 明显不合理）
3. 唯一性（父表字段必须唯一才能被引用；子表字段唯一则为 1:1，否则 1:N）
4. 关联覆盖率（子表取值在父表中能匹配到的比例）

重要约束：**候选不自动生效**。所有候选默认 status=pending，
未经人工确认不得写入正式知识图谱（由调用方执行持久化时过滤）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from src.application.schema_analyzer import (
    TYPE_INTEGER,
    TYPE_NUMBER,
    TYPE_STRING,
    FieldProfile,
    TableProfile,
)
from src.domain.models import KG_STATUS_PENDING

# 候选成为「可用建议」的最低置信度；低于此值不展示为推荐项
MIN_FK_CONFIDENCE = 0.6
# 覆盖率低于此值直接排除（大量匹配不上说明不是同一组编码体系）
MIN_COVERAGE = 0.5
# 参与类型比较的等价类型组
_TYPE_GROUPS: dict[str, str] = {
    TYPE_INTEGER: "numeric",
    TYPE_NUMBER: "numeric",
    TYPE_STRING: "text",
}


@dataclass
class ForeignKeyCandidate:
    child_table: str
    child_field: str
    parent_table: str
    parent_field: str
    name_similarity: float
    coverage: float
    type_match: bool
    confidence: float
    cardinality: str  # "1:1" / "1:N"
    evidence: str
    status: str = KG_STATUS_PENDING


@dataclass
class JunctionCandidate:
    """关联表（多对多）：本表通过两个外键把两张表连起来。"""

    table: str
    left_table: str
    left_field: str
    right_table: str
    right_field: str
    confidence: float
    evidence: str
    relation_type: str = "related_to"
    status: str = KG_STATUS_PENDING

    @property
    def label(self) -> str:
        return f"{self.left_table} ↔ {self.right_table}"


@dataclass
class RelationPlan:
    """一次关联导入的分析结果（供前端展示与人工确认）。"""

    primary_keys: dict[str, tuple[str, ...]] = field(default_factory=dict)
    foreign_keys: list[ForeignKeyCandidate] = field(default_factory=list)
    junctions: list[JunctionCandidate] = field(default_factory=list)

    @property
    def confirmed_needed(self) -> int:
        """待人工确认的候选总数。"""
        return len(self.foreign_keys) + len(self.junctions)


def _normalize_name(name: str) -> str:
    return re.sub(r"[\s_\-]+", "", name or "").lower()


def _name_similarity(child: str, parent: str) -> float:
    a, b = _normalize_name(child), _normalize_name(parent)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    if a.endswith("id") and b == a[:-2]:
        return 0.85
    if b.endswith("id") and a == b[:-2]:
        return 0.85
    if a in b or b in a:
        return 0.6
    return 0.0


def _type_group(profile: FieldProfile) -> str:
    return _TYPE_GROUPS.get(profile.inferred_type, profile.inferred_type)


def _coverage(child: FieldProfile, parent: FieldProfile) -> float:
    """子表取值在父表中能匹配到的比例。"""
    if not child.distinct:
        return 0.0
    hit = sum(1 for key in child.distinct if key in parent.distinct)
    return hit / len(child.distinct)


def _pick_primary_key(table: TableProfile) -> str | None:
    candidates = table.primary_key_candidates
    return candidates[0].fields[0] if candidates else None


def _discover_foreign_keys(
    tables: dict[str, TableProfile], primary_keys: dict[str, str]
) -> list[ForeignKeyCandidate]:
    candidates: list[ForeignKeyCandidate] = []

    for child_name, child in tables.items():
        child_pk = primary_keys.get(child_name)
        for child_field in child.fields:
            for parent_name, parent in tables.items():
                if parent_name == child_name:
                    continue
                for parent_field in parent.fields:
                    # 父表字段必须唯一（按非空取值判定，容忍个别脏空值）才可能被引用
                    if not parent_field.is_unique_non_null:
                        continue
                    # 排除「两表各自的同名主键」这种噪声（A.id ↔ B.id）
                    if (
                        child_pk
                        and child_field.name == child_pk
                        and _name_similarity(child_field.name, parent_field.name) == 1.0
                        and primary_keys.get(parent_name) == parent_field.name
                    ):
                        continue

                    name_sim = _name_similarity(child_field.name, parent_field.name)
                    if name_sim == 0:
                        continue
                    coverage = _coverage(child_field, parent_field)
                    if coverage < MIN_COVERAGE:
                        continue

                    type_match = _type_group(child_field) == _type_group(parent_field)
                    score = 0.4 * name_sim + 0.45 * coverage + (0.15 if type_match else -0.15)
                    confidence = max(0.0, min(1.0, score))
                    if confidence < MIN_FK_CONFIDENCE:
                        continue

                    cardinality = "1:1" if child_field.is_unique else "1:N"
                    candidates.append(
                        ForeignKeyCandidate(
                            child_table=child_name,
                            child_field=child_field.name,
                            parent_table=parent_name,
                            parent_field=parent_field.name,
                            name_similarity=round(name_sim, 3),
                            coverage=round(coverage, 3),
                            type_match=type_match,
                            confidence=round(confidence, 3),
                            cardinality=cardinality,
                            evidence=(
                                f"名称相似 {name_sim:.0%}；覆盖率 {coverage:.0%}；"
                                f"类型{'一致' if type_match else '不一致'}"
                                f"（{child_field.inferred_type} ↔ {parent_field.inferred_type}）；"
                                f"基数 {cardinality}"
                            ),
                        )
                    )
    # 同子表同字段只保留置信度最高的候选，避免一个外键给出多个父表
    best: dict[tuple[str, str], ForeignKeyCandidate] = {}
    for item in candidates:
        key = (item.child_table, item.child_field)
        current = best.get(key)
        if current is None or item.confidence > current.confidence:
            best[key] = item
    result = list(best.values())
    result.sort(key=lambda c: (-c.confidence, c.child_table, c.child_field))
    return result


def _discover_junctions(
    tables: dict[str, TableProfile],
    foreign_keys: list[ForeignKeyCandidate],
    primary_keys: dict[str, str],
) -> list[JunctionCandidate]:
    by_table: dict[str, list[ForeignKeyCandidate]] = {}
    for fk in foreign_keys:
        by_table.setdefault(fk.child_table, []).append(fk)

    junctions: list[JunctionCandidate] = []
    for table_name, keys in by_table.items():
        table = tables[table_name]
        if table_name in primary_keys:
            # 有主键的是实体表：必须恰好两个外键，且非外键列很少，
            # 才可能是「纯关联表」，否则保留其自身实体语义。
            if len(keys) != 2:
                continue
            left, right = keys[0], keys[1]
            extra_limit = 2
        else:
            # 无主键的表通常是关联表/明细表。真实导出常在关联表里冗余术语名称、
            # 拼音等列，并因此多识别出一个指向父表的同义外键（实测 D3：8 列、
            # 3 个外键）。此时取置信度最高的两个不同父表外键作为多对多两端，
            # 其余列按关联属性保留，不再因「额外字段过多」漏掉整张关联表。
            if len(keys) < 2:
                continue
            picked: list[ForeignKeyCandidate] = []
            parents: set[str] = set()
            for fk in keys:
                if fk.parent_table in parents:
                    continue
                picked.append(fk)
                parents.add(fk.parent_table)
                if len(picked) == 2:
                    break
            if len(picked) < 2:
                continue
            left, right = picked
            extra_limit = None

        if left.parent_table == right.parent_table:
            continue

        left_unique = table.field(left.child_field)
        right_unique = table.field(right.child_field)
        if left_unique is None or right_unique is None:
            continue
        if left_unique.is_unique_non_null or right_unique.is_unique_non_null:
            # 有一侧在本表内唯一，说明不是「多对多」关联表
            continue

        # 非外键字段过多则不认为是纯关联表（仅对有主键的实体表生效）
        fk_fields = {left.child_field, right.child_field}
        extra_fields = [f.name for f in table.fields if f.name not in fk_fields]
        if extra_limit is not None and len(extra_fields) > extra_limit:
            continue

        confidence = round(min(left.confidence, right.confidence) * 0.95, 3)
        junctions.append(
            JunctionCandidate(
                table=table_name,
                left_table=left.parent_table,
                left_field=left.child_field,
                right_table=right.parent_table,
                right_field=right.child_field,
                confidence=confidence,
                evidence=(
                    f"本表含两个外键（{left.child_field}→{left.parent_table}、"
                    f"{right.child_field}→{right.parent_table}），且两侧在表内均重复出现；"
                    f"额外字段 {len(extra_fields)} 个"
                ),
            )
        )
    return junctions


def discover_relations(profiles: list[TableProfile]) -> RelationPlan:
    """基于多张表的画像发现主键、外键候选与关联表。"""
    tables = {p.table_name: p for p in profiles}
    primary_keys: dict[str, str] = {}
    for name, table in tables.items():
        picked = _pick_primary_key(table)
        if picked:
            primary_keys[name] = picked

    foreign_keys = _discover_foreign_keys(tables, primary_keys)
    junctions = _discover_junctions(tables, foreign_keys, primary_keys)

    return RelationPlan(
        primary_keys={name: (pk,) for name, pk in primary_keys.items()},
        foreign_keys=foreign_keys,
        junctions=junctions,
    )
