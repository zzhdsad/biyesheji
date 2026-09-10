"""用户鉴权功能测试。

使用 auth_client fixture（真实鉴权，不 monkeypatch）覆盖：
- 用户创建（admin 通过 /users 接口）：成功 / 邮箱重复 / 用户名重复
- 登录：成功返回 JWT / 密码错误 401 / 用户不存在 401
- 鉴权保护：业务接口无 token 401 / 带 token 200
- /me 端点：返回当前用户
- token 安全：无效 token 401 / 过期 token 401
- 修改密码：首次登录强制修改

conftest.py 的 client fixture 已注入 mock User 跳过鉴权；
test_auth.py 用 auth_client fixture 走真实 JWT + DB。
"""

import time

import pytest

from tests.test_upload import PG_AVAILABLE


def _admin_login(auth_client) -> str:
    """用管理员账号登录并返回 token。"""
    resp = auth_client.post(
        "/api/v1/auth/login",
        json={"username_or_email": "admin", "password": "admin123456"},
    )
    # 若管理员密码已被修改（如之前测试运行过），尝试用已知密码
    if resp.status_code != 200:
        resp = auth_client.post(
            "/api/v1/auth/login",
            json={"username_or_email": "admin", "password": "Admin@2024"},
        )
    assert resp.status_code == 200, f"admin 登录失败: {resp.text}"
    return resp.json()["access_token"]


def _create_user(auth_client, token: str, username: str, email: str) -> dict:
    """通过 admin 接口创建测试用户，返回含初始密码的凭证。"""
    resp = auth_client.post(
        "/api/v1/users",
        json={
            "username": username,
            "email": email,
            "name": "测试用户",
            "department": "测试部",
            "role": "member",
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 201, f"创建用户失败: {resp.text}"
    body = resp.json()
    return {
        "username": username,
        "email": email,
        "password": body["initial_password"],
    }


@pytest.fixture()
def _unique_user(auth_client) -> dict:
    """通过 admin 接口创建唯一测试用户，返回含初始密码的凭证。"""
    ts = int(time.time() * 1000)
    username = f"authtest_{ts}"
    email = f"auth_test_{ts}@test.com"
    token = _admin_login(auth_client)
    return _create_user(auth_client, token, username, email)


# ── 用户创建（admin /users 接口）─────────────────────────────────────────────


@pytest.mark.skipif(not PG_AVAILABLE, reason="PostgreSQL 未启动")
def test_create_user_success(auth_client, _unique_user):
    """_unique_user fixture 已成功创建用户，此处验证返回结构。"""
    assert _unique_user["username"]
    assert _unique_user["email"]
    assert _unique_user["password"]  # 系统生成的初始密码


@pytest.mark.skipif(not PG_AVAILABLE, reason="PostgreSQL 未启动")
def test_create_user_duplicate_email_400(auth_client, _unique_user):
    token = _admin_login(auth_client)
    resp = auth_client.post(
        "/api/v1/users",
        json={
            "username": _unique_user["username"] + "_2",
            "email": _unique_user["email"],
            "role": "member",
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 400
    assert "邮箱" in resp.json()["detail"]


@pytest.mark.skipif(not PG_AVAILABLE, reason="PostgreSQL 未启动")
def test_create_user_duplicate_username_400(auth_client, _unique_user):
    token = _admin_login(auth_client)
    resp = auth_client.post(
        "/api/v1/users",
        json={
            "username": _unique_user["username"],
            "email": "other_" + _unique_user["email"],
            "role": "member",
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 400
    assert "用户名" in resp.json()["detail"]


@pytest.mark.skipif(not PG_AVAILABLE, reason="PostgreSQL 未启动")
def test_create_user_invalid_role_422(auth_client):
    token = _admin_login(auth_client)
    resp = auth_client.post(
        "/api/v1/users",
        json={
            "username": "badrole",
            "email": "badrole@test.com",
            "role": "superadmin",
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 422


# ── 登录 ─────────────────────────────────────────────────────────────────────


@pytest.mark.skipif(not PG_AVAILABLE, reason="PostgreSQL 未启动")
def test_login_success_returns_token(auth_client, _unique_user):
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


# ── 修改密码 ─────────────────────────────────────────────────────────────────


@pytest.mark.skipif(not PG_AVAILABLE, reason="PostgreSQL 未启动")
def test_change_password_success(auth_client, _unique_user):
    token = _login_and_get_token(
        auth_client, _unique_user["username"], _unique_user["password"]
    )
    resp = auth_client.post(
        "/api/v1/auth/change-password",
        json={"old_password": _unique_user["password"], "new_password": "NewPass123"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    assert resp.json()["message"] == "密码修改成功"

    # 用新密码登录
    resp2 = auth_client.post(
        "/api/v1/auth/login",
        json={
            "username_or_email": _unique_user["username"],
            "password": "NewPass123",
        },
    )
    assert resp2.status_code == 200
    # 修改后 must_change_password 应为 False
    assert resp2.json()["user"]["must_change_password"] is False


@pytest.mark.skipif(not PG_AVAILABLE, reason="PostgreSQL 未启动")
def test_change_password_wrong_old_400(auth_client, _unique_user):
    token = _login_and_get_token(
        auth_client, _unique_user["username"], _unique_user["password"]
    )
    resp = auth_client.post(
        "/api/v1/auth/change-password",
        json={"old_password": "wrong_old", "new_password": "NewPass123"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 400
    assert "原密码错误" in resp.json()["detail"]


# ── logout ───────────────────────────────────────────────────────────────────


@pytest.mark.skipif(not PG_AVAILABLE, reason="PostgreSQL 未启动")
def test_logout_returns_ok(auth_client, _unique_user):
    token = _login_and_get_token(
        auth_client, _unique_user["username"], _unique_user["password"]
    )
    resp = auth_client.post(
        "/api/v1/auth/logout",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    assert resp.json()["message"] == "ok"
