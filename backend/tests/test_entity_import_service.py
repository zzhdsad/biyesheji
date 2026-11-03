"""实体与关系持久化服务集成测试（隔离测试库）。

覆盖 TASK 要求：
- 原始记录保存 + 稳定 record_key 幂等
- 通用实体映射与关系写入
- 1:N（直接外键）与 M:N（关联表）关系
- pending / confirmed / rejected 状态管理
- 仅 confirmed 的关系进入 KG
- 跨知识库隔离与权限校验
- 重复导入幂等、部分失败可恢复

数据规模按决策 13 保持小规模（每表 2~4 行）。
"""

from __future__ import annotations

import asyncio
import hashlib
import uuid

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from src.application.entity_import_service import EntityImportService, record_key_for
from src.application.relation_discovery import (
    ForeignKeyCandidate,
    JunctionCandidate,
    discover_relations,
)
from src.application.schema_analyzer import analyze_rows
from src.application.tabular_parser import ParsedRow
from src.core.config import settings
from src.domain.models import (
    KG_STATUS_CONFIRMED,
    KG_STATUS_PENDING,
    KG_STATUS_REJECTED,
    GenericEntity,
    GenericRelation,
    ImportFile,
    ImportRecord,
    KgEdge,
    KgNode,
    KnowledgeBase,
    User,
)

# ── 小规模测试数据集（与关系发现测试保持一致）──────────────────────────────
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

TEST_FILE_HASH = "d1e2f3"


def _postgres_available() -> bool:
    async def _ping() -> None:
        engine = create_async_engine(settings.DATABASE_URL)
        try:
            async with engine.connect() as conn:
                await conn.execute(select(1))
        finally:
            await engine.dispose()

    try:
        asyncio.run(_ping())
        return True
    except Exception:
        return False


PG_AVAILABLE = _postgres_available()

# 说明：容器内 /app/pyproject.toml 不存在，pytest 读不到 asyncio_mode=auto，
# 故统一使用 @pytest.mark.anyio（与仓库既有异步测试一致）。


def _new_session():
    engine = create_async_engine(settings.DATABASE_URL)
    return engine, async_sessionmaker(engine, expire_on_commit=False)


def _rows(records: list[dict]) -> list[ParsedRow]:
    return [ParsedRow(row_no=i, data=r) for i, r in enumerate(records, start=2)]


async def _seed(session, tag: str):
    user = User(
        email=f"{tag}@test.local",
        username=tag[:40],
        hashed_password="not-a-real-hash",
        role="admin",
        name="测试管理员",
        department="测试部",
        must_change_password=False,
    )
    session.add(user)
    await session.flush()
    kb = KnowledgeBase(name=f"KB-{tag}", owner_id=user.id, visibility="private")
    session.add(kb)
    await session.flush()
    return user, kb


async def _new_job(session, kb: KnowledgeBase, tag: str):
    """建真实导入任务（import_records.job_id 有外键约束，不能传随机 UUID）。"""
    from src.domain.models import ImportJob

    job = ImportJob(
        batch_id=tag,
        dataset_id=tag,
        dataset_name=tag,
        target_type="generic",
        status="pending",
        kb_id=kb.id,
        created_by=None,
    )
    session.add(job)
    await session.flush()
    return job


async def _count(session, model, **filters) -> int:
    stmt = select(func.count()).select_from(model)
    for key, value in filters.items():
        stmt = stmt.where(getattr(model, key) == value)
    return int(await session.scalar(stmt) or 0)


async def _cleanup(session, kb_id: uuid.UUID) -> None:
    kb = await session.get(KnowledgeBase, kb_id)
    if kb is not None:
        await session.delete(kb)
        await session.commit()


# ============================================================================
# 原始记录与实体
# ============================================================================


