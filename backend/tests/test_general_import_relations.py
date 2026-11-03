"""通用导入：Schema 分析与主外键发现的单元测试（小规模数据）。

数据结构参照 TASK 指定的真实数据集：
- D1 中医术语（父表）
- D2 中成药（父表）
- D3 中成药与术语关联（关联表）
并覆盖：缺失父表、重复主键、无效外键、多对多、跨表引用。
"""

from __future__ import annotations

from typing import Any

from src.application.relation_discovery import discover_relations
from src.application.schema_analyzer import (
    TYPE_INTEGER,
    TYPE_STRING,
    analyze_rows,
    schema_fingerprint,
)
from src.application.tabular_parser import ParsedRow
from src.domain.models import KG_STATUS_PENDING

# ── 测试数据集（小规模）────────────────────────────────────────────────────
D1_TERMS = [
    {"术语id": "T1", "术语名称": "脾胃虚弱"},
    {"术语id": "T2", "术语名称": "肝郁气滞"},
    {"术语id": "T3", "术语名称": "气血两虚"},
]
D2_MEDICINES = [
    {"中成药id": "M1", "名称": "四君子汤"},
    {"中成药id": "M2", "名称": "逍遥散"},
]
D3_LINKS = [
    {"中成药id": "M1", "术语id": "T1"},
    {"中成药id": "M1", "术语id": "T2"},
    {"中成药id": "M2", "术语id": "T1"},
]


def _profile(table_name: str, rows: list[dict[str, Any]], **kwargs: Any):
    parsed = [ParsedRow(row_no=i, data=row) for i, row in enumerate(rows, start=2)]
    return analyze_rows(parsed, table_name=table_name, **kwargs)


# ============================================================================
# Schema 分析
# ============================================================================


def test_unique_id_field_is_primary_key() -> None:
    profile = _profile("D1", D1_TERMS)

    assert profile.primary_key_candidates
    assert profile.primary_key_candidates[0].fields == ("术语id",)
    assert profile.primary_key_candidates[0].uniqueness == 1.0


def test_duplicate_values_produce_no_primary_key() -> None:
    rows = [
        {"术语id": "T1", "术语名称": "脾胃虚弱"},
        {"术语id": "T1", "术语名称": "肝郁气滞"},
    ]
    profile = _profile("D1_dup", rows)

    # 有重复值的字段绝不能成为主键候选
    assert profile.field("术语id").is_unique is False
    assert all(c.fields != ("术语id",) for c in profile.primary_key_candidates)
    assert profile.field("术语id").unique_ratio == 0.5


def test_field_types_are_inferred() -> None:
    rows = [
        {"id": 1, "名称": "四君子汤", "价格": 12.5, "启用": True},
        {"id": 2, "名称": "逍遥散", "价格": 9.0, "启用": False},
    ]
    profile = _profile("typed", rows)
    assert profile.field("id").inferred_type == TYPE_INTEGER
    assert profile.field("名称").inferred_type == TYPE_STRING
    assert profile.field("启用").inferred_type == "boolean"


def test_row_count_counts_all_rows_beyond_sample() -> None:
    rows = [{"id": i} for i in range(1, 11)]
    profile = _profile("big", rows, sample_size=3)

    assert profile.row_count == 10
    assert profile.analyzed_rows == 3
    assert profile.sampled is True


def test_sampled_profile_lowers_primary_key_confidence() -> None:
    rows = [{"id": i} for i in range(1, 11)]
    full = _profile("full", rows, sample_size=50)
    sampled = _profile("sampled", rows, sample_size=3)

    assert full.primary_key_candidates[0].confidence > sampled.primary_key_candidates[0].confidence
    assert "未全量验证" in sampled.primary_key_candidates[0].reason


def test_schema_fingerprint_ignores_column_order_and_case() -> None:
    assert schema_fingerprint(["术语id", "术语名称"]) == schema_fingerprint(["术语名称", "术语id"])
    assert schema_fingerprint(["ID", "Name"]) == schema_fingerprint(["id", "name"])
    assert schema_fingerprint(["术语id"]) != schema_fingerprint(["术语id", "术语名称"])


# ============================================================================
# 主外键发现
# ============================================================================


def test_foreign_keys_discovered_from_junction_to_parents() -> None:
    plan = discover_relations(
        [_profile("D1", D1_TERMS), _profile("D2", D2_MEDICINES), _profile("D3", D3_LINKS)]
    )

    pairs = {(fk.child_field, fk.parent_table) for fk in plan.foreign_keys}
    assert ("中成药id", "D2") in pairs
    assert ("术语id", "D1") in pairs
    assert plan.primary_keys["D1"] == ("术语id",)
    assert plan.primary_keys["D2"] == ("中成药id",)


def test_junction_table_detected_as_many_to_many() -> None:
    plan = discover_relations(
        [_profile("D1", D1_TERMS), _profile("D2", D2_MEDICINES), _profile("D3", D3_LINKS)]
    )

    assert len(plan.junctions) == 1
    junction = plan.junctions[0]
    assert junction.table == "D3"
    assert {junction.left_table, junction.right_table} == {"D1", "D2"}
    assert junction.confidence > 0.8


