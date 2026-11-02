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


JSONL_MEDICINES = (
    '{"id": "M1", "name": "四君子汤"}\n'
    '{"id": "M2", "name": \n'
    '{"id": "M3", "name": "逍遥散"}\n'
)


@pytest.mark.skipif(not PG_AVAILABLE, reason="PostgreSQL 未启动")
def test_relational_commit_skips_malformed_rows_and_reports_them(client: TestClient, kb: str):
    """畸形行只跳过自身：其余行照常入库，且错误明细随提交结果返回。"""
    analyze = _upload(
        client,
        "/api/v1/import/relational/analyze",
        [
            ("files", ("D1.csv", CSV_TERMS.encode("utf-8"), "text/csv")),
            ("files", ("D2.jsonl", JSONL_MEDICINES.encode("utf-8"), "application/jsonl")),
        ],
        paths=["D1.csv", "D2.jsonl"],
    )
    assert analyze.status_code == 200, analyze.text
    plan = analyze.json()
    # 分析阶段整文件解析失败只影响该文件，D1 仍可继续
    assert any("D2.jsonl" in e for e in plan["errors"])

    commit = client.post(
        "/api/v1/import/relational/commit",
        json={
            "session_id": plan["session_id"],
            "kb_id": kb,
            "primary_keys": {"D1": "术语id", "D2": "id"},
            "auto_confirm": False,
        },
    )
    assert commit.status_code == 200, commit.text
    result = commit.json()

    # 成功行照常入库：D1 两行 + D2 两条好行
    assert result["records"] == 4
    assert result["entities"] == 4
    # 坏行被跳过并回报，而不是抛出未定义变量导致 500
    assert result["failed_rows"] == 1
    assert len(result["errors"]) == 1
    detail = result["errors"][0]
    assert detail["file_name"] == "D2.jsonl"
    assert detail["row_no"] == 2
    assert "JSON 解析失败" in detail["message"]

    stats = client.get(f"/api/v1/import/stats?kb_id={kb}").json()
    assert stats["records"] == 4
    assert stats["entities"] == 4


@pytest.mark.skipif(not PG_AVAILABLE, reason="PostgreSQL 未启动")
def test_relational_analyze_rejects_same_table_name_from_different_dirs(client: TestClient):
    """不同目录的同名 CSV 会映射到同一张表：分析阶段必须明确拒绝。"""
    resp = _upload(
        client,
        "/api/v1/import/relational/analyze",
        [
            ("files", ("D1.csv", CSV_TERMS.encode("utf-8"), "text/csv")),
            ("files", ("D1.csv", CSV_MEDICINES.encode("utf-8"), "text/csv")),
        ],
        paths=["甲/D1.csv", "乙/D1.csv"],
    )
    assert resp.status_code == 400, resp.text
    detail = resp.json()["detail"]
    assert "甲/D1.csv" in detail
    assert "乙/D1.csv" in detail
    assert "静默覆盖" in detail


@pytest.mark.skipif(not PG_AVAILABLE, reason="PostgreSQL 未启动")
def test_relational_commit_rejects_duplicate_table_names_without_writing(
    client: TestClient, kb: str
):
    """即使绕过分析直接提交，同名表也必须被拒绝且不落库。"""
    scan = _upload(
        client,
        "/api/v1/import/scan",
        [
            ("files", ("D1.csv", CSV_TERMS.encode("utf-8"), "text/csv")),
            ("files", ("D1.csv", CSV_MEDICINES.encode("utf-8"), "text/csv")),
        ],
        paths=["甲/D1.csv", "乙/D1.csv"],
    )
    assert scan.status_code == 200, scan.text

    resp = client.post(
        "/api/v1/import/relational/commit",
        json={
            "session_id": scan.json()["session_id"],
            "kb_id": kb,
            "primary_keys": {"D1": "术语id"},
            "auto_confirm": False,
        },
    )
    assert resp.status_code == 400, resp.text
    detail = resp.json()["detail"]
    assert "甲/D1.csv" in detail
    assert "乙/D1.csv" in detail

    stats = client.get(f"/api/v1/import/stats?kb_id={kb}").json()
    assert stats["records"] == 0
    assert stats["entities"] == 0


@pytest.mark.skipif(not PG_AVAILABLE, reason="PostgreSQL 未启动")
def test_duplicate_relative_paths_are_rejected_before_staging(client: TestClient):
    """路径完全相同意味着无法区分来源，扫描与分析都必须整批拒绝。"""
    files = [
        ("files", ("同名.csv", CSV_TERMS.encode("utf-8"), "text/csv")),
        ("files", ("同名.csv", CSV_MEDICINES.encode("utf-8"), "text/csv")),
    ]
    scan = _upload(client, "/api/v1/import/scan", files, paths=["同名.csv", "同名.csv"])
    assert scan.status_code == 400, scan.text
    assert "重复的上传路径" in scan.json()["detail"]

    analyze = _upload(
        client,
        "/api/v1/import/relational/analyze",
        files,
        paths=["同名.csv", "同名.csv"],
    )
    assert analyze.status_code == 400, analyze.text
    assert "重复的上传路径" in analyze.json()["detail"]