@pytest.mark.anyio
@pytest.mark.skipif(not PG_AVAILABLE, reason="PostgreSQL 未启动")
async def test_records_are_idempotent_on_reimport():
    engine, factory = _new_session()
    async with factory() as session:
        user, kb = await _seed(session, f"rec-{uuid.uuid4().hex[:8]}")
        svc = EntityImportService(session)
        rows = _rows(D1_TERMS)
        try:
            job1 = await _new_job(session, kb, "job-1")
            job2 = await _new_job(session, kb, "job-2")
            await session.commit()
            # 关键：两次导入使用**不同任务**，验证按 kb 级幂等而非按任务幂等
            n1, err1 = await svc.save_records(
                job_id=job1.id, file_id=None, kb_id=kb.id,
                file_hash=TEST_FILE_HASH, entity_type="D1", rows=rows,
            )
            await session.commit()
            n2, err2 = await svc.save_records(
                job_id=job2.id, file_id=None, kb_id=kb.id,
                file_hash=TEST_FILE_HASH, entity_type="D1", rows=rows,
            )
            await session.commit()

            assert (n1, n2) == (3, 3)
            assert not err1 and not err2
            # 重复导入不新增记录（幂等）
            assert await _count(session, ImportRecord, kb_id=kb.id) == 3
        finally:
            await _cleanup(session, kb.id)
    await engine.dispose()


@pytest.mark.anyio
@pytest.mark.skipif(not PG_AVAILABLE, reason="PostgreSQL 未启动")
async def test_record_key_is_stable():
    assert record_key_for("abc", 7) == record_key_for("abc", 7)
    assert record_key_for("abc", 7) != record_key_for("abc", 8)
    assert record_key_for("abc", 7) != record_key_for("abd", 7)
    assert len(record_key_for("abc", 7)) == 64


@pytest.mark.anyio
@pytest.mark.skipif(not PG_AVAILABLE, reason="PostgreSQL 未启动")
async def test_entities_are_idempotent_and_keep_payload():
    engine, factory = _new_session()
    async with factory() as session:
        user, kb = await _seed(session, f"ent-{uuid.uuid4().hex[:8]}")
        svc = EntityImportService(session)
        rows = _rows(D1_TERMS)
        try:
            idx1, _ = await svc.upsert_entities(
                kb_id=kb.id, entity_type="D1", rows=rows, pk_field="术语id",
                file_hash=TEST_FILE_HASH, batch_id="b1",
            )
            await session.commit()
            idx2, _ = await svc.upsert_entities(
                kb_id=kb.id, entity_type="D1", rows=rows, pk_field="术语id",
                file_hash=TEST_FILE_HASH, batch_id="b2",
            )
            await session.commit()

            assert len(idx1) == len(idx2) == 3
            assert idx1 == idx2  # 同一实体，ID 不变
            assert await _count(session, GenericEntity, kb_id=kb.id) == 3

            entity = await session.scalar(
                select(GenericEntity).where(
                    GenericEntity.kb_id == kb.id, GenericEntity.source_pk == "T1"
                )
            )
            # 原始字段全量保留，不丢字段
            assert entity.payload == {"术语id": "T1", "术语名称": "脾胃虚弱"}
            assert entity.name == "脾胃虚弱"  # 显示名取「名称」类字段
        finally:
            await _cleanup(session, kb.id)
    await engine.dispose()


@pytest.mark.anyio
@pytest.mark.skipif(not PG_AVAILABLE, reason="PostgreSQL 未启动")
async def test_rows_without_primary_key_are_reported():
    engine, factory = _new_session()
    async with factory() as session:
        user, kb = await _seed(session, f"nopk-{uuid.uuid4().hex[:8]}")
        svc = EntityImportService(session)
        rows = [
            ParsedRow(row_no=2, data={"术语id": "T1", "术语名称": "脾胃虚弱"}),
            ParsedRow(row_no=3, data={"术语名称": "无主键行"}),  # 缺 术语id
        ]
        try:
            index, errors = await svc.upsert_entities(
                kb_id=kb.id, entity_type="D1", rows=rows, pk_field="术语id",
                file_hash=TEST_FILE_HASH, batch_id="b1",
            )
            await session.commit()

            assert len(index) == 1  # 缺主键的行被跳过，其余照常写入
            assert len(errors) == 1
            assert errors[0].row_no == 3
            assert "缺少主键" in errors[0].message
        finally:
            await _cleanup(session, kb.id)
    await engine.dispose()


# ============================================================================
# 关系：1:N / M:N / 幂等 / 状态
# ============================================================================


