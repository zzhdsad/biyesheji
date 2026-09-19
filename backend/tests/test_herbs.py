"""中药资源 API 测试（TASK-003 第三阶段）。

覆盖：
- CRUD / 详情字段完整性
- keyword 搜索（name / aliases / effects，ILIKE + unnest 相关子查询）
- 分页（total / limit / offset / 越界 offset）
- 分类关联：正常、不存在 400、resource_type 非 herb 400
- 标签关联：正常、不存在 400、tag_id 筛选、tag_ids 整体替换 / 清空 / 未提供保留
- 权限：member 读 200 写 403；admin POST 201 / PUT 200 / DELETE 204
- 错误：name 重复 409；不存在 404
- 删除联动：删除中药后 herb_tags 由 DB CASCADE 自动清理

Keeper 记录全部已创建资源，用例结束（含断言失败）后按创建逆序回收：
先删中药（解除 RESTRICT 引用），再删分类 / 标签，保证不残留。
"""

import uuid

import pytest

import src.core.deps as deps_mod
from tests.test_upload import PG_AVAILABLE

pytestmark = pytest.mark.skipif(not PG_AVAILABLE, reason="PostgreSQL 未启动")

_SUFFIX = uuid.uuid4().hex[:8]


class Keeper:
    """记录测试创建的中药 / 分类 / 标签，统一回收。"""

    def __init__(self, client) -> None:
        self.client = client
        self._created: list[tuple[str, str]] = []

    def cat(self, resource_type: str = "herb", name: str | None = None) -> dict:
        label = name or f"测试分类-{resource_type}-{_SUFFIX}"
        resp = self.client.post(
            "/api/v1/categories",
            json={"resource_type": resource_type, "name": label},
        )
        assert resp.status_code == 201, resp.text
        body = resp.json()
        self._created.append(("cat", body["id"]))
        return body

    def tag(self, name: str | None = None, **extra) -> dict:
        label = name or f"测试标签-{_SUFFIX}"
        resp = self.client.post(
            "/api/v1/tags", json={"name": label, **extra}
        )
        assert resp.status_code == 201, resp.text
        body = resp.json()
        self._created.append(("tag", body["id"]))
        return body

    def herb(self, name: str | None = None, **body) -> dict:
        label = name or f"测试中药-{_SUFFIX}"
        resp = self.client.post(
            "/api/v1/herbs", json={"name": label, **body}
        )
        assert resp.status_code == 201, resp.text
        out = resp.json()
        # 中药在分类/标签之后登记，逆序回收时先删中药以解除 RESTRICT
        self._created.append(("herb", out["id"]))
        return out

    def teardown(self) -> None:
        for kind, item_id in reversed(self._created):
            if kind == "herb":
                self.client.delete(f"/api/v1/herbs/{item_id}")
            elif kind == "cat":
                self.client.delete(f"/api/v1/categories/{item_id}")
            else:
                self.client.delete(f"/api/v1/tags/{item_id}")


@pytest.fixture
def keeper(client):
    k = Keeper(client)
    yield k
    k.teardown()


def _find_herb(items: list[dict], herb_id: str) -> dict | None:
    return next((i for i in items if i["id"] == herb_id), None)


# ── CRUD ────────────────────────────────────────────────────────────────────


def test_herb_create_and_list(client, keeper):
    cat = keeper.cat()
    tag = keeper.tag()
    herb = keeper.herb(
        name=f"中药CRUD-{_SUFFIX}",
        aliases=["别名A"],
        category_id=cat["id"],
        properties="苦，寒",
        channels=["肺经", "胃经"],
        effects="清热解毒",
        source="《中国药典》",
        description="测试描述",
        tag_ids=[tag["id"]],
    )

    assert herb["name"] == f"中药CRUD-{_SUFFIX}"
    assert herb["aliases"] == ["别名A"]
    assert herb["category"] is not None and herb["category"]["id"] == cat["id"]
    assert herb["category_id"] == cat["id"]
    assert herb["properties"] == "苦，寒"
    assert herb["channels"] == ["肺经", "胃经"]
    assert herb["effects"] == "清热解毒"
    assert herb["source"] == "《中国药典》"
    assert [t["id"] for t in herb["tags"]] == [tag["id"]]

    listing = client.get("/api/v1/herbs", params={"limit": 100}).json()
    assert listing["limit"] == 100
    assert listing["offset"] == 0
    assert _find_herb(listing["items"], herb["id"]) is not None


def test_herb_detail(client, keeper):
    herb = keeper.herb(name=f"详情中药-{_SUFFIX}")
    resp = client.get(f"/api/v1/herbs/{herb['id']}")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["id"] == herb["id"]
    for key in (
        "name",
        "aliases",
        "category_id",
        "category",
        "properties",
        "channels",
        "effects",
        "source",
        "description",
        "tags",
        "created_at",
        "updated_at",
    ):
        assert key in body


