"""阶段十三：知识图谱（KG）专项测试。

覆盖（需求 §12）：
1   Node 创建（资源 → 节点）
2   Edge 创建（contains / records / related_to）
3   重复构建不重复（幂等）+ rebuild
4   Herb ↔ Prescription 关系（contains 双向可查）
5   Literature → Resource 关系（records）
6   KG 查询（KgRetriever 由图遍历产出关系事实）
7   KG Evidence（进入统一 Evidence / 分组 / Citation）
8   KG + Vector 混合（同一策略下两类证据并存）
9   kg_enhanced Router（实体关系查询 → kg_enhanced）
10  普通策略不受影响（阶段十二六类映射不变）
11  /chat/ask（router_decision=kg_enhanced + kg_evidence）
12  /chat/ask-stream（事件名与顺序不变，citations 事件带 kg_evidence）
13  Citation / Evidence 兼容（KG 证据含全部旧字段）
14  fallback（KG 检索异常不影响问答）

说明：
- 单元测试不依赖任何外部中间件；
- 集成测试使用真实 PostgreSQL（PG_AVAILABLE 开关），向量侧沿用项目既有
  MockEmbedding / InMemoryVectorStore 方式，**未连接真实 Milvus**，
  不伪造真实 Milvus 结果。
"""

import asyncio
import json
import re
import uuid
from decimal import Decimal

import pytest
from sqlalchemy import delete, select

from src.application.dynamic_router import (
    ROUTER_VERSION,
    DynamicRouter,
)
from src.application.evaluation_service import EvaluationService
from src.application.evidence import (
    SOURCE_KIND_KG,
    hit_to_evidence,
    package_evidence,
)
from src.application.kg_retrieval import KG_SOURCE_KIND, KgRetriever
from src.application.kg_service import KgService
from src.application.query_analyzer import analyze_query
from src.application.rag_service import RagService
from src.application.retrieval_strategies import (
    BASELINE_STRATEGY,
    STRATEGIES,
    get_strategy,
    resolve_retrieval_config,
)
from src.domain.models import (
    KG_PROVENANCE_INGREDIENT,
    KG_PROVENANCE_SHARED_TAG,
    KG_PROVENANCE_SOURCE,
    KG_RELATION_CONTAINS,
    KG_RELATION_RELATED_TO,
    KG_RELATION_RECORDS,
    Herb,
    KnowledgeBaseResource,
    KgEdge,
    KgNode,
    Literature,
    Prescription,
    PrescriptionIngredient,
    Tag,
    Theory,
    herb_tags,
    theory_tags,
)
from tests.test_chat import _upload_doc
from tests.test_evaluation_experiment import _session, eval_kb  # noqa: F401
from tests.test_parse import _make_kb
from tests.test_upload import PG_AVAILABLE

_SUFFIX = uuid.uuid4().hex[:6]

HERB_A = f"金银花-{_SUFFIX}"
HERB_B = f"连翘-{_SUFFIX}"
PRESC = f"银翘散-{_SUFFIX}"
LITER = f"本草纲目-{_SUFFIX}"
THEORY = f"辛凉解表-{_SUFFIX}"

# 关系型/多实体问题（Router 选 kg_enhanced；KG 检索也以此为输入）
HERB_A_Q = f"{HERB_A}和{HERB_B}都能治疗什么？"


# ── 工具 ────────────────────────────────────────────────────────────────────


def _route(query: str):
    return DynamicRouter().route(analyze_query(query))


