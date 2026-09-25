"""列表接口可选分页（卡顿修复）回归测试。

背景：管理员登录后在知识库 / 用户管理页明显卡顿。排查发现
`GET /api/v1/kb` 与 `GET /api/v1/users` 均无分页参数，一次返回全量
（实测 4290 条 / 791KB、846 条 / 244KB），前端需整体解析后再渲染。

修复方式：新增**可选** `limit` / `offset`。

- 不传：返回全部，行为与改动前完全一致（侧边栏、资源挂载下拉等既有消费方依赖）
- 传参：服务端分页，单次上限 500

`/users` 另在响应头 `X-Total-Count` 暴露总数（不改动返回体结构），
供前端做真分页；总数头在两种模式下都必须存在。

需要 PostgreSQL，不可用则整模块 skip。
"""

import pytest

from tests.test_upload import PG_AVAILABLE

pytestmark = pytest.mark.skipif(not PG_AVAILABLE, reason="需要 PostgreSQL")


# ── GET /users ───────────────────────────────────────────────────────────────


def test_users_default_keeps_full_list(client):
    """不传 limit 时必须仍返回全量（兼容性红线）。"""
    resp = client.get("/api/v1/users")
    assert resp.status_code == 200
    body = resp.json()
    assert isinstance(body, list)

    total = resp.headers.get("x-total-count")
    assert total is not None, "总数响应头缺失，前端无法分页"
    assert int(total) == len(body), "全量模式下总数应与返回条数一致"


def test_users_limit_truncates_without_changing_total(client):
    total = int(client.get("/api/v1/users").headers["x-total-count"])
    if total == 0:
        pytest.skip("库中无用户数据")

    resp = client.get("/api/v1/users", params={"limit": 5})
    assert resp.status_code == 200
    assert len(resp.json()) == min(5, total)
    assert int(resp.headers["x-total-count"]) == total, "分页不得改变总数口径"


def test_users_offset_pages_do_not_overlap(client):
    total = int(client.get("/api/v1/users").headers["x-total-count"])
    if total < 3:
        pytest.skip("用户数不足以验证翻页")

    page1 = client.get("/api/v1/users", params={"limit": 2, "offset": 0}).json()
    page2 = client.get("/api/v1/users", params={"limit": 2, "offset": 2}).json()
    ids1 = {u["id"] for u in page1}
    ids2 = {u["id"] for u in page2}
    assert ids1, "第一页不应为空"
    assert not (ids1 & ids2), "相邻两页不得重复"


def test_users_out_of_range_offset_returns_empty(client):
    total = int(client.get("/api/v1/users").headers["x-total-count"])
    resp = client.get("/api/v1/users", params={"limit": 20, "offset": total + 1000})
    assert resp.status_code == 200
    assert resp.json() == []


def test_users_rejects_invalid_limit(client):
    assert client.get("/api/v1/users", params={"limit": 0}).status_code == 422
    assert client.get("/api/v1/users", params={"limit": 501}).status_code == 422


# ── GET /kb ──────────────────────────────────────────────────────────────────


def test_kb_default_keeps_full_list(client):
    resp = client.get("/api/v1/kb")
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


def test_kb_limit_truncates(client):
    all_kbs = client.get("/api/v1/kb").json()
    if not all_kbs:
        pytest.skip("库中无知识库数据")

    limited = client.get("/api/v1/kb", params={"limit": 3}).json()
    assert len(limited) == min(3, len(all_kbs))


def test_kb_offset_pages_do_not_overlap(client):
    all_kbs = client.get("/api/v1/kb").json()
    if len(all_kbs) < 3:
        pytest.skip("知识库数量不足以验证翻页")

    page1 = client.get("/api/v1/kb", params={"limit": 2, "offset": 0}).json()
    page2 = client.get("/api/v1/kb", params={"limit": 2, "offset": 2}).json()
    ids1 = {k["id"] for k in page1}
    ids2 = {k["id"] for k in page2}
    assert ids1
    assert not (ids1 & ids2), "相邻两页不得重复"


def test_kb_rejects_invalid_limit(client):
    assert client.get("/api/v1/kb", params={"limit": 0}).status_code == 422
    assert client.get("/api/v1/kb", params={"limit": 501}).status_code == 422
