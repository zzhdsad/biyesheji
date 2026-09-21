"""中医理论资源 API 测试（TASK-005 Stage 4）。

覆盖：
- CRUD / 详情字段完整性
- keyword 搜索（name / aliases / content / source，ILIKE + unnest 相关子查询）
- 分页（total / limit / offset / 越界 offset）
- 分类关联：正常、不存在 400、resource_type 非 theory 400
- 标签关联：tag_id 筛选、tag_ids 整体替换 / 未提供保留
- 权限：member 读 200 写 403；admin POST 201 / PUT 200 / DELETE 204
- 错误：name 重复 409；不存在 404；空 name 422
- 删除联动：theory_tags 由 DB CASCADE 自动清理

Keeper 记录全部已创建资源，用例结束（含断言失败）后按
理论 → 分类 → 标签 顺序回收，保证不残留。
"""

import uuid

import pytest

import src.core.deps as deps_mod
from tests.test_upload import PG_AVAILABLE

pytestmark = pytest.mark.skipif(not PG_AVAILABLE, reason="PostgreSQL 未启动")

_SUFFIX = uuid.uuid4().hex[:8]


class Keeper:
    """记录测试创建的理论 / 分类 / 标签，统一回收。"""

    def __init__(self, client) -> None:
        self.client = client
        self._created: list[tuple[str, str]] = []

    def cat(
        self, resource_type: str = "theory", name: str | None = None
    ) -> dict:
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

    def theory(self, name: str | None = None, **body) -> dict:
        label = name or f"测试理论-{_SUFFIX}"
        resp = self.client.post(
            "/api/v1/theories", json={"name": label, **body}
        )
        assert resp.status_code == 201, resp.text
        out = resp.json()
        # 理论最后登记，回收时最先删除以解除对标签的 RESTRICT
        self._created.append(("theory", out["id"]))
        return out

    def forget(self, kind: str, item_id: str) -> None:
        """从回收名单移除（删除级联类用例中已显式删除的资源）。"""
        self._created = [
            (k, i) for k, i in self._created if not (k == kind and i == item_id)
        ]

    def teardown(self) -> None:
        by_kind: dict[str, list[str]] = {
            "theory": [],
            "cat": [],
            "tag": [],
        }
        for kind, item_id in self._created:
            by_kind[kind].append(item_id)
        paths = (
            ("theory", "theories"),
            ("cat", "categories"),
            ("tag", "tags"),
        )
        for kind, path in paths:
            for item_id in reversed(by_kind[kind]):
                self.client.delete(f"/api/v1/{path}/{item_id}")


@pytest.fixture
def keeper(client):
    k = Keeper(client)
    yield k
    k.teardown()


def _find(items: list[dict], item_id: str) -> dict | None:
    return next((i for i in items if i["id"] == item_id), None)


# ── CRUD ────────────────────────────────────────────────────────────────────


def test_theory_create_and_list(client, keeper):
    cat = keeper.cat()
    tag = keeper.tag()
    t = keeper.theory(
        name=f"理论CRUD-{_SUFFIX}",
        aliases=[f"别名理-{_SUFFIX}"],
        category_id=cat["id"],
        content="阴阳学说是一切事物对立统一的总纲。",
        source="《素问·阴阳应象大论》",
        tag_ids=[tag["id"]],
    )

    assert t["name"] == f"理论CRUD-{_SUFFIX}"
    assert t["aliases"] == [f"别名理-{_SUFFIX}"]
    assert t["category"] is not None and t["category"]["id"] == cat["id"]
    assert t["category_id"] == cat["id"]
    assert t["content"] == "阴阳学说是一切事物对立统一的总纲。"
    assert t["source"] == "《素问·阴阳应象大论》"
    assert [tag_el["id"] for tag_el in t["tags"]] == [tag["id"]]

    listing = client.get("/api/v1/theories", params={"limit": 100}).json()
    assert listing["limit"] == 100
    assert listing["offset"] == 0
    assert _find(listing["items"], t["id"]) is not None


def test_theory_minimal_create(client, keeper):
    """只填 name：可选字段落默认值。"""
    t = keeper.theory(name=f"最小理论-{_SUFFIX}")
    assert t["aliases"] == []
    assert t["category"] is None and t["category_id"] is None
    assert t["content"] == ""
    assert t["source"] == ""
    assert t["tags"] == []


def test_theory_detail(client, keeper):
    t = keeper.theory(name=f"详情理论-{_SUFFIX}")
    resp = client.get(f"/api/v1/theories/{t['id']}")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["id"] == t["id"]
    for key in (
        "name",
        "aliases",
        "category_id",
        "category",
        "content",
        "source",
        "tags",
        "created_at",
        "updated_at",
    ):
        assert key in body


