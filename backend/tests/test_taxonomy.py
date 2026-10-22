"""分类与标签 API 测试（TASK-002）。

覆盖：
- Category CRUD / 层级树 / resource_type 校验 / 跨树挂载 / 重名 / 删除保护
- Tag CRUD / name 全局唯一
- 权限：非 admin 写操作 403、读操作允许

keeper fixture 记录所有已创建数据，用例结束（含断言失败）后按创建逆序回收，
子节点先删、根节点后删，保证不残留。
"""

import uuid

import pytest

import src.core.deps as deps_mod
from tests.test_upload import PG_AVAILABLE

pytestmark = pytest.mark.skipif(not PG_AVAILABLE, reason="PostgreSQL 未启动")

_SUFFIX = uuid.uuid4().hex[:8]


class Keeper:
    """记录测试创建的分类/标签，统一回收。"""

    def __init__(self, client) -> None:
        self.client = client
        self._created: list[tuple[str, str]] = []

    def root(self, resource_type: str, name: str | None = None) -> dict:
        label = name or f"测试根-{resource_type}-{_SUFFIX}"
        resp = self.client.post(
            "/api/v1/categories",
            json={"resource_type": resource_type, "name": label},
        )
        assert resp.status_code == 201, resp.text
        body = resp.json()
        self._created.append(("cat", body["id"]))
        return body

    def child(self, parent: dict, name: str) -> dict:
        resp = self.client.post(
            "/api/v1/categories",
            json={
                "resource_type": parent["resource_type"],
                "name": name,
                "parent_id": parent["id"],
            },
        )
        assert resp.status_code == 201, resp.text
        body = resp.json()
        # 子节点在父节点之后登记，逆序回收时先删子节点
        self._created.append(("cat", body["id"]))
        return body

    def tag(self, name: str, **extra) -> dict:
        resp = self.client.post(
            "/api/v1/tags", json={"name": name, **extra}
        )
        assert resp.status_code == 201, resp.text
        body = resp.json()
        self._created.append(("tag", body["id"]))
        return body

    def teardown(self) -> None:
        for kind, item_id in reversed(self._created):
            if kind == "cat":
                self.client.delete(f"/api/v1/categories/{item_id}")
            else:
                self.client.delete(f"/api/v1/tags/{item_id}")


@pytest.fixture
def keeper(client):
    k = Keeper(client)
    yield k
    k.teardown()


def _find(nodes: list[dict], node_id: str) -> dict | None:
    for n in nodes:
        if n["id"] == node_id:
            return n
    return None


# ── Category CRUD ────────────────────────────────────────────────────────────


def test_category_create_and_list(client, keeper):
    root = keeper.root("herb")
    listing = client.get(
        "/api/v1/categories", params={"resource_type": "herb"}
    ).json()
    node = _find(listing, root["id"])
    assert node is not None
    assert node["resource_type"] == "herb"
    assert node["parent_id"] is None
    assert node["sort_order"] == 0
    assert node["children"] == []


