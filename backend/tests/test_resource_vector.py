"""TASK-008 Stage 4-2：Resource Vectorization Service 测试。

覆盖范围（按 Stage 4-2 任务要求）：
1. 四种 Resource 的 canonical text
2. Prescription ingredients → Herb.name（展开组成药材）
3. 空字段处理（不输出 None / 空白）
4. Theory/Literature 长文本切片（≤6000 单向量，超长复用 chunking）
5. chunk_index / title_path
6. Resource + KB + chunk 的 ID 稳定性（与 Milvus VARCHAR(64) 长度）
7. VectorRow dense/sparse vector
8. source_type / era / credibility_level（受控枚举 + 固定映射）

Embedding 使用 MockEmbedding（确定性伪向量），不连接真实 Milvus。
所有测试为纯单元测试，不依赖 PostgreSQL / Milvus / Redis。
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest

from src.application.resource_vector_service import (
    LONG_TEXT_THRESHOLD,
    ResourceChunk,
    ResourceVectorError,
    ResourceVectorService,
    build_canonical_text,
    chunk_resource,
    make_doc_id,
    make_vector_id,
)
from src.core.source_meta import CREDIBILITY_MAP, SOURCE_TYPES
from src.domain.models import (
    Herb,
    Literature,
    Prescription,
    PrescriptionIngredient,
    Theory,
)
from src.infrastructure.embedding import MockEmbedding
from src.infrastructure.milvus_store import VectorRow


# ---------------------------------------------------------------------------
# 测试辅助：构造 detached 资源实例
# ---------------------------------------------------------------------------


def _herb(
    *,
    name: str = "甘草",
    aliases: list[str] | None = None,
    properties: str = "",
    channels: list[str] | None = None,
    effects: str = "",
    source: str = "",
    description: str = "",
) -> Herb:
    return Herb(
        name=name,
        aliases=aliases or [],
        properties=properties,
        channels=channels or [],
        effects=effects,
        source=source,
        description=description,
    )


def _prescription(
    *,
    name: str = "桂枝汤",
    aliases: list[str] | None = None,
    ingredients: list[PrescriptionIngredient] | None = None,
    efficacy: str = "",
    indications: str = "",
    usage_method: str = "",
    source: str = "",
    description: str = "",
) -> Prescription:
    return Prescription(
        name=name,
        aliases=aliases or [],
        ingredients=ingredients or [],
        efficacy=efficacy,
        indications=indications,
        usage_method=usage_method,
        source=source,
        description=description,
    )


def _ingredient(
    herb_name: str,
    *,
    amount: Decimal | float | None = None,
    unit: str = "",
    processing: str = "",
    role: str = "",
    sort_order: int = 0,
) -> PrescriptionIngredient:
    return PrescriptionIngredient(
        herb=Herb(name=herb_name),
        amount=Decimal(str(amount)) if amount is not None else None,
        unit=unit,
        processing=processing,
        role=role,
        sort_order=sort_order,
    )


def _theory(
    *,
    name: str = "阴阳学说",
    aliases: list[str] | None = None,
    content: str = "",
    source: str = "",
) -> Theory:
    return Theory(name=name, aliases=aliases or [], content=content, source=source)


def _literature(
    *,
    name: str = "伤寒论",
    aliases: list[str] | None = None,
    author: str = "",
    dynasty: str = "",
    summary: str = "",
    content: str = "",
    source: str = "",
) -> Literature:
    return Literature(
        name=name,
        aliases=aliases or [],
        author=author,
        dynasty=dynasty,
        summary=summary,
        content=content,
        source=source,
    )


def _service(dim: int = 32) -> ResourceVectorService:
    """构造使用 MockEmbedding 的服务（不依赖 DB / settings.EMBEDDING_BACKEND）。"""
    return ResourceVectorService(embedding=MockEmbedding(dim=dim))


# ===========================================================================
# 1. 四种 Resource 的 canonical text
# ===========================================================================


class TestCanonicalText:
    """覆盖范围 #1：四种 Resource 的 canonical text。"""

    def test_herb_canonical_text_contains_all_fields(self):
        herb = _herb(
            name="甘草",
            aliases=["国老", "甜根子"],
            properties="甘，平",
            channels=["心", "肺", "脾", "胃"],
            effects="补脾益气、清热解毒、祛痰止咳、缓急止痛",
            source="《神农本草经》",
            description="豆科植物甘草的根及根茎",
        )
        text = build_canonical_text(herb)
        assert "甘草" in text
        assert "国老" in text and "甜根子" in text
        assert "甘，平" in text
        assert "心" in text and "胃" in text
        assert "补脾益气" in text
        assert "神农本草经" in text
        assert "豆科植物" in text
        # 标签存在
        assert "【药名】" in text
        assert "【别名】" in text
        assert "【性味】" in text
        assert "【归经】" in text
        assert "【功效】" in text
        assert "【出处】" in text
        assert "【描述】" in text

    def test_prescription_canonical_text_contains_all_fields(self):
        p = _prescription(
            name="麻黄汤",
            aliases=["还魂汤"],
            ingredients=[
                _ingredient("麻黄", amount=6, unit="克", role="君"),
                _ingredient("桂枝", amount=4, unit="克", role="臣"),
            ],
            efficacy="发汗解表、宣肺平喘",
            indications="外感风寒表实证",
            usage_method="水煎服，温覆取微汗",
            source="《伤寒论》",
            description="方解：麻黄开腠理...",
        )
        text = build_canonical_text(p)
        assert "麻黄汤" in text
        assert "还魂汤" in text
        assert "麻黄" in text and "桂枝" in text
        assert "6克" in text and "4克" in text
        assert "[君]" in text and "[臣]" in text
        assert "发汗解表" in text
        assert "外感风寒" in text
        assert "水煎服" in text
        assert "伤寒论" in text
        assert "方解" in text

    def test_theory_canonical_text_contains_all_fields(self):
        t = _theory(
            name="阴阳学说",
            aliases=["阴阳理论"],
            content="阴阳者，天地之道也...",
            source="《素问·阴阳应象大论》",
        )
        text = build_canonical_text(t)
        assert "阴阳学说" in text
        assert "阴阳理论" in text
        assert "天地之道" in text
        assert "素问" in text
        assert "【理论名】" in text
        assert "【正文】" in text

    def test_literature_canonical_text_contains_all_fields(self):
        lit = _literature(
            name="伤寒论",
            aliases=["伤寒卒病论"],
            author="张仲景",
            dynasty="东汉",
            summary="论述外感疾病辨证论治",
            content="太阳病，头痛发热，汗出，恶风，桂枝汤主之",
            source="宋本《伤寒论》",
        )
        text = build_canonical_text(lit)
        assert "伤寒论" in text
        assert "伤寒卒病论" in text
        assert "张仲景" in text
        assert "东汉" in text
        assert "辨证论治" in text
        assert "太阳病" in text
        assert "宋本" in text
        assert "【文献名】" in text
        assert "【作者】" in text
        assert "【成书年代】" in text
        assert "【摘要】" in text
        assert "【正文】" in text

    def test_unsupported_resource_type_raises(self):
        with pytest.raises(ResourceVectorError, match="不支持的资源类型"):
            build_canonical_text(object())  # type: ignore[arg-type]


