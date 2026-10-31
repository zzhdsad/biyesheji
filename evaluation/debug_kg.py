"""调试：确认后 KG 检索为何取不到事实。"""

import asyncio
import hashlib
import sys
import uuid

sys.path.insert(0, "/app")

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from src.application.entity_import_service import EntityImportService
from src.application.kg_retrieval import KgRetriever
from src.application.kg_service import KgService
from src.application.query_analyzer import QueryAnalysis
from src.application.relation_discovery import discover_relations
from src.application.schema_analyzer import analyze_rows
from src.application.tabular_parser import ParsedRow
from src.core.config import settings
from src.domain.models import (
    ImportFile, ImportJob, KgEdge, KgNode, KnowledgeBase, User,
)

D1 = [{"术语id": "T1", "术语名称": "脾胃虚弱"}, {"术语id": "T2", "术语名称": "肝郁气滞"}]
D2 = [{"中成药id": "M1", "名称": "四君子汤"}, {"中成药id": "M2", "名称": "逍遥散"}]
D3 = [
    {"中成药id": "M1", "术语id": "T1"},
    {"中成药id": "M1", "术语id": "T2"},
    {"中成药id": "M2", "术语id": "T1"},
]


async def main() -> None:
    engine = create_async_engine(settings.DATABASE_URL)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        tag = f"dbgkg-{uuid.uuid4().hex[:6]}"
        user = User(email=f"{tag}@biyesheji.dev", username=tag, hashed_password="x",
                    role="admin", name="t", department="t", must_change_password=False)
        session.add(user)
        await session.flush()
        kb = KnowledgeBase(name=f"KB-{tag}", owner_id=user.id, visibility="private")
        session.add(kb)
        await session.flush()
        job = ImportJob(batch_id=tag, dataset_id=tag, dataset_name=tag, target_type="generic",
                        status="pending", kb_id=kb.id, created_by=user.id)
        session.add(job)
        await session.flush()

        tables = {"D1": D1, "D2": D2, "D3": D3}
        rows_by_table = {n: [ParsedRow(row_no=i + 2, data=dict(r)) for i, r in enumerate(rows)]
                         for n, rows in tables.items()}
        files_by_table = {}
        for name, rows in rows_by_table.items():
            f = ImportFile(job_id=job.id, relative_path=f"{name}.csv", file_name=f"{name}.csv",
                           file_ext=".csv", size_bytes=100,
                           file_hash=hashlib.sha256(name.encode()).hexdigest(),
                           encoding="utf-8", detected_format="csv", row_count=len(rows))
            session.add(f)
            await session.flush()
            files_by_table[name] = f
        await session.commit()

        profiles = [analyze_rows(r, table_name=n) for n, r in rows_by_table.items()]
        plan = discover_relations(profiles)
        print("主键:", {t: v for t, v in plan.primary_keys.items()})
        print("关联表:", [(j.table, j.left_table, j.right_table) for j in plan.junctions])

        svc = EntityImportService(session)
        stats = await svc.import_relational(
            kb_id=kb.id, user=user, job_id=job.id, rows_by_table=rows_by_table,
            files_by_table=files_by_table,
            primary_keys={t: plan.primary_keys[t][0] for t in plan.primary_keys},
            foreign_keys=[], junctions=plan.junctions, batch_id=tag, auto_confirm=False,
        )
        await session.commit()
        print("导入:", stats.to_dict())
        print("确认前 KG 边:", len((await session.execute(
            select(KgEdge).where(KgEdge.kb_id == kb.id))).scalars().all()))

        from src.domain.models import GenericRelation
        rels = (await session.execute(
            select(GenericRelation).where(GenericRelation.kb_id == kb.id))).scalars().all()
        print("关系:", [(str(r.id)[:8], r.status) for r in rels])
        updated = await svc.bulk_set_status(
            kb_id=kb.id, relation_ids=[r.id for r in rels], status="confirmed", user=user
        )
        await session.commit()
        print("bulk_set_status 更新:", updated)
        print("确认后关系状态:", [(r.status) for r in (await session.execute(
            select(GenericRelation).where(GenericRelation.kb_id == kb.id))).scalars().all()])
        synced = await svc.sync_confirmed_to_kg(kb.id)
        await session.commit()
        print("sync:", synced)

        nodes = (await session.execute(select(KgNode).where(KgNode.kb_id == kb.id))).scalars().all()
        print("KG 节点:", [(n.name, n.entity_id is not None, n.kb_id == kb.id) for n in nodes])
        edges = (await session.execute(select(KgEdge).where(KgEdge.kb_id == kb.id))).scalars().all()
        print("KG 边:", len(edges), [(e.relation_type, e.status) for e in edges])

        service = KgService(session)
        matches = await service.match_nodes(["脾胃虚弱"])
        print("match_nodes:", [(n.name, n.kb_id == kb.id, n.entity_id is not None, s)
                               for n, s in matches])

        analysis = QueryAnalysis(query="脾胃虚弱 相关中成药", question_type="relation",
                                 question_type_label="关系", resource_types=[],
                                 keywords=["脾胃虚弱"],
                                 entities=[{"text": "脾胃虚弱", "type": "generic"}])
        retriever = KgRetriever(session)
        facts, got_nodes = await retriever.facts([kb.id], analysis)
        print("facts:", [(f.relation, f.provenance) for f in facts])
        print("nodes:", {str(k): v.name for k, v in got_nodes.items()})

        await session.delete(kb)
        await session.commit()
    await engine.dispose()


asyncio.run(main())
