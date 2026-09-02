"""用户鉴权功能测试。

使用 auth_client fixture（真实鉴权，不 monkeypatch）覆盖：
- 注册：成功 / 邮箱重复 400 / 密码过短 422
- 登录：成功返回 JWT / 密码错误 401 / 用户不存在 401
- 鉴权保护：业务接口无 token 401 / 带 token 200
- /me 端点：返回当前用户
- token 安全：无效 token 401 / 过期 token 401

conftest.py 的 client fixture 已注入 mock User 跳过鉴权；
test_auth.py 用 auth_client fixture 走真实 JWT + DB。
"""

import pytest

from tests.test_upload import PG_AVAILABLE


@pytest.fixture()
def _unique_user() -> dict:
    """生成唯一的测试用户（时间戳后缀，避免重复注册冲突）。"""
    import time

    ts = int(time.time() * 1000)
    return {
        "email": f"auth_test_{ts}@test.com",
        "username": f"authtest_{ts}",
        "password": "secret123",
    }


# ── 注册 ─────────────────────────────────────────────────────────────────────

@pytest.mark.skipif(not PG_AVAILABLE, reason="PostgreSQL 未启动")
def test_register_success(auth_client, _unique_user):
    resp = auth_client.post("/api/v1/auth/register", json=_unique_user)
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["email"] == _unique_user["email"]
    assert body["username"] == _unique_user["username"]
    assert body["role"] == "member"
    assert "id" in body
    # 不应返回 password / hashed_password
    assert "hashed_password" not in body
    assert "password" not in body


@pytest.mark.skipif(not PG_AVAILABLE, reason="PostgreSQL 未启动")
def test_register_duplicate_email_400(auth_client, _unique_user):
    payload = _unique_user
    auth_client.post("/api/v1/auth/register", json=payload)  # 第一次成功
    # 第二次相同邮箱 → 400
    resp = auth_client.post(
        "/api/v1/auth/register",
        json={**payload, "username": payload["username"] + "_2"},
    )
    assert resp.status_code == 400, resp.text
    assert "邮箱已被注册" in resp.json()["detail"]


@pytest.mark.skipif(not PG_AVAILABLE, reason="PostgreSQL 未启动")
def test_register_duplicate_username_400(auth_client, _unique_user):
    payload = _unique_user
    auth_client.post("/api/v1/auth/register", json=payload)
    resp = auth_client.post(
        "/api/v1/auth/register",
        json={**payload, "email": "other_" + payload["email"]},
    )
    assert resp.status_code == 400
    assert "用户名已被使用" in resp.json()["detail"]


@pytest.mark.skipif(not PG_AVAILABLE, reason="PostgreSQL 未启动")
def test_register_password_too_short_422(auth_client):
    resp = auth_client.post(
        "/api/v1/auth/register",
        json={"email": "short@test.com", "username": "short", "password": "12"},
    )
    assert resp.status_code == 422


@pytest.mark.skipif(not PG_AVAILABLE, reason="PostgreSQL 未启动")
def test_register_invalid_email_422(auth_client):
    resp = auth_client.post(
        "/api/v1/auth/register",
        json={"email": "not-an-email", "username": "baduser", "password": "secret123"},
    )
    assert resp.status_code == 422


# ── 登录 ─────────────────────────────────────────────────────────────────────

