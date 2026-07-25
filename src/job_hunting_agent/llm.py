"""LangChain LLM 适配边界。

当前项目已经把“真实聊天模型”统一切到 LangChain ChatModel 接口：

- Agent 主流程通过 `create_agent` + `ChatOpenAI` 运行。
- 简历改写、对话入库判断等旧的“单次 prompt -> 单次文本”场景，仍然只依赖
  `LLMClient.complete()` 这个极小接口。

这样做的好处是：上层业务不用关心 DeepSeek、OpenAI-compatible、工具调用协议等
供应商细节；以后如果要换模型，只需要改 `.env` 或替换本模块适配器。
"""

from __future__ import annotations

from typing import Any, Protocol

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_openai import ChatOpenAI

from .config import LLMSettings


class LLMClient(Protocol):
    """业务层需要的最小 LLM 能力。

    这里故意不暴露温度、响应格式、工具调用等 LangChain 细节；这些能力都由
    适配器层消化。业务代码只关心“给一段 prompt，拿回一段文本”。
    """

    def complete(self, prompt: str) -> str:
        """根据 prompt 生成文本。"""


class StaticLLMClient:
    """测试或演示用的静态 LLM。"""

    def __init__(self, response: str):
        """保存固定响应文本。"""

        self.response = response

    def complete(self, prompt: str) -> str:
        """忽略 prompt，直接返回固定文本。"""

        return self.response


class LLMRequestError(RuntimeError):
    """调用真实模型失败时抛出的业务异常。"""


class LangChainLLMClient:
    """把 LangChain ChatModel 包装成现有业务可复用的 `LLMClient`。"""

    def __init__(self, model: BaseChatModel):
        """保存底层 LangChain 聊天模型。"""

        self.model = model

    def complete(self, prompt: str) -> str:
        """调用 LangChain ChatModel，并把返回结果压平成普通文本。"""

        try:
            response = self.model.invoke(prompt)
        except Exception as error:  # noqa: BLE001 - 这里统一转成业务异常，便于上层处理。
            raise LLMRequestError(f"LLM 调用失败：{error}") from error
        return extract_message_text(response)


def build_llm_client(settings: LLMSettings) -> LLMClient:
    """根据 `.env` 配置创建业务层可复用的 LLM 客户端。"""

    return LangChainLLMClient(build_chat_model(settings))


def build_chat_model(settings: LLMSettings, temperature: float = 0) -> BaseChatModel:
    """根据 `.env` 配置创建标准 LangChain ChatModel。

    当前项目默认使用 `langchain_openai.ChatOpenAI` 去适配 OpenAI-compatible
    接口，包括 DeepSeek。这样 Agent、工具调用、消息对象、后续中间件扩展都能
    走 LangChain 标准能力，而不是手写 HTTP 请求。
    """

    if settings.provider not in {"deepseek", "openai", "openai_compatible"}:
        raise ValueError(f"暂不支持的 LLM provider：{settings.provider}")

    extra_body: dict[str, object] = {}
    # DeepSeek 的 thinking 参数不属于 OpenAI 标准字段，所以通过 extra_body 透传。
    if settings.thinking:
        extra_body["thinking"] = {"type": settings.thinking}

    return ChatOpenAI(
        model=settings.model,
        api_key=settings.api_key,
        base_url=normalize_openai_compatible_base_url(settings.base_url),
        timeout=settings.timeout_seconds,
        temperature=temperature,
        max_retries=2,
        reasoning_effort=settings.reasoning_effort,
        extra_body=extra_body or None,
        # 当前接入的是 OpenAI-compatible 提供商，强制走 Chat Completions 更稳。
        use_responses_api=False,
        # 某些兼容供应商不会返回流式 token 使用量，这里关闭以减少兼容性问题。
        stream_usage=False,
    )


def normalize_openai_compatible_base_url(base_url: str) -> str:
    """把用户配置的接口地址归一化成 ChatOpenAI 需要的 base URL。

    用户有时会把 `.env` 写成根地址，有时会写成 `/chat/completions` 这样的具体
    端点。LangChain 这里需要“根 base URL”，所以要把具体端点后缀去掉。
    """

    stripped = base_url.rstrip("/")
    for suffix in ("/chat/completions", "/responses", "/embeddings"):
        if stripped.endswith(suffix):
            return stripped[: -len(suffix)]
    return stripped


def extract_message_text(message: BaseMessage | object) -> str:
    """从 LangChain 消息对象中提取纯文本正文。

    兼容两类常见返回：

    - `AIMessage.content` 直接就是字符串。
    - `AIMessage.content` 是富文本分段列表，例如 Responses API 的文本块。
    """

    content = getattr(message, "content", message)
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
                continue
            if not isinstance(item, dict):
                continue
            text = item.get("text")
            if isinstance(text, str):
                parts.append(text)
                continue
            # 一些消息块会把正文放在嵌套字段里，这里做一次保守兼容。
            if item.get("type") in {"text", "output_text"} and isinstance(item.get("content"), str):
                parts.append(str(item["content"]))
        return "\n".join(part for part in parts if part.strip()).strip()
    if isinstance(message, AIMessage):
        return str(message.content)
    if isinstance(content, (int, float)):
        return str(content)
    raise LLMRequestError("LLM 响应缺少可读取的文本内容")


def masked_chat_model_settings(model: BaseChatModel) -> dict[str, Any]:
    """返回适合调试展示的 LangChain ChatModel 摘要。"""

    return {
        "model_name": getattr(model, "model_name", None),
        "base_url": getattr(model, "openai_api_base", None),
        "timeout": getattr(model, "request_timeout", None),
        "reasoning_effort": getattr(model, "reasoning_effort", None),
        "uses_responses_api": getattr(model, "use_responses_api", None),
    }