def test_herb_update_partial(client, keeper):
    herb = keeper.herb(
        name=f"更新中药-{_SUFFIX}",
        effects="旧功效",
        properties="旧性味",
    )
    resp = client.put(
        f"/api/v1/herbs/{herb['id']}", json={"effects": "新功效"}
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["effects"] == "新功效"
    # 未提供的字段保持原值
    assert body["properties"] == "旧性味"
    assert body["name"] == f"更新中药-{_SUFFIX}"


def test_herb_delete(client, keeper):
    herb = keeper.herb(name=f"删除中药-{_SUFFIX}")
    resp = client.delete(f"/api/v1/herbs/{herb['id']}")
    assert resp.status_code == 204

    assert client.get(f"/api/v1/herbs/{herb['id']}").status_code == 404
    listing = client.get("/api/v1/herbs").json()
    assert _find_herb(listing["items"], herb["id"]) is None


# ── 关键词搜索 ───────────────────────────────────────────────────────────────


def test_search_by_name(client, keeper):
    herb = keeper.herb(name=f"搜索名-{_SUFFIX}")
    resp = client.get("/api/v1/herbs", params={"keyword": f"搜索名-{_SUFFIX}"})
    assert _find_herb(resp.json()["items"], herb["id"]) is not None


def test_search_by_alias(client, keeper):
    herb = keeper.herb(
        name=f"别名搜索-{_SUFFIX}",
        aliases=[f"XYZ别名-{_SUFFIX}"],
    )
    resp = client.get(
        "/api/v1/herbs", params={"keyword": f"XYZ别名-{_SUFFIX}"}
    )
    assert _find_herb(resp.json()["items"], herb["id"]) is not None


def test_search_by_effects(client, keeper):
    herb = keeper.herb(
        name=f"功效搜索-{_SUFFIX}",
        effects=f"ABC功效-{_SUFFIX}",
    )
    resp = client.get(
        "/api/v1/herbs", params={"keyword": f"ABC功效-{_SUFFIX}"}
    )
    assert _find_herb(resp.json()["items"], herb["id"]) is not None


# ── 分页 ─────────────────────────────────────────────────────────────────────


def test_pagination(client, keeper):
    cat = keeper.cat(name=f"分页分类-{_SUFFIX}")
    names = [f"分页{i}-{_SUFFIX}" for i in range(3)]
    for n in names:
        keeper.herb(name=n, category_id=cat["id"])

    base = {"category_id": cat["id"]}

    body = client.get("/api/v1/herbs", params=base).json()
    assert body["total"] == 3

    page1 = client.get(
        "/api/v1/herbs", params={**base, "limit": 2, "offset": 0}
    ).json()
    assert page1["total"] == 3
    assert len(page1["items"]) == 2

    page2 = client.get(
        "/api/v1/herbs", params={**base, "limit": 2, "offset": 2}
    ).json()
    assert len(page2["items"]) == 1

    # 两页不重叠，合计 3 条
    seen = {i["id"] for i in page1["items"]} | {
        i["id"] for i in page2["items"]
    }
    assert len(seen) == 3

    beyond = client.get(
        "/api/v1/herbs", params={**base, "offset": 99}
    ).json()
    assert beyond["items"] == []


# ── 分类校验 ─────────────────────────────────────────────────────────────────


def test_category_not_exist_400(client):
    resp = client.post(
        "/api/v1/herbs",
        json={"name": f"无分类-{_SUFFIX}", "category_id": str(uuid.uuid4())},
    )
    assert resp.status_code == 400


def test_category_wrong_resource_type_400(client, keeper):
    pcat = keeper.cat(
        resource_type="prescription", name=f"方剂分类-{_SUFFIX}"
    )
    resp = client.post(
        "/api/v1/herbs",
        json={"name": f"错分类-{_SUFFIX}", "category_id": pcat["id"]},
    )
    assert resp.status_code == 400, resp.text


# ── 标签关联 ─────────────────────────────────────────────────────────────────


def test_tag_association(client, keeper):
    tag = keeper.tag(name=f"关联标签-{_SUFFIX}")
    herb = keeper.herb(
        name=f"标签关联-{_SUFFIX}", tag_ids=[tag["id"]]
    )
    detail = client.get(f"/api/v1/herbs/{herb['id']}").json()
    assert [t["id"] for t in detail["tags"]] == [tag["id"]]


def test_tag_not_exist_400(client):
    resp = client.post(
        "/api/v1/herbs",
        json={"name": f"无标签-{_SUFFIX}", "tag_ids": [str(uuid.uuid4())]},
    )
    assert resp.status_code == 400


def test_tag_filter(client, keeper):
    tag = keeper.tag(name=f"筛选标签-{_SUFFIX}")
    herb = keeper.herb(
        name=f"标签筛选中药-{_SUFFIX}", tag_ids=[tag["id"]]
    )
    resp = client.get("/api/v1/herbs", params={"tag_id": tag["id"]})
    assert _find_herb(resp.json()["items"], herb["id"]) is not None


def test_tag_ids_replace(client, keeper):
    tag_a = keeper.tag(name=f"替换标签A-{_SUFFIX}")
    tag_b = keeper.tag(name=f"替换标签B-{_SUFFIX}")
    herb = keeper.herb(
        name=f"标签替换-{_SUFFIX}", tag_ids=[tag_a["id"]]
    )

    resp = client.put(
        f"/api/v1/herbs/{herb['id']}", json={"tag_ids": [tag_b["id"]]}
    )
    assert resp.status_code == 200, resp.text
    assert [t["id"] for t in resp.json()["tags"]] == [tag_b["id"]]

    # 旧标签下不再出现该中药
    under_a = client.get(
        "/api/v1/herbs", params={"tag_id": tag_a["id"]}
    ).json()
    assert _find_herb(under_a["items"], herb["id"]) is None
    under_b = client.get(
        "/api/v1/herbs", params={"tag_id": tag_b["id"]}
    ).json()
    assert _find_herb(under_b["items"], herb["id"]) is not None


def test_tag_ids_clear_with_empty_list(client, keeper):
    tag = keeper.tag(name=f"清空标签-{_SUFFIX}")
    herb = keeper.herb(
        name=f"标签清空-{_SUFFIX}", tag_ids=[tag["id"]]
    )
    resp = client.put(
        f"/api/v1/herbs/{herb['id']}", json={"tag_ids": []}
    )
    assert resp.status_code == 200
    assert resp.json()["tags"] == []


def test_tag_ids_omitted_keeps_relation(client, keeper):
    tag = keeper.tag(name=f"保留标签-{_SUFFIX}")
    herb = keeper.herb(
        name=f"标签保留-{_SUFFIX}", tag_ids=[tag["id"]]
    )
    # 只改 effects，不带 tag_ids → 标签关系必须保留
    resp = client.put(
        f"/api/v1/herbs/{herb['id']}",
        json={"effects": "改功效不动标签"},
    )
    assert resp.status_code == 200
    assert [t["id"] for t in resp.json()["tags"]] == [tag["id"]]


# ── 权限 ─────────────────────────────────────────────────────────────────────


def test_member_read_allowed_writes_forbidden(client, monkeypatch):
    monkeypatch.setattr(deps_mod.TEST_USER, "role", "member")

    # 写操作一律 403（权限先于存在性检查，故随机 id 也是 403）
    assert (
        client.post(
            "/api/v1/herbs", json={"name": f"member越权-{_SUFFIX}"}
        ).status_code
        == 403
    )
    assert (
        client.put(
            f"/api/v1/herbs/{uuid.uuid4()}", json={"effects": "x"}
        ).status_code
        == 403
    )
    assert (
        client.delete(f"/api/v1/herbs/{uuid.uuid4()}").status_code == 403
    )

    # 读操作允许；列表 200，不存在的详情 404（而非 403）
    assert client.get("/api/v1/herbs").status_code == 200
    assert client.get(f"/api/v1/herbs/{uuid.uuid4()}").status_code == 404


# ── 错误 ─────────────────────────────────────────────────────────────────────


def test_duplicate_name_409(client, keeper):
    name = f"重复名-{_SUFFIX}"
    keeper.herb(name=name)
    dup = client.post("/api/v1/herbs", json={"name": name})
    assert dup.status_code == 409

    # 编辑成已有名称同样 409
    other = keeper.herb(name=f"另一中药-{_SUFFIX}")
    rename = client.put(
        f"/api/v1/herbs/{other['id']}", json={"name": name}
    )
    assert rename.status_code == 409


def test_herb_404(client):
    missing = uuid.uuid4()
    assert client.get(f"/api/v1/herbs/{missing}").status_code == 404
    assert (
        client.put(
            f"/api/v1/herbs/{missing}", json={"effects": "y"}
        ).status_code
        == 404
    )
    assert client.delete(f"/api/v1/herbs/{missing}").status_code == 404


# ── 删除联动（herb_tags CASCADE）─────────────────────────────────────────────


def test_delete_herb_clears_herb_tags(client, keeper):
    tag = keeper.tag(name=f"级联标签-{_SUFFIX}")
    herb = keeper.herb(
        name=f"级联中药-{_SUFFIX}", tag_ids=[tag["id"]]
    )

    assert client.delete(f"/api/v1/herbs/{herb['id']}").status_code == 204

    # 关联已由 DB CASCADE 清理：该标签此时应可删除（若关联残留则 409）
    assert client.delete(f"/api/v1/tags/{tag['id']}").status_code == 204