@pytest.mark.skipif(not PG_AVAILABLE, reason="PostgreSQL 未启动")
def test_import_total_failures_return_4xx(client: TestClient, kb: str):
    """整体失败必须返回 4xx，不能以 200 + error 冒充成功。"""
    missing_session = client.post(
        "/api/v1/import/general/commit",
        json={"session_id": "missing-session", "kb_id": kb, "files": []},
    )
    assert missing_session.status_code == 400, missing_session.text
    assert "会话不存在" in missing_session.json()["detail"]

    missing_rel = client.post(
        "/api/v1/import/relational/commit",
        json={"session_id": "missing-session", "kb_id": kb},
    )
    assert missing_rel.status_code == 400, missing_rel.text
    assert "会话不存在" in missing_rel.json()["detail"]

    no_structured = _upload(
        client,
        "/api/v1/import/relational/analyze",
        [("files", ("说明.txt", "这是普通文本".encode("utf-8"), "text/plain"))],
        paths=["说明.txt"],
    )
    assert no_structured.status_code == 400, no_structured.text
    assert "没有可分析的结构化文件" in no_structured.json()["detail"]

    analyze = _upload(
        client,
        "/api/v1/import/relational/analyze",
        [("files", ("D1.csv", CSV_TERMS.encode("utf-8"), "text/csv"))],
        paths=["D1.csv"],
    )
    assert analyze.status_code == 200, analyze.text
    missing_template = client.post(
        "/api/v1/import/relational/commit",
        json={
            "session_id": analyze.json()["session_id"],
            "kb_id": kb,
            "template_id": str(uuid.uuid4()),
        },
    )
    assert missing_template.status_code == 400, missing_template.text
    assert "映射模板不存在" in missing_template.json()["detail"]


@pytest.mark.skipif(not PG_AVAILABLE, reason="PostgreSQL 未启动")
def test_mapping_template_match_save_and_apply(client: TestClient, kb: str):
    """模板保存 → 自动匹配 → 提交时由模板补齐映射；不兼容时明确拒绝套用。"""
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
    fingerprint = plan["dataset_fingerprint"]
    assert fingerprint, "分析结果必须返回数据集指纹"
    junction = plan["junctions"][0]

    tables = {table["table"]: [f["name"] for f in table["fields"]] for table in plan["tables"]}

    # 没有模板时必须返回 none，而不是伪造匹配结果
    match_none = client.post(
        "/api/v1/import/templates/match",
        json={"kb_id": kb, "tables": tables},
    )
    assert match_none.status_code == 200, match_none.text
    assert match_none.json()["status"] == "none"

    config = {
        "version": 2,
        "tables": {
            "D1": {"table": "D1", "entity_type": "术语", "pk_field": "术语id"},
            "D2": {"table": "D2", "entity_type": "中成药", "pk_field": "中成药id"},
            "D3": {"table": "D3", "entity_type": "关联", "pk_field": None},
        },
        "relations": [],
        "junctions": [
            {
                "table": "D3",
                "left_table": junction["left_table"],
                "left_field": junction["left_field"],
                "right_table": junction["right_table"],
                "right_field": junction["right_field"],
                "relation_type": "含术语",
            }
        ],
    }
    saved = client.post(
        "/api/v1/import/templates",
        json={
            "kb_id": kb,
            "name": "D1-D3 测试模板",
            "fingerprint": fingerprint,
            "config": config,
        },
    )
    assert saved.status_code == 200, saved.text
    template_id = saved.json()["id"]
    assert saved.json()["fingerprint"] == fingerprint

    # 相同数据集再次匹配：exact + 模板回显
    match_exact = client.post(
        "/api/v1/import/templates/match",
        json={"kb_id": kb, "tables": tables},
    )
    assert match_exact.status_code == 200, match_exact.text
    exact = match_exact.json()
    assert exact["status"] == "exact"
    assert exact["usable"] is True
    assert exact["template_id"] == template_id
    assert exact["missing_fields"] == {}

    # 模板依赖字段缺失：明确 mismatch 且不可用，前端据此禁止套用
    broken = {name: list(fields) for name, fields in tables.items()}
    broken["D1"] = ["术语名称"]
    match_broken = client.post(
        "/api/v1/import/templates/match",
        json={"kb_id": kb, "tables": broken},
    )
    assert match_broken.status_code == 200, match_broken.text
    broken_result = match_broken.json()
    assert broken_result["status"] == "mismatch"
    assert broken_result["usable"] is False
    assert "术语id" in broken_result["missing_fields"]["D1"]

    # 模板属于其他知识库时不得跨库应用（用独立会话，避免影响后续提交）
    other_kb = client.post(
        "/api/v1/kb", json={"name": f"模板跨库测试-{uuid.uuid4().hex[:8]}"}
    ).json()["id"]
    cross_analyze = _upload(
        client,
        "/api/v1/import/relational/analyze",
        [
            ("files", ("D1.csv", CSV_TERMS.encode("utf-8"), "text/csv")),
            ("files", ("D2.csv", CSV_MEDICINES.encode("utf-8"), "text/csv")),
            ("files", ("D3.csv", CSV_LINKS.encode("utf-8"), "text/csv")),
        ],
        paths=["D1.csv", "D2.csv", "D3.csv"],
    )
    assert cross_analyze.status_code == 200, cross_analyze.text
    cross = client.post(
        "/api/v1/import/relational/commit",
        json={
            "session_id": cross_analyze.json()["session_id"],
            "kb_id": other_kb,
            "primary_keys": {},
            "template_id": template_id,
            "auto_confirm": False,
        },
    )
    # 跨库模板属于请求错误，必须用 4xx 返回，前端才会走失败提示
    assert cross.status_code == 400, cross.text
    assert "其他知识库" in cross.json()["detail"]

    # 由模板补齐主键与关联关系后提交：不显式给映射也能导入
    commit = client.post(
        "/api/v1/import/relational/commit",
        json={
            "session_id": plan["session_id"],
            "kb_id": kb,
            "primary_keys": {},
            "auto_confirm": False,
            "template_id": template_id,
        },
    )
    assert commit.status_code == 200, commit.text
    result = commit.json()
    assert result["entities"] == 4
    assert result["relations"] == 3
    assert result["failed_rows"] == 0


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
def test_index_environment_reports_destructive_permission(client: TestClient, monkeypatch):
    monkeypatch.setattr(settings, "MILVUS_COLLECTION", "document_chunks_dev")
    monkeypatch.setattr(settings, "ENV", "dev")
    resp = client.get("/api/v1/import/index/environment")
    assert resp.status_code == 200, resp.text
    facts = resp.json()
    assert facts["milvus_collection"] == "document_chunks_dev"
    assert facts["isolated_collection"] is True
    assert facts["destructive_allowed"] is True

    monkeypatch.setattr(settings, "MILVUS_COLLECTION", "document_chunks")
    resp = client.get("/api/v1/import/index/environment")
    facts = resp.json()
    assert facts["isolated_collection"] is False
    assert facts["destructive_allowed"] is False