@pytest.mark.anyio
@pytest.mark.skipif(not PG_AVAILABLE, reason="PostgreSQL 未启动")
async def test_direct_foreign_key_creates_relations():
    engine, factory = _new_session()
    async with factory() as session:
        user, kb = await _seed(session, f"fk-{uuid.uuid4().hex[:8]}")
        svc = EntityImportService(session)
        try:
            parents, _ = await svc.upsert_entities(
                kb_id=kb.id, entity_type="D1", rows=_rows(D1_TERMS), pk_field="术语id",
            )
            children = [
                {"子id": "C1", "术语id": "T1"},
                {"子id": "C2", "术语id": "T1"},
                {"子id": "C3", "术语id": "T2"},
            ]
            child_idx, _ = await svc.upsert_entities(
                kb_id=kb.id, entity_type="子表", rows=_rows(children), pk_field="子id",
            )
            await session.commit()

            fk = ForeignKeyCandidate(
                child_table="子表", child_field="术语id",
                parent_table="D1", parent_field="术语id",
                name_similarity=1.0, coverage=1.0, type_match=True,
                confidence=0.95, cardinality="1:N", evidence="test",
            )
            created = await svc._link_direct(
                kb_id=kb.id, fk=fk,
                rows_by_table={"子表": _rows(children)},
                primary_keys={"子表": "子id"},
                status=KG_STATUS_CONFIRMED,
                batch_id="b1", user=user,
            )
            await session.commit()

            # 3 个子实体分别指向 T1/T1/T2
            assert created == 3
            assert await _count(session, GenericRelation, kb_id=kb.id) == 3
        finally:
            await _cleanup(session, kb.id)
    await engine.dispose()


@pytest.mark.anyio
@pytest.mark.skipif(not PG_AVAILABLE, reason="PostgreSQL 未启动")
async def test_relations_are_not_duplicated():
    engine, factory = _new_session()
    async with factory() as session:
        user, kb = await _seed(session, f"dup-{uuid.uuid4().hex[:8]}")
        svc = EntityImportService(session)
        try:
            idx, _ = await svc.upsert_entities(
                kb_id=kb.id, entity_type="D1", rows=_rows(D1_TERMS), pk_field="术语id",
            )
            await session.commit()
            ids = list(idx.values())
            triples = [(ids[0], ids[1], "related_to")] * 2  # 同一关系提交两次

            first = await svc.create_relations(kb_id=kb.id, triples=triples, relation_type="related_to")
            await session.commit()
            second = await svc.create_relations(kb_id=kb.id, triples=triples, relation_type="related_to")
            await session.commit()

            assert (first, second) == (1, 0)
            assert await _count(session, GenericRelation, kb_id=kb.id) == 1
        finally:
            await _cleanup(session, kb.id)
    await engine.dispose()


@pytest.mark.anyio
@pytest.mark.skipif(not PG_AVAILABLE, reason="PostgreSQL 未启动")
async def test_junction_creates_many_to_many_relations():
    engine, factory = _new_session()
    async with factory() as session:
        user, kb = await _seed(session, f"jn-{uuid.uuid4().hex[:8]}")
        svc = EntityImportService(session)
        try:
            await svc.upsert_entities(
                kb_id=kb.id, entity_type="D1", rows=_rows(D1_TERMS), pk_field="术语id",
            )
            await svc.upsert_entities(
                kb_id=kb.id, entity_type="D2", rows=_rows(D2_MEDICINES), pk_field="中成药id",
            )
            await session.commit()

            junction = JunctionCandidate(
                table="D3", left_table="D2", left_field="中成药id",
                right_table="D1", right_field="术语id",
                confidence=0.9, evidence="test", relation_type="含术语",
            )
            created = await svc._link_junction(
                kb_id=kb.id, junction=junction,
                rows_by_table={"D3": _rows(D3_LINKS)},
                status=KG_STATUS_CONFIRMED,
                batch_id="b1", user=user,
            )
            await session.commit()

            assert created == 3  # D3 三行 → 三条 M:N 关系
            assert await _count(session, GenericRelation, kb_id=kb.id) == 3
        finally:
            await _cleanup(session, kb.id)
    await engine.dispose()


