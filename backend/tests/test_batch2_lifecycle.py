"""第二轮 Batch 2（删除生命周期与证据可信性）回归测试。

覆盖：
- BUG-006 文档 purge 必须真正删除 Milvus 向量，且失败可见（不再静默残留）
- BUG-007 KB purge 必须清理该 KB 下全部向量（含 Document chunk）
- BUG-008 KG 多跳逐跳隔离（未挂载 / 他库资源不得成为事实验证）
- BUG-009 删除 Resource 同步清理 KgNode/KgEdge
- BUG-010 rebuild 原子化（中途失败不留下空图谱）
- BUG-045 卸载顺序：先删挂载再删向量（失败可回滚，不留"挂载在但向量没了"）
- BUG-046 重新向量化先插后删（写入失败不会让向量全灭）
- BUG-047 更新后重新向量化失败必须留痕，不能静默过期
- BUG-015 Reflection 引用编号按 source_index 解析（不与过滤后位序错位）
- BUG-016 Prompt / Evidence / Citation 三处编号统一

环境：PostgreSQL 用例需要 PG；向量侧沿用 conftest 注入的
InMemoryVectorStore（未连接真实 Milvus），不伪造 Milvus 结果。
"""

import asyncio
import uuid
from decimal import Decimal

import pytest
from sqlalchemy import select

from src.application.evidence import build_evidence, hit_to_evidence
from src.application.kg_retrieval import KgRetriever
from src.application.kg_service import KgService
from src.application.rag_service import RagService
from src.application.self_reflection import SelfReflection
from src.domain.models import (
    Herb,
    KnowledgeBaseResource,
    KgEdge,
    KgNode,
    Prescription,
    PrescriptionIngredient,
)
from src.infrastructure.embedding import MockEmbedding
from src.infrastructure.milvus_store import (
    InMemoryVectorStore,
    VectorRow,
    VectorStoreError,
)
from src.infrastructure.rerank import MockRerank
from tests.test_citation_evidence import _make_doc_hit, _mock_db
from tests.test_evaluation_experiment import _session
from tests.test_parse import _make_kb
from tests.test_upload import PG_AVAILABLE

pytestmark = pytest.mark.skipif(not PG_AVAILABLE, reason="PostgreSQL 未启动")

_SUFFIX = uuid.uuid4().hex[:6]


# ── 工具 ────────────────────────────────────────────────────────────────────