@pytest.mark.skipif(not PG_AVAILABLE, reason="PostgreSQL 未启动")
def test_index_apply_rejects_stale_collection(client: TestClient):
    """前端绑定的集合与后端不一致时必须拒绝，防止跨环境操作正式索引。"""
    resp = client.post(
        "/api/v1/import/index/apply",
        json={
            "fix_flags": True,
            "expected_milvus_collection": "document_chunks_prod",
        },
    )
    assert resp.status_code == 409, resp.text
    assert "milvus" in resp.json()["detail"].lower()


@pytest.mark.skipif(not PG_AVAILABLE, reason="PostgreSQL 未启动")
def test_index_rebuild_rejects_stale_collection(client: TestClient):
    resp = client.post(
        "/api/v1/import/index/rebuild",
        json={
            "retry_failed": True,
            "expected_milvus_collection": "document_chunks_prod",
        },
    )
    assert resp.status_code == 409, resp.text


@pytest.mark.skipif(not PG_AVAILABLE, reason="PostgreSQL 未启动")
def test_index_diff_rejects_stale_collection(client: TestClient):
    resp = client.get(
        "/api/v1/import/index/diff",
        params={"expected_milvus_collection": "document_chunks_prod"},
    )
    assert resp.status_code == 409, resp.text


@pytest.mark.skipif(not PG_AVAILABLE, reason="PostgreSQL 未启动")
def test_index_apply_refuses_deletion_outside_isolated_env(
    client: TestClient, monkeypatch
):
    """即便已确认，正式集合上的删除也必须拒绝。"""
    monkeypatch.setattr(settings, "MILVUS_COLLECTION", "document_chunks")
    monkeypatch.setattr(settings, "ENV", "dev")
    resp = client.post(
        "/api/v1/import/index/apply",
        json={
            "fix_flags": True,
            "delete_orphans": True,
            "delete_duplicates": True,
            "confirmed": True,
            "expected_milvus_collection": "document_chunks",
        },
    )
    assert resp.status_code == 403, resp.text
    assert "隔离环境" in resp.json()["detail"]


@pytest.mark.skipif(not PG_AVAILABLE, reason="PostgreSQL 未启动")
def test_general_import_requires_admin():
    """非管理员不得访问导入接口（服务端校验，不依赖前端隐藏按钮）。"""
    import src.core.deps as deps

    from src.main import app

    deps.TEST_MODE_ENABLED = False
    try:
        with TestClient(app) as guest:
            for method, path in (
                ("get", "/api/v1/import/index/diff"),
                ("get", "/api/v1/import/index/environment"),
                ("get", "/api/v1/import/templates"),
                ("post", "/api/v1/import/templates/match"),
            ):
                resp = getattr(guest, method)(
                    path, **({"json": {"tables": {}}} if method == "post" else {})
                )
                assert resp.status_code in (401, 403), f"{method} {path}: {resp.status_code}"
    finally:
        deps.TEST_MODE_ENABLED = True
