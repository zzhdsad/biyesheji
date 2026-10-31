"""索引一致性服务测试（PG ↔ Milvus）。

使用内存向量存储构造确定性的「PG 期望 vs 向量库实际」差异，
覆盖 TASK 要求：缺失向量、重复向量、孤儿向量检测、
不准确标记修正、重建与失败重试，以及**未经确认不得删除向量**。
"""

from __future__ import annotations

import asyncio
import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from src.application.index_state_service import IndexStateService
from src.core.config import settings
from src.domain.models import (
    INDEX_STATUS_FAILED,
    INDEX_STATUS_INDEXED,
    INDEX_STATUS_NOT_INDEXED,
    INDEX_STATUS_STALE,
    Chunk,
    Document,
    KnowledgeBase,
    User,
)
from src.infrastructure.milvus_store import InMemoryVectorStore, VectorRow


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

# 容器内 /app/pyproject.toml 不存在，pytest 读不到 asyncio_mode=auto，
# 故统一使用 @pytest.mark.anyio（与仓库既有异步测试一致）。


class _DupStore(InMemoryVectorStore):
    """返回重复行的存储：模拟重复写入导致同一分片多条向量。"""

    def query_rows(self, doc_id: str) -> list[dict]:
        rows = super().query_rows(doc_id)
        return rows + [dict(rows[0])] if rows else rows


class _BrokenStore(InMemoryVectorStore):
    """查询即失败：模拟 Milvus 不可用。"""

    def query_rows(self, doc_id: str) -> list[dict]:
        raise RuntimeError("Milvus 连接失败")


def _new_session():
    engine = create_async_engine(settings.DATABASE_URL)
    return engine, async_sessionmaker(engine, expire_on_commit=False)


def _vector(chunk_id: str, doc_id: str, kb_id: str, index: int) -> VectorRow:
    return VectorRow(
        id=chunk_id,
        doc_id=doc_id,
        kb_id=kb_id,
        chunk_index=index,
        content=f"内容{index}",
        page_num=None,
        title_path=None,
        dense_vector=[0.1, 0.2, 0.3],
    )


async def _seed_document(session, tag: str, *, chunk_count: int = 3, index_status: str = INDEX_STATUS_NOT_INDEXED):
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
    doc = Document(
        kb_id=kb.id,
        file_name=f"{tag}.pdf",
        file_type="pdf",
        file_size=1024,
        parse_status="success",
        chunk_count=chunk_count,
        index_status=index_status,
    )
    session.add(doc)
    await session.flush()
    chunks = []
    for i in range(chunk_count):
        chunk = Chunk(
            doc_id=doc.id, kb_id=kb.id, chunk_index=i, content=f"内容{i}", page_num=None
        )
        session.add(chunk)
        chunks.append(chunk)
    await session.flush()
    return user, kb, doc, chunks


async def _cleanup(session, kb_id: uuid.UUID) -> None:
    kb = await session.get(KnowledgeBase, kb_id)
    if kb is not None:
        await session.delete(kb)
        await session.commit()


# ============================================================================
# 差异检测
# ============================================================================


@pytest.mark.anyio
@pytest.mark.skipif(not PG_AVAILABLE, reason="PostgreSQL 未启动")
async def test_diff_detects_missing_vectors():
    engine, factory = _new_session()
    async with factory() as session:
        _, kb, doc, chunks = await _seed_document(
            session, f"miss-{uuid.uuid4().hex[:8]}", chunk_count=3,
            index_status=INDEX_STATUS_INDEXED,  # 标记已索引，实际只写了 1 条
        )
        store = InMemoryVectorStore()
        store.insert([_vector(str(chunks[0].id), str(doc.id), str(kb.id), 0)])
        svc = IndexStateService(session, store=store)
        try:
            report = await svc.diff(kb_id=kb.id)

            assert report.scanned_documents == 1
            diff = report.docs[0]
            assert diff.expected_chunks == 3
            assert diff.actual_vectors == 1
            assert len(diff.missing_chunk_ids) == 2
            assert diff.suggested_status == INDEX_STATUS_STALE
            assert diff.flag_inaccurate is True  # 标了 indexed 但实际缺 2 条
            assert report.total_missing == 2
            assert report.clean is False
        finally:
            await _cleanup(session, kb.id)
    await engine.dispose()


@pytest.mark.anyio
@pytest.mark.skipif(not PG_AVAILABLE, reason="PostgreSQL 未启动")
async def test_diff_detects_orphan_vectors():
    engine, factory = _new_session()
    async with factory() as session:
        _, kb, doc, chunks = await _seed_document(session, f"orph-{uuid.uuid4().hex[:8]}", chunk_count=2)
        store = InMemoryVectorStore()
        store.insert(
            [
                _vector(str(chunks[0].id), str(doc.id), str(kb.id), 0),
                _vector(str(chunks[1].id), str(doc.id), str(kb.id), 1),
                # 已被删除的分片留下的孤儿向量
                _vector(str(uuid.uuid4()), str(doc.id), str(kb.id), 2),
            ]
        )
        svc = IndexStateService(session, store=store)
        try:
            report = await svc.diff(doc_ids=[doc.id])

            diff = report.docs[0]
            assert len(diff.orphan_vector_ids) == 1
            assert len(diff.missing_chunk_ids) == 0
            # 分片全在，多余的是孤儿 → 状态应判为已索引，孤儿需单独清理
            assert diff.suggested_status == INDEX_STATUS_INDEXED
            assert report.total_orphan == 1
        finally:
            await _cleanup(session, kb.id)
    await engine.dispose()


