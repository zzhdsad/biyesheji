"""RAG 问答功能测试。

conftest 自动注入：mock 向量化 + mock LLM + 向量库（Milvus 可用→真实，否则内存）。
覆盖：检索引用溯源、多轮对话会话复用、空检索兜底（防幻觉）、参数校验、会话持久化。
"""

import asyncio
import uuid

import pytest

from src.infrastructure import redis_client
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
    """有资料的提问：答案带 [citation: 编号, 页码] 标注，citations 含文档名/标题路径/分数。"""
    kb_id = _make_kb(client)
    _upload_doc(client, kb_id, "考勤制度.txt")

    result = _ask(client, "公司实行什么工时制度？", kb_id)
    assert result["answer"]
    assert "[citation: 1" in result["answer"], "答案应含引用标注"
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
def test_history_cache_populated_and_hit(client):
    """缓存读路径：第一轮后缓存写入；删除缓存后续问触发回源 PG + 回填。

    用 InMemory 缓存（确定性、无 redis async 循环绑定问题），验证 RagService↔cache 接线。
    """
    inmem = redis_client.InMemoryConversationCache()
    original = redis_client.get_conversation_cache()  # 模块级单例，便于还原
    redis_client.set_conversation_cache(inmem)
    try:
        kb_id = _make_kb(client)
        _upload_doc(client, kb_id)

        conv_id = _ask(client, "标准工时是多少？", kb_id)["conversation_id"]
        # 第一轮后缓存应含 user + assistant 两条
        history = asyncio.run(inmem.get_history(conv_id))
        assert len(history) == 2
        assert [h["role"] for h in history] == ["user", "assistant"]

        # 删除缓存键 → 续问时未命中 → 回源 PG（2 条）→ 回填 + 追加本轮 2 条 = 4 条
        asyncio.run(inmem.delete(conv_id))
        assert asyncio.run(inmem.get_history(conv_id)) == []

        _ask(client, "那周末呢？", kb_id, conversation_id=conv_id)
        history_after = asyncio.run(inmem.get_history(conv_id))
        assert len(history_after) == 4  # 回源 2 条 + 本轮 2 条
        assert [h["role"] for h in history_after] == ["user", "assistant", "user", "assistant"]
    finally:
        redis_client.set_conversation_cache(original)  # 还原模块级单例


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


# ---------- 单元：引用标注格式解析 ----------


@pytest.mark.anyio
async def test_citation_pattern_parses_various_forms():
    """[citation: n, p] 正则兼容有无空格、页码可选、大小写，且不误匹配旧 [n]。"""
    from src.application.rag_service import _CITATION_PATTERN

    assert _CITATION_PATTERN.search("答案[citation: 1, 3]").group(1) == "1"
    assert _CITATION_PATTERN.search("答案[citation:1,3]").group(1) == "1"
    assert _CITATION_PATTERN.search("[citation: 2]").group(1) == "2"
    assert _CITATION_PATTERN.search("[CITATION: 2, 5]").group(1) == "2"
    assert _CITATION_PATTERN.search("答案[1]") is None  # 旧格式不匹配


@pytest.mark.anyio
async def test_build_citations_resolves_index_and_fallback():
    """_build_citations：解析标记→对应来源（页码取权威值）；无标记→全部来源兜底。"""
    from src.application.rag_service import RagService

    svc = RagService.__new__(RagService)  # 绕过 __init__，无需依赖项
    hits = [
        {"id": "c1", "doc_id": "d1", "doc_name": "手册.pdf", "page_num": 3,
         "title_path": "考勤", "content": "工时八小时", "score": 0.9},
        {"id": "c2", "doc_id": "d2", "doc_name": "报销.pdf", "page_num": 1,
         "title_path": "报销", "content": "报销流程", "score": 0.5},
    ]
    # 命中标记 1 → 仅返回第 1 个来源
    cits = await svc._build_citations("回答[citation: 1, 3]继续", hits)
    assert len(cits) == 1
    assert cits[0]["doc_name"] == "手册.pdf"
    assert cits[0]["page_num"] == 3
    assert cits[0]["chunk_id"] == "c1"
    # 无标记 → 兜底全部来源
    cits2 = await svc._build_citations("回答无标记", hits)
    assert len(cits2) == 2
    # 编号越界忽略
    cits3 = await svc._build_citations("[citation: 9, 1]", hits)
    assert len(cits3) == 2  # 越界无匹配 → 兜底全部