async def _seed(kb_id: uuid.UUID) -> dict:
    """写入一组资源并挂载到 KB（返回关键 id，供断言与清理）。"""
    async with _session() as db:
        herb_a = Herb(
            name=HERB_A, aliases=["忍冬"], properties="甘，寒",
            effects="清热解毒", source=f"《{LITER}》",
        )
        herb_b = Herb(name=HERB_B, properties="苦，微寒", effects="散结消肿")
        liter = Literature(name=LITER, author="李时珍", dynasty="明")
        presc = Prescription(name=PRESC, efficacy="辛凉透表", source="《温病条辨》")
        theory = Theory(name=THEORY, content="辛凉解表属治则体系")
        db.add_all([herb_a, herb_b, liter, presc, theory])
        await db.flush()

        db.add_all([
            PrescriptionIngredient(
                prescription_id=presc.id, herb_id=herb_a.id,
                amount=Decimal("9.00"), unit="克", role="君", sort_order=0,
            ),
            PrescriptionIngredient(
                prescription_id=presc.id, herb_id=herb_b.id,
                amount=Decimal("9.00"), unit="克", role="臣", sort_order=1,
            ),
        ])
        tag = Tag(name=f"清热-{_SUFFIX}")
        db.add(tag)
        await db.flush()
        # 共享标签（herb ↔ theory）→ related_to 的数据来源
        await db.execute(
            herb_tags.insert().values(herb_id=herb_a.id, tag_id=tag.id)
        )
        await db.execute(
            theory_tags.insert().values(theory_id=theory.id, tag_id=tag.id)
        )

        for rtype, rid in (
            ("herb", herb_a.id), ("herb", herb_b.id), ("prescription", presc.id),
            ("literature", liter.id), ("theory", theory.id),
        ):
            db.add(KnowledgeBaseResource(
                knowledge_base_id=kb_id, resource_type=rtype, resource_id=rid,
            ))
        await db.commit()
        return {
            "herb_a": herb_a.id, "herb_b": herb_b.id, "presc": presc.id,
            "liter": liter.id, "theory": theory.id, "tag": tag.id,
        }


async def _cleanup(ids: dict) -> None:
    """清理测试资源及其 KG 节点（边随 FK 级联删除）。"""
    async with _session() as db:
        await db.execute(
            delete(PrescriptionIngredient).where(
                PrescriptionIngredient.prescription_id == ids["presc"]
            )
        )
        await db.execute(
            delete(herb_tags).where(herb_tags.c.herb_id == ids["herb_a"])
        )
        await db.execute(
            delete(theory_tags).where(theory_tags.c.theory_id == ids["theory"])
        )
        await db.execute(delete(KgEdge))
        await db.execute(delete(KgNode))
        await db.execute(
            delete(KnowledgeBaseResource).where(
                KnowledgeBaseResource.resource_id.in_(list(ids.values()))
            )
        )
        for model, key in (
            (Herb, "herb_a"), (Herb, "herb_b"), (Prescription, "presc"),
            (Literature, "liter"), (Theory, "theory"), (Tag, "tag"),
        ):
            obj = await db.get(model, ids[key])
            if obj is not None:
                await db.delete(obj)
        await db.commit()


async def _build() -> None:
    """构建知识图谱（同步测试中通过 asyncio.run 调用）。"""
    async with _session() as db:
        await KgService(db).build()


# ── 1~2. Node / Edge 创建（集成，真实 PostgreSQL）────────────────────────────


@pytest.mark.skipif(not PG_AVAILABLE, reason="PostgreSQL 未启动")
async def test_build_creates_nodes_and_edges(client):
    """资源 → 节点；可靠业务关系 → 边（contains / records / related_to）。"""
    kb_id = uuid.UUID(_make_kb(client))
    ids = await _seed(kb_id)
    try:
        async with _session() as db:
            result = await KgService(db).build()
            assert result.nodes_created >= 5

            node = await db.scalar(
                select(KgNode).where(
                    KgNode.resource_type == "herb",
                    KgNode.resource_id == ids["herb_a"],
                )
            )
            assert node is not None
            assert node.node_type == "resource"
            assert node.name == HERB_A
            assert "忍冬" in node.aliases  # 别名用于实体匹配，不复制正文

            # contains：方剂 → 中药
            presc_node = await db.scalar(
                select(KgNode).where(
                    KgNode.resource_type == "prescription",
                    KgNode.resource_id == ids["presc"],
                )
            )
            contains = await db.scalar(
                select(KgEdge).where(
                    KgEdge.source_node_id == presc_node.id,
                    KgEdge.target_node_id == node.id,
                    KgEdge.relation_type == KG_RELATION_CONTAINS,
                )
            )
            assert contains is not None
            assert contains.provenance == KG_PROVENANCE_INGREDIENT
            assert "9克" in contains.description and "君" in contains.description
    finally:
        await _cleanup(ids)
        client.delete(f"/api/v1/kb/{kb_id}")