@pytest.mark.anyio
@pytest.mark.skipif(not PG_AVAILABLE, reason="PostgreSQL 未启动")
async def test_pending_relations_do_not_enter_kg():
    engine, factory = _new_session()
    async with factory() as session:
        user, kb = await _seed(session, f"pend-{uuid.uuid4().hex[:8]}")
        svc = EntityImportService(session)
        try:
            idx, _ = await svc.upsert_entities(
                kb_id=kb.id, entity_type="D1", rows=_rows(D1_TERMS), pk_field="术语id",
            )
            await session.commit()
            ids = list(idx.values())
            await svc.create_relations(
                kb_id=kb.id, triples=[(ids[0], ids[1], "related_to")],
                relation_type="related_to", status=KG_STATUS_PENDING,
            )
            await session.commit()

            nodes, edges = await svc.sync_confirmed_to_kg(kb.id)
            await session.commit()

            # 未确认：KG 里不应出现任何节点或边
            assert (nodes, edges) == (0, 0)
            assert await _count(session, KgEdge, kb_id=kb.id) == 0
            assert await _count(session, KgNode, kb_id=kb.id) == 0
        finally:
            await _cleanup(session, kb.id)
    await engine.dispose()


@pytest.mark.anyio
@pytest.mark.skipif(not PG_AVAILABLE, reason="PostgreSQL 未启动")
async def test_confirmed_relations_sync_to_kg_with_provenance():
    engine, factory = _new_session()
    async with factory() as session:
        user, kb = await _seed(session, f"conf-{uuid.uuid4().hex[:8]}")
        svc = EntityImportService(session)
        try:
            idx, _ = await svc.upsert_entities(
                kb_id=kb.id, entity_type="D1", rows=_rows(D1_TERMS), pk_field="术语id",
            )
            await session.commit()
            ids = list(idx.values())
            await svc.create_relations(
                kb_id=kb.id, triples=[(ids[0], ids[1], "related_to")],
                relation_type="related_to", status=KG_STATUS_CONFIRMED,
                provenance="D1.术语id→D1.术语id", batch_id="b1",
            )
            await session.commit()

            nodes, edges = await svc.sync_confirmed_to_kg(kb.id, batch_id="b1")
            await session.commit()

            assert (nodes, edges) == (2, 1)
            edge = await session.scalar(select(KgEdge).where(KgEdge.kb_id == kb.id))
            assert edge.provenance == "D1.术语id→D1.术语id"
            assert edge.status == KG_STATUS_CONFIRMED

            # 重复同步不产生重复边
            nodes2, edges2 = await svc.sync_confirmed_to_kg(kb.id, batch_id="b1")
            await session.commit()
            assert (nodes2, edges2) == (2, 0)
        finally:
            await _cleanup(session, kb.id)
    await engine.dispose()


@pytest.mark.anyio
@pytest.mark.skipif(not PG_AVAILABLE, reason="PostgreSQL 未启动")
async def test_rejected_relations_are_excluded_from_kg():
    engine, factory = _new_session()
    async with factory() as session:
        user, kb = await _seed(session, f"rej-{uuid.uuid4().hex[:8]}")
        svc = EntityImportService(session)
        try:
            idx, _ = await svc.upsert_entities(
                kb_id=kb.id, entity_type="D1", rows=_rows(D1_TERMS), pk_field="术语id",
            )
            await session.commit()
            ids = list(idx.values())
            await svc.create_relations(
                kb_id=kb.id, triples=[(ids[0], ids[1], "related_to")],
                relation_type="related_to", status=KG_STATUS_CONFIRMED,
            )
            await session.commit()

            relation = await session.scalar(
                select(GenericRelation).where(GenericRelation.kb_id == kb.id)
            )
            await svc.set_relation_status(
                kb_id=kb.id, relation_id=relation.id, status=KG_STATUS_REJECTED, user=user
            )
            await session.commit()

            nodes, edges = await svc.sync_confirmed_to_kg(kb.id)
            await session.commit()
            assert (nodes, edges) == (0, 0)
        finally:
            await _cleanup(session, kb.id)
    await engine.dispose()


# ============================================================================
# 跨知识库隔离与权限
# ============================================================================


