"""TASK-008 Stage 4-3：Resource 向量写入 Milvus + KB 挂载/卸载集成测试。

验证：
1. 挂载 Resource 后向量写入向量库（InMemoryVectorStore，conftest 自动注入）
2. 卸载 Resource 后向量被删除
3. 多 KB 挂载同一 Resource 隔离：删除一个 KB 的向量不影响其他 KB
4. 重新挂载幂等：先清旧向量再插入，不会产生重复
5. 向量携带 resource_type/resource_id/resource_name 元数据
6. 权限检查不受影响：非 owner/admin 挂载/卸载 → 403
7. 四种 resource_type 均可挂载并写入向量

复用 test_knowledge_base_resources.py 的 KBRKeeper 模式，保证测试隔离与清理。
"""

import uuid

import pytest

import src.core.deps as deps_mod  # noqa: E402
from src.domain.models import User  # noqa: E402
from src.infrastructure import milvus_store  # noqa: E402
from src.application.resource_vector_service import make_doc_id  # noqa: E402
from tests.test_upload import PG_AVAILABLE  # noqa: E402

pytestmark = pytest.mark.skipif(not PG_AVAILABLE, reason="PostgreSQL 未启动")

_SUFFIX = uuid.uuid4().hex[:8]


class _KBRIntegrationKeeper:
    """记录测试创建的 KB / Resource / 挂载，统一回收（含向量清理）。"""

    def __init__(self, client) -> None:
        self.client = client
        self._created: list[tuple[str, str, dict | None]] = []

    def kb(self, name: str | None = None) -> dict:
        label = name or f"集成KB-{_SUFFIX}"
        resp = self.client.post("/api/v1/kb", json={"name": label, "visibility": "private"})
        assert resp.status_code == 201, resp.text
        body = resp.json()
        self._created.append(("kb", body["id"], None))
        return body

    def herb(self, name: str | None = None) -> dict:
        label = name or f"集成药-{_SUFFIX}"
        resp = self.client.post("/api/v1/herbs", json={"name": label})
        assert resp.status_code == 201, resp.text
        body = resp.json()
        self._created.append(("herb", body["id"], None))
        return body

    def prescription(self, name: str | None = None) -> dict:
        label = name or f"集成方-{_SUFFIX}"
        resp = self.client.post("/api/v1/prescriptions", json={"name": label})
        assert resp.status_code == 201, resp.text
        body = resp.json()
        self._created.append(("prescription", body["id"], None))
        return body

    def theory(self, name: str | None = None) -> dict:
        label = name or f"集成理论-{_SUFFIX}"
        resp = self.client.post("/api/v1/theories", json={"name": label})
        assert resp.status_code == 201, resp.text
        body = resp.json()
        self._created.append(("theory", body["id"], None))
        return body

    def literature(self, name: str | None = None) -> dict:
        label = name or f"集成文献-{_SUFFIX}"
        resp = self.client.post(
            "/api/v1/literatures",
            json={"name": label, "author": "测试作者", "dynasty": "汉"},
        )
        assert resp.status_code == 201, resp.text
        body = resp.json()
        self._created.append(("literature", body["id"], None))
        return body

    def mount(self, kb_id: str, resource_type: str, resource_id: str) -> dict:
        resp = self.client.post(
            f"/api/v1/kb/{kb_id}/resources",
            json={"resource_type": resource_type, "resource_id": resource_id},
        )
        assert resp.status_code == 201, resp.text
        body = resp.json()
        self._created.append(("mount", body["id"], {
            "kb_id": kb_id, "resource_type": resource_type, "resource_id": resource_id,
        }))
        return body

    def teardown(self) -> None:
        for kind, item_id, ctx in reversed(self._created):
            if kind == "mount":
                self.client.delete(
                    f"/api/v1/kb/{ctx['kb_id']}/resources/{ctx['resource_type']}/{ctx['resource_id']}"
                )
            elif kind == "herb":
                self.client.delete(f"/api/v1/herbs/{item_id}")
            elif kind == "prescription":
                self.client.delete(f"/api/v1/prescriptions/{item_id}")
            elif kind == "theory":
                self.client.delete(f"/api/v1/theories/{item_id}")
            elif kind == "literature":
                self.client.delete(f"/api/v1/literatures/{item_id}")
            elif kind == "kb":
                self.client.delete(f"/api/v1/kb/{item_id}")


@pytest.fixture
def kbr_int_keeper(client):
    k = _KBRIntegrationKeeper(client)
    yield k
    k.teardown()


def _make_other_user() -> User:
    return User(
        id=uuid.uuid4(),
        email=f"int-other-{_SUFFIX}@example.com",
        username=f"int-other-{_SUFFIX}",
        hashed_password="",
        role="member",
    )


