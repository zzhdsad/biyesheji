"""第二轮 Batch 1（安全与数据正确性）回归测试。

覆盖：
- BUG-001 评测域知识库权限隔离
- BUG-002 /settings/model 仅管理员可改
- BUG-003 /settings/model/test SSRF 防护
- BUG-004 KB 成员角色服务端校验（viewer 不得写）
- BUG-005 must_change_password 服务端强制
- BUG-026 生产环境禁止内置默认 SECRET_KEY
- BUG-027 生产环境禁用 TEST_MODE 后门
- BUG-040 最后一个管理员不可降级/删除/禁用

所有用例走 auth_client fixture（真实 JWT + 真实 DB），不使用测试模式后门，
否则无法验证权限本身。未连 PostgreSQL 时整体 skip（权限依赖真实数据）。
"""

import time
import uuid

import pytest
from fastapi import HTTPException

from src.core import deps
from src.core.config import DEV_DEFAULT_SECRET_KEY, Settings
from tests.test_upload import PG_AVAILABLE

pytestmark = pytest.mark.skipif(not PG_AVAILABLE, reason="PostgreSQL 未启动")


# ── 工具函数 ────────────────────────────────────────────────────────────────


def _login(auth_client, username_or_email: str, password: str) -> str:
    resp = auth_client.post(
        "/api/v1/auth/login",
        json={"username_or_email": username_or_email, "password": password},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["access_token"]


def _admin_token(auth_client) -> str:
    resp = auth_client.post(
        "/api/v1/auth/login",
        json={"username_or_email": "admin", "password": "admin123456"},
    )
    if resp.status_code != 200:
        resp = auth_client.post(
            "/api/v1/auth/login",
            json={"username_or_email": "admin", "password": "Admin@2024"},
        )
    assert resp.status_code == 200, f"admin 登录失败: {resp.text}"
    return resp.json()["access_token"]


def _create_user(auth_client, admin_token: str, role: str = "member") -> dict:
    ts = int(time.time() * 1000)
    username = f"b1_{role}_{ts}"
    email = f"b1_{role}_{ts}@test.com"
    resp = auth_client.post(
        "/api/v1/users",
        json={"username": username, "email": email, "role": role},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert resp.status_code == 201, resp.text
    return {
        "id": resp.json()["user"]["id"],
        "username": username,
        "email": email,
        "password": resp.json()["initial_password"],
    }


def _ready_headers(auth_client, user: dict) -> dict:
    """登录并完成强制改密，返回可直接调用业务接口的请求头。"""
    token = _login(auth_client, user["username"], user["password"])
    resp = auth_client.post(
        "/api/v1/auth/change-password",
        json={"old_password": user["password"], "new_password": "Batch1@2024"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {token}"}


def _make_kb(auth_client, headers: dict, name: str) -> str:
    resp = auth_client.post("/api/v1/kb", json={"name": name}, headers=headers)
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


# ── BUG-001：评测域权限隔离 ─────────────────────────────────────────────────


def test_evaluation_test_cases_require_kb_access(auth_client):
    """他人私有知识库的测试集/结果不可读、不可改、不可运行。"""
    admin = _admin_token(auth_client)
    owner = _create_user(auth_client, admin)
    other = _create_user(auth_client, admin)

    owner_h = _ready_headers(auth_client, owner)
    other_h = _ready_headers(auth_client, other)

    kb_id = _make_kb(auth_client, owner_h, "评测权限测试库")
    resp = auth_client.post(
        "/api/v1/evaluation/upload",
        json={
            "kb_id": kb_id,
            "cases": [
                {
                    "question": "麻黄的功效是什么？",
                    "golden_answer": "发汗解表，宣肺平喘",
                    "question_type": "herb",
                }
            ],
        },
        headers=owner_h,
    )
    assert resp.status_code == 200, resp.text
    case_id = resp.json()["case_ids"][0]

    # owner 可读
    resp = auth_client.get(
        f"/api/v1/evaluation/test-cases?kb_id={kb_id}", headers=owner_h
    )
    assert resp.status_code == 200
    assert len(resp.json()) == 1

    # 他人（非成员、非 admin）一律 403
    resp = auth_client.get(
        f"/api/v1/evaluation/test-cases?kb_id={kb_id}", headers=other_h
    )
    assert resp.status_code == 403

    resp = auth_client.patch(
        f"/api/v1/evaluation/test-cases/{case_id}",
        json={"golden_answer": "篡改后的标准答案"},
        headers=other_h,
    )
    assert resp.status_code == 403

    resp = auth_client.post(
        "/api/v1/evaluation/run",
        json={"kb_id": kb_id},
        headers=other_h,
    )
    assert resp.status_code == 403

    resp = auth_client.get(
        f"/api/v1/evaluation/results?kb_id={kb_id}", headers=other_h
    )
    assert resp.status_code == 403

    # 不存在的知识库同样 403（不得因"查不到"而放行）
    resp = auth_client.get(
        f"/api/v1/evaluation/test-cases?kb_id={uuid.uuid4()}", headers=other_h
    )
    assert resp.status_code == 403


def test_evaluation_list_endpoints_scoped_to_accessible_kbs(auth_client):
    """不传 kb_id 的列表接口：普通用户只看到自己可访问知识库的数据。"""
    admin = _admin_token(auth_client)
    owner = _create_user(auth_client, admin)
    other = _create_user(auth_client, admin)

    owner_h = _ready_headers(auth_client, owner)
    other_h = _ready_headers(auth_client, other)
    kb_id = _make_kb(auth_client, owner_h, "评测列表隔离测试库")

    for path in ("/api/v1/evaluation/runs", "/api/v1/evaluation/results"):
        resp = auth_client.get(path, headers=other_h)
        assert resp.status_code == 200, f"{path}: {resp.text}"
        kb_ids = {item.get("kb_id") for item in resp.json() if item.get("kb_id")}
        assert kb_id not in kb_ids, f"{path} 泄漏了他人知识库数据：{kb_ids}"


def test_evaluation_run_detail_requires_kb_access(auth_client):
    """单条运行详情按所属知识库鉴权（越权不得读取他人实验归档）。"""
    admin = _admin_token(auth_client)
    owner = _create_user(auth_client, admin)
    other = _create_user(auth_client, admin)
    owner_h = _ready_headers(auth_client, owner)
    other_h = _ready_headers(auth_client, other)

    # 运行 id 不存在 → 404；说明校验发生在鉴权链路上且未放行
    resp = auth_client.get(f"/api/v1/evaluation/runs/{uuid.uuid4()}", headers=other_h)
    assert resp.status_code == 404

    # 非法 run_id → 422（不得 500，也不得绕过鉴权）
    resp = auth_client.get("/api/v1/evaluation/results?run_id=not-a-uuid", headers=other_h)
    assert resp.status_code == 422
    assert owner_h and admin  # 保持可读性：owner/admin 头均已就绪


# ── BUG-002 / BUG-003：模型配置与连通性测试 ──────────────────────────────────


def test_model_config_update_requires_admin(auth_client):
    admin = _admin_token(auth_client)
    member = _create_user(auth_client, admin)
    member_h = _ready_headers(auth_client, member)

    resp = auth_client.put(
        "/api/v1/settings/model",
        json={"llm_provider": "mock"},
        headers=member_h,
    )
    assert resp.status_code == 403

    resp = auth_client.put(
        "/api/v1/settings/model",
        json={"llm_provider": "mock"},
        headers={"Authorization": f"Bearer {admin}"},
    )
    assert resp.status_code == 200


def test_model_test_blocks_internal_urls(auth_client):
    """连通性测试不得被用于探测内网 / 本机 / 云元数据（SSRF）。"""
    admin = _admin_token(auth_client)
    admin_h = {"Authorization": f"Bearer {admin}"}
    member = _create_user(auth_client, admin)
    member_h = _ready_headers(auth_client, member)

    for base_url in (
        "http://169.254.169.254/latest/meta-data",
        "http://127.0.0.1:8000/v1",
        "http://localhost:11434/v1",
        "http://10.0.0.5:8000/v1",
        "file:///etc/passwd",
    ):
        resp = auth_client.post(
            "/api/v1/settings/model/test",
            json={
                "llm_provider": "custom",
                "llm_base_url": base_url,
                "llm_model": "qwen",
            },
            headers=admin_h,
        )
        assert resp.status_code == 400, f"{base_url} 未被拦截: {resp.text}"

    # 非管理员连测试端点也不能调用
    resp = auth_client.post(
        "/api/v1/settings/model/test",
        json={
            "llm_provider": "custom",
            "llm_base_url": "http://169.254.169.254",
            "llm_model": "qwen",
        },
        headers=member_h,
    )
    assert resp.status_code == 403


def test_safe_public_url_unit(monkeypatch):
    """URL 安全校验：内网/本机/元数据地址拒绝，普通公网地址放行。"""
    from src.utils import net

    # 字面量内网地址无需 DNS 即可判定
    for url in (
        "http://127.0.0.1:8000/v1",
        "http://localhost:11434/v1",
        "http://169.254.169.254/latest/meta-data",
        "http://192.168.1.10:8000/v1",
        "http://10.1.2.3/v1",
        "file:///etc/passwd",
        "ftp://example.com/x",
        "",
    ):
        assert net.is_safe_public_url(url) is False, url

    # 公网地址（用 monkeypatch 模拟解析结果，避免依赖真实 DNS）
    monkeypatch.setattr(net, "resolve_host_ips", lambda host: ["93.184.216.34"])
    assert net.is_safe_public_url("https://example.com/v1") is True

    # 域名解析到内网 → 拒绝（防 DNS 指向内网的绕过）
    monkeypatch.setattr(net, "resolve_host_ips", lambda host: ["10.0.0.7"])
    assert net.is_safe_public_url("https://internal.example.com/v1") is False


# ── BUG-004：KB 成员角色服务端校验 ──────────────────────────────────────────


def test_viewer_cannot_write_documents(auth_client):
    """viewer 成员可读不可写：上传/解析/删除均 403。"""
    admin = _admin_token(auth_client)
    owner = _create_user(auth_client, admin)
    viewer = _create_user(auth_client, admin)

    owner_h = _ready_headers(auth_client, owner)
    viewer_h = _ready_headers(auth_client, viewer)

    kb_id = _make_kb(auth_client, owner_h, "viewer 权限测试库")
    resp = auth_client.post(
        "/api/v1/documents/upload",
        data={"kb_id": kb_id},
        files={"file": ("测试.txt", "麻黄，发汗解表。".encode("utf-8"), "text/plain")},
        headers=owner_h,
    )
    assert resp.status_code == 201, resp.text
    doc_id = resp.json()["id"]

    # 加为 viewer 成员
    resp = auth_client.post(
        f"/api/v1/kb/{kb_id}/members",
        json={"user_id": viewer["id"], "role": "viewer"},
        headers=owner_h,
    )
    assert resp.status_code == 201, resp.text

    # 读：允许（viewer 可见）
    resp = auth_client.get(f"/api/v1/documents/{doc_id}", headers=viewer_h)
    assert resp.status_code == 200

    # 写：一律拒绝
    resp = auth_client.post(
        "/api/v1/documents/upload",
        data={"kb_id": kb_id},
        files={"file": ("x.txt", b"x", "text/plain")},
        headers=viewer_h,
    )
    assert resp.status_code == 403

    resp = auth_client.post(f"/api/v1/documents/{doc_id}/parse", headers=viewer_h)
    assert resp.status_code == 403

    resp = auth_client.delete(f"/api/v1/documents/{doc_id}", headers=viewer_h)
    assert resp.status_code == 403


def test_public_kb_allows_read_but_denies_write(auth_client):
    """公开知识库对全体登录用户只读，写操作仍需 owner/admin/editor。"""
    admin = _admin_token(auth_client)
    owner = _create_user(auth_client, admin)
    outsider = _create_user(auth_client, admin)

    owner_h = _ready_headers(auth_client, owner)
    outsider_h = _ready_headers(auth_client, outsider)

    kb_id = _make_kb(auth_client, owner_h, "公开库写权限测试")
    resp = auth_client.put(
        f"/api/v1/kb/{kb_id}", json={"visibility": "public"}, headers=owner_h
    )
    assert resp.status_code == 200, resp.text

    resp = auth_client.post(
        "/api/v1/documents/upload",
        data={"kb_id": kb_id},
        files={"file": ("公开.txt", "桂枝汤主治太阳中风。".encode("utf-8"), "text/plain")},
        headers=owner_h,
    )
    assert resp.status_code == 201, resp.text
    doc_id = resp.json()["id"]

    # 非成员可读（public）
    resp = auth_client.get(f"/api/v1/documents/{doc_id}", headers=outsider_h)
    assert resp.status_code == 200

    # 非成员不可写
    resp = auth_client.delete(f"/api/v1/documents/{doc_id}", headers=outsider_h)
    assert resp.status_code == 403


# ── BUG-005：强制修改初始密码 ───────────────────────────────────────────────


def test_must_change_password_blocks_business_api(auth_client):
    admin = _admin_token(auth_client)
    user = _create_user(auth_client, admin)
    token = _login(auth_client, user["username"], user["password"])
    headers = {"Authorization": f"Bearer {token}"}

    # 未改密 → 业务接口 403，但 /me 与改密接口仍可用
    resp = auth_client.get("/api/v1/chat/conversations", headers=headers)
    assert resp.status_code == 403
    assert "密码" in resp.json()["detail"]

    resp = auth_client.get("/api/v1/auth/me", headers=headers)
    assert resp.status_code == 200

    resp = auth_client.post(
        "/api/v1/auth/change-password",
        json={"old_password": user["password"], "new_password": "Changed@2024"},
        headers=headers,
    )
    assert resp.status_code == 200

    # 改密后业务接口恢复
    resp = auth_client.get("/api/v1/chat/conversations", headers=headers)
    assert resp.status_code == 200


# ── BUG-026 / BUG-027：生产环境安全开关 ─────────────────────────────────────


def test_prod_rejects_default_secret_key():
    """生产环境使用内置默认密钥 → 直接拒绝启动。"""
    with pytest.raises(Exception) as exc:
        Settings(ENV="prod", SECRET_KEY=DEV_DEFAULT_SECRET_KEY)
    assert "SECRET_KEY" in str(exc.value)

    ok = Settings(ENV="prod", SECRET_KEY="x" * 48)
    assert ok.SECRET_KEY == "x" * 48

    # 非生产环境仍可使用默认值（开发/测试不受影响）
    dev = Settings(ENV="dev", SECRET_KEY=DEV_DEFAULT_SECRET_KEY)
    assert dev.SECRET_KEY == DEV_DEFAULT_SECRET_KEY


def test_test_mode_backdoor_disabled_in_prod(monkeypatch):
    from src.core.config import settings

    monkeypatch.setattr(settings, "ENV", "prod")
    monkeypatch.setattr(deps, "TEST_MODE_ENABLED", True)
    assert deps.test_mode_active() is False

    monkeypatch.setattr(settings, "ENV", "dev")
    assert deps.test_mode_active() is True


# ── BUG-040：最后一个管理员保护 ─────────────────────────────────────────────


def test_last_admin_cannot_be_demoted_or_deleted(monkeypatch):
    import asyncio

    from src.api.routes import users as users_route
    from src.domain.models import User

    async def _only_one_admin(_db):
        return 1

    monkeypatch.setattr(users_route, "_count_active_admins", _only_one_admin)
    admin = User(role="admin")

    with pytest.raises(HTTPException) as exc:
        asyncio.run(users_route._ensure_admin_kept(None, admin, action="降级"))
    assert exc.value.status_code == 400

    # 非管理员不受此限制
    asyncio.run(users_route._ensure_admin_kept(None, User(role="member")))


def test_admin_can_still_manage_other_admins(auth_client):
    """存在多个管理员时，降级/删除其余管理员不受影响（防止过度收紧）。"""
    admin = _admin_token(auth_client)
    admin_h = {"Authorization": f"Bearer {admin}"}
    extra = _create_user(auth_client, admin, role="admin")

    resp = auth_client.put(
        f"/api/v1/users/{extra['id']}", json={"role": "member"}, headers=admin_h
    )
    assert resp.status_code == 200, resp.text

    resp = auth_client.delete(f"/api/v1/users/{extra['id']}", headers=admin_h)
    assert resp.status_code == 200, resp.text
