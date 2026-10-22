"""TASK-008 Stage 2/3：knowledge_base_resources 表约束行为 + 挂载 API 测试。

Stage 2 部分：DB 层约束（FK / NOT NULL / UNIQUE / CASCADE），通过 asyncpg
直接写入绕过 ORM 校验，确认约束真正落在数据库层。

Stage 3 部分：Backend Resource 挂载 API（GET / POST / DELETE），
通过 FastAPI TestClient 验证权限、校验链、重复挂载、跨 KB 隔离等行为。
"""

import asyncio
import uuid

import pytest
from sqlalchemy.engine import make_url

from src.core.config import settings

# 复用 smoke 测试的临时库管理工具，保证一致的隔离与清理
from tests.test_alembic_smoke import (
    _pg_available,
    _recreate_smoke_db,
    _drop_smoke_db,
    SMOKE_DB_NAME,
)


def _smoke_db_url() -> str:
    """临时库连接 URL（与 test_alembic_smoke 同构）。"""
    dev_url = make_url(settings.DATABASE_URL)
    return dev_url.set(database=SMOKE_DB_NAME).render_as_string(hide_password=False)


def _alembic_upgrade_head() -> None:
    """对临时库执行 alembic upgrade head（建全表）。"""
    from alembic import command
    from alembic.config import Config
    from pathlib import Path

    BACKEND_DIR = Path(__file__).resolve().parents[1]
    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    command.upgrade(cfg, "head")


def _insert_kbr(
    db_url: str,
    *,
    kb_id: str | None,
    resource_type: str | None,
    resource_id: str | None,
) -> None:
    """直接 asyncpg 插入 knowledge_base_resources，绕过 ORM 校验。"""
    import asyncpg

    url = make_url(db_url)

    async def _run():
        conn = await asyncpg.connect(
            host=url.host,
            port=url.port,
            user=url.username,
            password=url.password,
            database=url.database,
        )
        try:
            if kb_id is None or resource_type is None or resource_id is None:
                # 至少有一列 NULL：构造显式 NULL 列表
                cols = ["id", "knowledge_base_id", "resource_type", "resource_id", "created_at"]
                vals = [
                    f"'{uuid.uuid4()}'",
                    "NULL" if kb_id is None else f"'{kb_id}'",
                    "NULL" if resource_type is None else f"'{resource_type}'",
                    "NULL" if resource_id is None else f"'{resource_id}'",
                    "now()",
                ]
                sql = (
                    f"INSERT INTO knowledge_base_resources "
                    f"({', '.join(cols)}) VALUES ({', '.join(vals)})"
                )
            else:
                sql = (
                    f"INSERT INTO knowledge_base_resources "
                    f"(id, knowledge_base_id, resource_type, resource_id, created_at) "
                    f"VALUES ('{uuid.uuid4()}', '{kb_id}', '{resource_type}', '{resource_id}', now())"
                )
            await conn.execute(sql)
        finally:
            await conn.close()

    asyncio.run(_run())


def _seed_kb_and_user(db_url: str) -> str:
    """建一个 User + KnowledgeBase，返回 KB id（FK 链必需）。"""
    import asyncpg

    url = make_url(db_url)
    user_id = str(uuid.uuid4())
    kb_id = str(uuid.uuid4())

    async def _run():
        conn = await asyncpg.connect(
            host=url.host, port=url.port,
            user=url.username, password=url.password,
            database=url.database,
        )
        try:
            await conn.execute(
                f"INSERT INTO users (id, email, username, hashed_password, role, "
                f"name, department, must_change_password, is_active, created_at, updated_at) "
                f"VALUES ('{user_id}', 'kbr_{user_id[:8]}@test', 'kbr_{user_id[:8]}', "
                f"'x', 'admin', '', '', false, true, now(), now())"
            )
            await conn.execute(
                f"INSERT INTO knowledge_bases (id, name, description, visibility, "
                f"owner_id, created_at, updated_at) "
                f"VALUES ('{kb_id}', 'KB1', '', 'private', '{user_id}', now(), now())"
            )
        finally:
            await conn.close()

    asyncio.run(_run())
    return kb_id


