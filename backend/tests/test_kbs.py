"""知识库管理 API 测试（创建 / 列表 / 编辑 / 删除）。

覆盖：
- POST /kb 创建知识库
- GET  /kb 列表（含新建项）
- PUT  /kb/{id} 编辑（全量 / partial）
- PUT  /kb/{id} 404 不存在
- PUT  /kb/{id} 403 越权（非 owner 非 admin）
- PUT  /kb/{id} 422 校验失败（空 name / 非法 visibility）
- DELETE /kb/{id} 删除
- DELETE /kb/{id} 404 不存在
"""

import uuid

import pytest

import src.core.deps as deps_mod
from src.domain.models import User
from tests.test_parse import _make_kb
from tests.test_upload import PG_AVAILABLE


@pytest.mark.skipif(not PG_AVAILABLE, reason="PostgreSQL 未启动")
def test_create_and_list_kb(client):
    """创建后列表中可见，且返回字段齐全。"""
    resp = client.post("/api/v1/kb", json={"name": "测试库 A", "description": "desc", "visibility": "public"})
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["name"] == "测试库 A"
    assert body["description"] == "desc"
    assert body["visibility"] == "public"
    assert body["id"]

    listing = client.get("/api/v1/kb").json()
    ids = [it["id"] for it in listing]
    assert body["id"] in ids


@pytest.mark.skipif(not PG_AVAILABLE, reason="PostgreSQL 未启动")
def test_update_kb_full_fields(client):
    """PUT 全量更新 name/description/visibility。"""
    kb_id = _make_kb(client)
    resp = client.put(
        f"/api/v1/kb/{kb_id}",
        json={"name": "改名后", "description": "新描述", "visibility": "private"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["id"] == kb_id
    assert body["name"] == "改名后"
    assert body["description"] == "新描述"
    assert body["visibility"] == "private"


@pytest.mark.skipif(not PG_AVAILABLE, reason="PostgreSQL 未启动")
def test_update_kb_partial_preserves_other_fields(client):
    """partial update：仅传 name，description/visibility 保持原值。"""
    create = client.post(
        "/api/v1/kb",
        json={"name": "原名", "description": "保留描述", "visibility": "public"},
    ).json()
    kb_id = create["id"]

    resp = client.put(f"/api/v1/kb/{kb_id}", json={"name": "新名"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["name"] == "新名"
    # 未传入的字段保持原值
    assert body["description"] == "保留描述"
    assert body["visibility"] == "public"


@pytest.mark.skipif(not PG_AVAILABLE, reason="PostgreSQL 未启动")
def test_update_kb_404_for_missing(client):
    """PUT 不存在的 kb_id → 404。"""
    fake = str(uuid.uuid4())
    resp = client.put(f"/api/v1/kb/{fake}", json={"name": "x"})
    assert resp.status_code == 404


@pytest.mark.skipif(not PG_AVAILABLE, reason="PostgreSQL 未启动")
def test_update_kb_403_for_non_owner_non_admin(client, monkeypatch):
    """非 owner 且非 admin 用户编辑他人知识库 → 403。

    TEST_MODE 下默认请求用户为 admin（可编辑任意库）。这里临时把 TEST_USER
    替换为一个 member 角色、且 id 不同于库 owner 的用户，模拟越权场景。
    """
    kb_id = _make_kb(client)  # 由默认 admin（TEST_USER）创建

    other = User(
        id=uuid.uuid4(),
        email="other@example.com",
        username="other",
        hashed_password="",
        role="member",
    )
    monkeypatch.setattr(deps_mod, "TEST_USER", other)

    resp = client.put(f"/api/v1/kb/{kb_id}", json={"name": "hacked"})
    assert resp.status_code == 403


@pytest.mark.skipif(not PG_AVAILABLE, reason="PostgreSQL 未启动")
def test_update_kb_rejects_empty_name(client):
    """name 为空字符串 → 422（min_length=1）。"""
    kb_id = _make_kb(client)
    resp = client.put(f"/api/v1/kb/{kb_id}", json={"name": ""})
    assert resp.status_code == 422


def test_update_kb_rejects_invalid_visibility(client):
    """visibility 非法值 → 422（无需数据库）。"""
    resp = client.put(f"/api/v1/kb/{uuid.uuid4()}", json={"visibility": "secret"})
    assert resp.status_code == 422


@pytest.mark.skipif(not PG_AVAILABLE, reason="PostgreSQL 未启动")
def test_delete_kb_success(client):
    """删除成功后列表中不再出现。"""
    kb_id = _make_kb(client)
    resp = client.delete(f"/api/v1/kb/{kb_id}")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["id"] == kb_id
    assert body["deleted"] is True
    # BUSINESS_RULES §3：删除移入回收站，返回保留天数提示
    assert "message" in body

    listing = client.get("/api/v1/kb").json()
    ids = [it["id"] for it in listing]
    assert kb_id not in ids


@pytest.mark.skipif(not PG_AVAILABLE, reason="PostgreSQL 未启动")
def test_delete_kb_404_for_missing(client):
    """DELETE 不存在的 kb_id → 404。"""
    resp = client.delete(f"/api/v1/kb/{uuid.uuid4()}")
    assert resp.status_code == 404
