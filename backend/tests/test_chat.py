"""RAG 问答功能测试。

conftest 自动注入：mock 向量化 + mock LLM + 向量库（Milvus 可用→真实，否则内存）。
覆盖：检索引用溯源、多轮对话会话复用、空检索兜底（防幻觉）、参数校验、会话持久化。
"""

import uuid

import pytest

from tests.test_parse import _make_kb
from tests.test_upload import PG_AVAILABLE


def _upload_doc(client, kb_id: str, name: str = "考勤制度.txt") -> str:
    """上传并等待自动流水线（解析→切片→向量化）完成，返回 doc_id。"""
    text = "# 员工手册\n\n## 考勤制度\n\n" + ("公司实行标准工时制度，每日工作八小时。" * 30)
    resp = client.post(
        "/api/v1/documents/upload",
        data={"kb_id": kb_id},
        files={"file": (name, text.encode("utf-8"), "text/plain")},
    )
    assert resp.status_code == 201, resp.text
    doc_id = resp.json()["id"]
    detail = client.get(f"/api/v1/documents/{doc_id}").json()
    assert detail["parse_status"] == "completed", detail
    return doc_id


def _ask(client, question: str, kb_id: str, conversation_id: str | None = None) -> dict:
    payload = {"question": question, "kb_ids": [kb_id]}
    if conversation_id:
        payload["conversation_id"] = conversation_id
    resp = client.post("/api/v1/chat/ask", json=payload)
    assert resp.status_code == 200, resp.text
    return resp.json()


@pytest.mark.skipif(not PG_AVAILABLE, reason="PostgreSQL 未启动")
def test_ask_returns_answer_with_citations(client):
    """有资料的提问：答案带 [n] 标注，citations 含文档名/标题路径/分数。"""
    kb_id = _make_kb(client)
    _upload_doc(client, kb_id, "考勤制度.txt")

    result = _ask(client, "公司实行什么工时制度？", kb_id)
    assert result["answer"]
    assert result["conversation_id"] and result["message_id"]
    assert result["citations"], "答案应携带引用来源"
    top = result["citations"][0]
    assert top["doc_name"] == "考勤制度.txt"
    assert top["doc_id"] and top["chunk_id"]
    assert top["title_path"] == "员工手册 > 考勤制度"
    assert top["score"] >= 0
    assert top["content"]


@pytest.mark.skipif(not PG_AVAILABLE, reason="PostgreSQL 未启动")
def test_ask_multi_turn_reuses_conversation(client):
    """多轮对话：携带 conversation_id 续问复用同一会话，消息持久化 4 条。"""
    kb_id = _make_kb(client)
    _upload_doc(client, kb_id)

    first = _ask(client, "标准工时是多少？", kb_id)
    second = _ask(client, "那周末呢？", kb_id, conversation_id=first["conversation_id"])
    assert second["conversation_id"] == first["conversation_id"]

    messages = client.get(
        f"/api/v1/chat/conversations/{first['conversation_id']}/messages"
    ).json()
    assert len(messages) == 4  # 2 轮：user/assistant × 2
    assert [m["role"] for m in messages] == ["user", "assistant", "user", "assistant"]
    # 助手消息持久化引用 JSON
    assert messages[1]["citations"]["sources"]


@pytest.mark.skipif(not PG_AVAILABLE, reason="PostgreSQL 未启动")
def test_ask_empty_kb_returns_unable_and_no_citations(client):
    """空知识库提问（防幻觉路径）：明确告知无法回答且无引用。"""
    kb_id = _make_kb(client)
    result = _ask(client, "年度预算是多少？", kb_id)
    assert "无法回答" in result["answer"]
    assert result["citations"] == []


@pytest.mark.skipif(not PG_AVAILABLE, reason="PostgreSQL 未启动")
def test_ask_without_kb_ids_returns_422(client):
    resp = client.post("/api/v1/chat/ask", json={"question": "问题"})
    assert resp.status_code == 422
    assert "kb_ids" in resp.json()["message"]


@pytest.mark.skipif(not PG_AVAILABLE, reason="PostgreSQL 未启动")
def test_ask_unknown_conversation_returns_404(client):
    kb_id = _make_kb(client)
    resp = client.post(
        "/api/v1/chat/ask",
        json={"question": "问题", "kb_ids": [kb_id], "conversation_id": str(uuid.uuid4())},
    )
    assert resp.status_code == 404


@pytest.mark.skipif(not PG_AVAILABLE, reason="PostgreSQL 未启动")
def test_conversations_listed(client):
    kb_id = _make_kb(client)
    _upload_doc(client, kb_id)
    result = _ask(client, "提问测试", kb_id)
    convs = client.get("/api/v1/chat/conversations").json()
    assert any(c["id"] == result["conversation_id"] for c in convs)


# ---------- 单元：MockLLM 防幻觉行为 ----------


@pytest.mark.anyio
async def test_mock_llm_without_context_refuses():
    from src.infrastructure.llm import MockLLM

    answer = await MockLLM().chat([{"role": "user", "content": "随便一个问题"}])
    assert "无法回答" in answer


@pytest.mark.anyio
async def test_mock_llm_with_context_cites():
    from src.infrastructure.llm import MockLLM

    content = "公司实行标准工时制度。\n\n参考资料：\n[1] （来源：手册）\n工时八小时"
    answer = await MockLLM().chat([{"role": "user", "content": content}])
    assert "[1]" in answer
