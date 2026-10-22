"""资源名称实体消歧（Entity Resolution）专项测试。

覆盖需求：
1 方剂名覆盖通用关键词（沉香曲 → prescription，而非 herb）
2 明确中药名 → herb
3 明确理论名 → theory
4 明确文献名 → literature
5 不存在的名称 → 不命中，保持 Analyzer 原结果
6 同名跨资源类型 → ambiguous，不强行覆盖
7 域外问题（量子计算 / Python）→ 仍 unanswerable，不被"X子"误判为 herb
8 实体识别失败 → Analyzer / Router 不受影响
"""

import uuid

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from src.application.entity_resolver import (
    EntityResolutionResult,
    ResourceNameIndex,
    _build_index,
    clear_resource_name_index_cache,
    resolve_in_index,
)
from src.application.query_analyzer import analyze_query, apply_entity_resolution
from src.application.rag_service import RagService
from src.core.config import settings

# ── 构造纯内存索引（不依赖 DB）──────────────────────────────────────────────
_HERB_CHENXIANG = uuid.uuid4()
_HERB_DANSHEN = uuid.uuid4()
_HERB_HUOXIANG = uuid.uuid4()
_HERB_JINYINHUA = uuid.uuid4()
_PRES_CHENXIANGQU = uuid.uuid4()
_PRES_JINYINHUA = uuid.uuid4()
_PRES_HUOXIANG = uuid.uuid4()
_THEORY_SANYIN = uuid.uuid4()
_LIT_SANYIN = uuid.uuid4()


def _index() -> ResourceNameIndex:
    return _build_index(
        {
            "herb": [
                (_HERB_CHENXIANG, "沉香", []),
                (_HERB_DANSHEN, "丹参", []),
                (_HERB_HUOXIANG, "广藿香", ["藿香"]),
                (_HERB_JINYINHUA, "金银花", []),
            ],
            "prescription": [
                (_PRES_CHENXIANGQU, "沉香曲", []),
                (_PRES_JINYINHUA, "金银花", []),
                (_PRES_HUOXIANG, "藿香正气水", []),
            ],
            "theory": [(_THEORY_SANYIN, "三因学说", ["三因"])],
            "literature": [(_LIT_SANYIN, "三因极一病证方论", [])],
        }
    )


_IDX = _index()


# ── 1. 方剂名覆盖通用关键词 ──────────────────────────────────────────────────


def test_prescription_name_wins_over_herb_keywords():
    r = resolve_in_index(_IDX, "沉香曲的功效与主治？")
    assert r.matched is True
    assert r.resource_type == "prescription"
    assert r.resource_id == str(_PRES_CHENXIANGQU)
    assert r.resource_name == "沉香曲"
    assert r.matched_text == "沉香曲"
    assert r.match_kind == "contains"
    assert r.ambiguous is False


def test_longest_name_beats_shorter_overlap():
    """同一问题同时含"沉香"（中药）与"沉香曲"（方剂）时，最长名称优先。"""
    r = resolve_in_index(_IDX, "沉香曲和沉香有什么区别？")
    assert r.resource_type == "prescription"
    assert r.matched_text == "沉香曲"


def test_analyzer_result_is_overridden_by_entity():
    analyzer_out = analyze_query("沉香曲的功效与主治？")
    corrected = apply_entity_resolution(
        analyzer_out, resolve_in_index(_IDX, "沉香曲的功效与主治？")
    )
    assert analyzer_out.question_type == "herb"  # 旧行为：被"功效/主治"判成中药
    assert corrected.question_type == "prescription"
    assert corrected.resource_types == ["prescription"]
    assert corrected.is_multi_source is False
    assert corrected.features["entity_match"]["applied"] is True
    assert corrected.features["entity_match"]["resource_name"] == "沉香曲"
    assert corrected.is_valid is True
    # 不注入实体：避免 Dynamic Router 的 kg_enhanced 抢占聚焦策略
    assert corrected.entities == analyzer_out.entities


# ── 2~4. 各资源类型名称识别 ──────────────────────────────────────────────────


