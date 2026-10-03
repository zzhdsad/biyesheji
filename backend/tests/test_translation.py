"""证据原文翻译接口测试（POST /chat/evidence/translate）。

覆盖（问题4/5/十二）：
- 中文原文 / 少量英文术语：不调用 LLM 直接返回 is_chinese=True；
- 英文原文：调用 LLM 翻译并缓存，第二次请求 cached=True 且不再调 LLM；
- LLM 失败：503 + 统一 fallback 文案（不影响正常问答链路）；
- 长度边界：空文本 / 超长文本被 422 拒绝；
- 判定口径：_is_mostly_chinese 与前端 needsTranslation 一致。
"""

import pytest
from fastapi.testclient import TestClient

from src.api.routes.chat import _TRANSLATION_CACHE, _is_mostly_chinese


@pytest.fixture(autouse=True)
def _clear_cache():
    _TRANSLATION_CACHE.clear()
    yield
    _TRANSLATION_CACHE.clear()


class _TranslateLLM:
    """固定译文桩：记录调用次数，用于验证缓存命中后不再调用。"""

    calls = 0

    async def chat(self, messages):  # noqa: ANN001
        _TranslateLLM.calls += 1
        return "桂枝汤适用于外感风寒表虚证。"

    async def chat_stream(self, messages):  # noqa: ANN001
        yield "桂枝汤适用于外感风寒表虚证。"


class _FailingLLM:
    from src.infrastructure.llm import LLMError

    async def chat(self, messages):  # noqa: ANN001
        raise self.LLMError("llm down")

    async def chat_stream(self, messages):  # noqa: ANN001
        yield ""


@pytest.fixture()
def client_with_llm(monkeypatch):
    """构造带 LLM 桩的应用（绕过运行时模型配置读取，测试不依赖 DB 配置内容）。"""
    from src.api.routes import chat as chat_mod
    from src.application import model_config_service as mcs_mod
    from src.infrastructure import llm as llm_mod
    from src.main import app

    async def _fake_config(db):  # noqa: ANN001, ARG001
        return {"llm_base_url": "http://test", "llm_model": "test"}

    monkeypatch.setattr(mcs_mod, "get_effective_config_cached", _fake_config)
    monkeypatch.setattr(chat_mod, "_check_model_configured", _fake_config)

    def _factory(llm):
        monkeypatch.setattr(llm_mod, "get_llm", lambda config=None: llm)
        return TestClient(app)

    return _factory


def _post(client, text):  # noqa: ANN001
    return client.post("/api/v1/chat/evidence/translate", json={"text": text})


class TestMostlyChinese:
    def test_pure_chinese(self):
        assert _is_mostly_chinese("太阳中风，阳浮而阴弱。") is True

    def test_few_english_terms(self):
        # 少量英文术语的中文原文：不翻译（与前端 needsTranslation 口径一致，阈值 0.5）
        assert _is_mostly_chinese("丹参具有活血化瘀的功效，其有效成分包括 Tanshinone IIA 等。") is True

    def test_english_dominant(self):
        assert _is_mostly_chinese("Guizhi Decoction for Taiyang syndrome") is False

    def test_empty(self):
        assert _is_mostly_chinese("123 · ……") is True


class TestTranslateEndpoint:
    def test_chinese_text_skips_llm(self, client_with_llm):
        client = client_with_llm(_TranslateLLM())
        _TranslateLLM.calls = 0
        resp = _post(client, "桂枝汤适用于太阳中风证。")
        assert resp.status_code == 200
        body = resp.json()
        assert body["is_chinese"] is True
        assert body["translation"] is None
        assert _TranslateLLM.calls == 0, "中文原文不得调用 LLM"

    def test_english_text_translates_and_caches(self, client_with_llm):
        client = client_with_llm(_TranslateLLM())
        _TranslateLLM.calls = 0
        text = "Guizhi Decoction treats exterior deficiency with sweating."

        first = _post(client, text)
        assert first.status_code == 200
        first_body = first.json()
        assert first_body["is_chinese"] is False
        assert first_body["cached"] is False
        assert "桂枝汤" in first_body["translation"]
        assert _TranslateLLM.calls == 1

        second = _post(client, text)
        second_body = second.json()
        assert second.status_code == 200
        assert second_body["cached"] is True
        assert second_body["translation"] == first_body["translation"]
        assert _TranslateLLM.calls == 1, "缓存命中后不得再次调用 LLM"

    def test_llm_failure_returns_503_with_fallback(self, client_with_llm):
        client = client_with_llm(_FailingLLM())
        resp = _post(client, "English text that fails.")
        assert resp.status_code == 503
        assert "暂时无法生成中文翻译" in resp.json()["message"]

    def test_empty_text_rejected(self, client_with_llm):
        client = client_with_llm(_TranslateLLM())
        resp = _post(client, "")
        assert resp.status_code == 422

    def test_overlong_text_rejected(self, client_with_llm):
        client = client_with_llm(_TranslateLLM())
        resp = _post(client, "a" * 8001)
        assert resp.status_code == 422
