"""中医文献资源 API 测试（TASK-006 Stage 4）。

覆盖：
- CRUD / 详情字段完整性（name/aliases/author/dynasty/summary/content/source）
- keyword 搜索 7 字段（name/aliases/author/dynasty/summary/content/source，
  ILIKE + unnest 相关子查询）
- 分页（total / limit / offset / 越界 offset）
- 分类关联：正常、不存在 400、resource_type 非 literature 400
- 标签关联：tag_id 筛选、tag_ids 整体替换 / 未提供保留
- 权限：未登录 401；member 读 200 写 403；admin POST 201 / PUT 200 / DELETE 204
- 错误：name 重复 409；不存在 404；空 name / 超长 name 422；aliases/tag_ids 超限 422
- 删除联动：literature_tags 由 DB CASCADE 自动清理

Keeper 记录全部已创建资源，用例结束（含断言失败）后按
文献 → 分类 → 标签 顺序回收，保证不残留。
"""

import uuid

import pytest

import src.core.deps as deps_mod
from tests.test_upload import PG_AVAILABLE

pytestmark = pytest.mark.skipif(not PG_AVAILABLE, reason="PostgreSQL 未启动")

_SUFFIX = uuid.uuid4().hex[:8]


class Keeper:
    """记录测试创建的文献 / 分类 / 标签，统一回收。"""

    def __init__(self, client) -> None:
        self.client = client
        self._created: list[tuple[str, str]] = []

    def cat(
        self, resource_type: str = "literature", name: str | None = None
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

    def literature(self, name: str | None = None, **body) -> dict:
        label = name or f"测试文献-{_SUFFIX}"
        resp = self.client.post(
            "/api/v1/literatures", json={"name": label, **body}
        )
        assert resp.status_code == 201, resp.text
        out = resp.json()
        # 文献最后登记，回收时最先删除以解除对标签/分类的 RESTRICT
        self._created.append(("lit", out["id"]))
        return out

    def forget(self, kind: str, item_id: str) -> None:
        """从回收名单移除（删除级联类用例中已显式删除的资源）。"""
        self._created = [
            (k, i) for k, i in self._created if not (k == kind and i == item_id)
        ]

    def teardown(self) -> None:
        by_kind: dict[str, list[str]] = {
            "lit": [],
            "cat": [],
            "tag": [],
        }
        for kind, item_id in self._created:
            by_kind[kind].append(item_id)
        paths = (
            ("lit", "literatures"),
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


# ── Read ─────────────────────────────────────────────────────────────────────


def test_unauthenticated_rejected(auth_client):
    """未携带 token：真实鉴权链路返回 401。"""
    assert auth_client.get("/api/v1/literatures").status_code == 401
    assert (
        auth_client.get(f"/api/v1/literatures/{uuid.uuid4()}").status_code
        == 401
    )


def test_literature_create_and_list(client, keeper):
    cat = keeper.cat()
    tag = keeper.tag()
    lit = keeper.literature(
        name=f"文献CRUD-{_SUFFIX}",
        aliases=[f"别名文-{_SUFFIX}"],
        category_id=cat["id"],
        author="黄帝·岐伯",
        dynasty="先秦",
        summary="中医理论奠基之作。",
        content="昔在黄帝，生而神灵……",
        source="人民卫生出版社校注本",
        tag_ids=[tag["id"]],
    )

    assert lit["name"] == f"文献CRUD-{_SUFFIX}"
    assert lit["aliases"] == [f"别名文-{_SUFFIX}"]
    assert lit["category"] is not None and lit["category"]["id"] == cat["id"]
    assert lit["category_id"] == cat["id"]
    assert lit["author"] == "黄帝·岐伯"
    assert lit["dynasty"] == "先秦"
    assert lit["summary"] == "中医理论奠基之作。"
    assert lit["content"] == "昔在黄帝，生而神灵……"
    assert lit["source"] == "人民卫生出版社校注本"
    assert [t["id"] for t in lit["tags"]] == [tag["id"]]

    listing = client.get("/api/v1/literatures", params={"limit": 100}).json()
    assert listing["limit"] == 100
    assert listing["offset"] == 0
    assert _find(listing["items"], lit["id"]) is not None


def test_literature_minimal_create(client, keeper):
    """只填 name：可选字段落默认值。"""
    lit = keeper.literature(name=f"最小文献-{_SUFFIX}")
    assert lit["aliases"] == []
    assert lit["category"] is None and lit["category_id"] is None
    assert lit["author"] == ""
    assert lit["dynasty"] == ""
    assert lit["summary"] == ""
    assert lit["content"] == ""
    assert lit["source"] == ""
    assert lit["tags"] == []


def test_literature_detail(client, keeper):
    lit = keeper.literature(name=f"详情文献-{_SUFFIX}")
    resp = client.get(f"/api/v1/literatures/{lit['id']}")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["id"] == lit["id"]
    for key in (
        "name",
        "aliases",
        "category_id",
        "category",
        "author",
        "dynasty",
        "summary",
        "content",
        "source",
        "tags",
        "created_at",
        "updated_at",
    ):
        assert key in body


def test_literature_detail_404(client):
    assert (
        client.get(f"/api/v1/literatures/{uuid.uuid4()}").status_code == 404
    )


# ── Create 校验 ───────────────────────────────────────────────────────────────


def test_member_create_forbidden(client, monkeypatch):
    monkeypatch.setattr(deps_mod.TEST_USER, "role", "member")
    resp = client.post(
        "/api/v1/literatures", json={"name": f"member越权文献-{_SUFFIX}"}
    )
    assert resp.status_code == 403


def test_empty_name_422(client):
    resp = client.post("/api/v1/literatures", json={"name": ""})
    assert resp.status_code == 422


def test_name_too_long_422(client):
    resp = client.post(
        "/api/v1/literatures", json={"name": "文" * 129}
    )
    assert resp.status_code == 422


def test_aliases_over_limit_422(client):
    resp = client.post(
        "/api/v1/literatures",
        json={
            "name": f"别名超限文献-{_SUFFIX}",
            "aliases": [f"别名{i}" for i in range(21)],
        },
    )
    assert resp.status_code == 422


def test_aliases_normalized(client, keeper):
    """strip / 去空串 / 保序去重。"""
    lit = keeper.literature(
        name=f"别名规范文献-{_SUFFIX}",
        aliases=["  内经  ", "", "内经", f"素问-{_SUFFIX}"],
    )
    assert lit["aliases"] == ["内经", f"素问-{_SUFFIX}"]


def test_tag_ids_over_limit_422(client):
    resp = client.post(
        "/api/v1/literatures",
        json={
            "name": f"标签超限文献-{_SUFFIX}",
            "tag_ids": [str(uuid.uuid4()) for _ in range(21)],
        },
    )
    assert resp.status_code == 422


def test_tag_not_exist_400(client):
    resp = client.post(
        "/api/v1/literatures",
        json={
            "name": f"无标签文献-{_SUFFIX}",
            "tag_ids": [str(uuid.uuid4())],
        },
    )
    assert resp.status_code == 400


def test_category_not_exist_400(client):
    resp = client.post(
        "/api/v1/literatures",
        json={
            "name": f"无分类文献-{_SUFFIX}",
            "category_id": str(uuid.uuid4()),
        },
    )
    assert resp.status_code == 400


def test_category_wrong_resource_type_400(client, keeper):
    hcat = keeper.cat(resource_type="herb", name=f"中药分类-{_SUFFIX}")
    resp = client.post(
        "/api/v1/literatures",
        json={
            "name": f"错分类文献-{_SUFFIX}",
            "category_id": hcat["id"],
        },
    )
    assert resp.status_code == 400, resp.text


def test_duplicate_name_409(client, keeper):
    name = f"文献重名-{_SUFFIX}"
    keeper.literature(name=name)
    dup = client.post("/api/v1/literatures", json={"name": name})
    assert dup.status_code == 409


# ── 关键词搜索（7 字段）────────────────────────────────────────────────────────


def test_search_by_name(client, keeper):
    lit = keeper.literature(name=f"文献搜索名-{_SUFFIX}")
    resp = client.get(
        "/api/v1/literatures", params={"keyword": f"文献搜索名-{_SUFFIX}"}
    )
    assert _find(resp.json()["items"], lit["id"]) is not None


def test_search_by_alias(client, keeper):
    lit = keeper.literature(
        name=f"文献别名搜索-{_SUFFIX}",
        aliases=[f"XYZ文别名-{_SUFFIX}"],
    )
    resp = client.get(
        "/api/v1/literatures",
        params={"keyword": f"XYZ文别名-{_SUFFIX}"},
    )
    assert _find(resp.json()["items"], lit["id"]) is not None


def test_search_by_author(client, keeper):
    lit = keeper.literature(
        name=f"作者搜索文-{_SUFFIX}", author=f"AUTHOR-{_SUFFIX}"
    )
    resp = client.get(
        "/api/v1/literatures", params={"keyword": f"AUTHOR-{_SUFFIX}"}
    )
    assert _find(resp.json()["items"], lit["id"]) is not None


def test_search_by_dynasty(client, keeper):
    lit = keeper.literature(
        name=f"朝代搜索文-{_SUFFIX}", dynasty=f"DYNASTY-{_SUFFIX}"
    )
    resp = client.get(
        "/api/v1/literatures", params={"keyword": f"DYNASTY-{_SUFFIX}"}
    )
    assert _find(resp.json()["items"], lit["id"]) is not None


def test_search_by_summary(client, keeper):
    lit = keeper.literature(
        name=f"摘要搜索文-{_SUFFIX}", summary=f"SUMSUM-{_SUFFIX}"
    )
    resp = client.get(
        "/api/v1/literatures", params={"keyword": f"SUMSUM-{_SUFFIX}"}
    )
    assert _find(resp.json()["items"], lit["id"]) is not None


def test_search_by_content(client, keeper):
    lit = keeper.literature(
        name=f"正文搜索文-{_SUFFIX}", content=f"BODYBODY-{_SUFFIX}"
    )
    resp = client.get(
        "/api/v1/literatures", params={"keyword": f"BODYBODY-{_SUFFIX}"}
    )
    assert _find(resp.json()["items"], lit["id"]) is not None


def test_search_by_source(client, keeper):
    lit = keeper.literature(
        name=f"出处搜索文-{_SUFFIX}", source=f"SRCSRC-{_SUFFIX}"
    )
    resp = client.get(
        "/api/v1/literatures", params={"keyword": f"SRCSRC-{_SUFFIX}"}
    )
    assert _find(resp.json()["items"], lit["id"]) is not None


# ── 分页 / 过滤 ───────────────────────────────────────────────────────────────


def test_pagination(client, keeper):
    cat = keeper.cat(name=f"文献分页分类-{_SUFFIX}")
    names = [f"文献分页{i}-{_SUFFIX}" for i in range(3)]
    for n in names:
        keeper.literature(name=n, category_id=cat["id"])

    base = {"category_id": cat["id"]}

    body = client.get("/api/v1/literatures", params=base).json()
    assert body["total"] == 3

    page1 = client.get(
        "/api/v1/literatures", params={**base, "limit": 2, "offset": 0}
    ).json()
    assert page1["total"] == 3
    assert len(page1["items"]) == 2

    page2 = client.get(
        "/api/v1/literatures", params={**base, "limit": 2, "offset": 2}
    ).json()
    assert len(page2["items"]) == 1

    seen = {i["id"] for i in page1["items"]} | {
        i["id"] for i in page2["items"]
    }
    assert len(seen) == 3

    beyond = client.get(
        "/api/v1/literatures", params={**base, "offset": 99}
    ).json()
    assert beyond["items"] == []


def test_category_filter(client, keeper):
    cat_a = keeper.cat(name=f"文献分类A-{_SUFFIX}")
    cat_b = keeper.cat(name=f"文献分类B-{_SUFFIX}")
    lit_a = keeper.literature(name=f"过滤文A-{_SUFFIX}", category_id=cat_a["id"])
    keeper.literature(name=f"过滤文B-{_SUFFIX}", category_id=cat_b["id"])

    resp = client.get(
        "/api/v1/literatures", params={"category_id": cat_a["id"]}
    )
    items = resp.json()["items"]
    assert _find(items, lit_a["id"]) is not None
    assert all(i["category_id"] == cat_a["id"] for i in items)


def test_tag_filter(client, keeper):
    tag = keeper.tag(name=f"文献筛选标签-{_SUFFIX}")
    lit = keeper.literature(
        name=f"标签筛选文-{_SUFFIX}", tag_ids=[tag["id"]]
    )
    resp = client.get(
        "/api/v1/literatures", params={"tag_id": tag["id"]}
    )
    assert _find(resp.json()["items"], lit["id"]) is not None


# ── Update ────────────────────────────────────────────────────────────────────


def test_member_update_forbidden(client, monkeypatch):
    monkeypatch.setattr(deps_mod.TEST_USER, "role", "member")
    resp = client.put(
        f"/api/v1/literatures/{uuid.uuid4()}", json={"content": "x"}
    )
    assert resp.status_code == 403


def test_literature_update_partial(client, keeper):
    lit = keeper.literature(
        name=f"更新文献-{_SUFFIX}",
        author="旧作者",
        dynasty="旧朝代",
        summary="旧摘要",
        content="旧内容",
        source="《旧版本》",
    )
    resp = client.put(
        f"/api/v1/literatures/{lit['id']}",
        json={"content": "新内容", "author": "新作者"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["content"] == "新内容"
    assert body["author"] == "新作者"
    # 未提供的字段保持原值
    assert body["dynasty"] == "旧朝代"
    assert body["summary"] == "旧摘要"
    assert body["source"] == "《旧版本》"
    assert body["name"] == f"更新文献-{_SUFFIX}"


def test_update_404(client):
    assert (
        client.put(
            f"/api/v1/literatures/{uuid.uuid4()}", json={"content": "y"}
        ).status_code
        == 404
    )


def test_update_category_revalidates_resource_type(client, keeper):
    lit_cat = keeper.cat(name=f"正确文献分类-{_SUFFIX}")
    herb_cat = keeper.cat(resource_type="herb", name=f"错配中药分类-{_SUFFIX}")
    lit = keeper.literature(
        name=f"改分类文献-{_SUFFIX}", category_id=lit_cat["id"]
    )

    resp = client.put(
        f"/api/v1/literatures/{lit['id']}",
        json={"category_id": herb_cat["id"]},
    )
    assert resp.status_code == 400, resp.text

    # 原分类未被改动
    body = client.get(f"/api/v1/literatures/{lit['id']}").json()
    assert body["category_id"] == lit_cat["id"]


def test_tag_ids_replace_and_keep(client, keeper):
    tag_a = keeper.tag(name=f"文献标签A-{_SUFFIX}")
    tag_b = keeper.tag(name=f"文献标签B-{_SUFFIX}")
    lit = keeper.literature(
        name=f"文献标签替换-{_SUFFIX}", tag_ids=[tag_a["id"]]
    )

    # 未提供 tag_ids → 保留
    resp = client.put(
        f"/api/v1/literatures/{lit['id']}", json={"content": "不动标签"}
    )
    assert resp.status_code == 200
    assert [t["id"] for t in resp.json()["tags"]] == [tag_a["id"]]

    # 提供 tag_ids → 整体替换
    resp = client.put(
        f"/api/v1/literatures/{lit['id']}", json={"tag_ids": [tag_b["id"]]}
    )
    assert resp.status_code == 200
    assert [t["id"] for t in resp.json()["tags"]] == [tag_b["id"]]

    # 显式空列表 → 清空
    resp = client.put(
        f"/api/v1/literatures/{lit['id']}", json={"tag_ids": []}
    )
    assert resp.status_code == 200
    assert resp.json()["tags"] == []


def test_update_duplicate_name_409(client, keeper):
    name = f"更新撞名-{_SUFFIX}"
    keeper.literature(name=name)
    other = keeper.literature(name=f"另一文献-{_SUFFIX}")
    resp = client.put(
        f"/api/v1/literatures/{other['id']}", json={"name": name}
    )
    assert resp.status_code == 409


# ── Delete ─────────────────────────────────────────────────────────────────────


def test_member_delete_forbidden(client, monkeypatch):
    monkeypatch.setattr(deps_mod.TEST_USER, "role", "member")
    assert (
        client.delete(f"/api/v1/literatures/{uuid.uuid4()}").status_code
        == 403
    )


def test_admin_delete_chain(client, keeper):
    lit = keeper.literature(name=f"管理员链路文献-{_SUFFIX}")
    resp = client.delete(f"/api/v1/literatures/{lit['id']}")
    assert resp.status_code == 204
    keeper.forget("lit", lit["id"])
    assert (
        client.get(f"/api/v1/literatures/{lit['id']}").status_code == 404
    )


def test_delete_404(client):
    assert (
        client.delete(f"/api/v1/literatures/{uuid.uuid4()}").status_code
        == 404
    )


def test_delete_cascades_tags(client, keeper):
    tag = keeper.tag(name=f"级联文献标签-{_SUFFIX}")
    lit = keeper.literature(
        name=f"级联文献-{_SUFFIX}",
        tag_ids=[tag["id"]],
    )

    assert client.delete(f"/api/v1/literatures/{lit['id']}").status_code == 204
    keeper.forget("lit", lit["id"])

    # literature_tags 已 CASCADE → 标签可删除（若残留会 409）
    assert client.delete(f"/api/v1/tags/{tag['id']}").status_code == 204
    keeper.forget("tag", tag["id"])


# ── member 读权限补充 ──────────────────────────────────────────────────────────


def test_member_read_allowed(client, monkeypatch):
    monkeypatch.setattr(deps_mod.TEST_USER, "role", "member")
    assert client.get("/api/v1/literatures").status_code == 200
    assert (
        client.get(f"/api/v1/literatures/{uuid.uuid4()}").status_code == 404
    )
