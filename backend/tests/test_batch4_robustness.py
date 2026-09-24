"""第四轮 Batch 4（健壮性与一致性）回归测试。

覆盖：
- BUG-012 审计写入与业务提交解耦（审计失败不得回滚业务）
- BUG-017 Reranker 不可用时降级为 RRF 顺序（不得 422 中断）
- BUG-021 老集合 Resource 命中不得按 UUID 查 documents 表
- BUG-029 健康检查如实报告降级 + 关闭 Redis 连接（见 test_health.py）
- BUG-034 模型未配置 → 503（语义正确的状态码）
- BUG-035 SSE 心跳与总超时
- BUG-036 失败路径不留下"无回答"的孤儿用户消息
- BUG-037 批量删除会话时缓存清理失败可见
- BUG-038 反馈归属 + 唯一约束 + 仅助手消息可反馈
- BUG-043 purge 必须先经过回收站
- BUG-044 所有权不得转移给已删除/停用用户

需要 PostgreSQL 的用例统一 skip（其余为纯逻辑用例）。
"""

import asyncio
import hashlib
import time
import uuid
from contextlib import asynccontextmanager

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from src.main import app

from src.application.audit_service import AuditService
from src.application.rag_service import RagService
from src.core.config import settings
from src.core.exceptions import AppException
from src.domain.models import AuditLog, Conversation, KnowledgeBase, User
from src.infrastructure.rerank import RerankError
from src.infrastructure.redis_client import InMemoryConversationCache
from tests.test_chat import _upload_doc
from tests.test_chat_stream import _parse_sse
from tests.test_parse import _make_kb
from tests.test_upload import PG_AVAILABLE


@asynccontextmanager
async def _session():
    """独立 engine 的一次性会话，避免连接池绑定到其他事件循环。"""
    engine = create_async_engine(settings.DATABASE_URL)
    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as db:
            yield db
    finally:
        await engine.dispose()


class _FakeStore:
    """只提供两路召回的向量库桩（不触达 Milvus）。"""

    def __init__(self, hits: list[dict]) -> None:
        self.hits = hits

    def search(self, *_args, **_kwargs) -> list[dict]:
        return list(self.hits)

    def search_sparse(self, *_args, **_kwargs) -> list[dict]:
        return []


class _BoomRerank:
    """始终失败的重排器（模拟 Reranker 服务不可用）。"""

    def rerank(self, question: str, hits: list[dict], top_k: int) -> list[dict]:
        raise RerankError("重排服务不可用（测试注入）")


def _hit(doc_id: str, content: str, score: float = 0.9) -> dict:
    return {
        "id": f"chunk-{doc_id[:8]}",
        "doc_id": doc_id,
        "kb_id": None,
        "content": content,
        "score": score,
        "chunk_index": 0,
    }


# ── BUG-012：审计与业务解耦 ──────────────────────────────────────────────────


@pytest.mark.skipif(not PG_AVAILABLE, reason="PostgreSQL 未启动")
async def test_audit_failure_does_not_roll_back_business():
    """审计写入失败只回滚 SAVEPOINT，业务变更照常提交（旧实现会静默丢数据）。"""
    kb_name = f"审计解耦测试-{uuid.uuid4().hex[:8]}"
    async with _session() as db:
        owner = (await db.scalars(select(User).limit(1))).first()
        assert owner is not None
        kb = KnowledgeBase(name=kb_name, owner_id=owner.id, visibility="private")
        db.add(kb)
        await db.flush()  # 资源 CRUD 的实际姿态：只 flush，靠 audit.log() 提交

        svc = AuditService(db)
        # operation 列为 String(64)：超长值使审计 INSERT 失败（业务已 flush 未提交）
        await svc.log(
            operator_id=owner.id,
            operator_name="tester",
            operation="x" * 200,
            target_type="kb",
            target_id=str(kb.id),
        )
        kb_id = kb.id

    try:
        async with _session() as db:
            stored = await db.scalar(
                select(KnowledgeBase).where(KnowledgeBase.id == kb_id)
            )
            assert stored is not None, "审计失败不得连带回滚业务变更"
            assert stored.name == kb_name
            audit = await db.scalar(
                select(AuditLog).where(AuditLog.target_id == str(kb_id))
            )
            assert audit is None, "审计写入失败时不应落库审计行"
    finally:
        async with _session() as db:
            kb = await db.get(KnowledgeBase, kb_id)
            if kb is not None:
                await db.delete(kb)
                await db.commit()


