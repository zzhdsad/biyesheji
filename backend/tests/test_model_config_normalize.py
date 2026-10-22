"""模型配置规范化：存量（历史）mock 配置必须被改写为真实后端。"""

from src.application.model_config_service import normalize_backends
from src.core.config import settings


def test_legacy_mock_backends_are_replaced(monkeypatch):
    """早期版本各后端默认值是 mock，升级后必须规范化为真实实现。"""
    monkeypatch.setattr(settings, "LLM_BACKEND", "openai")
    monkeypatch.setattr(settings, "EMBEDDING_BACKEND", "flagembedding")
    monkeypatch.setattr(settings, "RERANK_BACKEND", "flagreranker")
    monkeypatch.setattr(settings, "HYDE_BACKEND", "openai")

    cfg = normalize_backends(
        {
            "llm_provider": "mock",
            "embedding_backend": "mock",
            "rerank_backend": "mock",
            "hyde_backend": "mock",
        }
    )
    assert cfg == {
        "llm_provider": "openai",
        "embedding_backend": "flagembedding",
        "rerank_backend": "flagreranker",
        "hyde_backend": "openai",
    }


def test_real_backends_are_untouched():
    """非 mock 配置保持原样（大小写不敏感地识别 mock）。"""
    cfg = {
        "llm_provider": "deepseek",
        "embedding_backend": "flagembedding",
        "rerank_backend": "flagreranker",
        "hyde_backend": "openai",
    }
    assert normalize_backends(dict(cfg)) == cfg
    # 大小写不敏感
    assert normalize_backends({"llm_provider": "Mock"})["llm_provider"] == settings.LLM_BACKEND
