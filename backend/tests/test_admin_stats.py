"""系统仪表盘统计接口测试（PRD §5.2：GET /admin/stats）。

覆盖：
- admin 可访问，返回 4 个统计字段且类型正确
- 统计反映真实数据（上传文档 + 提问后计数增长，平均延迟非负）
- 非 admin（member）→ 403
"""

import pytest

import src.core.deps as deps_mod
from tests.test_chat import _ask, _upload_doc
from tests.test_parse import _make_kb
from tests.test_upload import PG_AVAILABLE


@pytest.mark.skipif(not PG_AVAILABLE, reason="PostgreSQL 未启动")
def test_admin_stats_returns_four_fields(client):
    """admin 访问 /admin/stats → 200，含 total_docs/total_kbs/total_qa/avg_latency_ms。"""
    resp = client.get("/api/v1/admin/stats")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert set(body.keys()) == {"total_docs", "total_kbs", "total_qa", "avg_latency_ms"}
    assert isinstance(body["total_docs"], int)
    assert isinstance(body["total_kbs"], int)
    assert isinstance(body["total_qa"], int)
    assert isinstance(body["avg_latency_ms"], int)


@pytest.mark.skipif(not PG_AVAILABLE, reason="PostgreSQL 未启动")
def test_admin_stats_reflect_real_data(client):
    """上传 1 个文档 + 提问 1 轮后：计数增长，平均延迟为非负整数。"""
    before = client.get("/api/v1/admin/stats").json()

    kb_id = _make_kb(client)
    _upload_doc(client, kb_id)
    _ask(client, "公司实行什么工时制度？", kb_id)

    after = client.get("/api/v1/admin/stats").json()
    assert after["total_docs"] >= before["total_docs"] + 1
    assert after["total_kbs"] >= before["total_kbs"] + 1
    assert after["total_qa"] >= before["total_qa"] + 1
    assert after["avg_latency_ms"] >= 0


@pytest.mark.skipif(not PG_AVAILABLE, reason="PostgreSQL 未启动")
def test_admin_stats_forbidden_for_member(client, monkeypatch):
    """非 admin 角色访问 → 403（TEST_MODE 下把 TEST_USER 临时降为 member）。"""
    monkeypatch.setattr(deps_mod.TEST_USER, "role", "member")
    resp = client.get("/api/v1/admin/stats")
    assert resp.status_code == 403
    assert "管理员" in resp.json()["message"]