def _count_kbr_by_kb(db_url: str, kb_id: str) -> int:
    import asyncpg

    url = make_url(db_url)

    async def _run():
        conn = await asyncpg.connect(
            host=url.host, port=url.port,
            user=url.username, password=url.password,
            database=url.database,
        )
        try:
            return await conn.fetchval(
                "SELECT count(*) FROM knowledge_base_resources WHERE knowledge_base_id = $1",
                uuid.UUID(kb_id),
            )
        finally:
            await conn.close()

    return asyncio.run(_run())


def _delete_kb(db_url: str, kb_id: str) -> None:
    import asyncpg

    url = make_url(db_url)

    async def _run():
        conn = await asyncpg.connect(
            host=url.host, port=url.port,
            user=url.username, password=url.password,
            database=url.database,
        )
        try:
            await conn.execute(
                "DELETE FROM knowledge_bases WHERE id = $1", uuid.UUID(kb_id)
            )
        finally:
            await conn.close()

    asyncio.run(_run())


@pytest.mark.skipif(not _pg_available(), reason="PostgreSQL 不可用")
def test_constraints_behavior(monkeypatch) -> None:
    """验证 knowledge_base_resources 的全部数据库层约束。"""
    from sqlalchemy.engine import make_url as _mu

    # 1. 重建临时库 + alembic upgrade head
    _recreate_smoke_db()
    # env.py 通过 settings.DATABASE_URL 读取目标库，patch 后 upgrade 作用于临时库
    smoke_url_str = _smoke_db_url()
    monkeypatch.setattr(settings, "DATABASE_URL", smoke_url_str)
    _alembic_upgrade_head()

    try:
        # 2. seed User + KB（FK 链必需）
        kb_id = _seed_kb_and_user(smoke_url_str)
        resource_id = str(uuid.uuid4())
        resource_id_2 = str(uuid.uuid4())

        # 3. 正常插入应成功
        _insert_kbr(smoke_url_str, kb_id=kb_id, resource_type="herb", resource_id=resource_id)
        assert _count_kbr_by_kb(smoke_url_str, kb_id) == 1

        # 4. resource_type NOT NULL
        with pytest.raises(Exception):
            _insert_kbr(smoke_url_str, kb_id=kb_id, resource_type=None, resource_id=resource_id_2)

        # 5. resource_id NOT NULL
        with pytest.raises(Exception):
            _insert_kbr(smoke_url_str, kb_id=kb_id, resource_type="theory", resource_id=None)

        # 6. knowledge_base_id NOT NULL
        with pytest.raises(Exception):
            _insert_kbr(smoke_url_str, kb_id=None, resource_type="herb", resource_id=resource_id_2)

        # 7. knowledge_base_id FK：不存在 KB → 违反
        with pytest.raises(Exception):
            _insert_kbr(
                smoke_url_str,
                kb_id=str(uuid.uuid4()),
                resource_type="herb",
                resource_id=resource_id_2,
            )

        # 8. UNIQUE (kb_id, resource_type, resource_id)：重复挂载应失败
        with pytest.raises(Exception):
            _insert_kbr(smoke_url_str, kb_id=kb_id, resource_type="herb", resource_id=resource_id)

        # 9. 跨 resource_type 允许：同 KB 同 resource_id 不同 type → 允许
        _insert_kbr(smoke_url_str, kb_id=kb_id, resource_type="literature", resource_id=resource_id)
        assert _count_kbr_by_kb(smoke_url_str, kb_id) == 2

        # 10. 跨 KB 允许：同 resource_type+resource_id 在另一 KB → 允许
        kb_id_2 = _seed_kb_and_user(smoke_url_str)
        _insert_kbr(smoke_url_str, kb_id=kb_id_2, resource_type="herb", resource_id=resource_id)
        assert _count_kbr_by_kb(smoke_url_str, kb_id_2) == 1

        # 11. KB delete → CASCADE 清理关联
        _delete_kb(smoke_url_str, kb_id)
        assert _count_kbr_by_kb(smoke_url_str, kb_id) == 0
        # kb_id_2 上的挂载不受影响
        assert _count_kbr_by_kb(smoke_url_str, kb_id_2) == 1
    finally:
        _drop_smoke_db()


