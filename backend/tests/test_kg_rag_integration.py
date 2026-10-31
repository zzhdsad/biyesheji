"""已确认关联能被 RAG 实际利用，且来源可追溯（真实 PostgreSQL）。

这不是"任务状态成功"就算数，而是走**真实检索链路** KgRetriever（RAG 组装
Prompt 前取图谱事实的同一条路径）验证：
1. 未确认 → 检索不出任何图谱事实（不会参与问答）
2. 确认后 → 能检索到事实，且事实可回溯到：原始表 → 原始文件 → 原始行
3. 拒绝后 → 事实从检索结果中消失（图谱边已同步撤销）
"""

from __future__ import annotations

import asyncio
import hashlib
import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from src.application.entity_import_service import EntityImportService
from src.application.kg_retrieval import KgRetriever
from src.application.query_analyzer import QueryAnalysis
from src.application.relation_discovery import discover_relations
from src.application.schema_analyzer import analyze_rows
from src.core.config import settings
from src.domain.models import (
    KG_STATUS_CONFIRMED,
    GenericEntity,
    GenericRelation,
    ImportFile,
    ImportJob,
    ImportRecord,
    KnowledgeBase,
    User,
)


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


def _new_session():
    engine = create_async_engine(settings.DATABASE_URL)
    return engine, async_sessionmaker(engine, expire_on_commit=False)


D1_TERMS = [
    {"术语id": "T1", "术语名称": "脾胃虚弱"},
    {"术语id": "T2", "术语名称": "肝郁气滞"},
]
D2_MEDICINES = [
    {"中成药id": "M1", "名称": "四君子汤"},
    {"中成药id": "M2", "名称": "逍遥散"},
]
# 必须让两端都不唯一，才能被判定为多对多关联表：
# 只有两行时「术语id」恰好互不相同，会被当成实体表而非关联表。
D3_LINKS = [
    {"中成药id": "M1", "术语id": "T1"},
    {"中成药id": "M1", "术语id": "T2"},
    {"中成药id": "M2", "术语id": "T1"},
]


def _rows(payloads: list[dict]):
    from src.application.tabular_parser import ParsedRow

    return [ParsedRow(row_no=i + 2, data=dict(p)) for i, p in enumerate(payloads)]


def _analysis(text: str) -> QueryAnalysis:
    return QueryAnalysis(
        query=f"{text} 相关中成药有哪些？",
        question_type="relation",
        question_type_label="关系查询",
        resource_types=[],
        keywords=[text],
        entities=[{"text": text, "type": "generic"}],
    )


async def _seed(session, tag: str):
    user = User(
        email=f"{tag}@biyesheji.dev",
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
    job = ImportJob(
        batch_id=tag, dataset_id=tag, dataset_name=tag, target_type="generic",
        status="pending", kb_id=kb.id, created_by=user.id,
    )
    session.add(job)
    await session.flush()
    return user, kb, job


@pytest.mark.anyio
@pytest.mark.skipif(not PG_AVAILABLE, reason="PostgreSQL 未启动")
async def test_confirmed_relation_is_retrievable_and_traceable():
    engine, factory = _new_session()
    async with factory() as session:
        user, kb, job = await _seed(session, f"kgrag-{uuid.uuid4().hex[:8]}")
        svc = EntityImportService(session)

        tables = {"D1": D1_TERMS, "D2": D2_MEDICINES, "D3": D3_LINKS}
        rows_by_table = {name: _rows(rows) for name, rows in tables.items()}
        files_by_table = {}
        for name, rows in rows_by_table.items():
            import_file = ImportFile(
                job_id=job.id,
                relative_path=f"{name}.csv",
                file_name=f"{name}.csv",
                file_ext=".csv",
                size_bytes=512,
                file_hash=hashlib.sha256(name.encode("utf-8")).hexdigest(),
                encoding="utf-8",
                detected_format="csv",
                row_count=len(rows),
            )
            session.add(import_file)
            await session.flush()
            files_by_table[name] = import_file
        await session.commit()

        profiles = [analyze_rows(r, table_name=n) for n, r in rows_by_table.items()]
        plan = discover_relations(profiles)
        try:
            # ① 不自动确认 → 关系为 pending
            await svc.import_relational(
                kb_id=kb.id, user=user, job_id=job.id,
                rows_by_table=rows_by_table, files_by_table=files_by_table,
                primary_keys={t: plan.primary_keys[t][0] for t in plan.primary_keys},
                foreign_keys=[], junctions=plan.junctions,
                batch_id="rag-batch", auto_confirm=False,
            )
            await session.commit()

            pending = (await session.execute(
                select(GenericRelation).where(GenericRelation.kb_id == kb.id)
            )).scalars().all()
            assert len(pending) == 3
            assert all(r.status == "pending" for r in pending)

            # ② 未确认时，RAG 检索链路取不到任何图谱事实
            retriever = KgRetriever(session)
            facts, _ = await retriever.facts([kb.id], _analysis("脾胃虚弱"))
            assert facts == [], f"未确认关系不应参与问答，却检索到 {facts}"

            # ③ 确认后，RAG 能检索到事实
            ids = [r.id for r in pending]
            await svc.bulk_set_status(
                kb_id=kb.id, relation_ids=ids, status=KG_STATUS_CONFIRMED, user=user
            )
            await session.commit()
            await svc.sync_confirmed_to_kg(kb.id)
            await session.commit()

            facts, nodes = await retriever.facts([kb.id], _analysis("脾胃虚弱"))
            assert facts, "已确认关系必须能被 RAG 检索到"
            target_names = {nodes[f.target_node_id].name for f in facts} | {
                nodes[f.source_node_id].name for f in facts
            }
            assert "脾胃虚弱" in target_names, target_names

            # ④ 事实可回溯：kg_edge provenance → 关联表 → 实体 → 原始记录 → 原始文件
            fact = facts[0]
            assert "junction:D3" in fact.provenance, fact.provenance
            entity = await session.scalar(
                select(GenericEntity).where(
                    GenericEntity.kb_id == kb.id, GenericEntity.name == "脾胃虚弱"
                )
            )
            assert entity is not None
            assert entity.entity_type == "D1"
            record = await session.scalar(
                select(ImportRecord).where(
                    ImportRecord.kb_id == kb.id,
                    ImportRecord.record_key == entity.record_key,
                )
            )
            assert record is not None, "实体必须能回指到原始记录"
            assert record.payload["术语id"] == "T1"
            source_file = await session.get(ImportFile, record.file_id)
            assert source_file is not None and source_file.file_name == "D1.csv"
            assert record.row_no == 2, record.row_no  # 表头占第 1 行

            # ⑤ 拒绝后，事实从检索结果中消失
            await svc.bulk_set_status(
                kb_id=kb.id, relation_ids=ids, status="rejected", user=user
            )
            await session.commit()
            facts_after, _ = await retriever.facts([kb.id], _analysis("脾胃虚弱"))
            assert facts_after == [], "拒绝后不应再被检索到"
        finally:
            await session.delete(kb)
            await session.commit()
    await engine.dispose()