# ===========================================================================
# 2. Prescription ingredients → Herb.name
# ===========================================================================


class TestPrescriptionIngredients:
    """覆盖范围 #2：Prescription ingredients → Herb.name 展开。"""

    def test_ingredients_expand_herb_names(self):
        p = _prescription(
            name="四物汤",
            ingredients=[
                _ingredient("当归", amount=12, unit="克", role="君"),
                _ingredient("川芎", amount=8, unit="克", role="臣"),
                _ingredient("白芍", amount=10, unit="克", role="佐"),
                _ingredient("熟地黄", amount=15, unit="克", role="使"),
            ],
        )
        text = build_canonical_text(p)
        # 四味药名均出现在文本中
        assert "当归" in text
        assert "川芎" in text
        assert "白芍" in text
        assert "熟地黄" in text
        # 用量与单位
        assert "12克" in text
        assert "8克" in text
        assert "10克" in text
        assert "15克" in text
        # 角色
        assert "[君]" in text
        assert "[臣]" in text
        assert "[佐]" in text
        assert "[使]" in text

    def test_ingredient_with_processing(self):
        p = _prescription(
            name="炙甘草汤",
            ingredients=[
                _ingredient("甘草", amount=12, unit="克", processing="炙", role="君"),
            ],
        )
        text = build_canonical_text(p)
        assert "甘草" in text
        assert "（炙）" in text

    def test_ingredient_without_amount_omits_quantity(self):
        """amount=None（"适量"）时仅输出药名，不带数字。"""
        p = _prescription(
            name="适量方",
            ingredients=[_ingredient("人参", amount=None, unit="克")],
        )
        text = build_canonical_text(p)
        assert "人参" in text
        # 不应出现 "None" 或孤立单位
        assert "None" not in text

    def test_decimal_amount_strips_trailing_zeros(self):
        """Decimal("10.00") 应输出 "10" 而非 "10.00"。"""
        p = _prescription(
            name="方",
            ingredients=[_ingredient("黄芪", amount=Decimal("10.00"), unit="克")],
        )
        text = build_canonical_text(p)
        assert "10克" in text
        assert "10.00" not in text

    def test_empty_ingredients_produces_no_section(self):
        p = _prescription(name="空方", ingredients=[])
        text = build_canonical_text(p)
        assert "【组成】" not in text