@pytest.mark.skipif(not PG_AVAILABLE, reason="PostgreSQL 未启动")
async def test_build_creates_records_and_related_to_edges(client):
    """records（文献 → 资源）与 related_to（共享标签）均来自真实业务数据。"""
    kb_id = uuid.UUID(_make_kb(client))
    ids = await _seed(kb_id)
    try:
        async with _session() as db:
            await KgService(db).build()

            liter_node = await db.scalar(
                select(KgNode).where(
                    KgNode.resource_type == "literature",
                    KgNode.resource_id == ids["liter"],
                )
            )
            herb_node = await db.scalar(
                select(KgNode).where(
                    KgNode.resource_type == "herb",
                    KgNode.resource_id == ids["herb_a"],
                )
            )
            records = await db.scalar(
                select(KgEdge).where(
                    KgEdge.source_node_id == liter_node.id,
                    KgEdge.target_node_id == herb_node.id,
                    KgEdge.relation_type == KG_RELATION_RECORDS,
                )
            )
            assert records is not None
            assert records.provenance == KG_PROVENANCE_SOURCE

            theory_node = await db.scalar(
                select(KgNode).where(
                    KgNode.resource_type == "theory",
                    KgNode.resource_id == ids["theory"],
                )
            )
            related = await db.scalar(
                select(KgEdge).where(
                    KgEdge.relation_type == KG_RELATION_RELATED_TO,
                    KgEdge.source_node_id == herb_node.id,
                    KgEdge.target_node_id == theory_node.id,
                )
            )
            assert related is not None
            assert related.provenance == KG_PROVENANCE_SHARED_TAG
    finally:
        await _cleanup(ids)
        client.delete(f"/api/v1/kb/{kb_id}")


# ── 3. 重复构建不重复 ────────────────────────────────────────────────────────


@pytest.mark.skipif(not PG_AVAILABLE, reason="PostgreSQL 未启动")
async def test_repeated_build_is_idempotent(client):
    """需求 §6：可重复执行、不产生重复节点/边、支持重建。"""
    kb_id = uuid.UUID(_make_kb(client))
    ids = await _seed(kb_id)
    try:
        async with _session() as db:
            svc = KgService(db)
            first = await svc.build()
            second = await svc.build()
            assert second.nodes_created == 0
            assert second.edges_created == 0
            assert second.node_count == first.node_count
            assert second.edge_count == first.edge_count

            third = await svc.build(rebuild=True)
            assert third.rebuilt is True
            assert third.node_count == first.node_count
            assert third.edge_count == first.edge_count
    finally:
        await _cleanup(ids)
        client.delete(f"/api/v1/kb/{kb_id}")


# ── 4~6. 关系查询与 KG Retrieval ────────────────────────────────────────────


@pytest.mark.skipif(not PG_AVAILABLE, reason="PostgreSQL 未启动")
async def test_kg_retrieval_finds_herb_prescription_relation(client):
    """Herb ↔ Prescription：contains 关系可从中药侧反查到方剂。"""
    kb_id = uuid.UUID(_make_kb(client))
    ids = await _seed(kb_id)
    try:
        async with _session() as db:
            await KgService(db).build()
            analysis = analyze_query(HERB_A_Q)
            hits = await KgRetriever(db).retrieve([kb_id], analysis, max_hops=1)
            assert hits
            titles = [h["title_path"] for h in hits]
            assert any(f"{PRESC} → 组成 → {HERB_A}" == t for t in titles)
            assert all(h["source_kind"] == KG_SOURCE_KIND for h in hits)
            # contains 来自方剂组成；共享标签关系可能同时出现（均为真实业务数据）
            assert any(h["kg_relation"] == KG_RELATION_CONTAINS for h in hits)
    finally:
        await _cleanup(ids)
        client.delete(f"/api/v1/kb/{kb_id}")


@pytest.mark.skipif(not PG_AVAILABLE, reason="PostgreSQL 未启动")
async def test_kg_retrieval_finds_literature_records_relation(client):
    """Literature → Resource：records 关系（资源出处命中文献名）。"""
    kb_id = uuid.UUID(_make_kb(client))
    ids = await _seed(kb_id)
    try:
        async with _session() as db:
            await KgService(db).build()
            analysis = analyze_query(f"{HERB_A}在《{LITER}》中有什么记载？")
            hits = await KgRetriever(db).retrieve([kb_id], analysis, max_hops=1)
            assert hits
            assert any(
                h["kg_relation"] == KG_RELATION_RECORDS and LITER in h["content"]
                for h in hits
            )
    finally:
        await _cleanup(ids)
        client.delete(f"/api/v1/kb/{kb_id}")