def test_denormalized_junction_with_redundant_columns_is_detected() -> None:
    """关联表冗余了术语名称等列、并因此多出同义外键时，仍应识别为多对多。

    真实导出（D3）在关联表里冗余了术语名称/拼音/分组，主键还混入一个
    指向父表的同义外键（术语名称→D1.术语名称）。旧的「恰好两个外键 + 最多
    两个额外字段」规则会整张表漏判，随后把外键当直接关系写入，报出
    「子表缺少主键」的假失败。
    """
    d3_rows = [
        {"中成药id": "M1", "术语id": "T1", "术语名称": "脾胃虚弱", "拼音": "piwei", "分组": "a"},
        {"中成药id": "M1", "术语id": "T2", "术语名称": "肝郁气滞", "拼音": "ganyu", "分组": "a"},
        {"中成药id": "M2", "术语id": "T1", "术语名称": "脾胃虚弱", "拼音": "piwei", "分组": "b"},
    ]

    plan = discover_relations(
        [_profile("D1", D1_TERMS), _profile("D2", D2_MEDICINES), _profile("D3", d3_rows)]
    )

    assert plan.primary_keys.get("D3") is None
    junctions = [j for j in plan.junctions if j.table == "D3"]
    assert len(junctions) == 1
    junction = junctions[0]
    assert {junction.left_table, junction.right_table} == {"D1", "D2"}
    assert {junction.left_field, junction.right_field} == {"术语id", "中成药id"}


def test_foreign_key_cardinality_is_one_to_many_in_junction() -> None:
    plan = discover_relations(
        [_profile("D1", D1_TERMS), _profile("D2", D2_MEDICINES), _profile("D3", D3_LINKS)]
    )

    cardinalities = {fk.child_field: fk.cardinality for fk in plan.foreign_keys}
    assert cardinalities["中成药id"] == "1:N"
    assert cardinalities["术语id"] == "1:N"


def test_one_to_one_cardinality_when_child_field_unique() -> None:
    """子表有自己的主键、另有唯一的外键列指向父表 → 1:1。

    注意区别于 test_identical_primary_keys_across_tables_is_not_foreign_key：
    若外键列同时是子表自己的主键且与父表主键同名（共享主键形态），
    在结构上与「两张实体表恰好有同名的自增 id」无法区分，
    按既有决策当作噪声排除（见该用例）。因此这里的子表带独立主键 子id。
    """
    parents = _profile("parents", [{"父id": "P1", "名称": "甲"}, {"父id": "P2", "名称": "乙"}])
    children = _profile(
        "children",
        [
            {"子id": "C1", "父id": "P1", "备注": "x"},
            {"子id": "C2", "父id": "P2", "备注": "y"},
        ],
    )

    plan = discover_relations([parents, children])

    fk = next(f for f in plan.foreign_keys if f.child_table == "children")
    assert fk.child_field == "父id"
    assert fk.cardinality == "1:1"


def test_candidates_default_to_pending_status() -> None:
    plan = discover_relations(
        [_profile("D1", D1_TERMS), _profile("D2", D2_MEDICINES), _profile("D3", D3_LINKS)]
    )

    assert all(fk.status == KG_STATUS_PENDING for fk in plan.foreign_keys)
    assert all(j.status == KG_STATUS_PENDING for j in plan.junctions)


def test_missing_parent_table_yields_no_foreign_key() -> None:
    orphan = _profile("孤儿表", [{"术语id": "T1", "名称": "甲"}, {"术语id": "T2", "名称": "乙"}])

    plan = discover_relations([orphan])

    assert plan.foreign_keys == []
    assert plan.junctions == []


def test_low_coverage_foreign_key_rejected() -> None:
    parents = _profile("parents", [{"父id": "P1"}, {"父id": "P2"}])
    children = _profile("children", [{"父id": "X9"}, {"父id": "X8"}])

    plan = discover_relations([parents, children])

    assert plan.foreign_keys == []


def test_identical_primary_keys_across_tables_is_not_foreign_key() -> None:
    table_a = _profile("A", [{"id": "1", "v": "a"}, {"id": "2", "v": "b"}])
    table_b = _profile("B", [{"id": "1", "v": "c"}, {"id": "2", "v": "d"}])

    plan = discover_relations([table_a, table_b])

    # A.id 与 B.id 同名且都是各自主键，不应被当成外键
    assert plan.foreign_keys == []


def test_foreign_key_evidence_contains_coverage_and_type() -> None:
    plan = discover_relations(
        [_profile("D1", D1_TERMS), _profile("D2", D2_MEDICINES), _profile("D3", D3_LINKS)]
    )

    evidence = plan.foreign_keys[0].evidence
    assert "覆盖率" in evidence
    assert "类型" in evidence
    assert "基数" in evidence


def test_relation_plan_reports_pending_count() -> None:
    plan = discover_relations(
        [_profile("D1", D1_TERMS), _profile("D2", D2_MEDICINES), _profile("D3", D3_LINKS)]
    )

    assert plan.confirmed_needed == len(plan.foreign_keys) + len(plan.junctions) == 3
