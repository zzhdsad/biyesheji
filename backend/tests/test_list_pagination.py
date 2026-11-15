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

import uuid

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


def test_users_list_tolerates_reserved_domain_email(client):
    """历史/测试数据可能存在 @test.local 等保留域名。

    EmailStr 会拒绝该域名，若响应模型继续强校验，一条脏数据就让整个列表 500。
    """
    import asyncio

    from sqlalchemy import delete
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from src.core.config import settings
    from src.domain.models import User

    username = f"reserved-{uuid.uuid4().hex[:8]}"
    user_id = uuid.uuid4()

    async def _write(action):
        engine = create_async_engine(settings.DATABASE_URL)
        try:
            async with async_sessionmaker(engine, expire_on_commit=False)() as session:
                await action(session)
                await session.commit()
        finally:
            await engine.dispose()

    async def _add(session):
        session.add(
            User(
                id=user_id,
                email=f"{username}@test.local",
                username=username,
                hashed_password="not-a-real-hash",
                role="member",
            )
        )

    async def _remove(session):
        await session.execute(delete(User).where(User.id == user_id))

    asyncio.run(_write(_add))
    try:
        resp = client.get("/api/v1/users")
        assert resp.status_code == 200, resp.text
        row = next(u for u in resp.json() if u["id"] == str(user_id))
        assert row["email"] == f"{username}@test.local"
    finally:
        asyncio.run(_write(_remove))


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


# ── GET /documents ───────────────────────────────────────────────────────────


def _doc_kb(client) -> str:
    resp = client.post("/api/v1/kb", json={"name": f"文档分页KB-{uuid.uuid4().hex[:8]}"})
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def test_documents_limit_and_total_header(client):
    kb_id = _doc_kb(client)
    for name in ("分页1.txt", "分页2.txt"):
        resp = client.post(
            "/api/v1/documents/upload",
            data={"kb_id": kb_id},
            files={"file": (name, f"hello world {name}".encode(), "text/plain")},
        )
        assert resp.status_code == 201, resp.text

    all_resp = client.get("/api/v1/documents", params={"kb_id": kb_id})
    assert all_resp.status_code == 200
    assert len(all_resp.json()) >= 2
    assert int(all_resp.headers["x-total-count"]) == len(all_resp.json())

    limited = client.get(
        "/api/v1/documents", params={"kb_id": kb_id, "limit": 1}
    )
    assert limited.status_code == 200
    assert len(limited.json()) == 1
    assert int(limited.headers["x-total-count"]) >= 2


def test_documents_offset_pages_do_not_overlap(client):
    kb_id = _doc_kb(client)
    for name in ("翻页1.txt", "翻页2.txt"):
        resp = client.post(
            "/api/v1/documents/upload",
            data={"kb_id": kb_id},
            files={"file": (name, f"hello world {name}".encode(), "text/plain")},
        )
        assert resp.status_code == 201, resp.text

    page1 = client.get(
        "/api/v1/documents", params={"kb_id": kb_id, "limit": 1, "offset": 0}
    ).json()
    page2 = client.get(
        "/api/v1/documents", params={"kb_id": kb_id, "limit": 1, "offset": 1}
    ).json()
    assert page1 and page2
    assert page1[0]["id"] != page2[0]["id"]