@pytest.mark.anyio
@pytest.mark.skipif(not PG_AVAILABLE, reason="PostgreSQL 未启动")
async def test_kg_node_name_follows_entity_display_name():
    """实体名被修正后，已有 KG 节点名必须同步，否则图谱匹配仍命中旧主键名。"""
    engine, factory = _new_session()
    async with factory() as session:
        user, kb = await _seed(session, f"kgname-{uuid.uuid4().hex[:8]}")
        svc = EntityImportService(session)
        try:
            entity = GenericEntity(
                kb_id=kb.id,
                entity_type="D6",
                name="C1",
                payload={"CHP_ID": "C1"},
                source_pk="C1",
            )
            session.add(entity)
            await session.flush()

            mapping = await svc._ensure_kg_nodes(kb.id, [entity.id])
            await session.flush()
            node = await session.get(KgNode, mapping[entity.id])
            assert node.name == "C1"

            entity.name = "阿氏蒿"
            await session.flush()
            await svc._ensure_kg_nodes(kb.id, [entity.id])
            await session.refresh(node)
            assert node.name == "阿氏蒿"
        finally:
            await _cleanup(session, kb.id)
    await engine.dispose()


@pytest.mark.anyio
@pytest.mark.skipif(not PG_AVAILABLE, reason="PostgreSQL 未启动")
async def test_relation_from_other_kb_is_not_visible():
    engine, factory = _new_session()
    async with factory() as session:
        user, kb_a = await _seed(session, f"isoA-{uuid.uuid4().hex[:8]}")
        _, kb_b = await _seed(session, f"isoB-{uuid.uuid4().hex[:8]}")
        svc = EntityImportService(session)
        try:
            idx, _ = await svc.upsert_entities(
                kb_id=kb_a.id, entity_type="D1", rows=_rows(D1_TERMS), pk_field="术语id",
            )
            await session.commit()
            ids = list(idx.values())
            await svc.create_relations(
                kb_id=kb_a.id, triples=[(ids[0], ids[1], "related_to")],
                relation_type="related_to", status=KG_STATUS_CONFIRMED,
            )
            await session.commit()

            relation = await session.scalar(
                select(GenericRelation).where(GenericRelation.kb_id == kb_a.id)
            )
            # 用另一个知识库的 id 去改状态 → 查不到（隔离）
            with pytest.raises(LookupError):
                await svc.set_relation_status(
                    kb_id=kb_b.id, relation_id=relation.id,
                    status=KG_STATUS_REJECTED, user=user,
                )
            # 另一个知识库同步 KG 不应带入 A 库的关系
            nodes, edges = await svc.sync_confirmed_to_kg(kb_b.id)
            assert (nodes, edges) == (0, 0)
        finally:
            await _cleanup(session, kb_a.id)
            await _cleanup(session, kb_b.id)
    await engine.dispose()


@pytest.mark.anyio
@pytest.mark.skipif(not PG_AVAILABLE, reason="PostgreSQL 未启动")
async def test_entities_are_isolated_between_kbs():
    engine, factory = _new_session()
    async with factory() as session:
        user, kb_a = await _seed(session, f"isoC-{uuid.uuid4().hex[:8]}")
        _, kb_b = await _seed(session, f"isoD-{uuid.uuid4().hex[:8]}")
        svc = EntityImportService(session)
        try:
            await svc.upsert_entities(
                kb_id=kb_a.id, entity_type="D1", rows=_rows(D1_TERMS), pk_field="术语id",
            )
            await session.commit()

            # 同一主键值在 B 库查不到（不跨库引用）
            resolved = await svc.resolve_entities(kb_b.id, "D1", ["T1", "T2"])
            assert resolved == {}
            assert await _count(session, GenericEntity, kb_id=kb_b.id) == 0
        finally:
            await _cleanup(session, kb_a.id)
            await _cleanup(session, kb_b.id)
    await engine.dispose()


# ============================================================================
# 端到端：D1 / D2 / D3 关联导入
# ============================================================================