@pytest.mark.skipif(not PG_AVAILABLE, reason="PostgreSQL 未启动")
async def test_audit_and_business_commit_atomically_when_healthy():
    """DB 健康时审计与业务仍在同一事务内落盘（SAVEPOINT 不牺牲原子性）。"""
    kb_name = f"审计原子测试-{uuid.uuid4().hex[:8]}"
    async with _session() as db:
        owner = (await db.scalars(select(User).limit(1))).first()
        kb = KnowledgeBase(name=kb_name, owner_id=owner.id, visibility="private")
        db.add(kb)
        await db.flush()
        await AuditService(db).log(
            operator_id=owner.id,
            operator_name="tester",
            operation="create",
            target_type="kb",
            target_id=str(kb.id),
        )
        kb_id = kb.id

    try:
        async with _session() as db:
            assert await db.get(KnowledgeBase, kb_id) is not None
            audit = await db.scalar(
                select(AuditLog).where(AuditLog.target_id == str(kb_id))
            )
            assert audit is not None, "正常路径下审计必须落库"
            assert audit.operation == "create"
    finally:
        async with _session() as db:
            kb = await db.get(KnowledgeBase, kb_id)
            if kb is not None:
                await db.delete(kb)
                await db.commit()
            await db.execute(
                AuditLog.__table__.delete().where(AuditLog.target_id == str(kb_id))
            )
            await db.commit()


# ── BUG-017：Reranker 降级 ───────────────────────────────────────────────────


@pytest.mark.skipif(not PG_AVAILABLE, reason="PostgreSQL 未启动")
async def test_rerank_failure_degrades_to_rrf_order():
    """Reranker 不可用不得中断问答：降级为 RRF 融合顺序（与 KG 一致的语义）。"""
    hits = [_hit(hashlib.sha256(b"a").hexdigest(), "内容一", 0.9),
            _hit(hashlib.sha256(b"b").hexdigest(), "内容二", 0.8)]
    async with _session() as db:
        svc = RagService(
            db,
            store=_FakeStore(hits),
            rerank=_BoomRerank(),
            cache=InMemoryConversationCache(),
        )
        got = await svc._retrieve([uuid.uuid4()], "测试问题")

    assert len(got) == len(hits), "重排失败应保留 RRF 融合结果，而不是抛出 422"
    assert {h["id"] for h in got} == {h["id"] for h in hits}


# ── BUG-021：老集合 Resource 命中不查 documents 表 ───────────────────────────


async def test_old_collection_resource_hits_are_not_queried_as_documents():
    """无动态字段的老集合：Resource 行 doc_id 为 SHA256 且取不到 resource_type，
    旧实现会把它送进 Document.id（UUID 列）→ PG 报 invalid input syntax for uuid。"""
    sha = hashlib.sha256(b"herb:1:kb:1").hexdigest()
    hits = [_hit(sha, "黄芪补气", 0.8)]

    async with _session() as db:
        svc = RagService(db, store=_FakeStore([]), cache=InMemoryConversationCache())
        await svc._enrich_hits_with_doc_name(hits)  # 不得抛 DataError/ProgrammingError

    assert hits[0]["doc_name"] == "资源"
    assert hits[0]["source_kind"] == "resource", "不得误标为 document 证据"


async def test_kg_hits_keep_kg_source_kind():
    """KG 命中 doc_id = "kg:<id>"（非 UUID）→ 归入资源分支，保留 source_kind=kg。"""
    hits = [{
        "id": "kg:1", "doc_id": "kg:1", "kb_id": None, "content": "黄芪-补气",
        "score": 0.7, "chunk_index": 0, "source_kind": "kg",
        "resource_type": "herb", "resource_name": "黄芪",
    }]
    async with _session() as db:
        svc = RagService(db, store=_FakeStore([]), cache=InMemoryConversationCache())
        await svc._enrich_hits_with_doc_name(hits)

    assert hits[0]["source_kind"] == "kg"
    assert hits[0]["doc_name"] == "黄芪"


# ── BUG-035：SSE 心跳与总超时 ────────────────────────────────────────────────


async def test_heartbeat_frames_keep_stream_alive(monkeypatch):
    """等待业务事件的间隙发送注释帧心跳（代理不会因空闲掐断连接）。"""
    from src.api.routes.chat import _with_heartbeat

    monkeypatch.setattr(settings, "SSE_HEARTBEAT_INTERVAL_SECONDS", 1)

    async def _slow():
        await asyncio.sleep(1.2)
        yield "event: delta\ndata: {\"content\": \"x\"}\n\n"

    out = [chunk async for chunk in _with_heartbeat(_slow())]
    assert any(c.startswith(": ping") for c in out), "长等待期间必须有心跳帧"
    assert out[-1].startswith("event: delta"), "心跳不得改变业务事件顺序"


