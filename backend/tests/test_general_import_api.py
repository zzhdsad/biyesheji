"""通用导入 API 端到端测试（隔离测试库 + TestClient）。

覆盖 TASK 要求：
- 普通导入：多文件上传、类型识别、预览、错误展示、按记录入库
- 关联导入：Schema 与主外键候选分析、关系确认、提交统计
- 导入后统计：实体 / 关系 / 失败记录
- 索引一致性：差异报告、未确认不得删除向量
"""

from __future__ import annotations

import json
import uuid

import pytest
from fastapi.testclient import TestClient

from src.core.config import settings
from tests.test_upload import PG_AVAILABLE

CSV_TERMS = "术语id,术语名称,来源\nT1,脾胃虚弱,神农本草经\nT2,肝郁气滞,伤寒论\n"
CSV_MEDICINES = "中成药id,名称\nM1,四君子汤\nM2,逍遥散\n"
CSV_LINKS = "中成药id,术语id\nM1,T1\nM1,T2\nM2,T1\n"


@pytest.fixture(scope="module")
def client() -> TestClient:  # noqa: F811 - 覆盖 conftest 的同名 fixture
    from src.main import app

    with TestClient(app) as c:
        yield c


@pytest.fixture(scope="module")
def _test_user():
    """把测试模式的 TEST_USER 真正落库。

    TEST_USER 是游离的 ORM 对象（不在 users 表），而 knowledge_bases.owner_id
    有外键约束，直接建库会 ForeignKeyViolation。
    """
    import asyncio

    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from src.core.deps import TEST_USER
    from src.domain.models import User

    async def _ensure() -> None:
        engine = create_async_engine(settings.DATABASE_URL)
        try:
            async with async_sessionmaker(engine)() as session:
                exists = await session.scalar(select(User).where(User.id == TEST_USER.id))
                if exists is not None:
                    return
                same_name = await session.scalar(
                    select(User).where(User.username == TEST_USER.username)
                )
                if same_name is not None:
                    # init_db 已 seed 同名管理员：把测试用户的 id 对齐到它，
                    # 既不重复插入，也让后续外键引用指向真实存在的行
                    TEST_USER.id = same_name.id
                    return
                session.add(
                    User(
                        id=TEST_USER.id,
                        email=TEST_USER.email,
                        username=TEST_USER.username,
                        hashed_password="",
                        role="admin",
                        name="测试管理员",
                        department="测试部",
                        must_change_password=False,
                    )
                )
                await session.commit()
        finally:
            await engine.dispose()

    asyncio.run(_ensure())


@pytest.fixture()
def kb(client: TestClient, _test_user) -> str:
    resp = client.post("/api/v1/kb", json={"name": f"导入测试库-{uuid.uuid4().hex[:8]}"})
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def _count_kg_edges(kb_id: str) -> int:
    """直接查库确认 KG 边数（接口层不暴露该统计，测试需独立校验）。"""
    import asyncio

    from sqlalchemy import func, select
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from src.domain.models import KgEdge

    async def _run() -> int:
        engine = create_async_engine(settings.DATABASE_URL)
        try:
            async with async_sessionmaker(engine)() as session:
                stmt = select(func.count()).select_from(KgEdge).where(KgEdge.kb_id == kb_id)
                return int(await session.scalar(stmt) or 0)
        finally:
            await engine.dispose()

    return asyncio.run(_run())


def _upload(client: TestClient, url: str, files: list[tuple], paths: list[str] | None = None):
    data = {"paths": json.dumps(paths, ensure_ascii=False)} if paths else None
    return client.post(url, files=files, data=data)


# ============================================================================
# 扫描与识别
# ============================================================================


@pytest.mark.skipif(not PG_AVAILABLE, reason="PostgreSQL 未启动")
def test_scan_detects_format_encoding_and_preview(client: TestClient):
    resp = _upload(
        client,
        "/api/v1/import/scan",
        [
            ("files", ("术语.csv", CSV_TERMS.encode("utf-8"), "text/csv")),
            ("files", ("说明.txt", "这是普通文本".encode("utf-8"), "text/plain")),
        ],
        paths=["术语.csv", "说明.txt"],
    )
    assert resp.status_code == 200, resp.text
    payload = resp.json()
    by_name = {f["file_name"]: f for f in payload["files"]}

    csv_file = by_name["术语.csv"]
    assert csv_file["detected_format"] == "csv"
    assert csv_file["encoding"] == "utf-8"
    assert csv_file["row_count"] == 2
    assert csv_file["preview"][0]["row_no"] == 2  # 表头占第 1 行
    assert csv_file["preview"][0]["data"]["术语名称"] == "脾胃虚弱"

    assert by_name["说明.txt"]["detected_format"] == "txt"
    assert by_name["说明.txt"]["row_count"] == 0