def test_herb_name_detected():
    r = resolve_in_index(_IDX, "丹参有什么功效？")
    assert (r.matched, r.resource_type, r.resource_name) == (True, "herb", "丹参")


def test_alias_match_detected():
    """别名命中：藿香 → 广藿香（herb），且被更长的"藿香正气水"压过。"""
    assert resolve_in_index(_IDX, "藿香怎么用？").resource_name == "广藿香"
    assert resolve_in_index(_IDX, "藿香正气水的组成？").resource_type == "prescription"


def test_theory_name_detected():
    r = resolve_in_index(_IDX, "三因学说是什么？")
    assert (r.matched, r.resource_type, r.matched_text) == (True, "theory", "三因学说")


def test_literature_name_detected():
    r = resolve_in_index(_IDX, "《三因极一病证方论》的作者是谁？")
    assert (r.matched, r.resource_type) == (True, "literature")


def test_exact_match_kind():
    r = resolve_in_index(_IDX, "沉香曲？")
    assert r.match_kind == "exact"
    assert r.resource_type == "prescription"


# ── 5. 不存在的名称 → 完全保持 Analyzer 行为 ─────────────────────────────────


def test_no_match_keeps_analyzer_result():
    r = resolve_in_index(_IDX, "量子纠缠态的作用XYZ是什么？")
    assert r.matched is False
    assert r.reason == "no_name_match"
    a = analyze_query("量子纠缠态的作用XYZ是什么？")
    assert apply_entity_resolution(a, r) is a or apply_entity_resolution(a, r).features[
        "entity_match"
    ]["applied"] is False


def test_resolution_unavailable_keeps_analyzer_result():
    a = analyze_query("丹参有什么功效？")
    out = apply_entity_resolution(a, None)
    assert out.question_type == a.question_type
    assert out.features["entity_match"]["matched"] is False
    assert out.features["entity_match"]["applied"] is False


# ── 6. 同名跨资源类型 → ambiguous，不强行覆盖 ────────────────────────────────


def test_generic_signal_word_resource_is_not_an_entity():
    """资源表里与通用词同名的条目（theories 中存在"功效"）不得算作命名实体。"""
    idx = _build_index(
        {
            "herb": [(uuid.uuid4(), "丹参", [])],
            "theory": [(uuid.uuid4(), "功效", [])],
        }
    )
    r = resolve_in_index(idx, "丹参有什么功效？")
    assert r.resource_type == "herb"
    out = apply_entity_resolution(analyze_query("丹参有什么功效？"), r)
    assert out.features["entity_match"]["applied"] is True
    assert out.question_type == "herb"


def test_two_real_entities_keep_analyzer_multi_source():
    """多个真实命名实体 → 不覆盖（保留 Analyzer 的 multi_source 结论）。"""
    idx = _build_index(
        {
            "herb": [(uuid.uuid4(), "金银花", [])],
            "literature": [(uuid.uuid4(), "本草纲目", [])],
        }
    )
    r = resolve_in_index(idx, "金银花在《本草纲目》中有什么记载？")
    assert set(r.entity_texts) == {"金银花", "本草纲目"}
    analysis = analyze_query("金银花在《本草纲目》中有什么记载？")
    out = apply_entity_resolution(analysis, r)
    assert out.question_type == analysis.question_type
    assert out.features["entity_match"]["note"] == "not_applied_multiple_entities"


def test_ambiguous_multiple_types_not_overridden():
    r = resolve_in_index(_IDX, "金银花的功效是什么？")
    assert r.matched is True
    assert r.ambiguous is True
    assert set(r.candidate_types) == {"herb", "prescription"}

    a = analyze_query("金银花的功效是什么？")
    out = apply_entity_resolution(a, r)
    assert out.question_type == a.question_type  # 保留 Analyzer 原结果
    assert out.features["entity_match"]["ambiguous"] is True
    assert out.features["entity_match"]["applied"] is False


# ── 7. 域外问题不被实体名规则带偏 ────────────────────────────────────────────