# ============================================================================
# TASK-008 Stage 3：Backend Resource 挂载 API
# 通过 FastAPI TestClient 走完整 HTTP 链路；与 test_herbs.py / test_kbs.py 同构
# ============================================================================

import src.core.deps as deps_mod  # noqa: E402
from src.domain.models import User  # noqa: E402
from tests.test_upload import PG_AVAILABLE  # noqa: E402

pytestmark_api = pytest.mark.skipif(not PG_AVAILABLE, reason="PostgreSQL 未启动")

_SUFFIX = uuid.uuid4().hex[:8]


class KBRKeeper:
    """记录测试创建的 KB / Resource / 挂载，统一回收。

    逆序回收：先取消挂载 → 删资源 → 删 KB，避免 FK/RESTRICT 残留。
    """

    def __init__(self, client) -> None:
        self.client = client
        self._created: list[tuple[str, str, dict | None]] = []

    def kb(self, name: str | None = None) -> dict:
        label = name or f"挂载测试KB-{_SUFFIX}"
        resp = self.client.post("/api/v1/kb", json={"name": label, "visibility": "private"})
        assert resp.status_code == 201, resp.text
        body = resp.json()
        self._created.append(("kb", body["id"], None))
        return body

    def herb(self, name: str | None = None) -> dict:
        label = name or f"挂载测试药-{_SUFFIX}"
        resp = self.client.post("/api/v1/herbs", json={"name": label})
        assert resp.status_code == 201, resp.text
        body = resp.json()
        self._created.append(("herb", body["id"], None))
        return body

    def prescription(self, name: str | None = None) -> dict:
        label = name or f"挂载测试方-{_SUFFIX}"
        resp = self.client.post("/api/v1/prescriptions", json={"name": label})
        assert resp.status_code == 201, resp.text
        body = resp.json()
        self._created.append(("prescription", body["id"], None))
        return body

    def theory(self, name: str | None = None) -> dict:
        label = name or f"挂载测试理论-{_SUFFIX}"
        resp = self.client.post("/api/v1/theories", json={"name": label})
        assert resp.status_code == 201, resp.text
        body = resp.json()
        self._created.append(("theory", body["id"], None))
        return body

    def literature(self, name: str | None = None) -> dict:
        label = name or f"挂载测试文献-{_SUFFIX}"
        resp = self.client.post(
            "/api/v1/literatures",
            json={"name": label, "author": "测试作者", "dynasty": "汉"},
        )
        assert resp.status_code == 201, resp.text
        body = resp.json()
        self._created.append(("literature", body["id"], None))
        return body

    def mount(self, kb_id: str, resource_type: str, resource_id: str) -> dict:
        """挂载并记录，便于回收时取消挂载。"""
        resp = self.client.post(
            f"/api/v1/kb/{kb_id}/resources",
            json={"resource_type": resource_type, "resource_id": resource_id},
        )
        assert resp.status_code == 201, resp.text
        body = resp.json()
        self._created.append(("mount", body["id"], {
            "kb_id": kb_id, "resource_type": resource_type, "resource_id": resource_id,
        }))
        return body

    def teardown(self) -> None:
        for kind, item_id, ctx in reversed(self._created):
            if kind == "mount":
                self.client.delete(
                    f"/api/v1/kb/{ctx['kb_id']}/resources/{ctx['resource_type']}/{ctx['resource_id']}"
                )
            elif kind == "herb":
                self.client.delete(f"/api/v1/herbs/{item_id}")
            elif kind == "prescription":
                self.client.delete(f"/api/v1/prescriptions/{item_id}")
            elif kind == "theory":
                self.client.delete(f"/api/v1/theories/{item_id}")
            elif kind == "literature":
                self.client.delete(f"/api/v1/literatures/{item_id}")
            elif kind == "kb":
                self.client.delete(f"/api/v1/kb/{item_id}")