async def test_total_timeout_emits_error_event(monkeypatch):
    """总超时到期先发 error 事件再关闭流，避免客户端无限等待。"""
    from src.api.routes.chat import _with_heartbeat

    monkeypatch.setattr(settings, "SSE_TOTAL_TIMEOUT_SECONDS", 1)

    async def _stuck():
        await asyncio.sleep(5)
        yield "event: done\ndata: {}\n\n"

    out = [chunk async for chunk in _with_heartbeat(_stuck())]
    assert any("event: error" in c for c in out), "超时必须下发 error 事件"
    assert not any("event: done" in c for c in out)


# ── BUG-034 / 036 / 037 / 038 / 043 / 044：API 层 ────────────────────────────


@pytest.mark.skipif(not PG_AVAILABLE, reason="PostgreSQL 未启动")
def test_ask_returns_503_when_model_not_configured(monkeypatch):
    """BUG-034：模型未配置是服务端缺配置 → 503（旧实现误用 412）。"""
    import src.application.model_config_service as mcs

    async def _unconfigured(db):
        return {
            "llm_provider": "openai", "llm_base_url": "", "llm_model": "",
            "embedding_backend": "mock", "embedding_model": "mock",
            "rerank_backend": "mock", "rerank_model": "mock",
            "hyde_enabled": False, "hyde_backend": "mock",
        }

    monkeypatch.setattr(mcs, "get_effective_config_cached", _unconfigured)

    with TestClient(app) as client:
        kb_id = _make_kb(client)
        resp = client.post(
            "/api/v1/chat/ask", json={"question": "工时？", "kb_ids": [kb_id]}
        )
    assert resp.status_code == 503, resp.text


@pytest.mark.skipif(not PG_AVAILABLE, reason="PostgreSQL 未启动")
def test_stream_failure_removes_orphan_user_message(monkeypatch):
    """BUG-036：检索失败不留下"有提问无回答"的孤儿消息（重试会产生重复提问）。"""

    async def _boom(self, *_args, **_kwargs):
        raise AppException(422, "检索失败（测试注入）")

    monkeypatch.setattr(RagService, "_retrieve", _boom)

    with TestClient(app) as client:
        kb_id = _make_kb(client)
        resp = client.post(
            "/api/v1/chat/ask-stream", json={"question": "工时？", "kb_ids": [kb_id]}
        )
        assert resp.status_code == 200, resp.text
        events = _parse_sse(resp.text)
        kinds = [e for e, _ in events]
        assert "start" in kinds and "error" in kinds, events

        conv_id = next(d["conversation_id"] for e, d in events if e == "start")
        msgs = client.get(f"/api/v1/chat/conversations/{conv_id}/messages").json()
    assert msgs == [], "失败路径不得残留用户消息"