@pytest.mark.anyio
@pytest.mark.skipif(not PG_AVAILABLE, reason="PostgreSQL 未启动")
async def test_diff_detects_duplicate_vectors():
    engine, factory = _new_session()
    async with factory() as session:
        _, kb, doc, chunks = await _seed_document(session, f"dupv-{uuid.uuid4().hex[:8]}", chunk_count=1)
        store = _DupStore()
        store.insert([_vector(str(chunks[0].id), str(doc.id), str(kb.id), 0)])
        svc = IndexStateService(session, store=store)
        try:
            report = await svc.diff(doc_ids=[doc.id])

            diff = report.docs[0]
            assert len(diff.duplicate_vector_ids) == 1
            assert diff.actual_vectors == 1  # 去重后仍是 1 条
            assert report.total_duplicate == 1
        finally:
            await _cleanup(session, kb.id)
    await engine.dispose()


@pytest.mark.anyio
@pytest.mark.skipif(not PG_AVAILABLE, reason="PostgreSQL 未启动")
async def test_diff_reports_clean_state():
    engine, factory = _new_session()
    async with factory() as session:
        _, kb, doc, chunks = await _seed_document(session, f"clean-{uuid.uuid4().hex[:8]}", chunk_count=2)
        store = InMemoryVectorStore()
        store.insert(
            [_vector(str(c.id), str(doc.id), str(kb.id), i) for i, c in enumerate(chunks)]
        )
        svc = IndexStateService(session, store=store)
        try:
            # 先把标记改对，再校验应为「干净」
            report = await svc.diff(doc_ids=[doc.id])
            await svc.apply(report, fix_flags=True)
            await session.commit()

            report = await svc.diff(doc_ids=[doc.id])
            assert report.clean is True
            assert report.stale_flag_docs == []
        finally:
            await _cleanup(session, kb.id)
    await engine.dispose()


@pytest.mark.anyio
@pytest.mark.skipif(not PG_AVAILABLE, reason="PostgreSQL 未启动")
async def test_diff_is_read_only():
    engine, factory = _new_session()
    async with factory() as session:
        _, kb, doc, chunks = await _seed_document(
            session, f"ro-{uuid.uuid4().hex[:8]}", index_status=INDEX_STATUS_INDEXED
        )
        store = InMemoryVectorStore()  # 一条向量都没有
        svc = IndexStateService(session, store=store)
        try:
            report = await svc.diff(doc_ids=[doc.id])
            assert report.docs[0].suggested_status == INDEX_STATUS_NOT_INDEXED

            # diff 只读：数据库状态不应被改动
            await session.refresh(doc)
            assert doc.index_status == INDEX_STATUS_INDEXED
            assert len(store.query_rows(str(doc.id))) == 0
        finally:
            await _cleanup(session, kb.id)
    await engine.dispose()


@pytest.mark.anyio
@pytest.mark.skipif(not PG_AVAILABLE, reason="PostgreSQL 未启动")
async def test_diff_survives_milvus_outage():
    engine, factory = _new_session()
    async with factory() as session:
        _, kb, doc, _ = await _seed_document(session, f"down-{uuid.uuid4().hex[:8]}")
        svc = IndexStateService(session, store=_BrokenStore())
        try:
            report = await svc.diff(doc_ids=[doc.id])

            assert report.milvus_available is False
            assert "Milvus" in report.error
        finally:
            await _cleanup(session, kb.id)
    await engine.dispose()


# ============================================================================
# 修复：标记修正 / 删除需确认
# ============================================================================


@pytest.mark.anyio
@pytest.mark.skipif(not PG_AVAILABLE, reason="PostgreSQL 未启动")
async def test_apply_fixes_inaccurate_flags():
    engine, factory = _new_session()
    async with factory() as session:
        _, kb, doc, chunks = await _seed_document(
            session, f"fix-{uuid.uuid4().hex[:8]}", chunk_count=2,
            index_status=INDEX_STATUS_INDEXED,  # 不准确：实际 0 条向量
        )
        store = InMemoryVectorStore()
        svc = IndexStateService(session, store=store)
        try:
            report = await svc.diff(doc_ids=[doc.id])
            result = await svc.apply(report, fix_flags=True)
            await session.commit()

            assert result.flags_updated == 1
            assert result.orphans_deleted == 0
            await session.refresh(doc)
            assert doc.index_status == INDEX_STATUS_NOT_INDEXED
            assert doc.vector_count == 0
        finally:
            await _cleanup(session, kb.id)
    await engine.dispose()