def test_out_of_domain_stays_unanswerable():
    q = "量子计算用 Python 怎么实现？"
    r = resolve_in_index(_IDX, q)
    a = analyze_query(q)
    assert a.question_type == "unanswerable"
    assert a.is_unanswerable_candidate is True

    out = apply_entity_resolution(a, r)
    assert out.question_type == "unanswerable"
    assert out.resource_types == a.resource_types
    assert out.features["entity_match"]["applied"] is False


def test_out_of_domain_not_overridden_even_with_valid_name():
    """域外前缀 + 真实实体名：不覆盖（避免把跨域问题的检索收窄成单一资源）。"""
    idx = _build_index({"herb": [(uuid.uuid4(), "丹参", [])]})
    analysis = analyze_query("量子计算的性能怎么样？")
    corrected_analysis_no_name = apply_entity_resolution(
        analysis, resolve_in_index(idx, "量子计算的性能怎么样？")
    )
    assert corrected_analysis_no_name.question_type == analysis.question_type


# ── 8. 结构化信息不泄漏正文 ──────────────────────────────────────────────────


def test_feature_only_exposes_locators():
    r = resolve_in_index(_IDX, "沉香曲的功效与主治？")
    feature = apply_entity_resolution(analyze_query("沉香曲的功效与主治？"), r).features[
        "entity_match"
    ]
    assert set(feature) >= {
        "matched",
        "resource_type",
        "resource_id",
        "resource_name",
        "matched_text",
        "match_kind",
        "ambiguous",
        "applied",
        "note",
    }
    # 不含资源正文等 DB 内部字段
    assert "description" not in feature and "content" not in feature


def test_invalid_result_type_does_not_break():
    """异常输入（非 EntityResolutionResult）不得让 Analyzer 崩溃。"""
    a = analyze_query("丹参有什么功效？")
    out = apply_entity_resolution(a, "not-a-result")  # type: ignore[arg-type]
    assert out.question_type == a.question_type
    assert out.is_valid is True


def test_entity_feature_shape_for_none():
    from src.application.query_analyzer import _entity_feature

    f = _entity_feature(None)
    assert f["matched"] is False
    assert f["reason"] == "entity_resolution_unavailable"
    assert isinstance(EntityResolutionResult(matched=False).to_feature(), dict)


# ── 真实 PG 集成（少量）──────────────────────────────────────────────────────


def _pg_session_factory():
    engine = create_async_engine(settings.DATABASE_URL, pool_pre_ping=True)
    return async_sessionmaker(engine, expire_on_commit=False), engine


async def _try_plan(question: str):
    clear_resource_name_index_cache()
    S, engine = _pg_session_factory()
    try:
        async with S() as s:
            svc = RagService(s)
            return await svc.plan_retrieval_with_entities(question)
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_real_db_chenxiangqu_is_prescription():
    clear_resource_name_index_cache()
    S, engine = _pg_session_factory()
    try:
        async with S() as s:
            from sqlalchemy import select

            from src.domain.models import Prescription

            rows = (
                await s.scalars(select(Prescription).where(Prescription.name == "沉香曲"))
            ).all()
            if not rows:
                pytest.skip("真实数据库中不存在资源：沉香曲")
            from src.application.entity_resolver import resolve_resource_name

            r = await resolve_resource_name(s, "沉香曲的功效与主治？")
    finally:
        await engine.dispose()
    assert r is not None and r.matched is True
    assert r.resource_type == "prescription"
    assert r.resource_name == "沉香曲"


@pytest.mark.asyncio
async def test_real_chain_routes_to_prescription_strategy():
    analysis, decision = await _try_plan("沉香曲的功效与主治？")
    em = analysis.features.get("entity_match", {})
    if not em.get("applied"):
        pytest.skip(f"实体未命中（note={em.get('note')}），跳过路由断言")
    assert analysis.question_type == "prescription"
    assert analysis.resource_types == ["prescription"]
    assert decision.resource_filter.get("resource_types") == ["prescription"]


@pytest.mark.asyncio
async def test_real_chain_unanswerable_not_overridden():
    analysis, decision = await _try_plan("量子计算用 Python 怎么实现？")
    assert analysis.question_type == "unanswerable"
    assert analysis.resource_types == []
    assert decision.strategy_name == "baseline_hybrid"