def _store():
    """获取当前全局向量库（conftest 注入的 InMemoryVectorStore 或 MilvusStore）。"""
    return milvus_store.get_vector_store()


# ── 挂载写入向量 ────────────────────────────────────────────────────────────


def test_mount_herb_writes_vectors(kbr_int_keeper, client):
    """挂载 Herb 后向量库中存在对应的向量行。"""
    kb = kbr_int_keeper.kb()
    h = kbr_int_keeper.herb(name=f"向量写入药-{_SUFFIX}")
    kbr_int_keeper.mount(kb["id"], "herb", h["id"])

    doc_id = make_doc_id("herb", uuid.UUID(h["id"]), uuid.UUID(kb["id"]))
    rows = _store().query_rows(doc_id)
    assert len(rows) >= 1
    r = rows[0]
    assert r["resource_type"] == "herb"
    assert r["resource_id"] == h["id"]
    assert r["resource_name"] == f"向量写入药-{_SUFFIX}"
    assert r["kb_id"] == kb["id"]


def test_mount_all_four_resource_types(kbr_int_keeper, client):
    """四种 resource_type 均可挂载并写入向量。"""
    kb = kbr_int_keeper.kb()
    h = kbr_int_keeper.herb(name=f"四类药-{_SUFFIX}")
    p = kbr_int_keeper.prescription(name=f"四类方-{_SUFFIX}")
    t = kbr_int_keeper.theory(name=f"四类理论-{_SUFFIX}")
    l = kbr_int_keeper.literature(name=f"四类文献-{_SUFFIX}")

    kbr_int_keeper.mount(kb["id"], "herb", h["id"])
    kbr_int_keeper.mount(kb["id"], "prescription", p["id"])
    kbr_int_keeper.mount(kb["id"], "theory", t["id"])
    kbr_int_keeper.mount(kb["id"], "literature", l["id"])

    for rtype, rid in [
        ("herb", h["id"]),
        ("prescription", p["id"]),
        ("theory", t["id"]),
        ("literature", l["id"]),
    ]:
        doc_id = make_doc_id(rtype, uuid.UUID(rid), uuid.UUID(kb["id"]))
        rows = _store().query_rows(doc_id)
        assert len(rows) >= 1, f"{rtype} 向量未写入"
        assert rows[0]["resource_type"] == rtype


# ── 卸载删除向量 ────────────────────────────────────────────────────────────


def test_unmount_deletes_vectors(kbr_int_keeper, client):
    """卸载 Resource 后向量库中对应 doc_id 的向量被删除。"""
    kb = kbr_int_keeper.kb()
    h = kbr_int_keeper.herb(name=f"删除向量药-{_SUFFIX}")
    kbr_int_keeper.mount(kb["id"], "herb", h["id"])

    doc_id = make_doc_id("herb", uuid.UUID(h["id"]), uuid.UUID(kb["id"]))
    assert len(_store().query_rows(doc_id)) >= 1

    # 手动 DELETE 不走 keeper（避免 teardown 重复）
    resp = client.delete(f"/api/v1/kb/{kb['id']}/resources/herb/{h['id']}")
    assert resp.status_code == 200, resp.text
    assert resp.json()["unmounted"] is True

    # 向量已被删除
    assert len(_store().query_rows(doc_id)) == 0
    # 从 keeper 移除已删 mount，避免 teardown 重复 DELETE
    kbr_int_keeper._created = [
        c for c in kbr_int_keeper._created
        if not (c[0] == "mount" and c[2] and c[2].get("resource_id") == h["id"])
    ]


# ── 多 KB 隔离 ───────────────────────────────────────────────────────────────


def test_cross_kb_isolation_vectors(kbr_int_keeper, client):
    """同一 Herb 挂载到 KB1 和 KB2；卸载 KB1 的挂载不影响 KB2 的向量。"""
    kb1 = kbr_int_keeper.kb(name=f"隔离KB1-{_SUFFIX}")
    kb2 = kbr_int_keeper.kb(name=f"隔离KB2-{_SUFFIX}")
    h = kbr_int_keeper.herb(name=f"共享向量药-{_SUFFIX}")

    m1 = kbr_int_keeper.mount(kb1["id"], "herb", h["id"])
    kbr_int_keeper.mount(kb2["id"], "herb", h["id"])

    doc_id_1 = make_doc_id("herb", uuid.UUID(h["id"]), uuid.UUID(kb1["id"]))
    doc_id_2 = make_doc_id("herb", uuid.UUID(h["id"]), uuid.UUID(kb2["id"]))

    # 两个 KB 各有独立向量
    assert len(_store().query_rows(doc_id_1)) >= 1
    assert len(_store().query_rows(doc_id_2)) >= 1

    # 删除 KB1 的挂载
    resp = client.delete(f"/api/v1/kb/{kb1['id']}/resources/herb/{h['id']}")
    assert resp.status_code == 200
    # 从 keeper 移除已删 mount
    kbr_int_keeper._created = [
        c for c in kbr_int_keeper._created if c[1] != m1["id"]
    ]

    # KB1 向量已删除
    assert len(_store().query_rows(doc_id_1)) == 0
    # KB2 向量不受影响
    assert len(_store().query_rows(doc_id_2)) >= 1