def _upload_txt(client, kb_id: str, name: str = "batch2.txt") -> str:
    text = "# 手册\n\n" + "\n\n".join(
        f"第{i}段，" + "内容" * 200 + "。" for i in range(3)
    )
    resp = client.post(
        "/api/v1/documents/upload",
        data={"kb_id": kb_id},
        files={"file": (name, text.encode("utf-8"), "text/plain")},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def _row(doc_id: str, chunk_id: str) -> VectorRow:
    return VectorRow(
        id=chunk_id,
        doc_id=doc_id,
        kb_id="kb-batch2",
        chunk_index=0,
        content="内容",
        page_num=None,
        title_path=None,
        dense_vector=[0.1] * 4,
        sparse_vector={1: 0.5},
    )


async def _kg_counts() -> tuple[int, int]:
    from sqlalchemy import func

    async with _session() as db:
        nodes = await db.scalar(select(func.count()).select_from(KgNode))
        edges = await db.scalar(select(func.count()).select_from(KgEdge))
    return int(nodes or 0), int(edges or 0)


# ── BUG-006：文档 purge 清理向量 + 失败可见 ──────────────────────────────────


def test_document_purge_deletes_vectors(client, vector_store):
    """purge 后该文档向量必须清零（旧实现调用了不存在的 delete_by_doc_id）。"""
    kb_id = _make_kb(client)
    doc_id = _upload_txt(client, kb_id)
    assert vector_store.count_by_doc(doc_id) > 0, "上传后应有向量"

    resp = client.delete(f"/api/v1/documents/{doc_id}/purge")
    assert resp.status_code == 200, resp.text
    # Milvus 的删除在 flush 前对查询不可见（本部署实测：no-flush=3 / flush=0），
    # 统计前显式 flush，避免把"删除尚未落盘"误判为"清理失败"。
    vector_store.flush()
    assert vector_store.count_by_doc(doc_id) == 0, "purge 后向量应被清理"


def test_document_purge_fails_loud_when_vector_cleanup_fails(
    client, vector_store, monkeypatch
):
    """向量清理失败必须 422 且保留 PG 记录：不允许留下孤儿向量。"""
    kb_id = _make_kb(client)
    doc_id = _upload_txt(client, kb_id)

    def _boom(_doc_id: str) -> None:
        raise VectorStoreError("Milvus 不可用（测试注入）")

    monkeypatch.setattr(vector_store, "delete_by_doc", _boom)

    resp = client.delete(f"/api/v1/documents/{doc_id}/purge")
    assert resp.status_code == 422, resp.text
    # PG 记录仍在（未产生"PG 已删但向量残留"）
    assert client.get(f"/api/v1/documents/{doc_id}").status_code == 200


# ── BUG-007：KB purge 清理该 KB 下全部向量 ───────────────────────────────────


def test_kb_purge_deletes_document_vectors(client, vector_store):
    """KB purge 后该 KB 下 Document chunk 向量必须清零（旧实现只清 Resource）。"""
    kb_id = _make_kb(client)
    doc_id = _upload_txt(client, kb_id)
    assert vector_store.count_by_doc(doc_id) > 0

    assert client.delete(f"/api/v1/kb/{kb_id}").status_code == 200
    resp = client.delete(f"/api/v1/kb/{kb_id}/purge")
    assert resp.status_code == 200, resp.text
    vector_store.flush()  # 同上：删除需落盘后才对查询可见
    assert vector_store.count_by_doc(doc_id) == 0, "KB purge 后文档向量应被清理"


def test_kb_purge_fails_loud_when_vector_cleanup_fails(
    client, vector_store, monkeypatch
):
    """KB 向量清理失败 → 422，KB 不被删除（避免不可逆且残留）。"""
    kb_id = _make_kb(client)

    def _boom(_kb_id: str) -> None:
        raise VectorStoreError("Milvus 不可用（测试注入）")

    monkeypatch.setattr(vector_store, "delete_by_kb", _boom)

    assert client.delete(f"/api/v1/kb/{kb_id}").status_code == 200  # 软删进回收站
    resp = client.delete(f"/api/v1/kb/{kb_id}/purge")
    assert resp.status_code == 422, resp.text
    trash = client.get("/api/v1/kb/trash").json()
    assert any(item["id"] == kb_id for item in trash), "KB 应仍在回收站"


# ── BUG-008：KG 多跳逐跳隔离 ────────────────────────────────────────────────


async def _seed_presc_only_mounted(client) -> dict:
    """写入 方剂→(中药A, 中药B) 的 contains 关系，只把方剂挂载到 KB。"""
    kb_id = uuid.UUID(_make_kb(client))
    async with _session() as db:
        herb_a = Herb(name=f"隔离药A-{_SUFFIX}", effects="清热")
        herb_b = Herb(name=f"隔离药B-{_SUFFIX}", effects="解毒")
        presc = Prescription(name=f"隔离方-{_SUFFIX}", efficacy="辛凉透表")
        db.add_all([herb_a, herb_b, presc])
        await db.flush()
        db.add_all(
            [
                PrescriptionIngredient(
                    prescription_id=presc.id, herb_id=herb_a.id,
                    amount=Decimal("9.00"), unit="克", role="君", sort_order=0,
                ),
                PrescriptionIngredient(
                    prescription_id=presc.id, herb_id=herb_b.id,
                    amount=Decimal("9.00"), unit="克", role="臣", sort_order=1,
                ),
            ]
        )
        # 只挂载方剂：两味中药均未挂载（甚至可能在别的用户库里）
        db.add(
            KnowledgeBaseResource(
                knowledge_base_id=kb_id,
                resource_type="prescription",
                resource_id=presc.id,
            )
        )
        await db.commit()
        ids = {
            "kb_id": kb_id,
            "herb_a": herb_a.id,
            "herb_b": herb_b.id,
            "presc": presc.id,
            "presc_name": presc.name,
        }

    async def _build() -> None:
        async with _session() as db:
            await KgService(db).build()

    await _build()
    return ids


async def _cleanup_presc(ids: dict) -> None:
    from sqlalchemy import delete as _delete

    async with _session() as db:
        await db.execute(_delete(KgEdge))
        await db.execute(_delete(KgNode))
        await db.execute(
            _delete(PrescriptionIngredient).where(
                PrescriptionIngredient.prescription_id == ids["presc"]
            )
        )
        await db.execute(
            _delete(KnowledgeBaseResource).where(
                KnowledgeBaseResource.knowledge_base_id == ids["kb_id"]
            )
        )
        for model, key in (
            (Prescription, "presc"), (Herb, "herb_a"), (Herb, "herb_b"),
        ):
            obj = await db.get(model, ids[key])
            if obj is not None:
                await db.delete(obj)
        await db.commit()


def test_kg_multihop_respects_mount_isolation(client, monkeypatch):
    """未挂载的资源不得通过多跳进入事实集（旧实现只过滤第 1 跳种子）。"""
    from src.application.query_analyzer import QueryAnalysis

    ids = asyncio.run(_seed_presc_only_mounted(client))
    try:
        mounted = {("prescription", ids["presc"])}
        # 直接构造分析结论，避免依赖实体抽取规则（本用例只验证挂载隔离）
        analysis = QueryAnalysis(
            query=f"{ids['presc_name']}由哪些中药组成？",
            question_type="relation",
            question_type_label="关系",
            resource_types=["prescription"],
            entities=[{"text": ids["presc_name"], "type": "prescription"}],
        )

        async def _run() -> tuple[list, dict, int]:
            async with _session() as db:
                # 前置校验：图谱里确实存在指向未挂载中药的边（过滤器真的在工作）
                presc_nodes = (
                    await db.scalars(
                        select(KgNode).where(
                            KgNode.resource_type == "prescription",
                            KgNode.resource_id == ids["presc"],
                        )
                    )
                ).all()
                assert presc_nodes, "方剂节点应已构建"
                neighbors = await KgService(db).edges_of([presc_nodes[0].id])
                assert neighbors, "方剂应有 contains 出边（指向未挂载中药）"
                # 种子可匹配（避免本用例因"没匹配到任何实体"而空转）
                matches = await KgService(db).match_nodes(
                    [ids["presc_name"]], resource_types=["prescription"], limit=10
                )
                assert matches, "种子应能匹配到方剂节点"

                facts, nodes = await KgRetriever(db).facts(
                    [ids["kb_id"]], analysis, max_hops=2
                )
                return facts, nodes, len(neighbors)

        facts, nodes, neighbor_count = asyncio.run(_run())
        assert neighbor_count > 0
        # 方剂的全部出边都指向未挂载中药 → 逐跳隔离下不应产出任何事实
        assert facts == [], "未挂载资源的边被当成事实验证（跨 KB 泄露）"
        for f in facts:
            for nid in (f.source_node_id, f.target_node_id):
                node = nodes.get(nid)
                assert node is not None
                assert (
                    node.resource_type,
                    node.resource_id,
                ) in mounted, "事实引用了未挂载资源（跨 KB 泄露）"
        # 未挂载的中药绝不应出现在证据节点里
        assert ids["herb_b"] not in {
            n.resource_id for n in nodes.values() if n.resource_type == "herb"
        }
    finally:
        asyncio.run(_cleanup_presc(ids))
        client.delete(f"/api/v1/kb/{ids['kb_id']}")


# ── BUG-009：删除 Resource 同步清理 KG ──────────────────────────────────────


def test_delete_herb_cleans_kg_nodes(client):
    """删除中药后 kg_nodes 不再残留该资源（边随 FK CASCADE）。"""
    resp = client.post("/api/v1/herbs", json={"name": f"KG清理药-{_SUFFIX}"})
    assert resp.status_code == 201, resp.text
    herb_id = uuid.UUID(resp.json()["id"])

    async def _build_and_check() -> int:
        async with _session() as db:
            await KgService(db).build()
            rows = (
                await db.scalars(
                    select(KgNode).where(KgNode.resource_id == herb_id)
                )
            ).all()
            return len(rows)

    assert asyncio.run(_build_and_check()) == 1, "构建后应存在该资源节点"

    # 新生命周期：DELETE = 移入回收站（200），purge 才做 KG / 向量清理
    assert client.delete(f"/api/v1/herbs/{herb_id}").status_code == 200
    assert client.delete(f"/api/v1/herbs/{herb_id}/purge").status_code == 200

    async def _after() -> int:
        async with _session() as db:
            rows = (
                await db.scalars(
                    select(KgNode).where(KgNode.resource_id == herb_id)
                )
            ).all()
            return len(rows)

    assert asyncio.run(_after()) == 0, "删除资源后 KG 节点必须同步清理"


# ── BUG-010：rebuild 原子化 ─────────────────────────────────────────────────


def test_kg_rebuild_is_atomic(client, monkeypatch):
    """rebuild 中途失败必须整体回滚，不能留下已清空的空图谱。"""
    resp = client.post("/api/v1/herbs", json={"name": f"原子药-{_SUFFIX}"})
    assert resp.status_code == 201, resp.text
    herb_id = uuid.UUID(resp.json()["id"])

    async def _build() -> None:
        async with _session() as db:
            await KgService(db).build(rebuild=True)

    asyncio.run(_build())
    before = asyncio.run(_kg_counts())
    assert before[0] > 0

    async def _boom(self, index):  # noqa: ANN001
        raise RuntimeError("重建失败（测试注入）")

    monkeypatch.setattr(KgService, "_sync_edges", _boom)

    async def _rebuild_fail() -> None:
        async with _session() as db:
            with pytest.raises(RuntimeError):
                await KgService(db).build(rebuild=True)

    asyncio.run(_rebuild_fail())
    after = asyncio.run(_kg_counts())
    assert after == before, f"重建失败后图谱必须完整保留，实际 {after} vs {before}"

    client.delete(f"/api/v1/herbs/{herb_id}")


# ── BUG-045：卸载顺序（先删挂载，再删向量）──────────────────────────────────


def test_vector_cleanup_failure_restores_vectors(client, vector_store, monkeypatch):
    """部分挂载清理失败：挂载回滚 + 已删向量补偿回写（不留"挂载在但检索不到"）。"""
    from src.application.resource_vector_service import make_doc_id

    kb1 = _make_kb(client)
    kb2 = _make_kb(client)
    herb = client.post("/api/v1/herbs", json={"name": f"顺序药-{_SUFFIX}"}).json()
    herb_id = uuid.UUID(herb["id"])
    for kb in (kb1, kb2):
        resp = client.post(
            f"/api/v1/kb/{kb}/resources",
            json={"resource_type": "herb", "resource_id": str(herb_id)},
        )
        assert resp.status_code == 201, resp.text

    doc1 = make_doc_id("herb", herb_id, uuid.UUID(kb1))
    doc2 = make_doc_id("herb", herb_id, uuid.UUID(kb2))
    before1 = vector_store.count_by_doc(doc1)
    before2 = vector_store.count_by_doc(doc2)
    assert before1 > 0 and before2 > 0

    # 第 1 个 KB 删除成功、第 2 个失败：必须把第 1 个的向量补回来
    calls = {"n": 0}

    def _flaky(doc_id: str) -> None:
        calls["n"] += 1
        if calls["n"] == 1:
            vector_store.__class__.delete_by_doc(vector_store, doc_id)  # 真实删除
            return
        raise VectorStoreError("Milvus 不可用（测试注入）")

    monkeypatch.setattr(vector_store, "delete_by_doc", _flaky)

    # 管理中心统一回收站：DELETE 只是软删除（不清理向量），
    # 向量清理发生在回收站 purge 阶段——因此 422 应当在 purge 时出现。
    assert client.delete(f"/api/v1/herbs/{herb_id}").status_code == 200
    resp = client.delete(f"/api/v1/herbs/{herb_id}/purge")
    assert resp.status_code == 422, resp.text

    async def _mount_count() -> int:
        async with _session() as db:
            rows = (
                await db.scalars(
                    select(KnowledgeBaseResource).where(
                        KnowledgeBaseResource.resource_type == "herb",
                        KnowledgeBaseResource.resource_id == herb_id,
                    )
                )
            ).all()
            return len(rows)

    assert asyncio.run(_mount_count()) == 2, "清理失败时挂载必须全部回滚保留"
    assert vector_store.count_by_doc(doc1) == before1, "已删向量必须被补偿回写"
    assert vector_store.count_by_doc(doc2) == before2, "已删向量必须被补偿回写"

    # 恢复后彻底删除成功（本次向量清理正常，不再注入失败）
    monkeypatch.undo()
    # 上一步 purge 失败，资源仍在回收站中 → 直接彻底删除
    assert client.delete(f"/api/v1/herbs/{herb_id}/purge").status_code == 200
    assert asyncio.run(_mount_count()) == 0
    vector_store.flush()
    assert vector_store.count_by_doc(doc1) == 0
    assert vector_store.count_by_doc(doc2) == 0


# ── BUG-046：先插后删（写入失败不丢旧向量）──────────────────────────────────


def test_replace_doc_inserts_before_deleting_stale():
    """整体替换后条数正确，多余旧分片被清理。"""
    store = InMemoryVectorStore()
    store.replace_doc("d1", [_row("d1", "c1"), _row("d1", "c2"), _row("d1", "c3")])
    assert store.count_by_doc("d1") == 3

    store.replace_doc("d1", [_row("d1", "c1"), _row("d1", "c2")])
    assert store.count_by_doc("d1") == 2
    assert set(store.ids_by_doc("d1")) == {"c1", "c2"}


def test_replace_doc_keeps_old_vectors_when_insert_fails(monkeypatch):
    """插入失败时旧向量必须仍在（旧实现先 delete 会导致向量全灭）。"""
    store = InMemoryVectorStore()
    store.replace_doc("d1", [_row("d1", "c1"), _row("d1", "c2")])

    def _boom(_rows):  # noqa: ANN001
        raise VectorStoreError("写入失败（测试注入）")

    monkeypatch.setattr(store, "insert", _boom)
    with pytest.raises(VectorStoreError):
        store.replace_doc("d1", [_row("d1", "c9")])
    assert store.count_by_doc("d1") == 2, "写入失败后旧向量必须保留"


# ── BUG-047：重新向量化失败必须留痕 ─────────────────────────────────────────


def test_revectorize_failure_is_recorded(client, vector_store, monkeypatch):
    """更新资源：向量化失败不阻塞更新（best-effort），但必须写进审计详情。"""
    kb_id = _make_kb(client)
    herb = client.post("/api/v1/herbs", json={"name": f"留痕药-{_SUFFIX}"}).json()
    herb_id = herb["id"]
    resp = client.post(
        f"/api/v1/kb/{kb_id}/resources",
        json={"resource_type": "herb", "resource_id": herb_id},
    )
    assert resp.status_code == 201, resp.text

    def _boom(_rows):  # noqa: ANN001
        raise VectorStoreError("向量写入失败（测试注入）")

    monkeypatch.setattr(vector_store, "insert", _boom)

    # 捕获审计详情（BUG-047 的"标记"落点）
    from src.application import audit_service

    captured: list[dict] = []
    original_log = audit_service.AuditService.log

    async def _spy(self, *args, **kwargs):  # noqa: ANN001
        captured.append(kwargs.get("detail") or {})
        return await original_log(self, *args, **kwargs)

    monkeypatch.setattr(audit_service.AuditService, "log", _spy)

    resp = client.put(f"/api/v1/herbs/{herb_id}", json={"effects": "新版功效"})
    assert resp.status_code == 200, resp.text  # best-effort：不阻塞更新
    assert captured and any(
        "_vector_revectorize_failed" in detail for detail in captured
    ), f"重新向量化失败未留痕：{captured}"

    monkeypatch.undo()
    client.delete(f"/api/v1/herbs/{herb_id}")


# ── BUG-015：Reflection 按 source_index 解析引用编号 ────────────────────────


def _evidence(source_index: int) -> dict:
    hit = _make_doc_hit(score=0.9)
    hit["id"] = f"chunk-{source_index}"
    return hit_to_evidence(hit, source_index)


def test_reflection_uses_source_index_not_position(monkeypatch):
    """evidence 编号不连续时（过滤产生），引用编号必须按 source_index 解析。"""
    import src.application.self_reflection as sr

    fake = [_evidence(2), _evidence(5)]
    monkeypatch.setattr(sr, "build_evidence", lambda hits: fake)

    # [citation:5] 指向第二条证据（编号为 5）→ 合法
    decision = SelfReflection().evaluate("答案是太阳病[citation: 5, 0]", [])
    assert "citation_out_of_range" not in decision.issues, decision.issues
    assert decision.details["cited_evidence_count"] == 1
    assert decision.details["invalid_citation_count"] == 0

    # [citation:3] 不存在 → 越界
    decision = SelfReflection().evaluate("答案是太阳病[citation: 3, 0]", [])
    assert "citation_out_of_range" in decision.issues
    assert decision.details["invalid_citation_count"] == 1


def test_reflection_prompt_numbering_matches_source_index():
    """LLM 复核/一致性提示中的编号必须等于 source_index（与答案同一空间）。"""
    from src.application.self_reflection import build_revision_messages

    fake = [_evidence(2), _evidence(5)]
    user = build_revision_messages("问题？", fake, "草稿答案")[1]["content"]
    assert "[2]" in user and "[5]" in user
    assert "[1]" not in user, "不得按过滤后的位序重新编号"


# ── BUG-016：Prompt / Evidence / Citation 编号统一 ──────────────────────────


@pytest.mark.asyncio
async def test_prompt_evidence_citation_share_numbering(monkeypatch):
    """低于阈值的命中不进入编号空间，答案的引用编号必有对应卡片。"""
    import src.core.config as config_mod

    original = config_mod.settings.RELEVANCE_THRESHOLD
    config_mod.settings.RELEVANCE_THRESHOLD = 0.5
    try:
        high = _make_doc_hit(score=0.9)
        low = _make_doc_hit(score=0.1)
        low["id"] = "chunk-low"
        hits = [high, low]

        svc = RagService(_mock_db(), embedding=MockEmbedding(), rerank=MockRerank())

        # Evidence：只保留可展示命中，编号连续
        evidence = build_evidence(hits)
        assert [e["source_index"] for e in evidence] == [1]

        # Prompt：只为可展示命中编号（模型不可能引用没有卡片的编号）
        prompt = svc._build_user_prompt("问题？", hits)
        assert "[1]" in prompt
        assert "[2]" not in prompt

        # Citation：显式引用 [citation:1] → 有且仅有一张卡片
        citations = await svc._build_citations("答案[citation: 1, 0]", hits)
        assert len(citations) == 1
        assert citations[0]["source_index"] == 1
    finally:
        config_mod.settings.RELEVANCE_THRESHOLD = original