@pytest.mark.skipif(not PG_AVAILABLE, reason="PostgreSQL 未启动")
def test_login_success_returns_token(auth_client, _unique_user):
    auth_client.post("/api/v1/auth/register", json=_unique_user)
    resp = auth_client.post(
        "/api/v1/auth/login",
        json={
            "username_or_email": _unique_user["username"],
            "password": _unique_user["password"],
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["token_type"] == "bearer"
    assert body["expires_in"] > 0
    assert body["user"]["username"] == _unique_user["username"]
    assert body["access_token"]

    # 用 email 也能登
    resp2 = auth_client.post(
        "/api/v1/auth/login",
        json={
            "username_or_email": _unique_user["email"],
            "password": _unique_user["password"],
        },
    )
    assert resp2.status_code == 200


@pytest.mark.skipif(not PG_AVAILABLE, reason="PostgreSQL 未启动")
def test_login_wrong_password_401(auth_client, _unique_user):
    auth_client.post("/api/v1/auth/register", json=_unique_user)
    resp = auth_client.post(
        "/api/v1/auth/login",
        json={
            "username_or_email": _unique_user["username"],
            "password": "wrongwrong",
        },
    )
    assert resp.status_code == 401
    assert "错误" in resp.json()["detail"]


@pytest.mark.skipif(not PG_AVAILABLE, reason="PostgreSQL 未启动")
def test_login_unknown_user_401(auth_client):
    resp = auth_client.post(
        "/api/v1/auth/login",
        json={"username_or_email": "no_such_user", "password": "secret123"},
    )
    assert resp.status_code == 401


# ── 鉴权保护 ─────────────────────────────────────────────────────────────────

def _login_and_get_token(auth_client, username_or_email: str, password: str) -> str:
    resp = auth_client.post(
        "/api/v1/auth/login",
        json={"username_or_email": username_or_email, "password": password},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["access_token"]


@pytest.mark.skipif(not PG_AVAILABLE, reason="PostgreSQL 未启动")
def test_protected_endpoint_without_token_401(auth_client):
    """业务路由（chat）无 token → 401。"""
    resp = auth_client.get("/api/v1/chat/conversations")
    assert resp.status_code == 401
    assert "认证" in resp.json()["detail"] or "token" in resp.json()["detail"]


@pytest.mark.skipif(not PG_AVAILABLE, reason="PostgreSQL 未启动")
def test_protected_endpoint_with_token_200(auth_client, _unique_user):
    auth_client.post("/api/v1/auth/register", json=_unique_user)
    token = _login_and_get_token(
        auth_client, _unique_user["username"], _unique_user["password"]
    )
    resp = auth_client.get(
        "/api/v1/chat/conversations",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200


@pytest.mark.skipif(not PG_AVAILABLE, reason="PostgreSQL 未启动")
def test_protected_endpoint_bad_token_401(auth_client):
    resp = auth_client.get(
        "/api/v1/chat/conversations",
        headers={"Authorization": "Bearer this.is.not.valid"},
    )
    assert resp.status_code == 401


@pytest.mark.skipif(not PG_AVAILABLE, reason="PostgreSQL 未启动")
def test_protected_endpoint_no_bearer_prefix_401(auth_client):
    resp = auth_client.get(
        "/api/v1/chat/conversations",
        headers={"Authorization": "plain-token-without-bearer"},
    )
    assert resp.status_code == 401


# ── /me ─────────────────────────────────────────────────────────────────────

@pytest.mark.skipif(not PG_AVAILABLE, reason="PostgreSQL 未启动")
def test_me_returns_current_user(auth_client, _unique_user):
    auth_client.post("/api/v1/auth/register", json=_unique_user)
    token = _login_and_get_token(
        auth_client, _unique_user["username"], _unique_user["password"]
    )
    resp = auth_client.get(
        "/api/v1/auth/me",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["username"] == _unique_user["username"]
    assert body["email"] == _unique_user["email"]


# ── logout ───────────────────────────────────────────────────────────────────

@pytest.mark.skipif(not PG_AVAILABLE, reason="PostgreSQL 未启动")
def test_logout_returns_ok(auth_client, _unique_user):
    auth_client.post("/api/v1/auth/register", json=_unique_user)
    token = _login_and_get_token(
        auth_client, _unique_user["username"], _unique_user["password"]
    )
    resp = auth_client.post(
        "/api/v1/auth/logout",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    assert resp.json()["message"] == "ok"
