"""SSE 流式问答测试。

覆盖：
- MockLLM.chat_stream 切片拼接等价于 chat()
- _hits_to_citations 全量来源、1-based 编号
- /chat/ask-stream 事件序列 start → citations → delta×N → done
- kb_ids 为空返回 422；空知识库走防幻觉路径（无法回答 + 无引用）
"""

import json
import uuid

import pytest

from tests.test_chat import _upload_doc
from tests.test_parse import _make_kb
from tests.test_upload import PG_AVAILABLE


def _parse_sse(text: str) -> list[tuple[str, dict]]:
    """解析 SSE 文本流为 [(event, data_dict), ...]。"""
    events: list[tuple[str, dict]] = []
    event = ""
    data = ""
    for line in text.split("\n"):
        if line.startswith("event: "):
            event = line[7:].strip()
        elif line.startswith("data: "):
            data += line[6:]
        elif line == "":
            if event and data:
                events.append((event, json.loads(data)))
            event = ""
            data = ""
    return events


def _ask_stream(client, question: str, kb_id: str, conversation_id: str | None = None):
    payload = {"question": question, "kb_ids": [kb_id]}
    if conversation_id:
        payload["conversation_id"] = conversation_id
    resp = client.post("/api/v1/chat/ask-stream", json=payload)
    assert resp.status_code == 200, resp.text
    return _parse_sse(resp.text)


# ---------- 单元：MockLLM 流式 ----------


@pytest.mark.anyio
async def test_mock_llm_chat_stream_concat_equals_chat():
    """MockLLM.chat_stream 各 chunk 拼接后应与 chat() 完全一致。"""
    from src.infrastructure.llm import MockLLM

    messages = [
        {"role": "user", "content": (
            "公司实行什么工时？\n\n"
            "参考资料（编号即引用标识）：\n"
            "[1] 文档：手册.pdf | 页码：3 | 标题：考勤\n每日工作八小时。"
        )}
    ]
    full = await MockLLM().chat(messages)
    chunks: list[str] = []
    async for chunk in MockLLM().chat_stream(messages):
        chunks.append(chunk)
    assert "".join(chunks) == full


@pytest.mark.anyio
async def test_mock_llm_chat_stream_empty_context_refuses():
    """无参考资料时流式也应输出防幻觉回答。"""
    from src.infrastructure.llm import MockLLM

    chunks: list[str] = []
    async for chunk in MockLLM().chat_stream([{"role": "user", "content": "无关问题"}]):
        chunks.append(chunk)
    assert "无法回答" in "".join(chunks)


# ---------- 单元：_hits_to_citations ----------


@pytest.mark.anyio
async def test_hits_to_citations_returns_all_with_1based_index():
    """_hits_to_citations 返回全部命中，source_index 从 1 递增。"""
    from src.application.rag_service import RagService

    svc = RagService.__new__(RagService)  # 绕过 __init__
    hits = [
        {"id": "c1", "doc_id": "d1", "doc_name": "手册.pdf", "page_num": 3,
         "title_path": "考勤", "content": "工时八小时", "score": 0.9},
        {"id": "c2", "doc_id": "d2", "doc_name": "报销.pdf", "page_num": 1,
         "title_path": "报销", "content": "报销流程", "rerank_score": 0.6},
    ]
    cits = svc._hits_to_citations(hits)
    assert len(cits) == 2
    assert [c["source_index"] for c in cits] == [1, 2]
    assert cits[0]["chunk_id"] == "c1" and cits[1]["chunk_id"] == "c2"
    # rerank_score 优先于 score
    assert cits[1]["score"] == 0.6


# ---------- 集成：/chat/ask-stream 事件序列 ----------


@pytest.mark.skipif(not PG_AVAILABLE, reason="PostgreSQL 未启动")
def test_ask_stream_event_sequence(client):
    """有资料提问：事件序列 start → citations → delta×N → done，答案含引用标注。"""
    kb_id = _make_kb(client)
    _upload_doc(client, kb_id, "考勤制度.txt")

    events = _ask_stream(client, "公司实行什么工时制度？", kb_id)
    names = [e for e, _ in events]
    assert names[0] == "start"
    assert "conversation_id" in events[0][1]
    assert names[1] == "citations"
    assert events[1][1]["citations"], "应推送引用来源"
    assert names[-1] == "done"
    assert events[-1][1]["message_id"] and events[-1][1]["conversation_id"]

    # delta 事件拼接为完整答案
    deltas = [data for name, data in events if name == "delta"]
    assert deltas, "应至少有一个 delta"
    answer = "".join(d["content"] for d in deltas)
    assert "[citation: 1" in answer, "答案应含引用标注"

    # done 中的 conversation_id 应与 start 一致
    assert events[-1][1]["conversation_id"] == events[0][1]["conversation_id"]

    # 助手消息已持久化：会话消息列表含本轮 user/assistant
    conv_id = events[-1][1]["conversation_id"]
    messages = client.get(
        f"/api/v1/chat/conversations/{conv_id}/messages"
    ).json()
    assert [m["role"] for m in messages] == ["user", "assistant"]
    assert messages[1]["content"] == answer
    assert messages[1]["citations"]["sources"]


@pytest.mark.skipif(not PG_AVAILABLE, reason="PostgreSQL 未启动")
def test_ask_stream_without_kb_ids_returns_422(client):
    resp = client.post("/api/v1/chat/ask-stream", json={"question": "问题"})
    assert resp.status_code == 422
    assert "kb_ids" in resp.json()["message"]


@pytest.mark.skipif(not PG_AVAILABLE, reason="PostgreSQL 未启动")
def test_ask_stream_empty_kb_returns_unable(client):
    """空知识库提问：防幻觉路径，无 citations 事件内容为空，答案含'无法回答'。"""
    kb_id = _make_kb(client)
    events = _ask_stream(client, "年度预算是多少？", kb_id)
    names = [e for e, _ in events]
    assert names[0] == "start"
    # 空检索：citations 事件 data 为空列表
    cit_events = [data for name, data in events if name == "citations"]
    if cit_events:
        assert cit_events[0]["citations"] == []
    deltas = [data for name, data in events if name == "delta"]
    answer = "".join(d["content"] for d in deltas)
    assert "无法回答" in answer
    assert names[-1] == "done"


@pytest.mark.skipif(not PG_AVAILABLE, reason="PostgreSQL 未启动")
def test_ask_stream_unknown_conversation_returns_404(client):
    kb_id = _make_kb(client)
    resp = client.post(
        "/api/v1/chat/ask-stream",
        json={"question": "问题", "kb_ids": [kb_id], "conversation_id": str(uuid.uuid4())},
    )
    assert resp.status_code == 404