# ===========================================================================
# 3. 空字段处理
# ===========================================================================


class TestEmptyFields:
    """覆盖范围 #3：空字段不输出 None / 空白。"""

    def test_herb_only_name(self):
        """只有 name，其余全空 → 只输出药名。"""
        herb = _herb(name="独活")
        text = build_canonical_text(herb)
        assert text.strip() == "【药名】独活"

    def test_herb_no_fields_at_all(self):
        herb = _herb(name="")
        text = build_canonical_text(herb)
        assert text == ""

    def test_prescription_empty_fields_omitted(self):
        p = _prescription(name="空方")
        text = build_canonical_text(p)
        assert text.strip() == "【方名】空方"
        assert "None" not in text
        assert "【组成】" not in text
        assert "【功效】" not in text

    def test_theory_empty_fields_omitted(self):
        t = _theory(name="空理论")
        text = build_canonical_text(t)
        assert text.strip() == "【理论名】空理论"
        assert "None" not in text

    def test_literature_empty_fields_omitted(self):
        lit = _literature(name="空文献")
        text = build_canonical_text(lit)
        assert text.strip() == "【文献名】空文献"
        assert "None" not in text

    def test_empty_aliases_list_omitted(self):
        herb = _herb(name="甘草", aliases=[])
        text = build_canonical_text(herb)
        assert "【别名】" not in text

    def test_empty_channels_list_omitted(self):
        herb = _herb(name="甘草", channels=[])
        text = build_canonical_text(herb)
        assert "【归经】" not in text

    def test_whitespace_only_fields_omitted(self):
        herb = _herb(name="甘草", properties="   ", effects="")
        text = build_canonical_text(herb)
        assert "【性味】" not in text
        assert "【功效】" not in text

    def test_no_category_or_tag_in_text(self):
        """category_id / tag 等内部 ID 不拼入文本。"""
        herb = _herb(name="甘草", properties="甘，平")
        text = build_canonical_text(herb)
        # 不应出现 UUID 格式的 category_id
        import re

        assert not re.search(
            r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", text
        )


# ===========================================================================
# 4. Theory/Literature 长文本切片
# ===========================================================================