@pytest.fixture
def kbr_keeper(client):
    k = KBRKeeper(client)
    yield k
    k.teardown()


def _make_other_user() -> User:
    """构造一个非 admin 且 id 不同于 TEST_USER 的用户（用于越权测试）。"""
    return User(
        id=uuid.uuid4(),
        email=f"other-{_SUFFIX}@example.com",
        username=f"other-{_SUFFIX}",
        hashed_password="",
        role="member",
    )


# ── List ─────────────────────────────────────────────────────────────────────


@pytestmark_api
def test_list_empty(kbr_keeper, client):
    """空 KB 的资源列表 → 200，items=[]，total=0。"""
    kb = kbr_keeper.kb()
    resp = client.get(f"/api/v1/kb/{kb['id']}/resources")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["items"] == []
    assert body["total"] == 0
    assert body["limit"] == 20
    assert body["offset"] == 0


@pytestmark_api
def test_list_with_multiple_resources(kbr_keeper, client):
    """挂载多类资源后列表返回全部，resource_name 来自对应资源表。"""
    kb = kbr_keeper.kb()
    h = kbr_keeper.herb(name=f"列表药1-{_SUFFIX}")
    p = kbr_keeper.prescription(name=f"列表方1-{_SUFFIX}")
    t = kbr_keeper.theory(name=f"列表理论1-{_SUFFIX}")
    kbr_keeper.mount(kb["id"], "herb", h["id"])
    kbr_keeper.mount(kb["id"], "prescription", p["id"])
    kbr_keeper.mount(kb["id"], "theory", t["id"])

    resp = client.get(f"/api/v1/kb/{kb['id']}/resources")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["total"] == 3
    types = {it["resource_type"] for it in body["items"]}
    assert types == {"herb", "prescription", "theory"}
    # resource_name 必须真实来自资源表，而非空或 id
    names = {it["resource_name"] for it in body["items"]}
    assert f"列表药1-{_SUFFIX}" in names
    assert f"列表方1-{_SUFFIX}" in names
    assert f"列表理论1-{_SUFFIX}" in names


