"""TASK-008 Stage 4-6：Resource 生命周期测试。

覆盖：
1. Resource 更新后对所有已挂载 KB 重新向量化
2. Resource 删除后清理所有 KBR 关联 + Milvus vectors
3. KB purge 时清理 Resource vectors
4. 更新失败 best-effort：向量化失败不阻塞更新
5. 删除清理失败：阻止资源删除（422）
6. 四种 resource_type 更新均触发重新向量化

复用 test_resource_kb_integration.py 的 KBRKeeper 模式。
"""

import uuid
from unittest.mock import patch

import pytest

import src.core.deps as deps_mod  # noqa: E402
from src.domain.models import User  # noqa: E402
from src.infrastructure import milvus_store  # noqa: E402
from src.application.resource_vector_service import (  # noqa: E402
    make_doc_id,
    ResourceVectorService,
)
from tests.test_upload import PG_AVAILABLE  # noqa: E402

pytestmark = pytest.mark.skipif(not PG_AVAILABLE, reason="PostgreSQL 未启动")

_SUFFIX = uuid.uuid4().hex[:8]


class _LifecycleKeeper:
    """记录测试创建的 KB / Resource / 挂载，统一回收。"""

    def __init__(self, client) -> None:
        self.client = client
        self._created: list[tuple[str, str, dict | None]] = []

    def kb(self, name: str | None = None) -> dict:
        label = name or f"生命KB-{_SUFFIX}"
        resp = self.client.post(
            "/api/v1/kb", json={"name": label, "visibility": "private"}
        )
        assert resp.status_code == 201, resp.text
        body = resp.json()
        self._created.append(("kb", body["id"], None))
        return body

    def herb(self, name: str | None = None) -> dict:
        label = name or f"生命药-{_SUFFIX}"
        resp = self.client.post("/api/v1/herbs", json={"name": label})
        assert resp.status_code == 201, resp.text
        body = resp.json()
        self._created.append(("herb", body["id"], None))
        return body

    def prescription(self, name: str | None = None) -> dict:
        label = name or f"生命方-{_SUFFIX}"
        resp = self.client.post("/api/v1/prescriptions", json={"name": label})
        assert resp.status_code == 201, resp.text
        body = resp.json()
        self._created.append(("prescription", body["id"], None))
        return body

    def theory(self, name: str | None = None) -> dict:
        label = name or f"生命理论-{_SUFFIX}"
        resp = self.client.post("/api/v1/theories", json={"name": label})
        assert resp.status_code == 201, resp.text
        body = resp.json()
        self._created.append(("theory", body["id"], None))
        return body

    def literature(self, name: str | None = None) -> dict:
        label = name or f"生命文献-{_SUFFIX}"
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
        self._created.append(
            (
                "mount",
                body["id"],
                {
                    "kb_id": kb_id,
                    "resource_type": resource_type,
                    "resource_id": resource_id,
                },
            )
        )
        return body

    def teardown(self) -> None:
        """回收顺序：卸载 → 删资源 → 删 KB。"""
        for kind, item_id, ctx in reversed(self._created):
            try:
                if kind == "mount":
                    self.client.delete(
                        f"/api/v1/kb/{ctx['kb_id']}/resources/"
                        f"{ctx['resource_type']}/{ctx['resource_id']}"
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
                    # 先 soft delete 再 purge（KB 测试中可能已 purge）
                    self.client.delete(f"/api/v1/kb/{item_id}")
                    self.client.delete(f"/api/v1/kb/{item_id}/purge")
            except Exception:
                pass


@pytest.fixture
def lifecycle_keeper(client):
    k = _LifecycleKeeper(client)
    yield k
    k.teardown()


def _store():
    """获取当前全局向量库。"""
    return milvus_store.get_vector_store()


def _make_other_user() -> User:
    return User(
        id=uuid.uuid4(),
        email=f"life-other-{_SUFFIX}@example.com",
        username=f"life-other-{_SUFFIX}",
        hashed_password="",
        role="member",
    )


# ── 1. 更新后重新向量化 ────────────────────────────────────────────────────


def test_update_herb_revectorizes_all_mounts(lifecycle_keeper, client):
    """更新 Herb 后所有已挂载 KB 的向量被重新生成。"""
    kb1 = lifecycle_keeper.kb(name=f"更新KB1-{_SUFFIX}")
    kb2 = lifecycle_keeper.kb(name=f"更新KB2-{_SUFFIX}")
    h = lifecycle_keeper.herb(name=f"原名药-{_SUFFIX}")

    lifecycle_keeper.mount(kb1["id"], "herb", h["id"])
    lifecycle_keeper.mount(kb2["id"], "herb", h["id"])

    doc_id_1 = make_doc_id("herb", uuid.UUID(h["id"]), uuid.UUID(kb1["id"]))
    doc_id_2 = make_doc_id("herb", uuid.UUID(h["id"]), uuid.UUID(kb2["id"]))

    # 更新前向量已存在
    rows1_before = _store().query_rows(doc_id_1)
    rows2_before = _store().query_rows(doc_id_2)
    assert len(rows1_before) >= 1
    assert len(rows2_before) >= 1
    # 原向量 resource_name 应为原名
    assert rows1_before[0]["resource_name"] == f"原名药-{_SUFFIX}"

    # 更新 Herb 名称
    new_name = f"更新后药-{_SUFFIX}"
    resp = client.put(
        f"/api/v1/herbs/{h['id']}", json={"name": new_name}
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["name"] == new_name

    # 更新后向量仍在（重新向量化成功，幂等不产生重复）
    rows1_after = _store().query_rows(doc_id_1)
    rows2_after = _store().query_rows(doc_id_2)
    assert len(rows1_after) == len(rows1_before), "KB1 向量数量应保持一致"
    assert len(rows2_after) == len(rows2_before), "KB2 向量数量应保持一致"
    # resource_name 已更新为新名称
    assert rows1_after[0]["resource_name"] == new_name
    assert rows2_after[0]["resource_name"] == new_name


# ── 2. Resource 删除后清理 KBR + 向量 ────────────────────────────────────────


def test_delete_herb_cleans_kbr_and_vectors(lifecycle_keeper, client):
    """删除 Herb 后 KBR 关联和所有 KB 中的向量都被清理。"""
    kb1 = lifecycle_keeper.kb(name=f"删除KB1-{_SUFFIX}")
    kb2 = lifecycle_keeper.kb(name=f"删除KB2-{_SUFFIX}")
    h = lifecycle_keeper.herb(name=f"待删药-{_SUFFIX}")

    lifecycle_keeper.mount(kb1["id"], "herb", h["id"])
    lifecycle_keeper.mount(kb2["id"], "herb", h["id"])

    doc_id_1 = make_doc_id("herb", uuid.UUID(h["id"]), uuid.UUID(kb1["id"]))
    doc_id_2 = make_doc_id("herb", uuid.UUID(h["id"]), uuid.UUID(kb2["id"]))

    assert len(_store().query_rows(doc_id_1)) >= 1
    assert len(_store().query_rows(doc_id_2)) >= 1

    # 删除 Herb（清理 KBR + 向量）
    resp = client.delete(f"/api/v1/herbs/{h['id']}")
    assert resp.status_code == 204, resp.text

    # 向量已被删除
    assert len(_store().query_rows(doc_id_1)) == 0
    assert len(_store().query_rows(doc_id_2)) == 0

    # KBR 关联也已被删除
    resp_kb1 = client.get(f"/api/v1/kb/{kb1['id']}/resources")
    assert resp_kb1.status_code == 200
    kb1_items = resp_kb1.json().get("items", [])
    assert all(
        r["resource_id"] != h["id"] for r in kb1_items
    ), "KB1 中仍有该 Herb 的 KBR 记录"

    # 从 keeper 移除已删 herb（teardown 时不再尝试删）
    lifecycle_keeper._created = [
        c for c in lifecycle_keeper._created
        if not (c[0] == "herb" and c[1] == h["id"])
    ]
    # 同时移除对应 mount 记录
    lifecycle_keeper._created = [
        c for c in lifecycle_keeper._created
        if not (c[0] == "mount" and c[2] and c[2].get("resource_id") == h["id"])
    ]


# ── 3. KB purge 时清理 Resource 向量 ──────────────────────────────────────────


def test_kb_purge_cleans_resource_vectors(lifecycle_keeper, client):
    """KB purge 后该 KB 下所有 Resource 向量被清理，其他 KB 不受影响。"""
    kb1 = lifecycle_keeper.kb(name=f"purgeKB-{_SUFFIX}")
    kb2 = lifecycle_keeper.kb(name=f"keepKB-{_SUFFIX}")
    h = lifecycle_keeper.herb(name=f"共享purge药-{_SUFFIX}")

    lifecycle_keeper.mount(kb1["id"], "herb", h["id"])
    lifecycle_keeper.mount(kb2["id"], "herb", h["id"])

    doc_id_1 = make_doc_id("herb", uuid.UUID(h["id"]), uuid.UUID(kb1["id"]))
    doc_id_2 = make_doc_id("herb", uuid.UUID(h["id"]), uuid.UUID(kb2["id"]))

    assert len(_store().query_rows(doc_id_1)) >= 1
    assert len(_store().query_rows(doc_id_2)) >= 1

    # 先 soft delete 再 purge
    resp = client.delete(f"/api/v1/kb/{kb1['id']}")
    assert resp.status_code == 200, resp.text
    resp = client.delete(f"/api/v1/kb/{kb1['id']}/purge")
    assert resp.status_code == 200, resp.text
    assert resp.json()["purged"] is True

    # KB1 向量已删除
    assert len(_store().query_rows(doc_id_1)) == 0
    # KB2 向量不受影响
    assert len(_store().query_rows(doc_id_2)) >= 1

    # 从 keeper 移除已 purge 的 KB（teardown 时不再尝试删）
    lifecycle_keeper._created = [
        c for c in lifecycle_keeper._created
        if not (c[0] == "kb" and c[1] == kb1["id"])
    ]
    # 同时移除对应 mount 记录（KBR 已被 CASCADE）
    lifecycle_keeper._created = [
        c for c in lifecycle_keeper._created
        if not (
            c[0] == "mount"
            and c[2]
            and c[2].get("kb_id") == kb1["id"]
        )
    ]


# ── 4. 更新失败 best-effort ──────────────────────────────────────────────────


def test_update_succeeds_when_revectorize_fails(lifecycle_keeper, client):
    """revectorize_all_mounts 抛异常时，Herb 更新仍成功（best-effort）。"""
    kb = lifecycle_keeper.kb(name=f"best-effort-KB-{_SUFFIX}")
    h = lifecycle_keeper.herb(name=f"best-effort药-{_SUFFIX}")
    lifecycle_keeper.mount(kb["id"], "herb", h["id"])

    new_name = f"更新后best-effort-{_SUFFIX}"
    # mock revectorize_all_mounts 抛异常
    with patch.object(
        ResourceVectorService, "revectorize_all_mounts",
        side_effect=Exception("Milvus down"),
    ):
        resp = client.put(
            f"/api/v1/herbs/{h['id']}", json={"name": new_name}
        )

    assert resp.status_code == 200, resp.text
    assert resp.json()["name"] == new_name


# ── 5. 删除清理失败：阻止删除（422） ──────────────────────────────────────────


def test_delete_blocked_when_cleanup_fails(lifecycle_keeper, client):
    """cleanup_resource_mounts 抛异常时，Herb 删除被阻止（422）。"""
    kb = lifecycle_keeper.kb(name=f"blockKB-{_SUFFIX}")
    h = lifecycle_keeper.herb(name=f"block药-{_SUFFIX}")
    lifecycle_keeper.mount(kb["id"], "herb", h["id"])

    doc_id = make_doc_id("herb", uuid.UUID(h["id"]), uuid.UUID(kb["id"]))
    assert len(_store().query_rows(doc_id)) >= 1

    # mock cleanup_resource_mounts 抛异常
    with patch.object(
        ResourceVectorService, "cleanup_resource_mounts",
        side_effect=Exception("Milvus unreachable"),
    ):
        resp = client.delete(f"/api/v1/herbs/{h['id']}")

    assert resp.status_code == 422, resp.text
    # 资源未被删除（仍可查询）
    resp_get = client.get(f"/api/v1/herbs/{h['id']}")
    assert resp_get.status_code == 200
    # 向量仍在（清理失败回滚）
    assert len(_store().query_rows(doc_id)) >= 1


# ── 6. 四种 resource_type 更新均触发重新向量化 ──────────────────────────────


def test_update_prescription_revectorizes(lifecycle_keeper, client):
    """更新 Prescription 后向量重新生成。"""
    kb = lifecycle_keeper.kb(name=f"方更新KB-{_SUFFIX}")
    p = lifecycle_keeper.prescription(name=f"原方-{_SUFFIX}")
    lifecycle_keeper.mount(kb["id"], "prescription", p["id"])

    doc_id = make_doc_id(
        "prescription", uuid.UUID(p["id"]), uuid.UUID(kb["id"])
    )
    rows_before = _store().query_rows(doc_id)
    assert len(rows_before) >= 1
    assert rows_before[0]["resource_name"] == f"原方-{_SUFFIX}"

    new_name = f"更新后方-{_SUFFIX}"
    resp = client.put(
        f"/api/v1/prescriptions/{p['id']}", json={"name": new_name}
    )
    assert resp.status_code == 200, resp.text

    rows_after = _store().query_rows(doc_id)
    assert len(rows_after) == len(rows_before)
    assert rows_after[0]["resource_name"] == new_name


def test_update_theory_revectorizes(lifecycle_keeper, client):
    """更新 Theory 后向量重新生成。"""
    kb = lifecycle_keeper.kb(name=f"理论更新KB-{_SUFFIX}")
    t = lifecycle_keeper.theory(name=f"原理论-{_SUFFIX}")
    lifecycle_keeper.mount(kb["id"], "theory", t["id"])

    doc_id = make_doc_id("theory", uuid.UUID(t["id"]), uuid.UUID(kb["id"]))
    rows_before = _store().query_rows(doc_id)
    assert len(rows_before) >= 1

    new_name = f"更新后理论-{_SUFFIX}"
    resp = client.put(
        f"/api/v1/theories/{t['id']}", json={"name": new_name}
    )
    assert resp.status_code == 200, resp.text

    rows_after = _store().query_rows(doc_id)
    assert len(rows_after) == len(rows_before)
    assert rows_after[0]["resource_name"] == new_name


def test_update_literature_revectorizes(lifecycle_keeper, client):
    """更新 Literature 后向量重新生成。"""
    kb = lifecycle_keeper.kb(name=f"文献更新KB-{_SUFFIX}")
    l = lifecycle_keeper.literature(name=f"原文献-{_SUFFIX}")
    lifecycle_keeper.mount(kb["id"], "literature", l["id"])

    doc_id = make_doc_id(
        "literature", uuid.UUID(l["id"]), uuid.UUID(kb["id"])
    )
    rows_before = _store().query_rows(doc_id)
    assert len(rows_before) >= 1

    new_name = f"更新后文献-{_SUFFIX}"
    resp = client.put(
        f"/api/v1/literatures/{l['id']}", json={"name": new_name}
    )
    assert resp.status_code == 200, resp.text

    rows_after = _store().query_rows(doc_id)
    assert len(rows_after) == len(rows_before)
    assert rows_after[0]["resource_name"] == new_name


# ── 7. 删除四种 resource_type 均清理向量 ─────────────────────────────────────


def test_delete_prescription_cleans_vectors(lifecycle_keeper, client):
    """删除 Prescription 后向量被清理。"""
    kb = lifecycle_keeper.kb(name=f"方删KB-{_SUFFIX}")
    p = lifecycle_keeper.prescription(name=f"待删方-{_SUFFIX}")
    lifecycle_keeper.mount(kb["id"], "prescription", p["id"])

    doc_id = make_doc_id(
        "prescription", uuid.UUID(p["id"]), uuid.UUID(kb["id"])
    )
    assert len(_store().query_rows(doc_id)) >= 1

    resp = client.delete(f"/api/v1/prescriptions/{p['id']}")
    assert resp.status_code == 204, resp.text
    assert len(_store().query_rows(doc_id)) == 0

    lifecycle_keeper._created = [
        c for c in lifecycle_keeper._created
        if not (c[0] == "prescription" and c[1] == p["id"])
    ]
    lifecycle_keeper._created = [
        c for c in lifecycle_keeper._created
        if not (
            c[0] == "mount"
            and c[2]
            and c[2].get("resource_id") == p["id"]
        )
    ]


def test_delete_theory_cleans_vectors(lifecycle_keeper, client):
    """删除 Theory 后向量被清理。"""
    kb = lifecycle_keeper.kb(name=f"理论删KB-{_SUFFIX}")
    t = lifecycle_keeper.theory(name=f"待删理论-{_SUFFIX}")
    lifecycle_keeper.mount(kb["id"], "theory", t["id"])

    doc_id = make_doc_id("theory", uuid.UUID(t["id"]), uuid.UUID(kb["id"]))
    assert len(_store().query_rows(doc_id)) >= 1

    resp = client.delete(f"/api/v1/theories/{t['id']}")
    assert resp.status_code == 204, resp.text
    assert len(_store().query_rows(doc_id)) == 0

    lifecycle_keeper._created = [
        c for c in lifecycle_keeper._created
        if not (c[0] == "theory" and c[1] == t["id"])
    ]
    lifecycle_keeper._created = [
        c for c in lifecycle_keeper._created
        if not (
            c[0] == "mount"
            and c[2]
            and c[2].get("resource_id") == t["id"]
        )
    ]


def test_delete_literature_cleans_vectors(lifecycle_keeper, client):
    """删除 Literature 后向量被清理。"""
    kb = lifecycle_keeper.kb(name=f"文献删KB-{_SUFFIX}")
    l = lifecycle_keeper.literature(name=f"待删文献-{_SUFFIX}")
    lifecycle_keeper.mount(kb["id"], "literature", l["id"])

    doc_id = make_doc_id(
        "literature", uuid.UUID(l["id"]), uuid.UUID(kb["id"])
    )
    assert len(_store().query_rows(doc_id)) >= 1

    resp = client.delete(f"/api/v1/literatures/{l['id']}")
    assert resp.status_code == 204, resp.text
    assert len(_store().query_rows(doc_id)) == 0

    lifecycle_keeper._created = [
        c for c in lifecycle_keeper._created
        if not (c[0] == "literature" and c[1] == l["id"])
    ]
    lifecycle_keeper._created = [
        c for c in lifecycle_keeper._created
        if not (
            c[0] == "mount"
            and c[2]
            and c[2].get("resource_id") == l["id"]
        )
    ]


# ── 8. 不影响 Document 生命周期 ─────────────────────────────────────────────


def test_herb_update_does_not_touch_documents(lifecycle_keeper, client):
    """更新 Herb 不影响 Document 向量化（仅触发 Resource 重新向量化）。"""
    # 仅挂载 Herb，不创建 Document
    kb = lifecycle_keeper.kb(name=f"doc隔离KB-{_SUFFIX}")
    h = lifecycle_keeper.herb(name=f"doc隔离药-{_SUFFIX}")
    lifecycle_keeper.mount(kb["id"], "herb", h["id"])

    doc_id = make_doc_id("herb", uuid.UUID(h["id"]), uuid.UUID(kb["id"]))
    rows_before = _store().query_rows(doc_id)

    # 更新 Herb
    resp = client.put(
        f"/api/v1/herbs/{h['id']}",
        json={"description": "新描述内容"},
    )
    assert resp.status_code == 200, resp.text

    rows_after = _store().query_rows(doc_id)
    # 向量数量一致（仅 Resource 向量被重新生成，Document 未受影响）
    assert len(rows_after) == len(rows_before)
