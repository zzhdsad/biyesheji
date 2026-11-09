"""知识图谱事实可被 RAG 检索，并能回溯到原始业务资源（真实 PostgreSQL）。

当前 KG 是资源配置表的派生物（``KgService.build``，见 kg_service 模块注释）：
- ``contains`` 边来自 ``prescription_ingredients``（方剂组成，业务事实）；
- 可见性由 ``knowledge_base_resources`` 挂载决定，与向量检索口径一致。

因此本测试不再依赖已下线的通用导入链路（EntityImportService / 关系发现），
改为验证当前真实链路：资源写入 → 全量构建 → ``KgRetriever`` 检索 → 溯源回业务表。
"""

from __future__ import annotations

import asyncio
import uuid
from decimal import Decimal

import pytest
from sqlalchemy import delete, or_, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from src.application.kg_retrieval import KgRetriever
from src.application.kg_service import KgService
from src.application.query_analyzer import QueryAnalysis
from src.core.config import settings
from src.domain.models import (
    KG_PROVENANCE_INGREDIENT,
    KG_RELATION_CONTAINS,
    Herb,
    KnowledgeBase,
    KnowledgeBaseResource,
    KgEdge,
    KgNode,
    Prescription,
    PrescriptionIngredient,
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


def _analysis(text: str) -> QueryAnalysis:
    return QueryAnalysis(
        query=f"{text} 相关方剂有哪些？",
        question_type="relation",
        question_type_label="关系查询",
        resource_types=[],
        keywords=[text],
        entities=[{"text": text, "type": "generic"}],
    )


async def _cleanup(factory, *, herb_id, presc_id, kb_ids, user_id) -> None:
    """删除本测试自己创建的数据。

    KG 节点/边是全局派生数据（``build`` 会同步全库资源），这里只清理本次
    测试两味资源对应的节点与边，不触碰其他测试/业务产生的图数据。
    """
    async with factory() as session:
        node_ids = (
            await session.scalars(
                select(KgNode.id).where(KgNode.resource_id.in_([herb_id, presc_id]))
            )
        ).all()
        if node_ids:
            await session.execute(
                delete(KgEdge).where(
                    or_(
                        KgEdge.source_node_id.in_(node_ids),
                        KgEdge.target_node_id.in_(node_ids),
                    )
                )
            )
            await session.execute(delete(KgNode).where(KgNode.id.in_(node_ids)))
        await session.execute(
            delete(KnowledgeBaseResource).where(
                KnowledgeBaseResource.resource_id.in_([herb_id, presc_id])
            )
        )
        await session.execute(
            delete(PrescriptionIngredient).where(
                PrescriptionIngredient.prescription_id == presc_id
            )
        )
        await session.execute(delete(Prescription).where(Prescription.id == presc_id))
        await session.execute(delete(Herb).where(Herb.id == herb_id))
        await session.execute(delete(KnowledgeBase).where(KnowledgeBase.id.in_(kb_ids)))
        await session.execute(delete(User).where(User.id == user_id))
        await session.commit()


@pytest.mark.anyio
@pytest.mark.skipif(not PG_AVAILABLE, reason="PostgreSQL 未启动")
async def test_kg_fact_is_retrievable_and_traceable():
    engine, factory = _new_session()
    tag = f"kgrag-{uuid.uuid4().hex[:8]}"
    herb_id = presc_id = user_id = None
    kb_ids: list[uuid.UUID] = []
    try:
        async with factory() as session:
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

            kb = KnowledgeBase(
                name=f"KB-{tag}", owner_id=user.id, visibility="private"
            )
            other_kb = KnowledgeBase(
                name=f"KB-other-{tag}", owner_id=user.id, visibility="private"
            )
            session.add_all([kb, other_kb])
            await session.flush()

            herb = Herb(
                name=f"测试中药-{tag}",
                aliases=[f"别名-{tag}"],
                properties="甘，平",
                source=f"测试出处-{tag}",
            )
            prescription = Prescription(name=f"测试方剂-{tag}", aliases=[], source="")
            session.add_all([herb, prescription])
            await session.flush()

            session.add(
                PrescriptionIngredient(
                    prescription_id=prescription.id,
                    herb_id=herb.id,
                    amount=Decimal("9"),
                    unit="克",
                    role="君",
                )
            )
            # KG 可见性口径：资源必须挂载到 KB 才会进入该库的检索结果
            session.add_all(
                [
                    KnowledgeBaseResource(
                        knowledge_base_id=kb.id,
                        resource_type="herb",
                        resource_id=herb.id,
                    ),
                    KnowledgeBaseResource(
                        knowledge_base_id=kb.id,
                        resource_type="prescription",
                        resource_id=prescription.id,
                    ),
                ]
            )
            await session.commit()

            herb_id, presc_id, user_id = herb.id, prescription.id, user.id
            kb_ids = [kb.id, other_kb.id]

            # ① 真实全量构建（与生产同一入口）
            result = await KgService(session).build()
            assert result.node_count >= 2, result.to_dict()
            assert result.edge_count >= 1, result.to_dict()

            retriever = KgRetriever(session)

            # ② 未挂载该资源的 KB 检索不到任何事实（可见性隔离）
            other_facts, _ = await retriever.facts([other_kb.id], _analysis(herb.name))
            assert other_facts == [], "未挂载的资源不得进入其他知识库的检索结果"

            # ③ 已挂载的 KB 能检索到 contains 事实
            facts, nodes = await retriever.facts([kb.id], _analysis(herb.name))
            assert facts, "已挂载资源的组成关系必须可被 KG 检索到"
            fact = next(f for f in facts if f.relation == KG_RELATION_CONTAINS)
            assert fact.provenance == KG_PROVENANCE_INGREDIENT, fact.provenance
            assert fact.description == "9克；君", fact.description

            # ④ 溯源：边两端节点回指资源配置表记录
            src = nodes[fact.source_node_id]
            tgt = nodes[fact.target_node_id]
            assert (src.resource_type, src.resource_id) == (
                "prescription",
                prescription.id,
            )
            assert (tgt.resource_type, tgt.resource_id) == ("herb", herb.id)

            src_record = await session.get(Prescription, src.resource_id)
            tgt_record = await session.get(Herb, tgt.resource_id)
            assert src_record is not None and src_record.name == prescription.name
            assert tgt_record is not None and tgt_record.name == herb.name
            assert tgt_record.properties == "甘，平"
            assert tgt_record.source == f"测试出处-{tag}"
    finally:
        if herb_id is not None and presc_id is not None and user_id is not None:
            await _cleanup(
                factory,
                herb_id=herb_id,
                presc_id=presc_id,
                kb_ids=kb_ids,
                user_id=user_id,
            )
        await engine.dispose()