@pytest.mark.skipif(not PG_AVAILABLE, reason="PostgreSQL 未启动")
async def test_kg_retrieval_respects_kb_scope_and_no_match(client):
    """权限隔离：未挂载资源的 KB 查不到 KG 证据；无实体时不查图。"""
    kb_id = uuid.UUID(_make_kb(client))
    other_kb = uuid.UUID(_make_kb(client))
    ids = await _seed(kb_id)
    try:
        async with _session() as db:
            await KgService(db).build()
            analysis = analyze_query(HERB_A_Q)

            assert await KgRetriever(db).retrieve([other_kb], analysis) == []
            assert await KgRetriever(db).retrieve([], analysis) == []

            # 无实体（普通属性问题）→ 不做图遍历
            plain = analyze_query("公司实行什么工时制度？")
            assert await KgRetriever(db).retrieve([kb_id], plain) == []
    finally:
        await _cleanup(ids)
        client.delete(f"/api/v1/kb/{kb_id}")
        client.delete(f"/api/v1/kb/{other_kb}")


# ── 7. KG Evidence 进入统一结构（单元）──────────────────────────────────────


def test_kg_hit_becomes_unified_evidence():
    """KG 命中 → 同一套 Evidence（不创建第二套结构）。"""
    hit = {
        "id": "kg:edge-1",
        "doc_id": "kg:edge-1",
        "kb_id": None,
        "content": "银翘散 组成 金银花（9克；君）",
        "page_num": None,
        "title_path": "银翘散 → 组成 → 金银花",
        "score": 0.9,
        "dense_score": 0.9,
        "source_kind": KG_SOURCE_KIND,
        "resource_type": "herb",
        "resource_id": "r-1",
        "resource_name": "金银花",
        "doc_name": "金银花",
        "kg_relation": KG_RELATION_CONTAINS,
        "kg_hop": 1,
    }
    ev = hit_to_evidence(hit, 1)
    # 旧 Citation 字段全部保留
    for field in ("chunk_id", "source_index", "doc_id", "doc_name", "page_num",
                  "title_path", "content", "score", "source_kind", "evidence_level"):
        assert field in ev, f"缺失兼容字段 {field}"
    # 阶段十统一 Evidence 字段
    for field in ("evidence_id", "source_id", "source_name", "source_label",
                  "evidence_text"):
        assert field in ev, f"缺失 Evidence 字段 {field}"
    assert ev["source_kind"] == SOURCE_KIND_KG
    assert ev["source_label"] == "图谱·中药"
    assert ev["resource_type"] == "herb"

    groups, summary = package_evidence([ev])
    assert [g["group_key"] for g in groups] == ["kg:herb"]
    assert groups[0]["source_kind"] == KG_SOURCE_KIND
    assert summary["evidence_count"] == 1
    assert summary["group_count"] == 1


# ── 9~10. Router ────────────────────────────────────────────────────────────


def test_kg_enhanced_strategy_registered():
    """阶段十三新增策略：kg_enhanced（向量检索 + KG 补充证据）。"""
    strategy = get_strategy("kg_enhanced")
    assert strategy is not None
    cfg = resolve_retrieval_config(strategy)
    assert cfg["kg_enabled"] is True
    assert cfg["kg_max_hops"] >= 1 and cfg["kg_top_k"] >= 1
    # KG 是补充：向量检索参数与 Baseline 一致（不缩小召回）
    baseline = resolve_retrieval_config(get_strategy(BASELINE_STRATEGY))
    assert cfg["recall_top_k"] == baseline["recall_top_k"]
    assert cfg["hyde_enabled"] == baseline["hyde_enabled"]
    # 其余策略默认关闭 KG（阶段十二行为不变）
    for name, s in STRATEGIES.items():
        if name != "kg_enhanced":
            assert s.kg_enabled is False, f"{name} 不应默认启用 KG"


def test_router_selects_kg_enhanced_for_relation_query():
    """明显实体关系查询（多实体 + herb/prescription）→ kg_enhanced。"""
    d = _route("麻杏石甘汤与银翘散的区别是什么？")
    assert d.strategy_name == "kg_enhanced"
    assert d.question_type == "prescription"
    assert d.router_version == ROUTER_VERSION
    assert "entities=" in d.reason
    assert d.is_valid is True

    herb_d = _route("金银花和连翘都能治疗什么？")
    assert herb_d.strategy_name == "kg_enhanced"