# ── 重新挂载幂等 ─────────────────────────────────────────────────────────────


def test_remount_idempotent(kbr_int_keeper, client):
    """卸载后重新挂载同一资源：先清旧向量再插入，不产生重复。"""
    kb = kbr_int_keeper.kb()
    h = kbr_int_keeper.herb(name=f"幂等药-{_SUFFIX}")
    kbr_int_keeper.mount(kb["id"], "herb", h["id"])

    doc_id = make_doc_id("herb", uuid.UUID(h["id"]), uuid.UUID(kb["id"]))
    first_count = len(_store().query_rows(doc_id))
    assert first_count >= 1

    # 卸载
    resp = client.delete(f"/api/v1/kb/{kb['id']}/resources/herb/{h['id']}")
    assert resp.status_code == 200
    assert len(_store().query_rows(doc_id)) == 0
    # 从 keeper 移除已删 mount
    kbr_int_keeper._created = [
        c for c in kbr_int_keeper._created
        if not (c[0] == "mount" and c[2] and c[2].get("resource_id") == h["id"])
    ]

    # 重新挂载
    kbr_int_keeper.mount(kb["id"], "herb", h["id"])
    second_count = len(_store().query_rows(doc_id))
    assert second_count == first_count  # 数量一致，无重复


# ── 向量元数据 ───────────────────────────────────────────────────────────────


def test_vector_metadata_present(kbr_int_keeper, client):
    """向量行携带 resource_type/resource_id/resource_name 元数据。"""
    kb = kbr_int_keeper.kb()
    p = kbr_int_keeper.prescription(name=f"元数据方-{_SUFFIX}")
    kbr_int_keeper.mount(kb["id"], "prescription", p["id"])

    doc_id = make_doc_id("prescription", uuid.UUID(p["id"]), uuid.UUID(kb["id"]))
    rows = _store().query_rows(doc_id)
    assert len(rows) >= 1
    r = rows[0]
    assert r["resource_type"] == "prescription"
    assert r["resource_id"] == p["id"]
    assert r["resource_name"] == f"元数据方-{_SUFFIX}"
    assert r["kb_id"] == kb["id"]
    # source_type/era 为 None（资源表无此字段，不猜测）
    assert r.get("source_type") in (None, "", None)
    assert r.get("era") in (None, "", None)


# ── 权限检查 ───────────────────────────────────────────────────────────────


def test_mount_forbidden_403(client, monkeypatch):
    """非 owner 非 admin member 挂载他人 KB → 403。"""
    resp = client.post("/api/v1/kb", json={"name": f"权限KB-{_SUFFIX}"})
    kb_id = resp.json()["id"]
    try:
        other = _make_other_user()
        monkeypatch.setattr(deps_mod, "TEST_USER", other)
        resp = client.post(
            f"/api/v1/kb/{kb_id}/resources",
            json={"resource_type": "herb", "resource_id": str(uuid.uuid4())},
        )
        assert resp.status_code == 403
    finally:
        monkeypatch.undo()
        client.delete(f"/api/v1/kb/{kb_id}")


def test_unmount_forbidden_403(client, monkeypatch):
    """非 owner 非 admin member 卸载他人 KB 的资源 → 403。"""
    resp = client.post("/api/v1/kb", json={"name": f"权限KB-{_SUFFIX}"})
    kb_id = resp.json()["id"]
    try:
        other = _make_other_user()
        monkeypatch.setattr(deps_mod, "TEST_USER", other)
        resp = client.delete(f"/api/v1/kb/{kb_id}/resources/herb/{uuid.uuid4()}")
        assert resp.status_code == 403
    finally:
        monkeypatch.undo()
        client.delete(f"/api/v1/kb/{kb_id}")


# ── 现有回归：挂载/卸载基础行为仍正常 ──────────────────────────────────────


def test_mount_still_returns_resource_name(kbr_int_keeper, client):
    """挂载响应仍正确返回 resource_name（从资源表查询，非空）。"""
    kb = kbr_int_keeper.kb()
    h = kbr_int_keeper.herb(name=f"回归药-{_SUFFIX}")
    body = kbr_int_keeper.mount(kb["id"], "herb", h["id"])
    assert body["resource_name"] == f"回归药-{_SUFFIX}"
    assert body["resource_type"] == "herb"
    assert body["resource_id"] == h["id"]
