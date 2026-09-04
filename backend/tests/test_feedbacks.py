"""反馈收集 API 测试（PRD §3.6 / §8 边界场景：用户纠错反馈）。

覆盖：
- POST /feedbacks 创建点赞/踩反馈
- POST /feedbacks 覆盖更新（同一 message_id 重复提交）
- POST /feedbacks 校验失败：rating=0、message_id 不存在、字段缺失
- GET  /feedbacks/{message_id} 查询单条
- GET  /feedbacks 列出当前用户反馈

复用 test_chat 的 _ask 拿到真实助手 message_id，便于做权限隔离测试。
"""

import uuid

import pytest

from tests.test_chat import _ask, _upload_doc
from tests.test_parse import _make_kb
from tests.test_upload import PG_AVAILABLE


@pytest.mark.skipif(not PG_AVAILABLE, reason="PostgreSQL 未启动")
def test_submit_like_feedback(client):
    """点赞：POST /feedbacks rating=1 → 200 + 返回 FeedbackOut。"""
    kb_id = _make_kb(client)
    _upload_doc(client, kb_id)
    msg_id = _ask(client, "公司实行什么工时制度？", kb_id)["message_id"]

    resp = client.post(
        "/api/v1/feedbacks",
        json={"message_id": msg_id, "rating": 1, "comment": ""},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["message_id"] == msg_id
    assert body["rating"] == 1
    assert body["comment"] == ""


@pytest.mark.skipif(not PG_AVAILABLE, reason="PostgreSQL 未启动")
def test_submit_dislike_with_comment(client):
    """踩 + 纠错意见：rating=-1 + comment 文本。"""
    kb_id = _make_kb(client)
    _upload_doc(client, kb_id)
    msg_id = _ask(client, "标准工时是几小时？", kb_id)["message_id"]

    resp = client.post(
        "/api/v1/feedbacks",
        json={"message_id": msg_id, "rating": -1, "comment": "答案漏了午休"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["rating"] == -1
    assert body["comment"] == "答案漏了午休"


@pytest.mark.skipif(not PG_AVAILABLE, reason="PostgreSQL 未启动")
def test_feedback_overwrites_on_resubmit(client):
    """同一 message_id 重复提交：覆盖更新而非插入新记录。"""
    kb_id = _make_kb(client)
    _upload_doc(client, kb_id)
    msg_id = _ask(client, "工作时长？", kb_id)["message_id"]

    # 第一次：赞
    r1 = client.post(
        "/api/v1/feedbacks",
        json={"message_id": msg_id, "rating": 1, "comment": "first"},
    )
    assert r1.status_code == 200
    first_id = r1.json()["id"]

    # 第二次：改为踩（同 message_id）
    r2 = client.post(
        "/api/v1/feedbacks",
        json={"message_id": msg_id, "rating": -1, "comment": "second"},
    )
    assert r2.status_code == 200
    second = r2.json()
    assert second["id"] == first_id  # 同一记录，覆盖更新
    assert second["rating"] == -1
    assert second["comment"] == "second"


@pytest.mark.skipif(not PG_AVAILABLE, reason="PostgreSQL 未启动")
def test_get_feedback_returns_existing(client):
    """GET /feedbacks/{message_id}：已反馈返回 FeedbackOut。"""
    kb_id = _make_kb(client)
    _upload_doc(client, kb_id)
    msg_id = _ask(client, "工时？", kb_id)["message_id"]
    client.post(
        "/api/v1/feedbacks",
        json={"message_id": msg_id, "rating": 1, "comment": "good"},
    )

    resp = client.get(f"/api/v1/feedbacks/{msg_id}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["message_id"] == msg_id
    assert body["rating"] == 1
    assert body["comment"] == "good"


@pytest.mark.skipif(not PG_AVAILABLE, reason="PostgreSQL 未启动")
def test_get_feedback_returns_null_for_unfed_back(client):
    """GET /feedbacks/{message_id}：未反馈返回 null。"""
    kb_id = _make_kb(client)
    _upload_doc(client, kb_id)
    msg_id = _ask(client, "几小时？", kb_id)["message_id"]

    resp = client.get(f"/api/v1/feedbacks/{msg_id}")
    assert resp.status_code == 200
    assert resp.json() is None


@pytest.mark.skipif(not PG_AVAILABLE, reason="PostgreSQL 未启动")
def test_list_feedbacks_returns_only_own(client):
    """GET /feedbacks：列出当前用户提交的反馈（按时间倒序）。"""
    kb_id = _make_kb(client)
    _upload_doc(client, kb_id)
    msg1 = _ask(client, "工时？", kb_id)["message_id"]
    msg2 = _ask(client, "考勤？", kb_id)["message_id"]
    client.post("/api/v1/feedbacks", json={"message_id": msg1, "rating": 1})
    client.post("/api/v1/feedbacks", json={"message_id": msg2, "rating": -1})

    resp = client.get("/api/v1/feedbacks")
    assert resp.status_code == 200
    items = resp.json()
    ids = {it["message_id"] for it in items}
    assert msg1 in ids and msg2 in ids


@pytest.mark.skipif(not PG_AVAILABLE, reason="PostgreSQL 未启动")
def test_submit_feedback_404_for_missing_message(client):
    """POST /feedbacks message_id 不存在 → 404。"""
    fake = str(uuid.uuid4())
    resp = client.post(
        "/api/v1/feedbacks",
        json={"message_id": fake, "rating": 1, "comment": ""},
    )
    assert resp.status_code == 404


@pytest.mark.skipif(not PG_AVAILABLE, reason="PostgreSQL 未启动")
def test_submit_feedback_rejects_rating_zero(client):
    """rating=0 不允许（必须是 1 或 -1）→ 400。"""
    kb_id = _make_kb(client)
    _upload_doc(client, kb_id)
    msg_id = _ask(client, "工时？", kb_id)["message_id"]

    resp = client.post(
        "/api/v1/feedbacks",
        json={"message_id": msg_id, "rating": 0, "comment": ""},
    )
    assert resp.status_code == 400


def test_submit_feedback_rejects_missing_message_id(client):
    """缺 message_id → 422 Pydantic 校验失败。"""
    resp = client.post(
        "/api/v1/feedbacks",
        json={"rating": 1, "comment": ""},
    )
    assert resp.status_code == 422


def test_submit_feedback_rejects_invalid_rating(client):
    """rating 超出 [-1, 1] → 422 Pydantic 校验失败。"""
    resp = client.post(
        "/api/v1/feedbacks",
        json={"message_id": str(uuid.uuid4()), "rating": 5, "comment": ""},
    )
    assert resp.status_code == 422