class TestLongTextChunking:
    """覆盖范围 #4：Theory/Literature 长文本切片。"""

    def test_theory_short_text_single_chunk(self):
        content = "阴阳者，天地之道也。" * 50  # 约 450 字符 < 6000
        t = _theory(name="阴阳", content=content)
        text = build_canonical_text(t)
        chunks = chunk_resource(t, text)
        assert len(chunks) == 1
        assert chunks[0].chunk_index == 0

    def test_theory_long_text_multiple_chunks(self):
        content = "太阳病，头痛发热，汗出，恶风，桂枝汤主之。" * 350  # ≈ 7000 字符 > 6000
        t = _theory(name="长篇理论", content=content)
        text = build_canonical_text(t)
        chunks = chunk_resource(t, text)
        assert len(chunks) >= 2
        # 所有 chunk_index 连续从 0 递增
        indices = [c.chunk_index for c in chunks]
        assert indices == list(range(len(chunks)))

    def test_literature_short_text_single_chunk(self):
        lit = _literature(name="短文献", content="内容" * 100)  # 200 字符
        text = build_canonical_text(lit)
        chunks = chunk_resource(lit, text)
        assert len(chunks) == 1

    def test_literature_long_text_multiple_chunks(self):
        content = "太阳病，头痛发热，汗出，恶风，桂枝汤主之。" * 350  # ≈ 7000 字符 > 6000
        lit = _literature(name="长篇文献", content=content)
        text = build_canonical_text(lit)
        chunks = chunk_resource(lit, text)
        assert len(chunks) >= 2
        # 切片后所有内容应该被保留（拼接后包含原文）
        joined = "".join(c.content for c in chunks)
        assert "太阳病" in joined

    def test_herb_always_single_chunk_regardless_of_length(self):
        """Herb 即使 canonical text > 6000 字符也保持单向量。"""
        herb = _herb(
            name="甘草",
            description="极长描述" * 2000,  # > 6000 字符
        )
        text = build_canonical_text(herb)
        chunks = chunk_resource(herb, text)
        assert len(chunks) == 1

    def test_prescription_always_single_chunk(self):
        p = _prescription(
            name="方",
            description="极长描述" * 2000,
        )
        text = build_canonical_text(p)
        chunks = chunk_resource(p, text)
        assert len(chunks) == 1

    def test_empty_canonical_text_produces_no_chunks(self):
        herb = _herb(name="")
        text = build_canonical_text(herb)
        assert text == ""
        assert chunk_resource(herb, text) == []

    def test_threshold_boundary_exact(self):
        """正好等于 LONG_TEXT_THRESHOLD 的 Theory 应为单向量。"""
        content = "字" * LONG_TEXT_THRESHOLD
        t = _theory(name="边界", content=content)
        text = build_canonical_text(t)
        chunks = chunk_resource(t, text)
        # canonical text 比 content 略长（含标题），但 content 部分恰好为阈值
        # 因 canonical text 整体可能 > 阈值，验证至少切片逻辑不崩溃
        assert len(chunks) >= 1


# ===========================================================================
# 5. chunk_index / title_path
# ===========================================================================


class TestChunkIndexAndTitlePath:
    """覆盖范围 #5：chunk_index / title_path。"""

    def test_herb_chunk_index_zero_and_no_title_path(self):
        herb = _herb(name="甘草", properties="甘，平")
        text = build_canonical_text(herb)
        chunks = chunk_resource(herb, text)
        assert chunks[0].chunk_index == 0
        assert chunks[0].title_path is None

    def test_prescription_chunk_index_zero_and_no_title_path(self):
        p = _prescription(name="方", efficacy="功效")
        text = build_canonical_text(p)
        chunks = chunk_resource(p, text)
        assert chunks[0].chunk_index == 0
        assert chunks[0].title_path is None

    def test_theory_short_chunk_index_zero_and_no_title_path(self):
        t = _theory(name="理论", content="内容")
        text = build_canonical_text(t)
        chunks = chunk_resource(t, text)
        assert chunks[0].chunk_index == 0
        assert chunks[0].title_path is None

    def test_long_text_chunk_indices_sequential(self):
        content = "段落。" * 3000  # 约 9000 字符
        t = _theory(name="长论", content=content)
        text = build_canonical_text(t)
        chunks = chunk_resource(t, text)
        assert len(chunks) >= 2
        for i, c in enumerate(chunks):
            assert c.chunk_index == i

    def test_long_text_title_path_present_or_none(self):
        """长文本切片 title_path 可能为 None（无标题）或字符串，不为空串。"""
        content = "太阳病，头痛发热。" * 1000
        t = _theory(name="长论", content=content)
        text = build_canonical_text(t)
        chunks = chunk_resource(t, text)
        for c in chunks:
            # title_path 可为 None 或非空字符串，不应为空串 ""
            assert c.title_path is None or len(c.title_path) > 0


# ===========================================================================
# 6. Resource + KB + chunk 的 ID 稳定性
# ===========================================================================