@pytest.mark.anyio
@pytest.mark.skipif(not PG_AVAILABLE, reason="PostgreSQL 未启动")
async def test_confirmed_relation_is_traceable_to_source_record():
    """已确认关联 → KG 边 → 实体 → 原始记录，全链路可追溯。"""
    engine, factory = _new_session()
    async with factory() as session:
        user, kb = await _seed(session, f"trace-{uuid.uuid4().hex[:8]}")
        job = await _new_job(session, kb, "trace-job")
        await session.commit()
        svc = EntityImportService(session)

        rows_by_table = {"D1": _rows(D1_TERMS), "D2": _rows(D2_MEDICINES), "D3": _rows(D3_LINKS)}
        profiles = [analyze_rows(r, table_name=n) for n, r in rows_by_table.items()]
        plan = discover_relations(profiles)

        # 用真实 ImportFile，保证 file_id 可回溯
        files_by_table = {}
        for name, rows in rows_by_table.items():
            import_file = ImportFile(
                job_id=job.id,
                relative_path=f"{name}.csv",
                file_name=f"{name}.csv",
                file_ext=".csv",
                size_bytes=1024,
                file_hash=hashlib.sha256(name.encode("utf-8")).hexdigest(),
                encoding="utf-8",
                detected_format="csv",
                row_count=len(rows),
            )
            session.add(import_file)
            await session.flush()
            files_by_table[name] = import_file
        await session.commit()

        try:
            await svc.import_relational(
                kb_id=kb.id, user=user, job_id=job.id,
                rows_by_table=rows_by_table, files_by_table=files_by_table,
                primary_keys={t: plan.primary_keys[t][0] for t in plan.primary_keys},
                foreign_keys=[], junctions=plan.junctions,
                batch_id="trace-batch", auto_confirm=True,
            )
            await session.commit()

            edge = await session.scalar(select(KgEdge).where(KgEdge.kb_id == kb.id))
            assert edge is not None
            # 1) 边带知识库归属与来源说明
            assert edge.kb_id == kb.id
            assert edge.provenance.startswith("junction:D3")
            assert edge.status == KG_STATUS_CONFIRMED

            # 2) 边 → 节点 → 实体
            node = await session.get(KgNode, edge.source_node_id)
            entity = await session.get(GenericEntity, node.entity_id)
            assert entity.kb_id == kb.id
            assert entity.import_batch_id == "trace-batch"
            # 原始字段完整保留（未被拼成文本块）
            assert set(entity.payload) == {"中成药id", "名称"}
            assert entity.source_file_id is not None
            assert entity.record_key

            # 3) 实体 → 原始记录（同一 record_key）
            record = await session.scalar(
                select(ImportRecord).where(
                    ImportRecord.kb_id == kb.id, ImportRecord.record_key == entity.record_key
                )
            )
            assert record is not None
            assert record.payload == entity.payload
            assert record.job_id == job.id
        finally:
            await _cleanup(session, kb.id)
    await engine.dispose()


@pytest.mark.anyio
@pytest.mark.skipif(not PG_AVAILABLE, reason="PostgreSQL 未启动")
async def test_end_to_end_relational_import_is_idempotent():
    engine, factory = _new_session()
    async with factory() as session:
        user, kb = await _seed(session, f"e2e-{uuid.uuid4().hex[:8]}")
        svc = EntityImportService(session)

        tables = {"D1": D1_TERMS, "D2": D2_MEDICINES, "D3": D3_LINKS}
        rows_by_table = {name: _rows(recs) for name, recs in tables.items()}
        profiles = [analyze_rows(rows, table_name=name) for name, rows in rows_by_table.items()]
        plan = discover_relations(profiles)
        assert plan.junctions, "应识别出 D3 为关联表"

        class _FakeFile:
            """模拟已登记的导入文件。

            注意：每个文件必须有**各自**的 file_hash——record_key = sha256(哈希:行号)，
            若多表共用同一哈希，不同文件的相同行号会撞成同一个 record_key
            （这正是 record_key 按文件而非仅按行号设计的原因）。
            """

            def __init__(self, name: str, file_hash: str) -> None:
                self.id = None
                self.file_name = name
                self.file_hash = file_hash

        files_by_table = {
            name: _FakeFile(f"{name}.csv", hashlib.sha256(name.encode("utf-8")).hexdigest())
            for name in tables
        }
        job1 = await _new_job(session, kb, "e2e-job-1")
        job2 = await _new_job(session, kb, "e2e-job-2")
        await session.commit()
        try:
            stats1 = await svc.import_relational(
                kb_id=kb.id, user=user, job_id=job1.id,
                rows_by_table=rows_by_table, files_by_table=files_by_table,
                primary_keys={t: plan.primary_keys[t][0] for t in plan.primary_keys},
                foreign_keys=plan.foreign_keys, junctions=plan.junctions,
                batch_id="batch-1", auto_confirm=True,
            )
            await session.commit()

            stats2 = await svc.import_relational(
                kb_id=kb.id, user=user, job_id=job2.id,
                rows_by_table=rows_by_table, files_by_table=files_by_table,
                primary_keys={t: plan.primary_keys[t][0] for t in plan.primary_keys},
                foreign_keys=plan.foreign_keys, junctions=plan.junctions,
                batch_id="batch-2", auto_confirm=True,
            )
            await session.commit()

            # 第一次：8 行原始记录（D1 3 + D2 2 + D3 3）
            assert stats1.records == 8
            # 实体只来自有主键的表：D1 3 + D2 2 = 5；D3 是关联表，不建实体
            assert stats1.entities == 5
            # D3 三行 → 三条 M:N 关系
            assert stats1.relations == 3
            assert stats1.kg_edges == 3
            assert stats1.failed_rows == 0

            # 第二次导入同样数据：实体数不变，关系不再新增（幂等）
            assert stats2.records == 8
            assert stats2.entities == 5
            assert stats2.relations == 0
            assert stats2.relations == 0
            assert await _count(session, GenericRelation, kb_id=kb.id) == 3
            assert await _count(session, ImportRecord, kb_id=kb.id) == 8
        finally:
            await _cleanup(session, kb.id)
    await engine.dispose()