def test_original_strategies_not_affected_by_kg():
    """阶段十二既有的六类映射保持不变（普通问题不走 KG）。"""
    got = {
        analyze_query(q).question_type: _route(q).strategy_name
        for q in (
            "金银花有什么功效？",
            "银翘散由哪些药物组成？",
            "什么是阴阳五行学说？",
            "《伤寒论》的成书背景是什么？",
            "金银花在《本草纲目》中有什么记载？",
            "公司实行什么工时制度？",
        )
    }
    assert got == {
        "herb": "herb_focused",
        "prescription": "prescription_focused",
        "theory": "theory_focused",
        "literature": "literature_focused",
        "multi_source": "multi_source",
        "general": BASELINE_STRATEGY,
    }


def test_kg_router_rule_is_deterministic():
    """同输入 → 同策略（不允许随机路由）。"""
    q = "麻杏石甘汤与银翘散的区别是什么？"
    analysis = analyze_query(q)
    runs = [DynamicRouter().route(analysis) for _ in range(3)]
    assert all(r.strategy_name == "kg_enhanced" for r in runs)
    assert all(r.to_dict() == runs[0].to_dict() for r in runs)


def test_kg_not_selected_for_fallback_or_unanswerable():
    """兜底分析 / unanswerable candidate 不启用 KG（仍需正常路由）。"""
    from src.application.query_analyzer import fallback_analysis

    fallback = fallback_analysis("任意问题", "unit_test")
    assert DynamicRouter().route(fallback).strategy_name == BASELINE_STRATEGY

    unanswerable = analyze_query("明天上海的股票涨跌如何？")
    assert unanswerable.is_unanswerable_candidate is True
    assert DynamicRouter().route(unanswerable).strategy_name == BASELINE_STRATEGY


# ── 8. KG + Vector 混合（集成）──────────────────────────────────────────────


@pytest.mark.skipif(not PG_AVAILABLE, reason="PostgreSQL 未启动")
async def test_kg_and_vector_hits_coexist(client):
    """kg_enhanced：向量命中保留，KG 命中作为补充证据并入。"""
    kb_id = uuid.UUID(_make_kb(client))
    ids = await _seed(kb_id)
    try:
        _upload_doc(client, str(kb_id), "图谱测试文档.txt")
        async with _session() as db:
            await KgService(db).build()
            svc = RagService(db)
            question = HERB_A_Q

            kg_answer_hits = await svc._retrieve(
                [kb_id], question,
                strategy=get_strategy("kg_enhanced"),
                analysis=analyze_query(question),
            )
            kinds = {h.get("source_kind") for h in kg_answer_hits}
            assert "kg" in kinds
            assert "document" in kinds or "resource" in kinds  # 向量命中未丢失

            # Baseline 策略不带 KG（行为与阶段十二一致）
            baseline_hits = await svc._retrieve([kb_id], question)
            assert not [h for h in baseline_hits if h.get("source_kind") == "kg"]
    finally:
        await _cleanup(ids)
        client.delete(f"/api/v1/kb/{kb_id}")


# ── 11~13. /chat/ask 与 /chat/ask-stream ────────────────────────────────────


class _CiteAllLLM:
    """测试用 LLM：为 Prompt 中每个 [n] 资料块输出 [citation: n, 0]。"""

    async def chat(self, messages):  # noqa: ANN001
        prompt = messages[-1]["content"]
        idxs = re.findall(r"^\[(\d+)\]", prompt, flags=re.MULTILINE)
        return "图谱回答" + "".join(f"[citation: {i}, 0]" for i in idxs[:10])

    async def chat_stream(self, messages):  # noqa: ANN001
        yield "图谱回答"


def _parse_sse(text: str) -> list[tuple[str, dict]]:
    events: list[tuple[str, dict]] = []
    event = ""
    data = ""
    for line in text.split("\n"):
        if line.startswith("event: "):
            event = line[7:].strip()
        elif line.startswith("data: "):
            data += line[6:]
        elif line == "":
            if event and data:
                events.append((event, json.loads(data)))
            event = ""
            data = ""
    return events


