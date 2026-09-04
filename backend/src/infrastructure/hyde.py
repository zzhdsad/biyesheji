"""HyDE（Hypothetical Document Embeddings）查询改写抽象层。

TECH_DESIGN：检索前用小模型（Qwen2.5-1.5B）根据用户问题生成一段"假设答案"，
用假设答案替换原问题进行向量检索（假设答案与真实文档的分布更接近，召回更准）。

要点：
- 仅改写"检索用的查询文本"，最终送 LLM 的 Prompt 仍用原始问题。
- 可配置开关 ``HYDE_ENABLED``；关闭时工厂返回 None，RagService 跳过改写。
- 失败回退原问题（不阻断主流程）。
"""

from __future__ import annotations

from loguru import logger

from src.core.config import settings


class HyDEError(Exception):
    """HyDE 改写失败。"""


# HyDE 引导 Prompt（用户需求 §4.5：基于问题生成"可能出现在技术文档中的答案片段"）
# 工程约束：限制字数 + 只输出文档内容、不解释反问，避免模型输出无关闲聊
_HYDE_PROMPT = (
    "请根据以下问题，写一段可能出现在技术文档中的答案片段：{question}\n"
    "要求：80-150字；只输出文档内容，不要解释、不要反问。\n"
)


class BaseHyDE:
    """HyDE 抽象：根据问题生成假设答案（供向量检索用）。"""

    def generate(self, question: str) -> str:
        raise NotImplementedError


class MockHyDE(BaseHyDE):
    """确定性伪改写（开发/测试）：生成比原问题更丰富的假设性回答文本。

    确定性：相同问题输出恒定；输出文本与原问题不同，可验证改写生效
    （MockEmbedding 对不同文本产出不同向量）。
    """

    def generate(self, question: str) -> str:
        # 模拟一段假设答案（与真实文档分布更接近），引入"规定/流程/要求"等检索友好的词
        return (
            f"（假设性回答）关于{question}，根据企业知识库相关规定，"
            f"通常需要遵循以下流程与要求：员工须按制度执行，"
            f"具体以公司正式文件为准。"
        )


class QwenHyDE(BaseHyDE):
    """Qwen2.5-1.5B（或同等小模型）假设答案生成，走 OpenAI 兼容接口。

    部署方式（任选其一，通过 HYDE_BASE_URL 配置）：
    - **vLLM**：``vllm serve Qwen/Qwen2.5-1.5B-Instruct --port 8001``
      → ``HYDE_BASE_URL=http://localhost:8001/v1``
    - **Ollama**：先 ``ollama pull qwen2.5:1.5b``，再 ``ollama serve``
      → ``HYDE_BASE_URL=http://localhost:11434/v1``（Ollama ≥0.1.x 兼容 OpenAI /v1）
      → ``HYDE_MODEL=qwen2.5:1.5b``
    - 未设置 ``HYDE_BASE_URL`` 时复用 ``LLM_BASE_URL``（与主 LLM 同一服务）。

    依赖：``pip install openai``
    """

    def __init__(self, model: str | None = None, base_url: str | None = None) -> None:
        self._model = model or settings.HYDE_MODEL
        # 优先用 HYDE_BASE_URL；为空则回退 LLM_BASE_URL（与主 LLM 同服务，节省部署）
        self._base_url = base_url or settings.HYDE_BASE_URL or settings.LLM_BASE_URL
        self._client = None  # 实例级缓存，进程内只创建一次 OpenAI client

    def _get_client(self):
        if self._client is None:
            try:
                from openai import OpenAI
            except ImportError as exc:
                raise HyDEError(
                    "未安装 openai：pip install openai"
                    "，或在 .env 设置 HYDE_BACKEND=mock"
                ) from exc
            try:
                # Ollama 不需要 api_key，传 "none" 占位即可
                self._client = OpenAI(base_url=self._base_url, api_key="none")
                logger.info(
                    f"HyDE 客户端就绪 model={self._model} base_url={self._base_url}"
                )
            except Exception as exc:
                raise HyDEError(f"HyDE 客户端初始化失败：{exc}") from exc
        return self._client

    def generate(self, question: str) -> str:
        """调用小模型生成假设答案，作为检索 Query 替代原问题。

        Args:
            question: 用户原始问题

        Returns:
            str — 假设答案文本（80-150 字，与真实文档分布更接近）

        Raises:
            HyDEError: 客户端未就绪 / 模型返回空 / 调用异常
        """
        client = self._get_client()
        try:
            resp = client.chat.completions.create(
                model=self._model,
                messages=[
                    {"role": "user", "content": _HYDE_PROMPT.format(question=question)}
                ],
                temperature=0.3,  # 低温度保证假设答案稳定，避免漂移
                max_tokens=200,
            )
            text = (resp.choices[0].message.content or "").strip()
            if not text:
                raise HyDEError("HyDE 模型返回空内容")
            return text
        except HyDEError:
            raise
        except Exception as exc:
            raise HyDEError(f"HyDE 生成失败：{exc}") from exc


# 别名：语义上接口是通用的 OpenAI 兼容，不仅限于 Qwen
OpenAIHyDE = QwenHyDE


_hyde: BaseHyDE | None = None


def get_hyde() -> BaseHyDE | None:
    """工厂：``HYDE_ENABLED`` 关闭时返回 None；开启时按 ``HYDE_BACKEND`` 注入。

    默认开启（TECH_DESIGN §4.5 / 用户需求：HYDE_ENABLED=true）；
    开发期无小模型服务时设 ``HYDE_BACKEND=mock`` 用确定性伪改写，
    或设 ``HYDE_ENABLED=false`` 完全跳过改写。
    """
    global _hyde
    if _hyde is not None:
        return _hyde
    if not settings.HYDE_ENABLED:
        return None
    backend = settings.HYDE_BACKEND.lower()
    if backend == "openai":
        _hyde = QwenHyDE()
        logger.info(
            f"HyDE 使用 OpenAI 兼容实现（model={settings.HYDE_MODEL}, "
            f"base_url={settings.HYDE_BASE_URL or 'LLM_BASE_URL'})"
        )
    else:
        _hyde = MockHyDE()
        logger.info("HyDE 使用 mock 实现（开发模式）")
    return _hyde


def set_hyde(hyde: BaseHyDE | None) -> None:
    """注入 HyDE（测试用）。传 None 可禁用。"""
    global _hyde
    _hyde = hyde