@pytest.mark.anyio
@pytest.mark.skipif(not PG_AVAILABLE, reason="PostgreSQL 未启动")
async def test_import_relational_reports_progress(monkeypatch):
    """关联导入全过程应上报递增进度，供前端轮询展示。"""
    from src.application import import_progress as ip

    ip.reset_progress_memory()
    monkeypatch.setattr(ip, "_get_client", lambda: None)
    session_id = f"progress-{uuid.uuid4().hex[:8]}"

    engine, factory = _new_session()
    async with factory() as session:
        user, kb = await _seed(session, f"progress-{uuid.uuid4().hex[:8]}")
        job = await _new_job(session, kb, "progress-job")
        await session.commit()
        svc = EntityImportService(session)

        rows_by_table = {"D1": _rows(D1_TERMS), "D2": _rows(D2_MEDICINES), "D3": _rows(D3_LINKS)}
        profiles = [analyze_rows(rows, table_name=name) for name, rows in rows_by_table.items()]
        plan = discover_relations(profiles)

        class _FakeFile:
            def __init__(self, name: str) -> None:
                self.id = None
                self.file_name = name
                self.file_hash = hashlib.sha256(name.encode("utf-8")).hexdigest()

        files_by_table = {name: _FakeFile(name) for name in rows_by_table}

        progress = ip.ImportProgress(session_id)
        writes: list[dict] = []
        original_write = progress._write

        async def capture(payload: dict) -> None:
            writes.append(payload)
            await original_write(payload)

        progress._write = capture  # type: ignore[method-assign]

        try:
            stats = await svc.import_relational(
                kb_id=kb.id, user=user, job_id=job.id,
                rows_by_table=rows_by_table, files_by_table=files_by_table,
                primary_keys={t: plan.primary_keys[t][0] for t in plan.primary_keys},
                foreign_keys=plan.foreign_keys, junctions=plan.junctions,
                batch_id="progress-batch", auto_confirm=True, progress=progress,
            )
            await session.commit()

            phases = [item["phase"] for item in writes]
            percents = [item["percent"] for item in writes]
            assert "write" in phases
            assert "relations" in phases
            assert phases[-1] == "kg"
            # 进度单调不减，且图谱阶段到达 99%（剩余 1% 由路由 finish 补满）
            assert percents == sorted(percents)
            assert percents[-1] == 99
            assert "图谱同步完成" in writes[-1]["message"]
            assert stats.kg_edges > 0

            snapshot = await ip.get_progress(session_id)
            assert snapshot is not None
            assert snapshot["percent"] == 99
        finally:
            await _cleanup(session, kb.id)
            ip.reset_progress_memory()
    await engine.dispose()
