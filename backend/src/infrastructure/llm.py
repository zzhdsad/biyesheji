"""大模型客户端抽象：OpenAI 兼容接口（vLLM/Qwen）/ mock 可切换（依赖倒置）。

TECH_DESIGN：LLM 通过 vLLM 提供 OpenAI 兼容接口，AWQ 4bit 量化部署。
支持流式生成（stream=True）供 SSE 端点逐 chunk 推送答案。
"""

import asyncio
import json
import re
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator

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
        """生成回答（一次性返回完整文本）。"""

    @abstractmethod
    def chat_stream(self, messages: list[Message]) -> AsyncIterator[str]:
        """流式生成：逐 chunk yield 答案文本片段（供 SSE 推送）。"""


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

    async def chat_stream(self, messages: list[Message]) -> AsyncIterator[str]:
        """流式生成：OpenAI SSE 协议（stream=true），逐 chunk yield delta.content。"""
        payload = {
            "model": self._model,
            "messages": messages,
            "temperature": 0.1,
            "max_tokens": 1024,
            "stream": True,
        }
        headers = {"Authorization": f"Bearer {self._api_key}"} if self._api_key else {}
        try:
            async with httpx.AsyncClient(timeout=settings.LLM_TIMEOUT_SECONDS) as client:
                async with client.stream(
                    "POST",
                    f"{self._base_url}/chat/completions",
                    json=payload,
                    headers=headers,
                ) as response:
                    response.raise_for_status()
                    async for line in response.aiter_lines():
                        if not line or not line.startswith("data:"):
                            continue
                        data = line[5:].strip()
                        if data == "[DONE]":
                            break
                        try:
                            obj = json.loads(data)
                        except json.JSONDecodeError:
                            continue
                        try:
                            delta = obj["choices"][0]["delta"].get("content")
                        except (KeyError, IndexError):
                            continue
                        if delta:
                            yield delta
        except httpx.HTTPError as exc:
            raise LLMError(f"LLM 流式请求失败（{self._base_url}）：{exc}") from exc


class MockLLM(BaseLLM):
    """确定性假回答（开发/测试）：验证 RAG 流程而不依赖真实模型。

    行为与防幻觉约束一致：有参考资料时生成带 [citation: 编号, 页码] 标注的回答；
    无资料时明确回答"不知道"。
    """

    _CTX_MARKER = "参考资料"

    async def chat(self, messages: list[Message]) -> str:
        return self._build_answer(messages)

    async def chat_stream(self, messages: list[Message]) -> AsyncIterator[str]:
        """模拟流式：先构造完整 mock 答案，再按固定步长切片 yield。"""
        answer = self._build_answer(messages)
        step = 4  # 每段 4 字符，模拟逐字输出
        for i in range(0, len(answer), step):
            yield answer[i : i + step]
            await asyncio.sleep(0.02)  # 增强打字机观感

    def _build_answer(self, messages: list[Message]) -> str:
        user_content = next(
            (m["content"] for m in reversed(messages) if m["role"] == "user"), ""
        )
        parts = user_content.split(self._CTX_MARKER, 1)
        context = parts[1].strip() if len(parts) == 2 else ""
        if not context:
            return "根据现有资料，我无法回答该问题。建议您补充相关文档后再试。"
        # 首个来源块：[1] 文档：... | 页码：N | 标题：...\n<原文>
        first = re.search(r"\[1\][^\n]*\n(.+)", context)
        snippet = first.group(1)[:50].strip() if first else context[:50]
        page_m = re.search(r"页码：(\d+)", context)
        page = page_m.group(1) if page_m else "0"
        question = parts[0].strip().splitlines()[0][:50]
        return (
            f"（mock 回答）关于「{question}」：根据参考资料，"
            f"{snippet}……[citation: 1, {page}]"
        )


def get_llm() -> BaseLLM:
    """按 LLM_BACKEND 创建客户端（工厂）。"""
    backend = settings.LLM_BACKEND
    if backend == "openai":
        return OpenAICompatibleLLM()
    if backend == "mock":
        return MockLLM()
    raise LLMError(f"未知 LLM_BACKEND：{backend}")