@pytest.mark.skipif(not PG_AVAILABLE, reason="PostgreSQL 未启动")
def test_chat_ask_returns_kg_evidence(client, monkeypatch):
    """/chat/ask：旧字段全在，新增 router_decision=kg_enhanced 与 kg_evidence。"""
    import src.application.rag_service as rag_mod

    kb_id = _make_kb(client)
    ids = asyncio.run(_seed(uuid.UUID(kb_id)))
    asyncio.run(_build())
    try:
        monkeypatch.setattr(rag_mod, "get_llm", lambda config: _CiteAllLLM())

        resp = client.post(
            "/api/v1/chat/ask",
            json={"question": HERB_A_Q, "kb_ids": [kb_id]},
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()

        for key in ("answer", "citations", "evidence", "evidence_groups",
                    "evidence_summary", "query_analysis", "router_decision",
                    "kg_evidence"):
            assert key in data, f"缺失字段 {key}"

        assert data["router_decision"]["strategy_name"] == "kg_enhanced"
        assert data["kg_evidence"], "KG 证据未进入响应"
        assert all(
            c["source_kind"] == SOURCE_KIND_KG for c in data["kg_evidence"]
        )
        assert any(g["group_key"].startswith("kg:") for g in data["evidence_groups"])
        assert data["answer"]
    finally:
        asyncio.run(_cleanup(ids))
        client.delete(f"/api/v1/kb/{kb_id}")


@pytest.mark.skipif(not PG_AVAILABLE, reason="PostgreSQL 未启动")
def test_chat_ask_stream_keeps_event_order_with_kg(client, monkeypatch):
    """/chat/ask-stream：事件名与顺序不变，citations 事件附带 kg_evidence。"""
    kb_id = _make_kb(client)
    ids = asyncio.run(_seed(uuid.UUID(kb_id)))
    asyncio.run(_build())
    try:
        resp = client.post(
            "/api/v1/chat/ask-stream",
            json={"question": HERB_A_Q, "kb_ids": [kb_id]},
        )
        assert resp.status_code == 200, resp.text
        events = _parse_sse(resp.text)
        names = [e for e, _ in events]
        assert names[0] == "start"
        assert names[1] == "citations"
        assert names[-1] == "done"

        start = events[0][1]
        assert start["router_decision"]["strategy_name"] == "kg_enhanced"
        assert start["query_analysis"]["question_type"] == "herb"

        citations_payload = events[1][1]
        assert citations_payload["citations"]
        assert "evidence_groups" in citations_payload
        assert "kg_evidence" in citations_payload
        assert citations_payload["kg_evidence"]
    finally:
        asyncio.run(_cleanup(ids))
        client.delete(f"/api/v1/kb/{kb_id}")


@pytest.mark.skipif(not PG_AVAILABLE, reason="PostgreSQL 未启动")
def test_kg_citation_keeps_legacy_structure(client, monkeypatch):
    """KG 证据进入 Citation 后，旧字段与 Evidence 等价关系不变。"""
    import src.application.rag_service as rag_mod

    kb_id = _make_kb(client)
    ids = asyncio.run(_seed(uuid.UUID(kb_id)))
    asyncio.run(_build())
    try:
        monkeypatch.setattr(rag_mod, "get_llm", lambda config: _CiteAllLLM())
        data = client.post(
            "/api/v1/chat/ask",
            json={"question": HERB_A_Q, "kb_ids": [kb_id]},
        ).json()

        legacy = ("chunk_id", "source_index", "doc_id", "doc_name", "page_num",
                  "title_path", "content", "score", "source_kind", "evidence_level")
        stage10 = ("evidence_id", "source_id", "source_name", "source_label",
                   "evidence_text")
        for c in data["citations"]:
            for field in legacy + stage10:
                assert field in c, f"缺失 {field}"
        # 阶段十约定：evidence 与 citations 同源同内容
        assert data["evidence"] == data["citations"]
        kg_cits = [c for c in data["citations"] if c["source_kind"] == SOURCE_KIND_KG]
        assert kg_cits
        assert kg_cits[0]["doc_name"]  # 注入的资源名
    finally:
        asyncio.run(_cleanup(ids))
        client.delete(f"/api/v1/kb/{kb_id}")


# ── 14. fallback ────────────────────────────────────────────────────────────


@pytest.mark.skipif(not PG_AVAILABLE, reason="PostgreSQL 未启动")
def test_kg_failure_does_not_break_chat(client, monkeypatch):
    """KG 检索异常 → 吞掉异常，问答继续（KG 不是单点故障）。"""
    import src.application.rag_service as rag_mod
    from src.application.kg_retrieval import KgRetriever

    kb_id = _make_kb(client)
    ids = asyncio.run(_seed(uuid.UUID(kb_id)))
    asyncio.run(_build())
    try:
        async def boom(*_args, **_kwargs):
            raise RuntimeError("kg down")

        monkeypatch.setattr(KgRetriever, "retrieve", boom)
        monkeypatch.setattr(rag_mod, "get_llm", lambda config: _CiteAllLLM())

        resp = client.post(
            "/api/v1/chat/ask",
            json={"question": HERB_A_Q, "kb_ids": [kb_id]},
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        # 策略仍是 kg_enhanced，但 KG 证据为空且问答正常完成
        assert data["router_decision"]["strategy_name"] == "kg_enhanced"
        assert data["kg_evidence"] == []
        assert data["answer"]
    finally:
        asyncio.run(_cleanup(ids))
        client.delete(f"/api/v1/kb/{kb_id}")


# ── API ─────────────────────────────────────────────────────────────────────


@pytest.mark.skipif(not PG_AVAILABLE, reason="PostgreSQL 未启动")
async def test_evaluation_supports_kg_strategy(client, eval_kb, monkeypatch):
    """需求 §11：复用阶段九 EvaluationRun.retrieval_strategy，kg_enhanced 可直接归档。"""
    from tests.test_evaluation_experiment import _case

    recorded: list[dict] = []

    async def _rec(self, kb_ids, question, history=None, strategy=None,
                  resource_types=None, analysis=None):
        recorded.append({
            "strategy": strategy.name if strategy else None,
            "has_analysis": analysis is not None,
        })
        return f"模拟答案：{question}", [
            {"content": f"模拟上下文：{question}", "score": 0.9}
        ]

    monkeypatch.setattr(RagService, "retrieve_and_answer", _rec)

    async with _session() as db:
        svc = EvaluationService(db)
        await svc.save_test_set(
            eval_kb,
            [_case(f"金银花有什么功效？-{uuid.uuid4().hex[:6]}", question_type="herb")],
            dataset_version="tcm-v1",
        )
        run, results = await svc.run(eval_kb, retrieval_strategy="kg_enhanced")

        assert run.retrieval_strategy == "kg_enhanced"
        assert results[0].retrieval_strategy == "kg_enhanced"
        assert recorded[0]["strategy"] == "kg_enhanced"
        # KG 策略必须由 QueryAnalysis 驱动（评测侧显式补算）
        assert recorded[0]["has_analysis"] is True
        assert run.config_snapshot["strategy"]["kg_enabled"] is True
        assert run.config_snapshot["router"]["router_version"] == ROUTER_VERSION


@pytest.mark.skipif(not PG_AVAILABLE, reason="PostgreSQL 未启动")
def test_kg_api_build_stats_relations_query(client):
    """KG 路由：build / stats / relations / query。"""
    kb_id = _make_kb(client)
    ids = asyncio.run(_seed(uuid.UUID(kb_id)))
    try:
        build = client.post("/api/v1/kg/build", json={"rebuild": False})
        assert build.status_code == 200, build.text
        assert build.json()["node_count"] >= 5

        stats = client.get("/api/v1/kg/stats")
        assert stats.status_code == 200
        assert stats.json()["node_count"] >= 5
        assert "by_resource_type" in stats.json()

        relations = client.get("/api/v1/kg/relations").json()
        values = {r["value"] for r in relations["relations"]}
        assert {KG_RELATION_CONTAINS, KG_RELATION_RECORDS,
                KG_RELATION_RELATED_TO} == values

        query = client.get(
            "/api/v1/kg/query",
            params={
                "question": HERB_A_Q,
                "kb_id": kb_id,
                "max_hops": 1,
            },
        )
        assert query.status_code == 200, query.text
        body = query.json()
        assert body["kg_hits"] >= 1
        assert body["evidence"]
        assert any(g["group_key"].startswith("kg:") for g in body["evidence_groups"])
    finally:
        asyncio.run(_cleanup(ids))
        client.delete(f"/api/v1/kb/{kb_id}")
