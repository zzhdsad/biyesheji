"""文档上传功能测试。

单元测试（无需数据库）：文件类型/大小校验、本地存储。
集成测试（需 PostgreSQL，docker compose up -d postgres）：上传闭环。
"""

import asyncio
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from src.core.config import settings
from src.infrastructure.storage import LocalStorage
from src.main import app


def _postgres_available() -> bool:
    """用独立 engine 探测 PostgreSQL（避免污染全局连接池的事件循环绑定）。"""

    async def _ping() -> None:
        engine = create_async_engine(settings.DATABASE_URL)
        try:
            async with engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
        finally:
            await engine.dispose()

    try:
        asyncio.run(_ping())
        return True
    except Exception:
        return False


PG_AVAILABLE = _postgres_available()


def _init_db_standalone() -> None:
    """测试环境 seed 默认管理员（**不建表**，Schema 由 Alembic 管理）。

    使用独立 engine 并在结束后 dispose：不依赖应用 lifespan（TestClient
    的 portal 循环与 pytest 事件循环策略冲突时 lifespan 内 init_db 会失败）。

    变更说明：原先此处执行 Base.metadata.create_all，会把 ORM 元数据直接写进
    settings.DATABASE_URL 指向的库（指向主库时即污染主库）。现在改为校验
    目标库 + 校验 Schema 已由 Alembic 就绪，缺表则直接报错提示跑迁移。
    """
    from sqlalchemy import inspect

    from src.infrastructure.db_safety import assert_safe_database, assert_test_database

    assert_safe_database(settings.DATABASE_URL, purpose="自动化测试")
    assert_test_database(settings.DATABASE_URL, purpose="测试数据库初始化")

    async def _run() -> None:
        from src.domain.models import User

        engine = create_async_engine(settings.DATABASE_URL)
        try:
            async with engine.begin() as conn:
                if not await conn.run_sync(lambda c: inspect(c).has_table("users")):
                    raise RuntimeError(
                        "测试库缺少 users 表：Schema 只能由 Alembic 管理，"
                        "请先对测试库执行 `alembic upgrade head`"
                    )
            async with async_sessionmaker(engine)() as session:
                # 按邮箱或用户名判重：旧测试库里可能已有 username=admin 但邮箱
                # 与当前 DEFAULT_ADMIN_EMAIL 不同的种子数据，只查邮箱会撞唯一约束。
                exists = await session.scalar(
                    select(User).where(
                        (User.email == settings.DEFAULT_ADMIN_EMAIL)
                        | (User.username == "admin")
                    )
                )
                if exists is None:
                    session.add(
                        User(
                            email=settings.DEFAULT_ADMIN_EMAIL,
                            username="admin",
                            hashed_password="not-set-yet",
                            role="admin",
                        )
                    )
                    await session.commit()
        finally:
            await engine.dispose()

    asyncio.run(_run())


@pytest.fixture(scope="module")
def client():
    """module 级共享：同一事件循环内完成 lifespan + 全部集成用例。"""
    if PG_AVAILABLE:
        _init_db_standalone()
    with TestClient(app) as c:
        yield c


# ---------- 单元测试（不依赖数据库） ----------


def test_upload_rejects_invalid_extension(client):
    """非白名单类型返回 400，且不触达数据库。"""
    resp = client.post(
        "/api/v1/documents/upload",
        data={"kb_id": str(uuid.uuid4())},
        files={"file": ("malware.exe", b"MZ fake", "application/octet-stream")},
    )
    assert resp.status_code == 400
    assert "不支持" in resp.json()["message"]


def test_upload_rejects_unparseable_office_formats(client):
    """未安装 Docling 时，PPTX/XLSX 不应被接受为可解析文档。"""
    for name, content in (
        ("slides.pptx", b"PK\x03\x04fake-pptx"),
        ("table.xlsx", b"PK\x03\x04fake-xlsx"),
    ):
        resp = client.post(
            "/api/v1/documents/upload",
            data={"kb_id": str(uuid.uuid4())},
            files={"file": (name, content, "application/octet-stream")},
        )
        assert resp.status_code == 400, resp.text
        assert "仅支持 PDF/DOCX/TXT/MD" in resp.json()["message"]


def test_upload_rejects_empty_file(client):
    resp = client.post(
        "/api/v1/documents/upload",
        data={"kb_id": str(uuid.uuid4())},
        files={"file": ("empty.pdf", b"", "application/pdf")},
    )
    assert resp.status_code == 400
    assert "为空" in resp.json()["message"]


@pytest.mark.skipif(not PG_AVAILABLE, reason="PostgreSQL 未启动")
def test_upload_rejects_oversized_file(client, monkeypatch):
    """超过大小限制返回 400。"""
    resp = client.post("/api/v1/kb", json={"name": f"上传大小KB-{uuid.uuid4().hex[:8]}"})
    assert resp.status_code == 201, resp.text
    kb_id = resp.json()["id"]

    monkeypatch.setattr(
        "src.application.document_service.get_system_value",
        lambda key: 1 if key == "max_file_size_mb" else settings.MAX_FILE_SIZE_MB,
    )
    big = b"x" * (1024 * 1024 + 1)
    resp = client.post(
        "/api/v1/documents/upload",
        data={"kb_id": kb_id},
        files={"file": ("big.pdf", big, "application/pdf")},
    )
    assert resp.status_code == 400
    assert "限制" in resp.json()["message"]


def test_local_storage_roundtrip(tmp_path, monkeypatch):
    """本地存储：保存 → 读取 → 删除，并拒绝路径穿越。"""
    monkeypatch.setattr(settings, "UPLOAD_DIR", str(tmp_path))
    storage = LocalStorage()

    ref = storage.save("kb1/doc1.pdf", b"%PDF-1.4 hello")
    assert ref.endswith("doc1.pdf")
    assert storage.load("kb1/doc1.pdf") == b"%PDF-1.4 hello"

    storage.delete("kb1/doc1.pdf")
    with pytest.raises(Exception):
        storage.load("kb1/doc1.pdf")

    with pytest.raises(Exception):
        storage.save("../escape.txt", b"bad")


# ---------- 集成测试（需 PostgreSQL） ----------


@pytest.mark.skipif(not PG_AVAILABLE, reason="PostgreSQL 未启动（docker compose up -d postgres）")
def test_upload_and_list_integration(client):
    """端到端：创建知识库 → 上传 → 返回 document_id → 列表可查。"""
    kb = client.post("/api/v1/kb", json={"name": "集成测试库"})
    assert kb.status_code == 201, kb.text
    kb_id = kb.json()["id"]

    resp = client.post(
        "/api/v1/documents/upload",
        data={"kb_id": kb_id},
        files={"file": ("员工手册.pdf", b"%PDF-1.4 fake content", "application/pdf")},
    )
    assert resp.status_code == 201, resp.text
    doc = resp.json()
    uuid.UUID(doc["id"])  # 返回合法 document_id
    assert doc["file_name"] == "员工手册.pdf"
    assert doc["parse_status"] == "pending"

    listing = client.get(f"/api/v1/documents?kb_id={kb_id}").json()
    assert any(d["id"] == doc["id"] for d in listing)


@pytest.mark.skipif(not PG_AVAILABLE, reason="PostgreSQL 未启动")
def test_upload_to_missing_kb_returns_404(client):
    resp = client.post(
        "/api/v1/documents/upload",
        data={"kb_id": str(uuid.uuid4())},
        files={"file": ("a.pdf", b"%PDF-1.4", "application/pdf")},
    )
    assert resp.status_code == 404
    assert "不存在" in resp.json()["message"]
