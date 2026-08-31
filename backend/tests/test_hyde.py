"""HyDE 查询改写测试：MockHyDE 行为、开关、改写对检索查询生效。"""

import time

import src.application.rag_service as rag_module
from src.core.config import settings
from src.infrastructure import hyde
from tests.test_upload import PG_AVAILABLE  # noqa: F401


# ---------- 单元：MockHyDE ----------


def test_mock_hyde_deterministic_and_differs_from_question():
    """MockHyDE：相同问题输出恒定，且与原问题文本不同。"""
    hyder = hyde.MockHyDE()
    q = "公司考勤制度？"
    a1 = hyder.generate(q)
    a2 = hyder.generate(q)
    assert a1 == a2  # 确定性
    assert a1 != q  # 改写后的文本与原问题不同
    assert "假设性回答" in a1


def test_get_hyde_disabled_returns_none(monkeypatch):
    """HYDE_ENABLED=False 时工厂返回 None。"""
    monkeypatch.setattr(settings, "HYDE_ENABLED", False)
    hyde.set_hyde(None)
    assert hyde.get_hyde() is None


def test_get_hyde_enabled_mock(monkeypatch):
    """HYDE_ENABLED + mock 后端 → 返回 MockHyDE 实例。"""
    monkeypatch.setattr(settings, "HYDE_ENABLED", True)
    monkeypatch.setattr(settings, "HYDE_BACKEND", "mock")
    hyde.set_hyde(None)
    h = hyde.get_hyde()
    assert isinstance(h, hyde.MockHyDE)


# ---------- 集成：改写对检索查询生效 ----------


def _make_kb(client) -> str:
    return client.post("/api/v1/kb", json={"name": "HyDE 测试库"}).json()["id"]


def _upload(client, kb_id, name, text):
    return client.post(
        "/api/v1/documents/upload",
        data={"kb_id": kb_id},
        files={"file": (name, text.encode("utf-8"), "text/plain")},
    ).json()


def _ask(client, question, kb_id):
    return client.post(
        "/api/v1/chat/ask",
        json={"question": question, "kb_ids": [kb_id]},
    ).json()


def test_hyde_rewrites_retrieval_query(client, monkeypatch):
    """开启 HyDE 后，embedding 收到的检索文本是假设答案而非原问题。"""
    # 开启 HyDE（mock）并保存原状态以便还原
    monkeypatch.setattr(settings, "HYDE_ENABLED", True)
    monkeypatch.setattr(settings, "HYDE_BACKEND", "mock")
    orig_hyde = hyde._hyde
    hyde.set_hyde(hyde.MockHyDE())

    # 在 embedding.encode 上埋点，捕获送入检索的查询文本
    real_emb = rag_module.get_embedding()
    captured: list[str] = []
    orig_encode = real_emb.encode  # 替换前先捕获原始方法，避免递归

    def spy_encode(texts):
        captured.append(texts[0])
        return orig_encode(texts)

    real_emb.encode = spy_encode  # type: ignore[assignment]
    monkeypatch.setattr(rag_module, "get_embedding", lambda: real_emb)

    try:
        kb_id = _make_kb(client)
        _upload(client, kb_id, "考勤.txt", "# 考勤\n公司考勤制度规定每日工作八小时。")
        time.sleep(1)

        question = "公司考勤制度？"
        _ask(client, question, kb_id)

        assert captured, "embedding.encode 应被调用"
        # 检索用的是假设答案，不是原问题
        assert captured[-1] != question
        assert "假设性回答" in captured[-1]
    finally:
        hyde.set_hyde(orig_hyde)


def test_hyde_disabled_uses_raw_question(client, monkeypatch):
    """关闭 HyDE 时，embedding 收到的就是原始问题（未改写）。"""
    monkeypatch.setattr(settings, "HYDE_ENABLED", False)
    hyde.set_hyde(None)

    real_emb = rag_module.get_embedding()
    captured: list[str] = []
    orig_encode = real_emb.encode  # 替换前先捕获原始方法，避免递归

    def spy_encode(texts):
        captured.append(texts[0])
        return orig_encode(texts)

    real_emb.encode = spy_encode  # type: ignore[assignment]
    monkeypatch.setattr(rag_module, "get_embedding", lambda: real_emb)

    kb_id = _make_kb(client)
    _upload(client, kb_id, "考勤.txt", "# 考勤\n公司考勤制度规定每日工作八小时。")
    time.sleep(1)

    question = "公司考勤制度？"
    _ask(client, question, kb_id)

    assert captured, "embedding.encode 应被调用"
    assert captured[-1] == question  # 未改写，原样送入
