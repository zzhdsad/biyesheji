"""文档解析功能测试。

单元：PDF 按页解析、切片 page_num 落库。
集成（需 PostgreSQL）：上传自动解析、手动解析/失败重试、切片查询。
"""

import io
import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.core.config import settings
from tests.test_upload import PG_AVAILABLE


def test_fallback_pdf_parse_pages_keeps_page_number():
    """PDF 降级解析器按页返回 1-based page_num（引用页码来源）。"""
    canvas_mod = pytest.importorskip("reportlab.pdfgen.canvas")
    from src.infrastructure.parser import FallbackParser

    buf = io.BytesIO()
    pdf = canvas_mod.Canvas(buf)
    pdf.drawString(72, 720, "page one content")
    pdf.showPage()
    pdf.drawString(72, 720, "page two content")
    pdf.save()

    pages = FallbackParser().parse_pages(buf.getvalue(), "pdf")
    assert [p[0] for p in pages] == [1, 2]
    assert "page one" in pages[0][1]
    assert "page two" in pages[1][1]


@pytest.mark.anyio
async def test_parse_service_writes_chunk_page_num():
    """解析服务必须把 parse_pages 的页码写入 Chunk.page_num。"""
    from src.application.parse_service import ParseService
    from src.domain.models import Chunk, Document

    doc = Document(
        id=uuid.uuid4(),
        kb_id=uuid.uuid4(),
        file_name="a.pdf",
        file_type="pdf",
        storage_path="a.pdf",
        parse_status="pending",
    )

    class _Parser:
        def parse_pages(self, content, file_type):
            return [
                (1, "第一页内容。" * 40),
                (2, "第二页内容。" * 40),
            ]

    class _Storage:
        def load(self, path):
            return b"fake"

    db = MagicMock()
    db.get = AsyncMock(return_value=doc)
    db.execute = AsyncMock()
    db.commit = AsyncMock()
    db.refresh = AsyncMock()
    db.add = MagicMock()

    await ParseService(db, storage=_Storage(), parser=_Parser()).run(doc.id)

    chunks = [call.args[0] for call in db.add.call_args_list if isinstance(call.args[0], Chunk)]
    assert chunks
    assert {c.page_num for c in chunks} == {1, 2}
    assert [c.chunk_index for c in chunks] == list(range(len(chunks)))


def _make_kb(client, visibility: str = "private") -> str:
    resp = client.post("/api/v1/kb", json={"name": "解析集成测试库", "visibility": visibility})
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


# ---------- TXT 全链路：上传 → 自动解析 → 切片入库 ----------


@pytest.mark.skipif(not PG_AVAILABLE, reason="PostgreSQL 未启动")
def test_upload_txt_auto_parse_and_chunks(client):
    """TXT 上传后自动解析：解析→切片→向量化流水线，最终状态 completed，chunks 入库且 token_count 受控。"""
    kb_id = _make_kb(client)

    # ≈ 1500 tokens 的 4 段文本（无标题 → title_path=None；有标题 → 保留路径）
    paragraphs = [f"第{i}段，" + "内容" * 250 + "。" for i in range(4)]
    long_text = "# 测试文档\n\n" + "\n\n".join(paragraphs)

    resp = client.post(
        "/api/v1/documents/upload",
        data={"kb_id": kb_id},
        files={"file": ("测试文档.txt", long_text.encode("utf-8"), "text/plain")},
    )
    assert resp.status_code == 201, resp.text
    doc_id = resp.json()["id"]

    # TestClient 中 BackgroundTasks 在响应后同步执行，此时解析+向量化应已完成
    detail = client.get(f"/api/v1/documents/{doc_id}").json()
    assert detail["parse_status"] == "completed", detail
    assert detail["chunk_count"] > 1
    assert detail["error_message"] == ""

    # 切片校验
    chunks = client.get(f"/api/v1/documents/{doc_id}/chunks").json()
    assert len(chunks) == detail["chunk_count"]
    assert [c["chunk_index"] for c in chunks] == list(range(len(chunks)))
    for c in chunks:
        assert c["doc_id"] == doc_id
        assert c["content"].strip()
        # 上限：chunk_size + overlap（TECH_DESIGN 512~1024）
        assert c["token_count"] <= settings.CHUNK_SIZE_TOKENS + settings.CHUNK_OVERLAP_TOKENS
    # title_path 保留标题层级
    assert chunks[0]["title_path"] == "测试文档"
    # 相邻块重叠
    if len(chunks) >= 2:
        assert chunks[1]["content"][:20] in chunks[0]["content"]


@pytest.mark.skipif(not PG_AVAILABLE, reason="PostgreSQL 未启动")
def test_manual_parse_and_failure_path(client):
    """手动解析端点 202；无效 PDF 走失败路径：status=failed + error_message。"""
    kb_id = _make_kb(client)
    resp = client.post(
        "/api/v1/documents/upload",
        data={"kb_id": kb_id},
        files={"file": ("fake.pdf", b"%PDF-1.4 not a real pdf", "application/pdf")},
    )
    assert resp.status_code == 201
    doc_id = resp.json()["id"]

    # 上传时自动解析失败（假 PDF）→ failed；重新触发仍失败但不抛 5xx
    detail = client.get(f"/api/v1/documents/{doc_id}").json()
    assert detail["parse_status"] == "failed"
    assert detail["error_message"]

    again = client.post(f"/api/v1/documents/{doc_id}/parse")
    assert again.status_code == 202
    detail = client.get(f"/api/v1/documents/{doc_id}").json()
    assert detail["parse_status"] == "failed"
    assert detail["error_message"]

    # 失败文档不应有切片
    chunks = client.get(f"/api/v1/documents/{doc_id}/chunks").json()
    assert chunks == []


@pytest.mark.skipif(not PG_AVAILABLE, reason="PostgreSQL 未启动")
def test_reparse_is_idempotent(client):
    """重复解析幂等：chunks 重建而非追加。"""
    kb_id = _make_kb(client)
    text = "标题\n\n" + "\n\n".join(f"第{i}段，" + "内容" * 200 + "。" for i in range(3))
    resp = client.post(
        "/api/v1/documents/upload",
        data={"kb_id": kb_id},
        files={"file": ("幂等.txt", text.encode("utf-8"), "text/plain")},
    )
    doc_id = resp.json()["id"]
    first = client.get(f"/api/v1/documents/{doc_id}/chunks").json()
    assert first

    client.post(f"/api/v1/documents/{doc_id}/parse")
    second = client.get(f"/api/v1/documents/{doc_id}/chunks").json()
    assert len(second) == len(first)
    assert [c["content"] for c in second] == [c["content"] for c in first]


def test_parse_missing_document_returns_404(client):
    import uuid

    resp = client.post(f"/api/v1/documents/{uuid.uuid4()}/parse")
    assert resp.status_code == 404
    chunks = client.get(f"/api/v1/documents/{uuid.uuid4()}/chunks")
    assert chunks.status_code == 404
