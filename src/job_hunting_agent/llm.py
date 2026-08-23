"""LangChain LLM 适配边界。

当前项目已经把“真实聊天模型”统一切到 LangChain ChatModel 接口：

- Agent 主流程通过 `create_agent` + `ChatOpenAI` 运行。
- 简历改写、对话入库判断等旧的“单次 prompt -> 单次文本”场景，仍然只依赖
  `LLMClient.complete()` 这个极小接口。

这样做的好处是：上层业务不用关心 DeepSeek、OpenAI-compatible、工具调用协议等
供应商细节；以后如果要换模型，只需要改 `.env` 或替换本模块适配器。
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Protocol

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_openai import ChatOpenAI

from .config import LLMSettings
from .concurrency_control import ConcurrencyControlError

logger = logging.getLogger(__name__)


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

    def __init__(
        self,
        model: BaseChatModel,
        usage_callback: Callable[[BaseMessage], None] | None = None,
    ):
        """保存底层 LangChain 聊天模型。"""

        self.model = model
        self.usage_callback = usage_callback

    def complete(self, prompt: str) -> str:
        """调用 LangChain ChatModel，并把返回结果压平成普通文本。"""

        try:
            response = self.model.invoke(prompt)
        except ConcurrencyControlError:
            raise
        except Exception as error:
            raise LLMRequestError(f"LLM 调用失败：{error}") from error
        if self.usage_callback is not None:
            # 计量失败不能让已经成功的业务调用失败；回调内部会把缺失 usage
            # 标记为 missing，并自行处理数据库异常。
            try:
                self.usage_callback(response)
            except Exception as error:  # noqa: BLE001 - 账单旁路失败不影响用户主流程。
                # 不记录模型响应或 prompt，避免可观测性日志变成新的隐私副本。
                logger.debug("LLM 用量记录失败：%s", type(error).__name__)
        return extract_message_text(response)


def build_llm_client(
    settings: LLMSettings,
    usage_callback: Callable[[BaseMessage], None] | None = None,
) -> LLMClient:
    """根据 `.env` 配置创建业务层可复用的 LLM 客户端。"""

    return LangChainLLMClient(build_chat_model(settings), usage_callback=usage_callback)


def build_chat_model(
    settings: LLMSettings,
    temperature: float = 0,
    max_retries: int = 2,
    callbacks: list[BaseCallbackHandler] | None = None,
) -> BaseChatModel:
    """根据 `.env` 配置创建标准 LangChain ChatModel。

    项目统一使用 `langchain_openai.ChatOpenAI` 适配 OpenAI-compatible 接口。
    `provider` 只作为日志和 Token 计量标签，因此 DeepSeek、OpenAI、本地服务及
    中转站都可以只改 `.env` 接入，不需要向代码白名单追加供应商名称。
    """

    extra_body: dict[str, object] = {}
    # 部分 OpenAI-compatible 服务通过额外布尔字段切换模型思考模式。
    if settings.enable_thinking is not None:
        extra_body["enable_thinking"] = settings.enable_thinking
    elif settings.thinking:
        # 兼容早期项目 `.env` 中的 DeepSeek 风格配置；新布尔键优先，避免同一请求
        # 同时携带两个供应商专属字段。
        extra_body["thinking"] = {"type": settings.thinking}

    return ChatOpenAI(
        model=settings.model,
        api_key=settings.api_key,
        base_url=normalize_openai_compatible_base_url(settings.base_url),
        timeout=settings.timeout_seconds,
        temperature=temperature,
        # 重试策略由内部 Model Gateway 传入；保留默认值以兼容直接调用本函数的测试。
        max_retries=max(0, max_retries),
        reasoning_effort=settings.reasoning_effort,
        extra_body=extra_body or None,
        # 当前接入的是 OpenAI-compatible 提供商，强制走 Chat Completions 更稳。
        use_responses_api=False,
        # Agent 网页聊天默认需要增量输出；显式开启 streaming，避免 LangGraph 只拿到完整 AIMessage。
        streaming=True,
        # 请求流式结束块携带 usage；如果供应商不支持，计量层会标记为 missing，
        # 而不是把估算值直接当成正式账单。
        stream_usage=True,
        callbacks=callbacks,
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