@pytest.mark.skipif(not PG_AVAILABLE, reason="PostgreSQL 未启动")
def test_delete_all_conversations_reports_cache_cleanup_failures(
    auth_client, monkeypatch
):
    """BUG-037：单条缓存清理失败不得中断其余清理，且失败清单随响应返回。"""
    from tests.test_batch1_security import _admin_token, _create_user, _ready_headers

    admin = _admin_token(auth_client)
    user = _create_user(auth_client, admin)
    headers = _ready_headers(auth_client, user)
    uid = uuid.UUID(user["id"])

    async def _seed() -> list[str]:
        async with _session() as db:
            convs = [
                Conversation(user_id=uid, kb_ids=[]),
                Conversation(user_id=uid, kb_ids=[]),
            ]
            db.add_all(convs)
            await db.commit()
            return [str(c.id) for c in convs]

    conv_ids = asyncio.run(_seed())
    failing = conv_ids[0]

    class _FlakyCache:
        def __init__(self) -> None:
            self.deleted: list[str] = []

        async def delete(self, conversation_id) -> None:
            if str(conversation_id) == failing:
                raise RuntimeError("Redis 抖动（测试注入）")
            self.deleted.append(str(conversation_id))

    cache = _FlakyCache()
    monkeypatch.setattr(
        "src.infrastructure.redis_client.get_conversation_cache", lambda: cache
    )

    resp = auth_client.delete("/api/v1/chat/conversations", headers=headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["deleted_count"] == 2
    assert body["cache_cleanup_failed"] == [failing], "失败必须如实上报"
    assert cache.deleted == [conv_ids[1]], "单条失败不得中断其余会话的缓存清理"


@pytest.mark.skipif(not PG_AVAILABLE, reason="PostgreSQL 未启动")
def test_feedback_rejects_non_assistant_message():
    """BUG-038：只能对助手消息反馈。"""
    with TestClient(app) as client:
        kb_id = _make_kb(client)
        _upload_doc(client, kb_id)
        resp = client.post(
            "/api/v1/chat/ask",
            json={"question": "公司实行什么工时制度？", "kb_ids": [kb_id]},
        )
        assert resp.status_code == 200, resp.text
        conv_id = resp.json()["conversation_id"]

        msgs = client.get(f"/api/v1/chat/conversations/{conv_id}/messages").json()
        user_msg_id = next(m["id"] for m in msgs if m["role"] == "user")

        resp = client.post(
            "/api/v1/feedbacks", json={"message_id": user_msg_id, "rating": 1}
        )
    assert resp.status_code == 400, resp.text


@pytest.mark.skipif(not PG_AVAILABLE, reason="PostgreSQL 未启动")
def test_feedback_carries_owner_and_overwrites_per_user():
    """BUG-038：反馈归属当前用户，同一用户重复提交为覆盖更新（唯一约束兜底）。"""
    with TestClient(app) as client:
        kb_id = _make_kb(client)
        _upload_doc(client, kb_id)
        msg_id = client.post(
            "/api/v1/chat/ask", json={"question": "工时？", "kb_ids": [kb_id]}
        ).json()["message_id"]

        r1 = client.post("/api/v1/feedbacks", json={"message_id": msg_id, "rating": 1})
        assert r1.status_code == 200, r1.text
        first_id = r1.json()["id"]

        r2 = client.post(
            "/api/v1/feedbacks",
            json={"message_id": msg_id, "rating": -1, "comment": "漏了午休"},
        )
    assert r2.status_code == 200, r2.text
    assert r2.json()["id"] == first_id, "同一用户重复提交应是覆盖更新"


@pytest.mark.skipif(not PG_AVAILABLE, reason="PostgreSQL 未启动")
def test_purge_requires_soft_deleted_kb():
    """BUG-043：purge 不可逆，必须先经过回收站（旧实现可绕过保护期直接硬删）。"""
    with TestClient(app) as client:
        kb_id = _make_kb(client)

        resp = client.delete(f"/api/v1/kb/{kb_id}/purge")
        assert resp.status_code == 400, resp.text
        assert "回收站" in resp.json()["message"]

        assert client.delete(f"/api/v1/kb/{kb_id}").status_code == 200
        resp = client.delete(f"/api/v1/kb/{kb_id}/purge")
    assert resp.status_code == 200, resp.text
    assert resp.json()["purged"] is True


@pytest.mark.skipif(not PG_AVAILABLE, reason="PostgreSQL 未启动")
def test_transfer_ownership_rejects_disabled_user():
    """BUG-044：成员行会随用户停用/软删残留，所有权不得转移给不可用账号。"""
    with TestClient(app) as client:
        ts = int(time.time() * 1000)
        resp = client.post(
            "/api/v1/users",
            json={"username": f"b4_{ts}", "email": f"b4_{ts}@test.com", "role": "member"},
        )
        assert resp.status_code == 201, resp.text
        target_id = uuid.UUID(resp.json()["user"]["id"])

        kb_id = _make_kb(client)
        resp = client.post(
            f"/api/v1/kb/{kb_id}/members",
            json={"user_id": str(target_id), "role": "admin"},
        )
        assert resp.status_code == 201, resp.text

        async def _deactivate() -> None:
            async with _session() as db:
                u = await db.get(User, target_id)
                u.is_active = False
                await db.commit()

        asyncio.run(_deactivate())

        resp = client.post(
            f"/api/v1/kb/{kb_id}/transfer-ownership",
            json={"new_owner_user_id": str(target_id)},
        )
        assert resp.status_code == 400, resp.text
        assert "停用" in resp.json()["message"] or "删除" in resp.json()["message"]

        # 恢复后转移成功（控制组：证明拦截来自账号状态而非成员关系）
        async def _reactivate() -> None:
            async with _session() as db:
                u = await db.get(User, target_id)
                u.is_active = True
                await db.commit()

        asyncio.run(_reactivate())
        resp = client.post(
            f"/api/v1/kb/{kb_id}/transfer-ownership",
            json={"new_owner_user_id": str(target_id)},
        )
        assert resp.status_code == 200, resp.text