@pytestmark_api
def test_list_filter_by_resource_type(kbr_keeper, client):
    """resource_type=herb 过滤只返回 herb 类型挂载。"""
    kb = kbr_keeper.kb()
    h1 = kbr_keeper.herb(name=f"过滤药1-{_SUFFIX}")
    h2 = kbr_keeper.herb(name=f"过滤药2-{_SUFFIX}")
    t = kbr_keeper.theory(name=f"过滤理论-{_SUFFIX}")
    kbr_keeper.mount(kb["id"], "herb", h1["id"])
    kbr_keeper.mount(kb["id"], "herb", h2["id"])
    kbr_keeper.mount(kb["id"], "theory", t["id"])

    resp = client.get(f"/api/v1/kb/{kb['id']}/resources", params={"resource_type": "herb"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["total"] == 2
    assert all(it["resource_type"] == "herb" for it in body["items"])


@pytestmark_api
def test_list_invalid_resource_type_400(kbr_keeper, client):
    """非法 resource_type → 400。"""
    kb = kbr_keeper.kb()
    resp = client.get(
        f"/api/v1/kb/{kb['id']}/resources", params={"resource_type": "xxx"}
    )
    assert resp.status_code == 400


@pytestmark_api
def test_list_kb_not_found_404(client):
    """不存在的 KB → 404。"""
    resp = client.get(f"/api/v1/kb/{uuid.uuid4()}/resources")
    assert resp.status_code == 404


@pytestmark_api
def test_list_member_can_read(client, monkeypatch, kbr_keeper):
    """Viewer/Member 角色可以读取（owner 视角下读自己创建的 KB 也应通过）。

    这里 TEST_USER 默认 admin，创建 KB 后变成 KB owner；
    切到 member 后该 KB 仍可通过 get_accessible_kb_ids（owner_id 匹配）命中，
    验证 _require_kb_member_or_admin 对 owner 的放行。
    """
    kb = kbr_keeper.kb()  # owner = TEST_USER
    # 切换为 member 角色，但仍是 owner_id（应放行）
    monkeypatch.setattr(deps_mod.TEST_USER, "role", "member")
    resp = client.get(f"/api/v1/kb/{kb['id']}/resources")
    assert resp.status_code == 200, resp.text


@pytestmark_api
def test_list_non_member_forbidden_403(client, monkeypatch):
    """完全切换为另一 member 用户访问他人 KB → 403。"""
    # 先以 admin 创建一个 KB（owner = TEST_USER.id）
    resp = client.post("/api/v1/kb", json={"name": f"隔离KB-{_SUFFIX}"})
    kb_id = resp.json()["id"]
    try:
        other = _make_other_user()
        monkeypatch.setattr(deps_mod, "TEST_USER", other)
        resp = client.get(f"/api/v1/kb/{kb_id}/resources")
        assert resp.status_code == 403
    finally:
        # 清理：切回 admin 删 KB
        monkeypatch.undo()
        client.delete(f"/api/v1/kb/{kb_id}")


# ── Create (mount) ───────────────────────────────────────────────────────────


@pytestmark_api
def test_mount_herb_success(kbr_keeper, client):
    """成功挂载 Herb。"""
    kb = kbr_keeper.kb()
    h = kbr_keeper.herb(name=f"挂载成功药-{_SUFFIX}")
    body = kbr_keeper.mount(kb["id"], "herb", h["id"])
    assert body["resource_type"] == "herb"
    assert body["resource_id"] == h["id"]
    assert body["resource_name"] == f"挂载成功药-{_SUFFIX}"
    assert body["knowledge_base_id"] == kb["id"]


@pytestmark_api
def test_mount_prescription_success(kbr_keeper, client):
    """成功挂载 Prescription。"""
    kb = kbr_keeper.kb()
    p = kbr_keeper.prescription(name=f"挂载成功方-{_SUFFIX}")
    body = kbr_keeper.mount(kb["id"], "prescription", p["id"])
    assert body["resource_type"] == "prescription"
    assert body["resource_name"] == f"挂载成功方-{_SUFFIX}"


@pytestmark_api
def test_mount_theory_success(kbr_keeper, client):
    """成功挂载 Theory。"""
    kb = kbr_keeper.kb()
    t = kbr_keeper.theory(name=f"挂载成功理论-{_SUFFIX}")
    body = kbr_keeper.mount(kb["id"], "theory", t["id"])
    assert body["resource_type"] == "theory"


@pytestmark_api
def test_mount_literature_success(kbr_keeper, client):
    """成功挂载 Literature。"""
    kb = kbr_keeper.kb()
    l = kbr_keeper.literature(name=f"挂载成功文献-{_SUFFIX}")
    body = kbr_keeper.mount(kb["id"], "literature", l["id"])
    assert body["resource_type"] == "literature"


@pytestmark_api
def test_mount_kb_not_found_404(client):
    """不存在的 KB → 404。"""
    h_id = uuid.uuid4()  # 不实际创建 herb，权限先于资源校验
    resp = client.post(
        f"/api/v1/kb/{uuid.uuid4()}/resources",
        json={"resource_type": "herb", "resource_id": str(h_id)},
    )
    assert resp.status_code == 404


@pytestmark_api
def test_mount_resource_not_found_404(kbr_keeper, client):
    """resource_id 在对应表不存在 → 404（严格类型匹配，不查其他表）。"""
    kb = kbr_keeper.kb()
    resp = client.post(
        f"/api/v1/kb/{kb['id']}/resources",
        json={"resource_type": "herb", "resource_id": str(uuid.uuid4())},
    )
    assert resp.status_code == 404


@pytestmark_api
def test_mount_invalid_resource_type_400(kbr_keeper, client):
    """resource_type 非法 → 400。"""
    kb = kbr_keeper.kb()
    resp = client.post(
        f"/api/v1/kb/{kb['id']}/resources",
        json={"resource_type": "xxx", "resource_id": str(uuid.uuid4())},
    )
    assert resp.status_code == 400


@pytestmark_api
def test_mount_invalid_uuid_422(kbr_keeper, client):
    """resource_id 非 UUID → 422（Pydantic 校验）。"""
    kb = kbr_keeper.kb()
    resp = client.post(
        f"/api/v1/kb/{kb['id']}/resources",
        json={"resource_type": "herb", "resource_id": "not-a-uuid"},
    )
    assert resp.status_code == 422


@pytestmark_api
def test_mount_duplicate_409(kbr_keeper, client):
    """重复挂载同一 (kb, type, id) → 409。"""
    kb = kbr_keeper.kb()
    h = kbr_keeper.herb(name=f"重复挂载药-{_SUFFIX}")
    kbr_keeper.mount(kb["id"], "herb", h["id"])
    # 第二次挂载（不走 keeper，避免 teardown 重复删）
    resp = client.post(
        f"/api/v1/kb/{kb['id']}/resources",
        json={"resource_type": "herb", "resource_id": h["id"]},
    )
    assert resp.status_code == 409


@pytestmark_api
def test_mount_forbidden_403(client, monkeypatch):
    """非 owner 非 admin member 挂载他人 KB → 403。"""
    resp = client.post("/api/v1/kb", json={"name": f"权限KB-{_SUFFIX}"})
    kb_id = resp.json()["id"]
    try:
        other = _make_other_user()
        monkeypatch.setattr(deps_mod, "TEST_USER", other)
        resp = client.post(
            f"/api/v1/kb/{kb_id}/resources",
            json={"resource_type": "herb", "resource_id": str(uuid.uuid4())},
        )
        assert resp.status_code == 403
    finally:
        monkeypatch.undo()
        client.delete(f"/api/v1/kb/{kb_id}")


@pytestmark_api
def test_mount_type_id_strict_match(kbr_keeper, client):
    """resource_type=herb 但 resource_id=prescription.id → 404（严格匹配）。

    不允许"任一表存在即通过"。herb 类型只查 herbs 表，prescription.id
    在 herbs 表中不存在 → 404。
    """
    kb = kbr_keeper.kb()
    p = kbr_keeper.prescription(name=f"类型错配方-{_SUFFIX}")  # 这是 prescription.id
    # 用 herb 类型挂载 prescription.id → 应失败
    resp = client.post(
        f"/api/v1/kb/{kb['id']}/resources",
        json={"resource_type": "herb", "resource_id": p["id"]},
    )
    assert resp.status_code == 404


@pytestmark_api
def test_mount_type_id_strict_match_reverse(kbr_keeper, client):
    """resource_type=prescription 但 resource_id=herb.id → 404。"""
    kb = kbr_keeper.kb()
    h = kbr_keeper.herb(name=f"类型错药-{_SUFFIX}")
    resp = client.post(
        f"/api/v1/kb/{kb['id']}/resources",
        json={"resource_type": "prescription", "resource_id": h["id"]},
    )
    assert resp.status_code == 404


# ── Delete (unmount) ────────────────────────────────────────────────────────


@pytestmark_api
def test_unmount_success(kbr_keeper, client):
    """成功取消挂载。"""
    kb = kbr_keeper.kb()
    h = kbr_keeper.herb(name=f"取消挂载药-{_SUFFIX}")
    body = kbr_keeper.mount(kb["id"], "herb", h["id"])

    # 直接 DELETE 不走 keeper（teardown 会重复），手动从 keeper 移除该 mount
    resp = client.delete(
        f"/api/v1/kb/{kb['id']}/resources/herb/{h['id']}"
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["unmounted"] is True
    # 列表中不再出现
    listing = client.get(f"/api/v1/kb/{kb['id']}/resources").json()
    assert all(it["resource_id"] != h["id"] for it in listing["items"])
    # 从 keeper 移除已删挂载，避免 teardown 重复 DELETE
    kbr_keeper._created = [c for c in kbr_keeper._created if c[0] != "mount" or c[1] != body["id"]]


@pytestmark_api
def test_unmount_not_found_404(kbr_keeper, client):
    """取消不存在的挂载 → 404。"""
    kb = kbr_keeper.kb()
    resp = client.delete(
        f"/api/v1/kb/{kb['id']}/resources/herb/{uuid.uuid4()}"
    )
    assert resp.status_code == 404


@pytestmark_api
def test_unmount_other_kb_record_404(kbr_keeper, client):
    """删除其他 KB 的挂载 → 404（三元组不匹配）。

    即使 (resource_type, resource_id) 在其他 KB 存在挂载，
    当前 KB 的删除必须返回 404。
    """
    kb1 = kbr_keeper.kb(name=f"KB1-{_SUFFIX}")
    kb2 = kbr_keeper.kb(name=f"KB2-{_SUFFIX}")
    h = kbr_keeper.herb(name=f"跨KB药-{_SUFFIX}")
    kbr_keeper.mount(kb1["id"], "herb", h["id"])  # 在 KB1 挂载

    # 在 KB2 取消该 herb 的挂载 → 404
    resp = client.delete(f"/api/v1/kb/{kb2['id']}/resources/herb/{h['id']}")
    assert resp.status_code == 404


@pytestmark_api
def test_unmount_forbidden_403(client, monkeypatch):
    """非 owner 非 admin 取消挂载 → 403。"""
    resp = client.post("/api/v1/kb", json={"name": f"权限KB-{_SUFFIX}"})
    kb_id = resp.json()["id"]
    try:
        other = _make_other_user()
        monkeypatch.setattr(deps_mod, "TEST_USER", other)
        resp = client.delete(
            f"/api/v1/kb/{kb_id}/resources/herb/{uuid.uuid4()}"
        )
        assert resp.status_code == 403
    finally:
        monkeypatch.undo()
        client.delete(f"/api/v1/kb/{kb_id}")


# ── 隔离性 ────────────────────────────────────────────────────────────────────


@pytestmark_api
def test_cross_kb_isolation(kbr_keeper, client):
    """KB1+Herb1 + KB2+Herb1；删除 KB1 挂载不影响 KB2 上的挂载。"""
    kb1 = kbr_keeper.kb(name=f"隔离KB1-{_SUFFIX}")
    kb2 = kbr_keeper.kb(name=f"隔离KB2-{_SUFFIX}")
    h = kbr_keeper.herb(name=f"共享药-{_SUFFIX}")
    m1 = kbr_keeper.mount(kb1["id"], "herb", h["id"])
    kbr_keeper.mount(kb2["id"], "herb", h["id"])

    # 删除 KB1 上的挂载（手动 DELETE 不走 keeper）
    resp = client.delete(f"/api/v1/kb/{kb1['id']}/resources/herb/{h['id']}")
    assert resp.status_code == 200
    # 从 keeper 移除已删 mount，避免 teardown 重复
    kbr_keeper._created = [c for c in kbr_keeper._created if c[1] != m1["id"]]

    # KB2 上的挂载仍存在
    listing = client.get(f"/api/v1/kb/{kb2['id']}/resources").json()
    assert any(it["resource_id"] == h["id"] for it in listing["items"])
    # KB1 上的挂载已清空
    listing1 = client.get(f"/api/v1/kb/{kb1['id']}/resources").json()
    assert all(it["resource_id"] != h["id"] for it in listing1["items"])