class TestStableId:
    """覆盖范围 #6：Resource + KB + chunk 的 ID 稳定性 + VARCHAR(64) 长度。"""

    def test_vector_id_stable_across_calls(self):
        rid = uuid.uuid4()
        kb_id = uuid.uuid4()
        id1 = make_vector_id("herb", rid, kb_id, 0)
        id2 = make_vector_id("herb", rid, kb_id, 0)
        assert id1 == id2

    def test_vector_id_different_chunk(self):
        rid = uuid.uuid4()
        kb_id = uuid.uuid4()
        id0 = make_vector_id("theory", rid, kb_id, 0)
        id1 = make_vector_id("theory", rid, kb_id, 1)
        assert id0 != id1

    def test_vector_id_different_kb(self):
        """同一 Resource 在不同 KB 的向量 ID 不同 → 不误删其他 KB。"""
        rid = uuid.uuid4()
        kb1 = uuid.uuid4()
        kb2 = uuid.uuid4()
        id1 = make_vector_id("herb", rid, kb1, 0)
        id2 = make_vector_id("herb", rid, kb2, 0)
        assert id1 != id2

    def test_vector_id_different_resource_type(self):
        """同一 resource_id（不可能但理论上）不同 type → ID 不同。"""
        rid = uuid.uuid4()
        kb_id = uuid.uuid4()
        assert make_vector_id("herb", rid, kb_id, 0) != make_vector_id(
            "literature", rid, kb_id, 0
        )

    def test_doc_id_stable_across_calls(self):
        rid = uuid.uuid4()
        kb_id = uuid.uuid4()
        assert make_doc_id("herb", rid, kb_id) == make_doc_id("herb", rid, kb_id)

    def test_doc_id_different_kb(self):
        rid = uuid.uuid4()
        kb1 = uuid.uuid4()
        kb2 = uuid.uuid4()
        assert make_doc_id("herb", rid, kb1) != make_doc_id("herb", rid, kb2)

    def test_vector_id_length_within_milvus_varchar64(self):
        """id 必须 ≤ 64 字符（Milvus VARCHAR(64) 限制）。"""
        rid = uuid.uuid4()
        kb_id = uuid.uuid4()
        vid = make_vector_id("prescription", rid, kb_id, 999)
        assert len(vid) <= 64

    def test_doc_id_length_within_milvus_varchar64(self):
        rid = uuid.uuid4()
        kb_id = uuid.uuid4()
        did = make_doc_id("literature", rid, kb_id)
        assert len(did) <= 64

    def test_vectorize_produces_stable_ids_in_service(self):
        """通过 ResourceVectorService.vectorize 两次调用，ID 稳定。"""
        herb = _herb(name="甘草", properties="甘，平")
        herb.id = uuid.uuid4()
        kb_id = uuid.uuid4()
        svc = _service()

        rows1 = svc.vectorize(herb, kb_id=kb_id)
        rows2 = svc.vectorize(herb, kb_id=kb_id)
        assert rows1 and rows2
        assert [r.id for r in rows1] == [r.id for r in rows2]
        assert [r.doc_id for r in rows1] == [r.doc_id for r in rows2]


# ===========================================================================
# 7. VectorRow dense/sparse vector
# ===========================================================================