def test_theory_update_partial(client, keeper):
    t = keeper.theory(
        name=f"更新理论-{_SUFFIX}",
        content="旧内容",
        source="《旧出处》",
    )
    resp = client.put(
        f"/api/v1/theories/{t['id']}", json={"content": "新内容"}
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["content"] == "新内容"
    # 未提供的字段保持原值
    assert body["source"] == "《旧出处》"
    assert body["name"] == f"更新理论-{_SUFFIX}"


def test_theory_delete(client, keeper):
    t = keeper.theory(name=f"删除理论-{_SUFFIX}")
    resp = client.delete(f"/api/v1/theories/{t['id']}")
    assert resp.status_code == 204

    assert client.get(f"/api/v1/theories/{t['id']}").status_code == 404
    listing = client.get("/api/v1/theories").json()
    assert _find(listing["items"], t["id"]) is None


# ── 标签整体替换 ──────────────────────────────────────────────────────────────


def test_tag_ids_replace_and_keep(client, keeper):
    tag_a = keeper.tag(name=f"理论标签A-{_SUFFIX}")
    tag_b = keeper.tag(name=f"理论标签B-{_SUFFIX}")
    t = keeper.theory(
        name=f"理论标签替换-{_SUFFIX}", tag_ids=[tag_a["id"]]
    )

    # 未提供 tag_ids → 保留
    resp = client.put(
        f"/api/v1/theories/{t['id']}", json={"content": "不动标签"}
    )
    assert resp.status_code == 200
    assert [tag_el["id"] for tag_el in resp.json()["tags"]] == [tag_a["id"]]

    # 提供 tag_ids → 整体替换
    resp = client.put(
        f"/api/v1/theories/{t['id']}", json={"tag_ids": [tag_b["id"]]}
    )
    assert resp.status_code == 200
    assert [tag_el["id"] for tag_el in resp.json()["tags"]] == [tag_b["id"]]


# ── 关键词搜索 ───────────────────────────────────────────────────────────────


def test_search_by_name(client, keeper):
    t = keeper.theory(name=f"理论搜索名-{_SUFFIX}")
    resp = client.get(
        "/api/v1/theories", params={"keyword": f"理论搜索名-{_SUFFIX}"}
    )
    assert _find(resp.json()["items"], t["id"]) is not None


def test_search_by_alias(client, keeper):
    t = keeper.theory(
        name=f"理论别名搜索-{_SUFFIX}",
        aliases=[f"XYZ理别名-{_SUFFIX}"],
    )
    resp = client.get(
        "/api/v1/theories",
        params={"keyword": f"XYZ理别名-{_SUFFIX}"},
    )
    assert _find(resp.json()["items"], t["id"]) is not None


def test_search_by_content(client, keeper):
    t = keeper.theory(
        name=f"正文搜索理-{_SUFFIX}",
        content=f"ABC正文-{_SUFFIX}",
    )
    resp = client.get(
        "/api/v1/theories",
        params={"keyword": f"ABC正文-{_SUFFIX}"},
    )
    assert _find(resp.json()["items"], t["id"]) is not None


def test_search_by_source(client, keeper):
    t = keeper.theory(
        name=f"出处搜索理-{_SUFFIX}",
        source=f"DEF出处-{_SUFFIX}",
    )
    resp = client.get(
        "/api/v1/theories",
        params={"keyword": f"DEF出处-{_SUFFIX}"},
    )
    assert _find(resp.json()["items"], t["id"]) is not None


# ── 分页 / 过滤 ──────────────────────────────────────────────────────────────


def test_pagination(client, keeper):
    cat = keeper.cat(name=f"理论分页分类-{_SUFFIX}")
    names = [f"理论分页{i}-{_SUFFIX}" for i in range(3)]
    for n in names:
        keeper.theory(name=n, category_id=cat["id"])

    base = {"category_id": cat["id"]}

    body = client.get("/api/v1/theories", params=base).json()
    assert body["total"] == 3

    page1 = client.get(
        "/api/v1/theories", params={**base, "limit": 2, "offset": 0}
    ).json()
    assert page1["total"] == 3
    assert len(page1["items"]) == 2

    page2 = client.get(
        "/api/v1/theories", params={**base, "limit": 2, "offset": 2}
    ).json()
    assert len(page2["items"]) == 1

    # 两页不重叠，合计 3 条
    seen = {i["id"] for i in page1["items"]} | {
        i["id"] for i in page2["items"]
    }
    assert len(seen) == 3

    beyond = client.get(
        "/api/v1/theories", params={**base, "offset": 99}
    ).json()
    assert beyond["items"] == []


def test_category_filter(client, keeper):
    cat_a = keeper.cat(name=f"理论分类A-{_SUFFIX}")
    cat_b = keeper.cat(name=f"理论分类B-{_SUFFIX}")
    t_a = keeper.theory(name=f"过滤理A-{_SUFFIX}", category_id=cat_a["id"])
    keeper.theory(name=f"过滤理B-{_SUFFIX}", category_id=cat_b["id"])

    resp = client.get(
        "/api/v1/theories", params={"category_id": cat_a["id"]}
    )
    items = resp.json()["items"]
    assert _find(items, t_a["id"]) is not None
    assert all(i["category_id"] == cat_a["id"] for i in items)


def test_tag_filter(client, keeper):
    tag = keeper.tag(name=f"理论筛选标签-{_SUFFIX}")
    t = keeper.theory(
        name=f"标签筛选理-{_SUFFIX}", tag_ids=[tag["id"]]
    )
    resp = client.get("/api/v1/theories", params={"tag_id": tag["id"]})
    assert _find(resp.json()["items"], t["id"]) is not None


# ── 分类 / 标签校验 ───────────────────────────────────────────────────────────


def test_category_not_exist_400(client):
    resp = client.post(
        "/api/v1/theories",
        json={"name": f"无分类理-{_SUFFIX}", "category_id": str(uuid.uuid4())},
    )
    assert resp.status_code == 400


def test_category_wrong_resource_type_400(client, keeper):
    hcat = keeper.cat(resource_type="herb", name=f"中药分类-{_SUFFIX}")
    resp = client.post(
        "/api/v1/theories",
        json={"name": f"错分类理-{_SUFFIX}", "category_id": hcat["id"]},
    )
    assert resp.status_code == 400, resp.text


def test_tag_not_exist_400(client):
    resp = client.post(
        "/api/v1/theories",
        json={"name": f"无标签理-{_SUFFIX}", "tag_ids": [str(uuid.uuid4())]},
    )
    assert resp.status_code == 400


# ── 权限 ─────────────────────────────────────────────────────────────────────


def test_member_read_allowed_writes_forbidden(client, monkeypatch):
    monkeypatch.setattr(deps_mod.TEST_USER, "role", "member")

    # 写操作一律 403（权限先于存在性检查，故随机 id 也是 403）
    assert (
        client.post(
            "/api/v1/theories", json={"name": f"member越权理-{_SUFFIX}"}
        ).status_code
        == 403
    )
    assert (
        client.put(
            f"/api/v1/theories/{uuid.uuid4()}", json={"content": "x"}
        ).status_code
        == 403
    )
    assert (
        client.delete(f"/api/v1/theories/{uuid.uuid4()}").status_code == 403
    )

    # 读操作允许；列表 200，不存在的详情 404（而非 403）
    assert client.get("/api/v1/theories").status_code == 200
    assert (
        client.get(f"/api/v1/theories/{uuid.uuid4()}").status_code == 404
    )


def test_admin_full_chain(client, keeper):
    """admin 全链路：POST 201 → PUT 200 → DELETE 204 → GET 404。"""
    t = keeper.theory(name=f"管理员链路理-{_SUFFIX}")
    assert t["name"] == f"管理员链路理-{_SUFFIX}"  # POST 201

    resp = client.put(
        f"/api/v1/theories/{t['id']}", json={"source": "《素问》"}
    )
    assert resp.status_code == 200  # PUT 200
    assert resp.json()["source"] == "《素问》"

    resp = client.delete(f"/api/v1/theories/{t['id']}")
    assert resp.status_code == 204  # DELETE 204
    keeper.forget("theory", t["id"])
    assert client.get(f"/api/v1/theories/{t['id']}").status_code == 404


# ── 错误 ─────────────────────────────────────────────────────────────────────


def test_duplicate_name_409(client, keeper):
    name = f"理论重名-{_SUFFIX}"
    keeper.theory(name=name)
    dup = client.post("/api/v1/theories", json={"name": name})
    assert dup.status_code == 409

    # 编辑成已有名称同样 409
    other = keeper.theory(name=f"另一理论-{_SUFFIX}")
    rename = client.put(
        f"/api/v1/theories/{other['id']}", json={"name": name}
    )
    assert rename.status_code == 409


def test_empty_name_422(client):
    """Pydantic min_length=1 触发 422。"""
    resp = client.post("/api/v1/theories", json={"name": ""})
    assert resp.status_code == 422


def test_theory_404(client):
    missing = uuid.uuid4()
    assert client.get(f"/api/v1/theories/{missing}").status_code == 404
    assert (
        client.put(
            f"/api/v1/theories/{missing}", json={"content": "y"}
        ).status_code
        == 404
    )
    assert (
        client.delete(f"/api/v1/theories/{missing}").status_code == 404
    )


# ── 删除联动（theory_tags CASCADE）──────────────────────────────────────────────


def test_delete_cascades(client, keeper):
    tag = keeper.tag(name=f"级联理论标签-{_SUFFIX}")
    t = keeper.theory(
        name=f"级联理论-{_SUFFIX}",
        tag_ids=[tag["id"]],
    )

    assert client.delete(f"/api/v1/theories/{t['id']}").status_code == 204
    keeper.forget("theory", t["id"])

    # theory_tags 已 CASCADE → 标签可删除（若残留会 409）
    assert client.delete(f"/api/v1/tags/{tag['id']}").status_code == 204
    keeper.forget("tag", tag["id"])
