"""方剂资源 API 测试（TASK-004 Stage 3）。

覆盖：
- CRUD / 详情字段完整性（含组成 ingredients 的 herb_name / amount / role）
- keyword 搜索（name / aliases / efficacy / indications，ILIKE + unnest 相关子查询）
- 分页（total / limit / offset / 越界 offset）
- 分类关联：正常、不存在 400、resource_type 非 prescription 400
- 标签关联：tag_id 筛选、tag_ids 整体替换 / 未提供保留
- 组成校验：herb 不存在 400、重复 herb_id 400、整体替换（含同药材重设）
- 权限：member 读 200 写 403；admin POST 201 / PUT 200 / DELETE 204
- 错误：name 重复 409；不存在 404
- 删除联动：prescription_ingredients / prescription_tags 由 DB CASCADE 自动清理

Keeper 记录全部已创建资源，用例结束（含断言失败）后按
方剂 → 中药 → 分类 → 标签 顺序回收，保证不残留。
"""

import uuid

import pytest

import src.core.deps as deps_mod
from tests.test_upload import PG_AVAILABLE

pytestmark = pytest.mark.skipif(not PG_AVAILABLE, reason="PostgreSQL 未启动")

_SUFFIX = uuid.uuid4().hex[:8]


class Keeper:
    """记录测试创建的方剂 / 中药 / 分类 / 标签，统一回收。"""

    def __init__(self, client) -> None:
        self.client = client
        self._created: list[tuple[str, str]] = []

    def cat(
        self, resource_type: str = "prescription", name: str | None = None
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

    def herb(self, name: str | None = None, **body) -> dict:
        label = name or f"测试药材-{_SUFFIX}"
        resp = self.client.post(
            "/api/v1/herbs", json={"name": label, **body}
        )
        assert resp.status_code == 201, resp.text
        out = resp.json()
        self._created.append(("herb", out["id"]))
        return out

    def prescription(self, name: str | None = None, **body) -> dict:
        label = name or f"测试方剂-{_SUFFIX}"
        resp = self.client.post(
            "/api/v1/prescriptions", json={"name": label, **body}
        )
        assert resp.status_code == 201, resp.text
        out = resp.json()
        # 方剂最后登记，回收时最先删除以解除对中药 / 标签的 RESTRICT
        self._created.append(("prescription", out["id"]))
        return out

    def forget(self, kind: str, item_id: str) -> None:
        """从回收名单移除（删除级联类用例中已显式删除的资源）。"""
        self._created = [
            (k, i) for k, i in self._created if not (k == kind and i == item_id)
        ]

    def teardown(self) -> None:
        by_kind: dict[str, list[str]] = {
            "prescription": [],
            "herb": [],
            "cat": [],
            "tag": [],
        }
        for kind, item_id in self._created:
            by_kind[kind].append(item_id)
        paths = (
            ("prescription", "prescriptions"),
            ("herb", "herbs"),
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


def test_prescription_create_and_list(client, keeper):
    cat = keeper.cat()
    tag = keeper.tag()
    herb_a = keeper.herb(name=f"君药-{_SUFFIX}")
    herb_b = keeper.herb(name=f"臣药-{_SUFFIX}")
    p = keeper.prescription(
        name=f"方剂CRUD-{_SUFFIX}",
        aliases=[f"别名方-{_SUFFIX}"],
        category_id=cat["id"],
        efficacy="解表散寒",
        indications="外感风寒表实证",
        usage_method="水煎服，每日一剂",
        source="《伤寒论》",
        description="测试方解",
        ingredients=[
            {
                "herb_id": herb_a["id"],
                "amount": 9,
                "unit": "克",
                "role": "君",
                "sort_order": 0,
            },
            {
                "herb_id": herb_b["id"],
                "amount": 6,
                "unit": "克",
                "processing": "炙",
                "role": "臣",
                "sort_order": 1,
            },
        ],
        tag_ids=[tag["id"]],
    )

    assert p["name"] == f"方剂CRUD-{_SUFFIX}"
    assert p["aliases"] == [f"别名方-{_SUFFIX}"]
    assert p["category"] is not None and p["category"]["id"] == cat["id"]
    assert p["category_id"] == cat["id"]
    assert p["efficacy"] == "解表散寒"
    assert p["indications"] == "外感风寒表实证"
    assert p["usage_method"] == "水煎服，每日一剂"
    assert p["source"] == "《伤寒论》"
    assert p["description"] == "测试方解"
    # 组成按 sort_order 输出，且带出药材名
    assert [i["herb_id"] for i in p["ingredients"]] == [
        herb_a["id"],
        herb_b["id"],
    ]
    assert [i["herb_name"] for i in p["ingredients"]] == [
        herb_a["name"],
        herb_b["name"],
    ]
    assert p["ingredients"][0]["amount"] == 9
    assert p["ingredients"][0]["role"] == "君"
    assert p["ingredients"][1]["processing"] == "炙"
    assert p["ingredients"][1]["sort_order"] == 1
    assert [t["id"] for t in p["tags"]] == [tag["id"]]

    listing = client.get("/api/v1/prescriptions", params={"limit": 100}).json()
    assert listing["limit"] == 100
    assert listing["offset"] == 0
    assert _find(listing["items"], p["id"]) is not None


def test_prescription_minimal_create(client, keeper):
    """只填 name：可选字段落默认值。"""
    p = keeper.prescription(name=f"最小方剂-{_SUFFIX}")
    assert p["aliases"] == []
    assert p["category"] is None and p["category_id"] is None
    assert p["efficacy"] == ""
    assert p["indications"] == ""
    assert p["ingredients"] == []
    assert p["tags"] == []


def test_prescription_detail(client, keeper):
    p = keeper.prescription(name=f"详情方剂-{_SUFFIX}")
    resp = client.get(f"/api/v1/prescriptions/{p['id']}")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["id"] == p["id"]
    for key in (
        "name",
        "aliases",
        "category_id",
        "category",
        "efficacy",
        "indications",
        "usage_method",
        "source",
        "description",
        "ingredients",
        "tags",
        "created_at",
        "updated_at",
    ):
        assert key in body


def test_prescription_update_partial(client, keeper):
    p = keeper.prescription(
        name=f"更新方剂-{_SUFFIX}",
        efficacy="旧功效",
        source="《旧出处》",
    )
    resp = client.put(
        f"/api/v1/prescriptions/{p['id']}", json={"efficacy": "新功效"}
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["efficacy"] == "新功效"
    # 未提供的字段保持原值
    assert body["source"] == "《旧出处》"
    assert body["name"] == f"更新方剂-{_SUFFIX}"


def test_prescription_delete(client, keeper):
    p = keeper.prescription(name=f"删除方剂-{_SUFFIX}")
    resp = client.delete(f"/api/v1/prescriptions/{p['id']}")
    assert resp.status_code == 204

    assert client.get(f"/api/v1/prescriptions/{p['id']}").status_code == 404
    listing = client.get("/api/v1/prescriptions").json()
    assert _find(listing["items"], p["id"]) is None


# ── 组成（ingredients）整体替换 ──────────────────────────────────────────────


def test_ingredients_replace(client, keeper):
    herb_a = keeper.herb(name=f"替换药A-{_SUFFIX}")
    herb_b = keeper.herb(name=f"替换药B-{_SUFFIX}")
    p = keeper.prescription(
        name=f"组成替换-{_SUFFIX}",
        ingredients=[
            {"herb_id": herb_a["id"], "amount": 9, "unit": "克"}
        ],
    )

    # 未提供 ingredients → 保持原组成
    resp = client.put(
        f"/api/v1/prescriptions/{p['id']}", json={"source": "《金匮要略》"}
    )
    assert resp.status_code == 200, resp.text
    assert [i["herb_id"] for i in resp.json()["ingredients"]] == [herb_a["id"]]

    # 提供 ingredients → 整体替换
    resp = client.put(
        f"/api/v1/prescriptions/{p['id']}",
        json={
            "ingredients": [
                {"herb_id": herb_b["id"], "amount": 3, "unit": "克"}
            ]
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()["ingredients"]
    assert [i["herb_id"] for i in body] == [herb_b["id"]]
    assert body[0]["amount"] == 3

    # 同一药材重设（旧行先物理清除，验证唯一键不冲突）
    resp = client.put(
        f"/api/v1/prescriptions/{p['id']}",
        json={
            "ingredients": [
                {"herb_id": herb_a["id"], "amount": 12, "unit": "克"}
            ]
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()["ingredients"]
    assert [i["herb_id"] for i in body] == [herb_a["id"]]
    assert body[0]["amount"] == 12

    # 空列表 → 清空组成
    resp = client.put(
        f"/api/v1/prescriptions/{p['id']}", json={"ingredients": []}
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["ingredients"] == []


def test_tag_ids_replace_and_keep(client, keeper):
    tag_a = keeper.tag(name=f"方剂标签A-{_SUFFIX}")
    tag_b = keeper.tag(name=f"方剂标签B-{_SUFFIX}")
    p = keeper.prescription(
        name=f"方剂标签替换-{_SUFFIX}", tag_ids=[tag_a["id"]]
    )

    # 未提供 tag_ids → 保留
    resp = client.put(
        f"/api/v1/prescriptions/{p['id']}", json={"efficacy": "不动标签"}
    )
    assert resp.status_code == 200
    assert [t["id"] for t in resp.json()["tags"]] == [tag_a["id"]]

    # 提供 tag_ids → 整体替换
    resp = client.put(
        f"/api/v1/prescriptions/{p['id']}", json={"tag_ids": [tag_b["id"]]}
    )
    assert resp.status_code == 200
    assert [t["id"] for t in resp.json()["tags"]] == [tag_b["id"]]


# ── 关键词搜索 ───────────────────────────────────────────────────────────────


def test_search_by_name(client, keeper):
    p = keeper.prescription(name=f"方剂搜索名-{_SUFFIX}")
    resp = client.get(
        "/api/v1/prescriptions", params={"keyword": f"方剂搜索名-{_SUFFIX}"}
    )
    assert _find(resp.json()["items"], p["id"]) is not None


def test_search_by_alias(client, keeper):
    p = keeper.prescription(
        name=f"方剂别名搜索-{_SUFFIX}",
        aliases=[f"XYZ方别名-{_SUFFIX}"],
    )
    resp = client.get(
        "/api/v1/prescriptions",
        params={"keyword": f"XYZ方别名-{_SUFFIX}"},
    )
    assert _find(resp.json()["items"], p["id"]) is not None


def test_search_by_efficacy(client, keeper):
    p = keeper.prescription(
        name=f"功效搜索方-{_SUFFIX}",
        efficacy=f"ABC功效-{_SUFFIX}",
    )
    resp = client.get(
        "/api/v1/prescriptions",
        params={"keyword": f"ABC功效-{_SUFFIX}"},
    )
    assert _find(resp.json()["items"], p["id"]) is not None


def test_search_by_indications(client, keeper):
    p = keeper.prescription(
        name=f"主治搜索方-{_SUFFIX}",
        indications=f"DEF主治-{_SUFFIX}",
    )
    resp = client.get(
        "/api/v1/prescriptions",
        params={"keyword": f"DEF主治-{_SUFFIX}"},
    )
    assert _find(resp.json()["items"], p["id"]) is not None


def test_search_by_description(client, keeper):
    p = keeper.prescription(
        name=f"描述搜索方-{_SUFFIX}",
        description=f"测试方剂描述关键词-{_SUFFIX}",
    )
    resp = client.get(
        "/api/v1/prescriptions",
        params={"keyword": "方剂描述"},
    )
    assert _find(resp.json()["items"], p["id"]) is not None


def test_search_by_usage_method(client, keeper):
    p = keeper.prescription(
        name=f"用法搜索方-{_SUFFIX}",
        usage_method=f"水煎服-{_SUFFIX}",
    )
    resp = client.get(
        "/api/v1/prescriptions",
        params={"keyword": f"水煎服-{_SUFFIX}"},
    )
    assert _find(resp.json()["items"], p["id"]) is not None


def test_search_by_source(client, keeper):
    p = keeper.prescription(
        name=f"出处搜索方-{_SUFFIX}",
        source=f"伤寒论-{_SUFFIX}",
    )
    resp = client.get(
        "/api/v1/prescriptions",
        params={"keyword": f"伤寒论-{_SUFFIX}"},
    )
    assert _find(resp.json()["items"], p["id"]) is not None


def test_search_by_ingredient_herb_name(client, keeper):
    """通过组成药材名称搜索方剂：方剂文本字段不含关键词，仅靠 ingredient herb.name 命中。"""
    herb = keeper.herb(name=f"桂枝药材-{_SUFFIX}")
    p = keeper.prescription(
        name=f"组成搜索方-{_SUFFIX}",
        ingredients=[
            {"herb_id": herb["id"], "amount": 9, "unit": "克"}
        ],
    )
    resp = client.get(
        "/api/v1/prescriptions",
        params={"keyword": f"桂枝药材-{_SUFFIX}"},
    )
    assert _find(resp.json()["items"], p["id"]) is not None


def test_search_by_ingredient_no_duplicate(client, keeper):
    """一个方剂有多味组成药材命中同一 keyword 时，只返回 1 条。"""
    herb_a = keeper.herb(name=f"匹配药甲-{_SUFFIX}")
    herb_b = keeper.herb(name=f"匹配药乙-{_SUFFIX}")
    p = keeper.prescription(
        name=f"多组成方-{_SUFFIX}",
        ingredients=[
            {"herb_id": herb_a["id"], "amount": 3, "unit": "克"},
            {"herb_id": herb_b["id"], "amount": 6, "unit": "克"},
        ],
    )
    resp = client.get(
        "/api/v1/prescriptions", params={"keyword": "匹配药"}
    )
    body = resp.json()
    matches = [i for i in body["items"] if i["id"] == p["id"]]
    assert len(matches) == 1


def test_search_keyword_with_category(client, keeper):
    """keyword + category_id 组合过滤：AND 逻辑。"""
    cat_a = keeper.cat(name=f"组合分类A-{_SUFFIX}")
    cat_b = keeper.cat(name=f"组合分类B-{_SUFFIX}")
    herb = keeper.herb(name=f"组合药材-{_SUFFIX}")
    p_a = keeper.prescription(
        name=f"组合方A-{_SUFFIX}",
        category_id=cat_a["id"],
        ingredients=[{"herb_id": herb["id"], "amount": 3, "unit": "克"}],
    )
    keeper.prescription(
        name=f"组合方B-{_SUFFIX}",
        category_id=cat_b["id"],
        ingredients=[{"herb_id": herb["id"], "amount": 6, "unit": "克"}],
    )
    resp = client.get(
        "/api/v1/prescriptions",
        params={"keyword": f"组合药材-{_SUFFIX}", "category_id": cat_a["id"]},
    )
    body = resp.json()
    assert _find(body["items"], p_a["id"]) is not None
    assert all(i["category_id"] == cat_a["id"] for i in body["items"])


def test_search_keyword_with_tag(client, keeper):
    """keyword + tag_id 组合过滤：AND 逻辑。"""
    tag_a = keeper.tag(name=f"组合标签A-{_SUFFIX}")
    tag_b = keeper.tag(name=f"组合标签B-{_SUFFIX}")
    herb = keeper.herb(name=f"标签组合药材-{_SUFFIX}")
    p_a = keeper.prescription(
        name=f"标签组合方A-{_SUFFIX}",
        tag_ids=[tag_a["id"]],
        ingredients=[{"herb_id": herb["id"], "amount": 3, "unit": "克"}],
    )
    keeper.prescription(
        name=f"标签组合方B-{_SUFFIX}",
        tag_ids=[tag_b["id"]],
        ingredients=[{"herb_id": herb["id"], "amount": 6, "unit": "克"}],
    )
    resp = client.get(
        "/api/v1/prescriptions",
        params={"keyword": f"标签组合药材-{_SUFFIX}", "tag_id": tag_a["id"]},
    )
    body = resp.json()
    assert _find(body["items"], p_a["id"]) is not None


# ── 分页 / 过滤 ──────────────────────────────────────────────────────────────


def test_pagination(client, keeper):
    cat = keeper.cat(name=f"方剂分页分类-{_SUFFIX}")
    names = [f"方剂分页{i}-{_SUFFIX}" for i in range(3)]
    for n in names:
        keeper.prescription(name=n, category_id=cat["id"])

    base = {"category_id": cat["id"]}

    body = client.get("/api/v1/prescriptions", params=base).json()
    assert body["total"] == 3

    page1 = client.get(
        "/api/v1/prescriptions", params={**base, "limit": 2, "offset": 0}
    ).json()
    assert page1["total"] == 3
    assert len(page1["items"]) == 2

    page2 = client.get(
        "/api/v1/prescriptions", params={**base, "limit": 2, "offset": 2}
    ).json()
    assert len(page2["items"]) == 1

    # 两页不重叠，合计 3 条
    seen = {i["id"] for i in page1["items"]} | {
        i["id"] for i in page2["items"]
    }
    assert len(seen) == 3

    beyond = client.get(
        "/api/v1/prescriptions", params={**base, "offset": 99}
    ).json()
    assert beyond["items"] == []


def test_category_filter(client, keeper):
    cat_a = keeper.cat(name=f"方剂分类A-{_SUFFIX}")
    cat_b = keeper.cat(name=f"方剂分类B-{_SUFFIX}")
    p_a = keeper.prescription(name=f"过滤方A-{_SUFFIX}", category_id=cat_a["id"])
    keeper.prescription(name=f"过滤方B-{_SUFFIX}", category_id=cat_b["id"])

    resp = client.get(
        "/api/v1/prescriptions", params={"category_id": cat_a["id"]}
    )
    items = resp.json()["items"]
    assert _find(items, p_a["id"]) is not None
    assert all(i["category_id"] == cat_a["id"] for i in items)


def test_tag_filter(client, keeper):
    tag = keeper.tag(name=f"方剂筛选标签-{_SUFFIX}")
    p = keeper.prescription(
        name=f"标签筛选方-{_SUFFIX}", tag_ids=[tag["id"]]
    )
    resp = client.get("/api/v1/prescriptions", params={"tag_id": tag["id"]})
    assert _find(resp.json()["items"], p["id"]) is not None


# ── 分类 / 药材 / 标签校验 ───────────────────────────────────────────────────


def test_category_not_exist_400(client):
    resp = client.post(
        "/api/v1/prescriptions",
        json={"name": f"无分类方-{_SUFFIX}", "category_id": str(uuid.uuid4())},
    )
    assert resp.status_code == 400


def test_category_wrong_resource_type_400(client, keeper):
    hcat = keeper.cat(resource_type="herb", name=f"中药分类-{_SUFFIX}")
    resp = client.post(
        "/api/v1/prescriptions",
        json={"name": f"错分类方-{_SUFFIX}", "category_id": hcat["id"]},
    )
    assert resp.status_code == 400, resp.text


def test_ingredient_herb_not_exist_400(client):
    resp = client.post(
        "/api/v1/prescriptions",
        json={
            "name": f"无药方-{_SUFFIX}",
            "ingredients": [{"herb_id": str(uuid.uuid4()), "amount": 5}],
        },
    )
    assert resp.status_code == 400


def test_ingredient_duplicate_herb_400(client, keeper):
    herb = keeper.herb(name=f"重复药-{_SUFFIX}")
    item = {"herb_id": herb["id"], "amount": 5, "unit": "克"}
    resp = client.post(
        "/api/v1/prescriptions",
        json={
            "name": f"重复药方-{_SUFFIX}",
            "ingredients": [item, dict(item, amount=10)],
        },
    )
    assert resp.status_code == 400, resp.text


def test_tag_not_exist_400(client):
    resp = client.post(
        "/api/v1/prescriptions",
        json={"name": f"无标签方-{_SUFFIX}", "tag_ids": [str(uuid.uuid4())]},
    )
    assert resp.status_code == 400


# ── 权限 ─────────────────────────────────────────────────────────────────────


def test_member_read_allowed_writes_forbidden(client, monkeypatch):
    monkeypatch.setattr(deps_mod.TEST_USER, "role", "member")

    # 写操作一律 403（权限先于存在性检查，故随机 id 也是 403）
    assert (
        client.post(
            "/api/v1/prescriptions", json={"name": f"member越权方-{_SUFFIX}"}
        ).status_code
        == 403
    )
    assert (
        client.put(
            f"/api/v1/prescriptions/{uuid.uuid4()}", json={"efficacy": "x"}
        ).status_code
        == 403
    )
    assert (
        client.delete(f"/api/v1/prescriptions/{uuid.uuid4()}").status_code
        == 403
    )

    # 读操作允许；列表 200，不存在的详情 404（而非 403）
    assert client.get("/api/v1/prescriptions").status_code == 200
    assert (
        client.get(f"/api/v1/prescriptions/{uuid.uuid4()}").status_code == 404
    )


def test_admin_full_chain(client, keeper):
    """admin 全链路：POST 201 → PUT 200 → DELETE 204 → GET 404。"""
    p = keeper.prescription(name=f"管理员链路方-{_SUFFIX}")
    assert p["name"] == f"管理员链路方-{_SUFFIX}"  # POST 201

    resp = client.put(
        f"/api/v1/prescriptions/{p['id']}", json={"usage_method": "研末服"}
    )
    assert resp.status_code == 200  # PUT 200
    assert resp.json()["usage_method"] == "研末服"

    resp = client.delete(f"/api/v1/prescriptions/{p['id']}")
    assert resp.status_code == 204  # DELETE 204
    keeper.forget("prescription", p["id"])
    assert client.get(f"/api/v1/prescriptions/{p['id']}").status_code == 404


# ── 错误 ─────────────────────────────────────────────────────────────────────


def test_duplicate_name_409(client, keeper):
    name = f"方剂重名-{_SUFFIX}"
    keeper.prescription(name=name)
    dup = client.post("/api/v1/prescriptions", json={"name": name})
    assert dup.status_code == 409

    # 编辑成已有名称同样 409
    other = keeper.prescription(name=f"另一方剂-{_SUFFIX}")
    rename = client.put(
        f"/api/v1/prescriptions/{other['id']}", json={"name": name}
    )
    assert rename.status_code == 409


def test_prescription_404(client):
    missing = uuid.uuid4()
    assert client.get(f"/api/v1/prescriptions/{missing}").status_code == 404
    assert (
        client.put(
            f"/api/v1/prescriptions/{missing}", json={"efficacy": "y"}
        ).status_code
        == 404
    )
    assert (
        client.delete(f"/api/v1/prescriptions/{missing}").status_code == 404
    )


# ── 删除联动（ingredients / prescription_tags CASCADE）──────────────────────


def test_delete_cascades(client, keeper):
    herb = keeper.herb(name=f"级联药材-{_SUFFIX}")
    tag = keeper.tag(name=f"级联方剂标签-{_SUFFIX}")
    p = keeper.prescription(
        name=f"级联方剂-{_SUFFIX}",
        ingredients=[
            {"herb_id": herb["id"], "amount": 6, "unit": "克"}
        ],
        tag_ids=[tag["id"]],
    )

    assert client.delete(f"/api/v1/prescriptions/{p['id']}").status_code == 204
    keeper.forget("prescription", p["id"])

    # 组成行已随方剂 CASCADE 清理 → 药材可删除（若残留 RESTRICT 会 409）
    assert client.delete(f"/api/v1/herbs/{herb['id']}").status_code == 204
    keeper.forget("herb", herb["id"])

    # prescription_tags 已 CASCADE → 标签可删除（若残留会 409）
    assert client.delete(f"/api/v1/tags/{tag['id']}").status_code == 204
    keeper.forget("tag", tag["id"])