@pytest.mark.skipif(not PG_AVAILABLE, reason="PostgreSQL 未启动")
def test_scan_reports_unsupported_file_with_reason(client: TestClient):
    resp = _upload(
        client,
        "/api/v1/import/scan",
        [("files", ("未知.bin", b"\x00\x01\x02\x03", "application/octet-stream"))],
        paths=["未知.bin"],
    )
    assert resp.status_code == 200, resp.text
    file_info = resp.json()["files"][0]
    assert file_info["supported"] is False
    assert "无法识别" in file_info["error"]


@pytest.mark.skipif(not PG_AVAILABLE, reason="PostgreSQL 未启动")
def test_scan_rejects_empty_file(client: TestClient):
    resp = _upload(
        client,
        "/api/v1/import/scan",
        [("files", ("空.csv", b"", "text/csv"))],
        paths=["空.csv"],
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["files"][0]["error"] == "文件为空"


# ============================================================================
# 普通导入提交
# ============================================================================


@pytest.mark.skipif(not PG_AVAILABLE, reason="PostgreSQL 未启动")
def test_general_commit_keeps_records(client: TestClient, kb: str):
    scan = _upload(
        client,
        "/api/v1/import/scan",
        [("files", ("术语.csv", CSV_TERMS.encode("utf-8"), "text/csv"))],
        paths=["术语.csv"],
    )
    session_id = scan.json()["session_id"]

    resp = client.post(
        "/api/v1/import/general/commit",
        json={
            "session_id": session_id,
            "kb_id": kb,
            "files": [{"path": "术语.csv", "entity_type": "中医术语", "pk_field": "术语id"}],
        },
    )
    assert resp.status_code == 200, resp.text
    payload = resp.json()
    assert payload["records"] == 2
    assert payload["entities"] == 2
    assert payload["failed"] == 0

    stats = client.get(f"/api/v1/import/stats?kb_id={kb}").json()
    assert stats["entities"] == 2
    assert stats["records"] == 2


# ============================================================================
# 关联导入：分析 → 提交 → 确认
# ============================================================================


@pytest.mark.skipif(not PG_AVAILABLE, reason="PostgreSQL 未启动")
def test_relational_analyze_and_commit(client: TestClient, kb: str):
    analyze = _upload(
        client,
        "/api/v1/import/relational/analyze",
        [
            ("files", ("D1.csv", CSV_TERMS.encode("utf-8"), "text/csv")),
            ("files", ("D2.csv", CSV_MEDICINES.encode("utf-8"), "text/csv")),
            ("files", ("D3.csv", CSV_LINKS.encode("utf-8"), "text/csv")),
        ],
        paths=["D1.csv", "D2.csv", "D3.csv"],
    )
    assert analyze.status_code == 200, analyze.text
    plan = analyze.json()
    assert len(plan["tables"]) == 3
    assert plan["primary_keys"]["D1"] == ["术语id"]
    assert plan["primary_keys"]["D2"] == ["中成药id"]
    assert plan["junctions"], "应识别出 D3 为关联表"
    assert plan["junctions"][0]["table"] == "D3"

    commit = client.post(
        "/api/v1/import/relational/commit",
        json={
            "session_id": plan["session_id"],
            "kb_id": kb,
            "primary_keys": {"D1": "术语id", "D2": "中成药id"},
            "junctions": [
                {
                    "table": "D3",
                    "left_table": plan["junctions"][0]["left_table"],
                    "left_field": plan["junctions"][0]["left_field"],
                    "right_table": plan["junctions"][0]["right_table"],
                    "right_field": plan["junctions"][0]["right_field"],
                    "relation_type": "含术语",
                }
            ],
            # 不自动确认：验证「未确认不入图谱」
            "auto_confirm": False,
        },
    )
    assert commit.status_code == 200, commit.text
    result = commit.json()
    assert result["relations"] == 3
    assert result["kg_edges"] == 0  # 未确认 → 不进 KG
    assert result["failed_rows"] == 0

    stats = client.get(f"/api/v1/import/stats?kb_id={kb}").json()
    assert stats["relations_pending"] == 3
    assert stats["relations_confirmed"] == 0

    # 确认后同步进知识图谱
    relations = client.get(
        f"/api/v1/import/relations?kb_id={kb}&status=pending&limit=50"
    ).json()["items"]
    assert len(relations) == 3
    ids = [r["id"] for r in relations]

    confirm = client.post(
        "/api/v1/import/relations/status",
        json={"kb_id": kb, "relation_ids": ids, "status": "confirmed"},
    )
    assert confirm.status_code == 200, confirm.text
    assert confirm.json()["updated"] == 3
    assert confirm.json()["kg_edges"] == 3  # 已确认 → 进 KG

    stats = client.get(f"/api/v1/import/stats?kb_id={kb}").json()
    assert stats["relations_confirmed"] == 3
    assert stats["relations_pending"] == 0


@pytest.mark.skipif(not PG_AVAILABLE, reason="PostgreSQL 未启动")
def test_cancelling_confirmed_relation_removes_kg_edge(client: TestClient, kb: str):
    """先确认入库、再人工取消：KG 里的对应边必须同步撤掉，不留无法溯源的关系。"""
    analyze = _upload(
        client,
        "/api/v1/import/relational/analyze",
        [
            ("files", ("D1.csv", CSV_TERMS.encode("utf-8"), "text/csv")),
            ("files", ("D2.csv", CSV_MEDICINES.encode("utf-8"), "text/csv")),
            ("files", ("D3.csv", CSV_LINKS.encode("utf-8"), "text/csv")),
        ],
        paths=["D1.csv", "D2.csv", "D3.csv"],
    )
    plan = analyze.json()
    commit = client.post(
        "/api/v1/import/relational/commit",
        json={
            "session_id": plan["session_id"],
            "kb_id": kb,
            "primary_keys": {"D1": "术语id", "D2": "中成药id"},
            "junctions": [
                {
                    "table": "D3",
                    "left_table": plan["junctions"][0]["left_table"],
                    "left_field": plan["junctions"][0]["left_field"],
                    "right_table": plan["junctions"][0]["right_table"],
                    "right_field": plan["junctions"][0]["right_field"],
                    "relation_type": "含术语",
                }
            ],
            "auto_confirm": True,
        },
    )
    assert commit.status_code == 200, commit.text
    assert commit.json()["kg_edges"] == 3

    ids = [r["id"] for r in client.get(f"/api/v1/import/relations?kb_id={kb}").json()["items"]]
    rejected = client.post(
        "/api/v1/import/relations/status",
        json={"kb_id": kb, "relation_ids": ids, "status": "rejected"},
    )
    assert rejected.status_code == 200, rejected.text

    stats = client.get(f"/api/v1/import/stats?kb_id={kb}").json()
    assert stats["relations_rejected"] == 3
    assert stats["relations_confirmed"] == 0

    # 图谱中不应残留这三条边
    edges = _count_kg_edges(kb)
    assert edges == 0


# ============================================================================
# 索引一致性接口
# ============================================================================


@pytest.mark.skipif(not PG_AVAILABLE, reason="PostgreSQL 未启动")
def test_index_diff_endpoint_returns_report(client: TestClient):
    resp = client.get("/api/v1/import/index/diff")
    assert resp.status_code == 200, resp.text
    payload = resp.json()
    assert "totals" in payload
    assert "documents" in payload
    assert "clean" in payload


@pytest.mark.skipif(not PG_AVAILABLE, reason="PostgreSQL 未启动")
def test_index_apply_refuses_deletion_without_confirmation(client: TestClient):
    resp = client.post(
        "/api/v1/import/index/apply",
        json={"fix_flags": True, "delete_orphans": True, "confirmed": False},
    )
    # 未确认删除必须被拒绝（400），而不是静默删向量
    assert resp.status_code == 400
    assert "confirmed" in resp.json()["detail"]


@pytest.mark.skipif(not PG_AVAILABLE, reason="PostgreSQL 未启动")
def test_index_apply_safe_fix_is_allowed(client: TestClient):
    resp = client.post(
        "/api/v1/import/index/apply",
        json={"fix_flags": True, "confirmed": False},
    )
    assert resp.status_code == 200, resp.text
    assert "flags_updated" in resp.json()


@pytest.mark.skipif(not PG_AVAILABLE, reason="PostgreSQL 未启动")
def test_general_import_requires_admin():
    """非管理员不得访问导入接口（服务端校验，不依赖前端隐藏按钮）。"""
    import src.core.deps as deps

    from src.main import app

    deps.TEST_MODE_ENABLED = False
    try:
        with TestClient(app) as guest:
            resp = guest.get("/api/v1/import/index/diff")
            assert resp.status_code in (401, 403)
    finally:
        deps.TEST_MODE_ENABLED = True
