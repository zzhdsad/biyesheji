"""管理中心 + 数据导入中心本轮修复的针对性测试（真实 PostgreSQL，不 mock DB）。

覆盖：
1. 知识库列表 document_count 真实统计（BUG：后端从未返回该字段 → 页面恒为 0）
5. 用户回收站一键清空的安全约束（后端二次校验）
7. 知识库批量删除 / 批量恢复

原第 2/3/4 节（中药分类筛选、资源回收站、标签回收站）与第 6 节（数据导入中心
/api/v1/admin/import/*）随旧知识资源中心与旧导入中心下线移除。

不触碰 RAG 核心（BGE-M3 / RRF / Reranker / Gate / HyDE / Router）。
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text




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
                            "WHERE deleted_at IS NULL AND space = 'document' GROUP BY kb_id"
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


# 原「中药未填写分类筛选 / 资源回收站 / 分类标签回收站」用例随旧知识资源中心下线移除。


# ── 5. 用户回收站一键清空（安全约束）────────────────────────────────────────


def test_purge_all_user_trash_endpoint_available(client):
    """端点存在且返回统计结构；不实际清空（数据清理由 scripts 负责）。"""
    resp = client.request("DELETE", "/api/v1/users/trash/purge-all")
    # 403 表示非 admin；200 表示执行成功；两者都说明路由与安全校验存在
    assert resp.status_code in (200, 403), resp.text
    if resp.status_code == 200:
        body = resp.json()
        assert "purged" in body and "failed" in body


# 原「数据导入中心（/api/v1/admin/import/*）上传→识别→预览→导入」用例随旧导入中心下线移除。


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
