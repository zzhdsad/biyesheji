"""大模型客户端抽象：OpenAI 兼容接口（vLLM/Qwen）/ mock 可切换（依赖倒置）。

TECH_DESIGN：LLM 通过 vLLM 提供 OpenAI 兼容接口，AWQ 4bit 量化部署。
"""

import re
from abc import ABC, abstractmethod

import httpx
from loguru import logger

from src.core.config import settings


class LLMError(Exception):
    """LLM 层异常。"""


Message = dict  # {"role": "system"|"user"|"assistant", "content": str}


class BaseLLM(ABC):
    """大模型接口：OpenAI messages 格式输入，纯文本输出。"""

    @abstractmethod
    async def chat(self, messages: list[Message]) -> str:
        """生成回答。"""


class OpenAICompatibleLLM(BaseLLM):
    """OpenAI 兼容 /chat/completions（vLLM、Ollama 等均支持）。"""

    def __init__(self, base_url: str | None = None, model: str | None = None) -> None:
        self._base_url = (base_url or settings.LLM_BASE_URL).rstrip("/")
        self._model = model or settings.LLM_MODEL
        self._api_key = settings.LLM_API_KEY

    async def chat(self, messages: list[Message]) -> str:
        payload = {
            "model": self._model,
            "messages": messages,
            "temperature": 0.1,  # 事实型问答低温，减少幻觉
            "max_tokens": 1024,
        }
        headers = {"Authorization": f"Bearer {self._api_key}"} if self._api_key else {}
        try:
            async with httpx.AsyncClient(timeout=settings.LLM_TIMEOUT_SECONDS) as client:
                resp = await client.post(
                    f"{self._base_url}/chat/completions",
                    json=payload,
                    headers=headers,
                )
                resp.raise_for_status()
        except httpx.HTTPError as exc:
            raise LLMError(f"LLM 请求失败（{self._base_url}）：{exc}") from exc
        try:
            return resp.json()["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError, ValueError) as exc:
            raise LLMError(f"LLM 响应格式异常：{exc}") from exc


class MockLLM(BaseLLM):
    """确定性假回答（开发/测试）：验证 RAG 流程而不依赖真实模型。

    行为与防幻觉约束一致：有参考资料时生成带 [n] 引用标注的回答；
    无资料时明确回答"不知道"。
    """

    _CTX_MARKER = "参考资料："

    async def chat(self, messages: list[Message]) -> str:
        user_content = next(
            (m["content"] for m in reversed(messages) if m["role"] == "user"), ""
        )
        parts = user_content.split(self._CTX_MARKER, 1)
        context = parts[1].strip() if len(parts) == 2 else ""
        if not context:
            return "根据现有资料，我无法回答该问题。建议您补充相关文档后再试。"
        # 提取首个上下文块，生成带引用标注的确定性回答
        first = re.search(r"\[1\]\s*(.+)", context)
        snippet = first.group(1)[:50].strip() if first else context[:50]
        question = parts[0].strip().splitlines()[0][:50]
        return (
            f"（mock 回答）关于「{question}」：根据参考资料，"
            f"{snippet}……[1]"
        )


def get_llm() -> BaseLLM:
    """按 LLM_BACKEND 创建客户端（工厂）。"""
    backend = settings.LLM_BACKEND
    if backend == "openai":
        return OpenAICompatibleLLM()
    if backend == "mock":
        return MockLLM()
    raise LLMError(f"未知 LLM_BACKEND：{backend}")