# ---------- 单元：Citation / Evidence Pydantic 模型保留 relevance_score（BUG-（相关度 3%））----------


def test_citation_pydantic_preserves_relevance_score():
    """Citation(**c) 必须保留 evidence dict 里的 relevance_score 字段，不能静默丢弃。

    修复前：Citation 模型未声明 relevance_score，Pydantic 默认忽略未声明字段 →
    /chat/ask 同步路径下的 citations / evidence 列表里 relevance_score 永远是 None，
    前端 EvidencePanel 只能拿到 ev.score（reranker sigmoid 0.0297）→ 显示「相关度 3%」。
    修复后：声明 relevance_score: float | None = None → 同步路径能直接下发给前端。
    """
    from src.api.routes.chat import Citation, Evidence

    base_dict = {
        "chunk_id": "c1",
        "source_index": 1,
        "doc_id": "d1",
        "doc_name": "丹参.pdf",
        "page_num": 5,
        "title_path": "丹参·活血",
        "content": "丹参活血祛瘀",
        "score": 0.0297,  # ev.score 仍是 reranker sigmoid
        "source_type": "经典古籍",
        "era": "汉",
        "credibility_level": 3,
        "source_kind": "document",
        "evidence_level": "high",
        "resource_type": None,
        "resource_id": None,
        "resource_name": None,
        "evidence_id": "e1",
        "source_id": "d1",
        "source_name": "丹参.pdf",
        "source_label": "文档",
        "evidence_text": "丹参活血祛瘀",
    }
    # dense=0.85, rerank=0.0297 → relevance_score 必须保留到 Pydantic 输出
    c = Citation(**{**base_dict, "relevance_score": 0.85})
    assert c.relevance_score == 0.85
    assert c.score == 0.0297  # ev.score 仍是 reranker，量纲未被改写

    # Evidence 继承 Citation：同样保留 relevance_score
    e = Evidence(**{**base_dict, "relevance_score": 0.85})
    assert e.relevance_score == 0.85

    # 缺失 relevance_score（历史消息 / 旧 fixture / 缺 dense 的命中） → 默认为 None
    c_legacy = Citation(**{**base_dict})
    assert c_legacy.relevance_score is None

    # model_dump 后 JSON 序列化输出仍带 relevance_score 字段（前端 type 已有）
    dumped = c.model_dump()
    assert dumped["relevance_score"] == 0.85
    dumped_legacy = c_legacy.model_dump()
    assert "relevance_score" in dumped_legacy
    assert dumped_legacy["relevance_score"] is None


def test_citation_pydantic_relevance_score_null_compatible_with_legacy_data():
    """缺失 relevance_score（历史数据） → 显式传 None 与字段缺省等价，向前兼容。

    旧客户端（如浏览器缓存的旧会话页面）解析 citations 时若严格校验字段类型，
    必须保证 relevance_score=null 与字段缺省都能被接受（TypeScript 端为
    `relevance_score?: number | null`）。
    """
    from src.api.routes.chat import Citation

    base = {
        "chunk_id": "c1", "source_index": 1, "doc_id": "d1", "doc_name": "x",
        "content": "x", "score": 0.5,
    }
    c_default = Citation(**base)
    c_explicit_null = Citation(**{**base, "relevance_score": None})
    c_explicit_zero = Citation(**{**base, "relevance_score": 0.0})

    assert c_default.relevance_score is None
    assert c_explicit_null.relevance_score is None
    assert c_explicit_zero.relevance_score == 0.0  # 0.0 也是合法 dense cosine


# ---------- 单元：MockLLM 防幻觉行为 ----------


@pytest.mark.anyio
async def test_mock_llm_without_context_refuses():
    from src.infrastructure.llm import MockLLM

    answer = await MockLLM().chat([{"role": "user", "content": "随便一个问题"}])
    assert "无法回答" in answer


@pytest.mark.anyio
async def test_mock_llm_with_context_cites():
    from src.infrastructure.llm import MockLLM

    content = (
        "公司实行标准工时制度。\n\n"
        "参考资料（编号即引用标识）：\n"
        "[1] 文档：手册.pdf | 页码：3 | 标题：考勤\n工时八小时"
    )
    answer = await MockLLM().chat([{"role": "user", "content": content}])
    assert "[citation: 1" in answer