def test_category_update(client, keeper):
    root = keeper.root("herb")
    resp = client.put(
        f"/api/v1/categories/{root['id']}",
        json={
            "name": f"改名-{_SUFFIX}",
            "sort_order": 5,
            "description": "新描述",
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["name"] == f"改名-{_SUFFIX}"
    assert body["sort_order"] == 5
    assert body["description"] == "新描述"
    # resource_type / parent_id 不在更新 schema 中，保持不变
    assert body["resource_type"] == "herb"
    assert body["parent_id"] is None


def test_category_update_404(client):
    resp = client.put(
        f"/api/v1/categories/{uuid.uuid4()}", json={"name": "x"}
    )
    assert resp.status_code == 404


# ── Category 层级 ────────────────────────────────────────────────────────────


def test_category_hierarchy_tree(client, keeper):
    root = keeper.root("herb")
    child_a = keeper.child(root, f"子分类A-{_SUFFIX}")
    child_b = keeper.child(root, f"子分类B-{_SUFFIX}")

    tree = client.get(
        "/api/v1/categories",
        params={"resource_type": "herb", "tree": "true"},
    ).json()
    node = _find(tree, root["id"])
    assert node is not None
    children_ids = [c["id"] for c in node["children"]]
    assert {child_a["id"], child_b["id"]}.issubset(set(children_ids))


# ── resource_type 校验 ───────────────────────────────────────────────────────


def test_category_invalid_resource_type(client):
    resp = client.post(
        "/api/v1/categories",
        json={"resource_type": "invalid_type", "name": "x"},
    )
    assert resp.status_code == 422


def test_category_cross_tree_parent_rejected(client, keeper):
    herb_root = keeper.root("herb")
    # prescription 分类挂到 herb 的节点下 → 400
    resp = client.post(
        "/api/v1/categories",
        json={
            "resource_type": "prescription",
            "name": f"跨树-{_SUFFIX}",
            "parent_id": herb_root["id"],
        },
    )
    assert resp.status_code == 400, resp.text


def test_category_missing_parent_rejected(client):
    resp = client.post(
        "/api/v1/categories",
        json={
            "resource_type": "herb",
            "name": f"无父-{_SUFFIX}",
            "parent_id": str(uuid.uuid4()),
        },
    )
    assert resp.status_code == 400


# ── 重名 ─────────────────────────────────────────────────────────────────────


def test_category_duplicate_sibling_name_409(client, keeper):
    root = keeper.root("herb")
    keeper.child(root, f"重名子-{_SUFFIX}")
    resp = client.post(
        "/api/v1/categories",
        json={
            "resource_type": "herb",
            "name": f"重名子-{_SUFFIX}",
            "parent_id": root["id"],
        },
    )
    assert resp.status_code == 409


def test_category_duplicate_root_name_409(client, keeper):
    """根节点（parent_id IS NULL）同名也必须被阻止（COALESCE 索引 + 应用校验）。"""
    keeper.root("theory", name=f"根重名-{_SUFFIX}")
    resp = client.post(
        "/api/v1/categories",
        json={"resource_type": "theory", "name": f"根重名-{_SUFFIX}"},
    )
    assert resp.status_code == 409


# ── 删除保护 ─────────────────────────────────────────────────────────────────


def test_category_delete_with_child_blocked(client, keeper):
    root = keeper.root("herb")
    keeper.child(root, f"删除保护-{_SUFFIX}")
    resp = client.delete(f"/api/v1/categories/{root['id']}")
    assert resp.status_code == 409
    # 根节点仍在
    listing = client.get(
        "/api/v1/categories", params={"resource_type": "herb"}
    ).json()
    assert _find(listing, root["id"]) is not None


def test_category_delete_404(client):
    resp = client.delete(f"/api/v1/categories/{uuid.uuid4()}")
    assert resp.status_code == 404


# ── Tag CRUD / 唯一性 ────────────────────────────────────────────────────────


def test_tag_crud(client, keeper):
    tag_name = f"测试标签-{_SUFFIX}"
    tag = keeper.tag(tag_name, color="gold", description="desc")

    listing = client.get("/api/v1/tags").json()
    assert _find(listing, tag["id"]) is not None

    keyword_hits = client.get(
        "/api/v1/tags", params={"keyword": tag_name[:6]}
    ).json()
    assert _find(keyword_hits, tag["id"]) is not None

    updated = client.put(
        f"/api/v1/tags/{tag['id']}",
        json={"color": "red", "description": "改描述"},
    )
    assert updated.status_code == 200
    assert updated.json()["color"] == "red"


def test_tag_duplicate_name_409(client, keeper):
    tag_name = f"唯一标签-{_SUFFIX}"
    keeper.tag(tag_name)
    dup = client.post("/api/v1/tags", json={"name": tag_name})
    assert dup.status_code == 409

    # 编辑成已有名称也 409
    other = keeper.tag(f"另一标签-{_SUFFIX}")
    rename = client.put(
        f"/api/v1/tags/{other['id']}", json={"name": tag_name}
    )
    assert rename.status_code == 409


def test_tag_404(client):
    assert (
        client.put(
            f"/api/v1/tags/{uuid.uuid4()}", json={"name": "x"}
        ).status_code
        == 404
    )
    assert client.delete(f"/api/v1/tags/{uuid.uuid4()}").status_code == 404


# ── 权限 ─────────────────────────────────────────────────────────────────────


def test_non_admin_write_forbidden(client, monkeypatch):
    monkeypatch.setattr(deps_mod.TEST_USER, "role", "member")

    assert (
        client.post(
            "/api/v1/categories",
            json={"resource_type": "herb", "name": f"越权-{_SUFFIX}"},
        ).status_code
        == 403
    )
    assert (
        client.post(
            "/api/v1/tags", json={"name": f"越权标签-{_SUFFIX}"}
        ).status_code
        == 403
    )

    # 查询仍允许
    assert client.get("/api/v1/categories").status_code == 200
    assert client.get("/api/v1/tags").status_code == 200
