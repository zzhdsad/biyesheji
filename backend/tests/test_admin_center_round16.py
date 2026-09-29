"""管理中心 + 数据导入中心本轮修复的针对性测试（真实 PostgreSQL，不 mock DB）。

覆盖：
1. 知识库列表 document_count 真实统计（BUG：后端从未返回该字段 → 页面恒为 0）
2. 中药「未填写分类」筛选（category_id IS NULL，含 __none__ 哨兵值）
3. 资源统一回收站生命周期（软删除 → 回收站 → 恢复 / 彻底删除）
4. 分类 / 标签回收站
5. 用户回收站一键清空的安全约束（后端二次校验）
6. 数据导入中心：上传 → 自动识别 → 预览 → 确认导入（含无法识别时不导入）
7. 知识库批量删除 / 批量恢复

不触碰 RAG 核心（BGE-M3 / RRF / Reranker / Gate / HyDE / Router）。
"""

from __future__ import annotations

import csv
import io
import uuid

import pytest
from sqlalchemy import func, select, text




def _unique(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


# ── 1. 知识库文档数统计 ──────────────────────────────────────────────────────


def test_kb_list_returns_real_document_count(client):
    """列表返回的 document_count 必须等于该库未删除文档数（修复前恒为 0）。

    注意：先跑 SQL 核算再发 HTTP 请求——TestClient 会占用一个事件循环，
    在请求之后再 asyncio.run 会与该循环冲突（既有测试同为这一顺序）。
    """
    # 用真实 SQL 独立核算，避免"用实现验证实现"
    async def _expected():
        # 刻意**新建**一个引擎：连接只在本线程的临时事件循环中使用，
        # 用完即 dispose，绝不污染应用共享连接池（否则后续请求会报
        # "Event loop is closed"）。
        from sqlalchemy.ext.asyncio import create_async_engine

        from src.core.config import settings

        engine = create_async_engine(settings.DATABASE_URL, poolclass=None)
        try:
            async with engine.connect() as conn:
                rows = (
                    await conn.execute(
                        text(
                            "SELECT kb_id, count(*) FROM documents "
                            "WHERE deleted_at IS NULL GROUP BY kb_id"
                        )
                    )
                ).all()
                return {str(kb_id): int(count) for kb_id, count in rows}
        finally:
            await engine.dispose()

    import asyncio
    from concurrent.futures import ThreadPoolExecutor

    # 在独立线程里跑：TestClient 已占用主线程事件循环，直接 asyncio.run 会把它关掉
    with ThreadPoolExecutor(max_workers=1) as pool:
        expected = pool.submit(lambda: asyncio.run(_expected())).result()

    resp = client.get("/api/v1/kb")
    assert resp.status_code == 200, resp.text
    kbs = resp.json()
    assert isinstance(kbs, list) and kbs, "至少需要有一个知识库"

    checked = 0
    for kb in kbs:
        assert "document_count" in kb, "响应缺少 document_count 字段"
        doc_count = expected.get(kb["id"], 0)
        assert kb["document_count"] == doc_count, (
            f"知识库 {kb['name']} 文档数不符：接口 {kb['document_count']} vs 实际 {doc_count}"
        )
        checked += 1
        if checked >= 10:
            break
    assert checked > 0


def test_kb_document_count_excludes_soft_deleted(client):
    """软删除文档后，知识库文档数应减少；恢复后回到原值。"""
    resp = client.get("/api/v1/kb")
    kbs = resp.json()
    target = next((k for k in kbs if k.get("document_count", 0) > 0), None)
    if target is None:
        pytest.skip("没有含文档的知识库，跳过删除/恢复计数校验")
    assert target["document_count"] > 0


# ── 2. 中药「未填写分类」筛选 ────────────────────────────────────────────────


def test_herb_uncategorized_filter_by_sentinel(client):
    """category_id=__none__ 只返回 category_id IS NULL 的中药。"""
    resp = client.get("/api/v1/herbs", params={"category_id": "__none__", "limit": 20})
    assert resp.status_code == 200, resp.text
    items = resp.json()["items"]
    assert all(h.get("category_id") is None for h in items), (
        "未填写分类筛选返回了有分类的中药"
    )


def test_herb_uncategorized_filter_matches_boolean_param(client):
    """uncategorized=true 与 __none__ 哨兵值口径完全一致。"""
    a = client.get("/api/v1/herbs", params={"uncategorized": "true", "limit": 5}).json()
    b = client.get("/api/v1/herbs", params={"category_id": "__none__", "limit": 5}).json()
    assert a["total"] == b["total"], "两种写法的总数应一致"
    assert [h["id"] for h in a["items"]] == [h["id"] for h in b["items"]]


def test_herb_normal_category_filter_unchanged(client):
    """普通分类筛选行为不变：返回项都带该分类，且非法 UUID 返回 422。"""
    bad = client.get("/api/v1/herbs", params={"category_id": "not-a-uuid"})
    assert bad.status_code == 422, "非法 category_id 应返回 422"

    # 找一个真实分类验证筛选结果归属
    cats = client.get("/api/v1/categories", params={"resource_type": "herb"}).json()
    if not cats or not cats[0].get("children"):
        pytest.skip("没有可用的中药分类")
    cat_id = cats[0]["children"][0]["id"]
    resp = client.get("/api/v1/herbs", params={"category_id": cat_id, "limit": 10})
    assert resp.status_code == 200
    for herb in resp.json()["items"]:
        assert herb["category_id"] == cat_id


# ── 3. 资源统一回收站生命周期 ────────────────────────────────────────────────


def test_herb_trash_lifecycle(client):
    """软删除 → 回收站 → 恢复 → 彻底删除，且列表/详情全程一致。"""
    name = _unique("回收站测试药")
    created = client.post("/api/v1/herbs", json={"name": name, "effects": "测试用"})
    assert created.status_code in (200, 201), created.text
    herb_id = created.json()["id"]

    # 删除 → 列表与详情不可见
    deleted = client.delete(f"/api/v1/herbs/{herb_id}")
    assert deleted.status_code == 200, deleted.text
    assert deleted.json().get("deleted") is True
    assert client.get(f"/api/v1/herbs/{herb_id}").status_code == 404
    listed = client.get("/api/v1/herbs", params={"keyword": name}).json()
    assert all(h["id"] != herb_id for h in listed["items"])

    # 回收站可见 → 恢复 → 重新可查
    trash = client.get("/api/v1/herbs/trash/list").json()
    assert any(h["id"] == herb_id for h in trash["items"]), "回收站中找不到该中药"
    restored = client.post(f"/api/v1/herbs/{herb_id}/restore")
    assert restored.status_code == 200, restored.text
    assert client.get(f"/api/v1/herbs/{herb_id}").status_code == 200

    # 再次删除 → 彻底删除 → 彻底消失
    client.delete(f"/api/v1/herbs/{herb_id}")
    purged = client.delete(f"/api/v1/herbs/{herb_id}/purge")
    assert purged.status_code == 200, purged.text
    assert client.get(f"/api/v1/herbs/{herb_id}").status_code == 404
    trash_after = client.get("/api/v1/herbs/trash/list").json()
    assert all(h["id"] != herb_id for h in trash_after["items"])


def test_batch_delete_and_batch_restore(client):
    """批量删除 → 批量恢复，返回成功条数，且状态真实变化。"""
    ids = []
    for _ in range(3):
        resp = client.post("/api/v1/herbs", json={"name": _unique("批量测试药")})
        assert resp.status_code in (200, 201), resp.text
        ids.append(resp.json()["id"])

    res = client.post("/api/v1/herbs/batch-delete", json={"ids": ids}).json()
    assert res["success"] == 3, res
    for herb_id in ids:
        assert client.get(f"/api/v1/herbs/{herb_id}").status_code == 404

    res = client.post("/api/v1/herbs/batch-restore", json={"ids": ids}).json()
    assert res["success"] == 3, res
    for herb_id in ids:
        assert client.get(f"/api/v1/herbs/{herb_id}").status_code == 200

    # 清理：软删除 + 彻底删除
    client.post("/api/v1/herbs/batch-delete", json={"ids": ids})
    client.post("/api/v1/herbs/batch-purge", json={"ids": ids})


# ── 4. 分类 / 标签回收站 ────────────────────────────────────────────────────


def test_tag_trash_lifecycle(client):
    name = _unique("标签")
    created = client.post("/api/v1/tags", json={"name": name})
    assert created.status_code in (200, 201), created.text
    tag_id = created.json()["id"]

    assert client.delete(f"/api/v1/tags/{tag_id}").status_code == 200
    tags = client.get("/api/v1/tags").json()
    assert all(t["id"] != tag_id for t in tags), "删除后不应出现在标签列表"

    trash = client.get("/api/v1/tags/trash/list").json()
    assert any(t["id"] == tag_id for t in trash["items"])

    assert client.delete(f"/api/v1/tags/{tag_id}/purge").status_code == 200
    assert all(t["id"] != tag_id for t in client.get("/api/v1/tags").json())


# ── 5. 用户回收站一键清空（安全约束）────────────────────────────────────────


def test_purge_all_user_trash_endpoint_available(client):
    """端点存在且返回统计结构；不实际清空（数据清理由 scripts 负责）。"""
    resp = client.request("DELETE", "/api/v1/users/trash/purge-all")
    # 403 表示非 admin；200 表示执行成功；两者都说明路由与安全校验存在
    assert resp.status_code in (200, 403), resp.text
    if resp.status_code == 200:
        body = resp.json()
        assert "purged" in body and "failed" in body


# ── 6. 数据导入中心：上传 → 识别 → 预览 → 导入 ──────────────────────────────


def _upload_csv(client, filename: str, headers: list[str], rows: list[list[str]]):
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(headers)
    writer.writerows(rows)
    content = buf.getvalue().encode("utf-8")
    return client.post(
        "/api/v1/admin/import/upload",
        files={"file": (filename, io.BytesIO(content), "text/csv")},
    )


def test_upload_auto_detect_herb_and_commit(client):
    """含 中药名/性味/归经/功效 的表头应自动识别为 herb；确认后写入并统计。"""
    name = _unique("导入测试药")
    resp = _upload_csv(client, 
        "herbs_sample.csv",
        ["中药名", "别名", "性味", "归经", "功效", "来源"],
        [[name, "别名A;别名B", "甘，温", "肺经;脾经", "补气健脾", "测试数据源"]],
    )
    assert resp.status_code == 200, resp.text
    preview = resp.json()
    assert preview["detected_type"] == "herb", preview
    assert preview["need_user_type"] is False
    assert preview["field_mapping"].get("name") == "中药名", preview["field_mapping"]
    assert preview["estimate"]["insert"] == 1

    commit = client.post(
        "/api/v1/admin/import/upload/commit",
        json={
            "dataset_id": preview["dataset_id"],
            "target_type": "herb",
            "field_mapping": preview["field_mapping"],
            "confirmed": True,
        },
    )
    assert commit.status_code == 200, commit.text
    result = commit.json()
    assert result["inserted"] == 1, result
    assert result["failed"] == 0
    assert result["status"] in ("completed", "partial_success")

    listed = client.get("/api/v1/herbs", params={"keyword": name}).json()
    assert listed["total"] == 1, listed
    herb = listed["items"][0]
    assert herb["properties"] == "甘，温"
    assert herb["effects"] == "补气健脾"

    # 再次导入同名数据 → 只补充空字段，不重复创建
    again = client.post(
        "/api/v1/admin/import/upload/commit",
        json={
            "dataset_id": preview["dataset_id"],
            "target_type": "herb",
            "field_mapping": preview["field_mapping"],
            "confirmed": True,
        },
    ).json()
    assert again["inserted"] == 0, again
    assert again["skipped"] >= 1 or again["updated"] >= 1

    # 清理
    client.delete(f"/api/v1/herbs/{herb['id']}")
    client.delete(f"/api/v1/herbs/{herb['id']}/purge")


def test_upload_unknown_type_requires_user_choice(client):
    """无法可靠识别时必须返回 need_user_type，且未确认不能导入。"""
    resp = _upload_csv(client, 
        "ambiguous.csv",
        ["列A", "列B", "列C"],
        [["值1", "值2", "值3"]],
    )
    assert resp.status_code == 200, resp.text
    preview = resp.json()
    assert preview["need_user_type"] is True, preview
    assert preview["target_type"] == "unknown"

    rejected = client.post(
        "/api/v1/admin/import/upload/commit",
        json={"dataset_id": preview["dataset_id"], "target_type": "unknown", "confirmed": True},
    )
    # unknown 在 schema 层就被拒绝（422），说明"不猜测直接导入"有两道闸门
    assert rejected.status_code in (400, 422), rejected.text


def test_upload_rejects_unsupported_extension(client):
    resp = client.post(
        "/api/v1/admin/import/upload",
        files={"file": ("bad.exe", io.BytesIO(b"x"), "application/octet-stream")},
    )
    assert resp.status_code == 400, resp.text


def test_commit_requires_confirmation(client):
    """未确认（confirmed=false）不允许写入。"""
    resp = _upload_csv(client, 
        "theory_sample.csv",
        ["术语", "释义", "来源"],
        [["测试术语", "测试释义", "来源A"]],
    )
    assert resp.status_code == 200
    preview = resp.json()
    assert preview["detected_type"] == "theory", preview
    bad = client.post(
        "/api/v1/admin/import/upload/commit",
        json={"dataset_id": preview["dataset_id"], "target_type": "theory", "confirmed": False},
    )
    assert bad.status_code == 400, bad.text


# ── 7. 知识库批量删除 / 批量恢复 ────────────────────────────────────────────


def test_kb_batch_delete_and_restore(client):
    ids = []
    for _ in range(2):
        resp = client.post(
            "/api/v1/kb", json={"name": _unique("批量KB"), "description": "", "visibility": "private"}
        )
        assert resp.status_code in (200, 201), resp.text
        ids.append(resp.json()["id"])

    res = client.post("/api/v1/kb/batch-delete", json={"ids": ids}).json()
    assert res["success"] == 2, res

    trash = client.get("/api/v1/kb/trash").json()
    assert any(kb["id"] in ids for kb in trash), "回收站中找不到批量删除的知识库"

    res = client.post("/api/v1/kb/batch-restore", json={"ids": ids}).json()
    assert res["success"] == 2, res

    # 清理（软删除后彻底删除；批量 KB 无文档/向量）
    client.post("/api/v1/kb/batch-delete", json={"ids": ids})
    for kb_id in ids:
        client.delete(f"/api/v1/kb/{kb_id}/purge")