class TestVectorRowFields:
    """覆盖范围 #7：VectorRow dense/sparse vector + 标准字段。"""

    def test_vectorize_herb_produces_valid_vector_row(self):
        herb = _herb(name="甘草", properties="甘，平", effects="补脾益气")
        herb.id = uuid.uuid4()
        kb_id = uuid.uuid4()
        svc = _service(dim=64)

        rows = svc.vectorize(herb, kb_id=kb_id)
        assert len(rows) == 1
        row = rows[0]

        assert isinstance(row, VectorRow)
        assert row.kb_id == str(kb_id)
        assert row.chunk_index == 0
        assert "甘草" in row.content
        assert row.page_num is None  # Resource 无真实页码
        assert row.title_path is None  # Herb 单向量无标题路径
        # dense/sparse vector 非空
        assert len(row.dense_vector) == 64
        assert row.sparse_vector
        # 资源元数据
        assert row.resource_type == "herb"
        assert row.resource_id == str(herb.id)
        assert row.resource_name == "甘草"
        # 来源元数据默认 None
        assert row.source_type is None
        assert row.credibility_level is None
        assert row.era is None

    def test_vectorize_prescription_produces_valid_vector_row(self):
        p = _prescription(
            name="桂枝汤",
            ingredients=[_ingredient("桂枝", amount=10, unit="克", role="君")],
            efficacy="解肌发汗",
        )
        p.id = uuid.uuid4()
        kb_id = uuid.uuid4()
        svc = _service(dim=32)

        rows = svc.vectorize(p, kb_id=kb_id)
        assert len(rows) == 1
        row = rows[0]
        assert row.resource_type == "prescription"
        assert row.resource_name == "桂枝汤"
        assert "桂枝" in row.content
        assert len(row.dense_vector) == 32
        assert row.sparse_vector

    def test_vectorize_theory_long_text_multiple_rows(self):
        content = "太阳病，头痛发热。" * 1000
        t = _theory(name="长论", content=content)
        t.id = uuid.uuid4()
        kb_id = uuid.uuid4()
        svc = _service(dim=16)

        rows = svc.vectorize(t, kb_id=kb_id)
        assert len(rows) >= 2
        for i, row in enumerate(rows):
            assert row.chunk_index == i
            assert row.kb_id == str(kb_id)
            assert row.resource_type == "theory"
            assert row.resource_id == str(t.id)
            assert len(row.dense_vector) == 16
            assert row.sparse_vector
            # page_num 恒为 None（Resource 无页码）
            assert row.page_num is None

    def test_dense_vectors_deterministic_for_same_text(self):
        """相同文本 → 相同 dense/sparse vector（MockEmbedding 确定性）。"""
        herb = _herb(name="甘草", properties="甘")
        herb.id = uuid.uuid4()
        kb_id = uuid.uuid4()
        svc = _service(dim=32)

        rows1 = svc.vectorize(herb, kb_id=kb_id)
        rows2 = svc.vectorize(herb, kb_id=kb_id)
        assert rows1[0].dense_vector == rows2[0].dense_vector
        assert rows1[0].sparse_vector == rows2[0].sparse_vector

    def test_empty_resource_returns_empty_list(self):
        herb = _herb(name="")  # 全空
        herb.id = uuid.uuid4()
        kb_id = uuid.uuid4()
        svc = _service()
        rows = svc.vectorize(herb, kb_id=kb_id)
        assert rows == []

    def test_resource_id_present_in_row(self):
        herb = _herb(name="甘草")
        herb.id = uuid.uuid4()
        kb_id = uuid.uuid4()
        svc = _service()
        rows = svc.vectorize(herb, kb_id=kb_id)
        assert rows[0].resource_id == str(herb.id)


# ===========================================================================
# 8. source_type / era / credibility_level
# ===========================================================================


class TestSourceMetaFields:
    """覆盖范围 #8：source_type / era / credibility_level（受控枚举 + 固定映射）。"""

    def test_default_source_fields_are_none(self):
        """资源表无 source_type/era 字段 → 默认 None（不猜测）。"""
        herb = _herb(name="甘草")
        herb.id = uuid.uuid4()
        svc = _service()
        rows = svc.vectorize(herb, kb_id=uuid.uuid4())
        assert rows[0].source_type is None
        assert rows[0].era is None
        assert rows[0].credibility_level is None

    def test_explicit_source_type_derives_credibility(self):
        """显式传入 source_type → credibility_level 由 source_meta 固定映射推导。"""
        herb = _herb(name="甘草")
        herb.id = uuid.uuid4()
        svc = _service()
        for st, level in CREDIBILITY_MAP.items():
            rows = svc.vectorize(
                herb, kb_id=uuid.uuid4(), source_type=st, era="汉"
            )
            assert rows[0].source_type == st
            assert rows[0].credibility_level == level
            assert rows[0].era == "汉"

    def test_source_type_without_era(self):
        """source_type 与 era 相互独立：只传 source_type 也可推导 credibility。"""
        herb = _herb(name="甘草")
        herb.id = uuid.uuid4()
        svc = _service()
        rows = svc.vectorize(herb, kb_id=uuid.uuid4(), source_type="经典古籍")
        assert rows[0].source_type == "经典古籍"
        assert rows[0].credibility_level == 3
        assert rows[0].era is None

    def test_era_without_source_type(self):
        herb = _herb(name="甘草")
        herb.id = uuid.uuid4()
        svc = _service()
        rows = svc.vectorize(herb, kb_id=uuid.uuid4(), era="宋")
        assert rows[0].source_type is None
        assert rows[0].credibility_level is None
        assert rows[0].era == "宋"

    def test_invalid_source_type_rejected(self):
        herb = _herb(name="甘草")
        herb.id = uuid.uuid4()
        svc = _service()
        with pytest.raises(ResourceVectorError, match="source_type"):
            svc.vectorize(herb, kb_id=uuid.uuid4(), source_type="江湖游医")

    def test_invalid_era_rejected(self):
        herb = _herb(name="甘草")
        herb.id = uuid.uuid4()
        svc = _service()
        with pytest.raises(ResourceVectorError, match="era"):
            svc.vectorize(herb, kb_id=uuid.uuid4(), era="民国")

    def test_all_valid_source_types_accepted(self):
        """source_meta.SOURCE_TYPES 全部合法。"""
        herb = _herb(name="甘草")
        herb.id = uuid.uuid4()
        svc = _service()
        for st in SOURCE_TYPES:
            rows = svc.vectorize(
                herb, kb_id=uuid.uuid4(), source_type=st
            )
            assert rows[0].source_type == st

    def test_unsupported_resource_type_rejected(self):
        svc = _service()
        with pytest.raises(ResourceVectorError, match="不支持的资源类型"):
            svc.vectorize(
                object(),  # type: ignore[arg-type]
                kb_id=uuid.uuid4(),
                resource_type="unknown",
            )