@pytest.mark.anyio
@pytest.mark.skipif(not PG_AVAILABLE, reason="PostgreSQL 未启动")
async def test_apply_refuses_deletion_without_confirmation():
    engine, factory = _new_session()
    async with factory() as session:
        _, kb, doc, chunks = await _seed_document(session, f"noc-{uuid.uuid4().hex[:8]}", chunk_count=1)
        store = InMemoryVectorStore()
        store.insert([_vector(str(uuid.uuid4()), str(doc.id), str(kb.id), 0)])  # 孤儿
        svc = IndexStateService(session, store=store)
        try:
            report = await svc.diff(doc_ids=[doc.id])
            assert report.total_orphan == 1

            with pytest.raises(ValueError) as exc:
                await svc.apply(report, delete_orphans=True)  # 未确认
            assert "confirmed" in str(exc.value)

            # 未确认 → 向量必须还在
            assert len(store.query_rows(str(doc.id))) == 1
        finally:
            await _cleanup(session, kb.id)
    await engine.dispose()


@pytest.mark.anyio
@pytest.mark.skipif(not PG_AVAILABLE, reason="PostgreSQL 未启动")
async def test_apply_deletes_orphans_when_confirmed():
    engine, factory = _new_session()
    async with factory() as session:
        _, kb, doc, chunks = await _seed_document(session, f"ok-{uuid.uuid4().hex[:8]}", chunk_count=1)
        store = InMemoryVectorStore()
        store.insert(
            [
                _vector(str(chunks[0].id), str(doc.id), str(kb.id), 0),
                _vector(str(uuid.uuid4()), str(doc.id), str(kb.id), 1),  # 孤儿
            ]
        )
        svc = IndexStateService(session, store=store)
        try:
            report = await svc.diff(doc_ids=[doc.id])
            result = await svc.apply(report, delete_orphans=True, confirmed=True)
            await session.commit()

            assert result.orphans_deleted == 1
            remaining = store.query_rows(str(doc.id))
            assert len(remaining) == 1
            assert remaining[0]["id"] == str(chunks[0].id)  # 正常向量保留
        finally:
            await _cleanup(session, kb.id)
    await engine.dispose()


# ============================================================================
# 重建与失败重试
# ============================================================================


@pytest.mark.anyio
@pytest.mark.skipif(not PG_AVAILABLE, reason="PostgreSQL 未启动")
async def test_mark_for_rebuild_resets_missing_documents():
    engine, factory = _new_session()
    async with factory() as session:
        _, kb, doc, _ = await _seed_document(
            session, f"rb-{uuid.uuid4().hex[:8]}", chunk_count=2,
            index_status=INDEX_STATUS_STALE,
        )
        svc = IndexStateService(session, store=InMemoryVectorStore())
        try:
            marked = await svc.mark_for_rebuild(doc_ids=[doc.id])
            await session.commit()

            assert marked == 1
            await session.refresh(doc)
            assert doc.index_status == INDEX_STATUS_NOT_INDEXED
            assert doc.index_error == ""
        finally:
            await _cleanup(session, kb.id)
    await engine.dispose()


@pytest.mark.anyio
@pytest.mark.skipif(not PG_AVAILABLE, reason="PostgreSQL 未启动")
async def test_retry_failed_resets_failed_documents():
    engine, factory = _new_session()
    async with factory() as session:
        _, kb, doc, _ = await _seed_document(
            session, f"rt-{uuid.uuid4().hex[:8]}", chunk_count=1,
            index_status=INDEX_STATUS_FAILED,
        )
        doc.index_error = "向量化失败：连接超时"
        await session.flush()
        svc = IndexStateService(session, store=InMemoryVectorStore())
        try:
            retried = await svc.retry_failed(kb_id=kb.id)
            await session.commit()

            assert retried == 1
            await session.refresh(doc)
            assert doc.index_status == INDEX_STATUS_NOT_INDEXED
            assert doc.index_error == ""
        finally:
            await _cleanup(session, kb.id)
    await engine.dispose()


@pytest.mark.anyio
@pytest.mark.skipif(not PG_AVAILABLE, reason="PostgreSQL 未启动")
async def test_report_is_reviewable():
    """报告必须包含可审查的明细（数量 + ID + 当前/建议状态）。"""
    engine, factory = _new_session()
    async with factory() as session:
        _, kb, doc, chunks = await _seed_document(
            session, f"rep-{uuid.uuid4().hex[:8]}", chunk_count=2,
            index_status=INDEX_STATUS_INDEXED,
        )
        svc = IndexStateService(session, store=InMemoryVectorStore())
        try:
            payload = (await svc.diff(doc_ids=[doc.id])).to_dict()

            assert payload["clean"] is False
            assert payload["totals"]["missing_vectors"] == 2
            assert payload["documents"][0]["missing_chunk_ids"]
            assert payload["documents"][0]["current_status"] == INDEX_STATUS_INDEXED
            assert payload["documents"][0]["suggested_status"] == INDEX_STATUS_NOT_INDEXED
        finally:
            await _cleanup(session, kb.id)
    await engine.dispose()