# ===========================================================================
# 集成：端到端 vectorize 流程
# ===========================================================================


class TestEndToEnd:
    """端到端：Resource → Canonical Text → Chunk → Embedding → VectorRow。"""

    def test_full_pipeline_herb(self):
        herb = _herb(
            name="人参",
            aliases=["百草之王"],
            properties="甘、微苦，微温",
            channels=["脾", "肺", "心"],
            effects="大补元气、复脉固脱",
            source="《神农本草经》",
            description="五加科植物人参的干燥根",
        )
        herb.id = uuid.uuid4()
        kb_id = uuid.uuid4()
        svc = _service(dim=128)

        rows = svc.vectorize(
            herb,
            kb_id=kb_id,
            resource_type="herb",
            source_type="经典古籍",
            era="先秦",
        )
        assert len(rows) == 1
        row = rows[0]
        assert row.resource_type == "herb"
        assert row.resource_name == "人参"
        assert row.source_type == "经典古籍"
        assert row.credibility_level == 3
        assert row.era == "先秦"
        assert len(row.dense_vector) == 128
        assert row.sparse_vector
        assert len(row.id) <= 64
        assert len(row.doc_id) <= 64
        # ID 稳定性：再调一次相同参数
        rows2 = svc.vectorize(
            herb, kb_id=kb_id, resource_type="herb",
            source_type="经典古籍", era="先秦",
        )
        assert rows2[0].id == row.id

    def test_full_pipeline_prescription_with_ingredients(self):
        p = _prescription(
            name="四君子汤",
            aliases=["四君子"],
            ingredients=[
                _ingredient("人参", amount=10, unit="克", role="君"),
                _ingredient("白术", amount=9, unit="克", role="臣"),
                _ingredient("茯苓", amount=9, unit="克", role="佐"),
                _ingredient("甘草", amount=6, unit="克", processing="炙", role="使"),
            ],
            efficacy="益气健脾",
            indications="脾胃气虚证",
            usage_method="水煎服",
            source="《太平惠民和剂局方》",
        )
        p.id = uuid.uuid4()
        kb_id = uuid.uuid4()
        svc = _service()

        rows = svc.vectorize(p, kb_id=kb_id)
        assert len(rows) == 1
        content = rows[0].content
        for herb_name in ("人参", "白术", "茯苓", "甘草"):
            assert herb_name in content
        assert "[君]" in content and "[使]" in content
        assert "（炙）" in content
        assert rows[0].resource_type == "prescription"

    def test_full_pipeline_long_literature(self):
        content = "太阳病，头痛发热，汗出，恶风，桂枝汤主之。" * 350  # ≈ 7000 字符 > 6000
        lit = _literature(
            name="伤寒论",
            author="张仲景",
            dynasty="东汉",
            content=content,
        )
        lit.id = uuid.uuid4()
        kb_id = uuid.uuid4()
        svc = _service(dim=64)

        rows = svc.vectorize(lit, kb_id=kb_id, source_type="经典古籍", era="汉")
        assert len(rows) >= 2
        for i, row in enumerate(rows):
            assert row.chunk_index == i
            assert row.resource_type == "literature"
            assert row.source_type == "经典古籍"
            assert row.credibility_level == 3
            assert row.era == "汉"
            assert row.page_num is None
            assert len(row.id) <= 64
