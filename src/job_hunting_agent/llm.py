"""LLM 适配器边界。

当前 MVP 不绑定任何具体大模型供应商。业务代码只依赖 `LLMClient`
这个极小接口：输入 prompt，返回文本。以后无论接 OpenAI、DeepSeek、
本地模型还是 LangChain 封装，都应该在这个模块里新增适配器，
不要让业务流程直接依赖某个厂商 SDK。
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from collections.abc import Callable
from typing import Protocol

from .config import LLMSettings


class LLMClient(Protocol):
    """业务层需要的最小 LLM 能力。

    这里故意不暴露 temperature、model、messages 等供应商细节；这些配置应由
    具体适配器内部处理。这样 `resume_writer.py` 可以专注真实性边界。
    """

    def complete(self, prompt: str) -> str:
        """根据 prompt 生成文本。"""


class StaticLLMClient:
    """测试或演示用的静态 LLM。

    它不会联网，也不需要 API Key。真实供应商适配器接入前，可以用它验证
    “LLM 输出进入业务流程后会被安全检查”这件事。
    """

    def __init__(self, response: str):
        """保存固定响应文本。"""

        self.response = response

    def complete(self, prompt: str) -> str:
        """忽略 prompt，直接返回固定文本。"""

        return self.response


class LLMRequestError(RuntimeError):
    """调用真实模型接口失败时抛出的业务异常。"""


class DeepSeekChatClient:
    """DeepSeek V4 Pro 的 OpenAI-compatible ChatCompletions 适配器。

    这里不写死 API Key、base URL 或模型名；它们都由 `LLMSettings` 传入。
    DeepSeek 官方接口兼容 OpenAI ChatCompletions 形态，所以后续如果接其他
    OpenAI-compatible 供应商，也可以复用这类实现。
    """

    def __init__(
        self,
        api_key: str,
        base_url: str,
        model: str,
        timeout_seconds: int = 60,
        thinking: str | None = None,
        reasoning_effort: str | None = None,
        transport: Callable[[str, dict[str, str], dict[str, object], int], dict[str, object]] | None = None,
    ):
        """保存模型调用配置。

        `transport` 是测试缝：生产环境使用 urllib；测试里注入假传输函数，
        就能验证请求结构而不访问真实网络。
        """

        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout_seconds = timeout_seconds
        self.thinking = thinking
        self.reasoning_effort = reasoning_effort
        self.transport = transport or post_json
        self.chat_completions_url = normalize_chat_completions_url(base_url)

    @classmethod
    def from_settings(cls, settings: LLMSettings) -> "DeepSeekChatClient":
        """从统一配置对象创建 DeepSeek 客户端。"""

        return cls(
            api_key=settings.api_key,
            base_url=settings.base_url,
            model=settings.model,
            timeout_seconds=settings.timeout_seconds,
            thinking=settings.thinking,
            reasoning_effort=settings.reasoning_effort,
        )

    def complete(self, prompt: str) -> str:
        """调用 DeepSeek ChatCompletions 接口并返回助手文本。"""

        payload: dict[str, object] = {
            "model": self.model,
            "messages": [{"role": "user", "content": prompt}],
        }
        # DeepSeek V4 Pro 支持 thinking 配置；只有 `.env` 显式配置时才发送，
        # 避免把供应商特有参数强加给所有模型。
        if self.thinking:
            payload["thinking"] = {"type": self.thinking}
        if self.reasoning_effort:
            payload["reasoning_effort"] = self.reasoning_effort

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        response = self.transport(self.chat_completions_url, headers, payload, self.timeout_seconds)
        return extract_chat_completion_text(response)


def build_llm_client(settings: LLMSettings) -> LLMClient:
    """根据配置创建 LLM 客户端。

    当前只实现 DeepSeek；后续如果要换模型，可以新增 provider 分支，但业务层仍然
    只依赖 `LLMClient.complete()`。
    """

    if settings.provider == "deepseek":
        return DeepSeekChatClient.from_settings(settings)
    raise ValueError(f"暂不支持的 LLM provider：{settings.provider}")


def normalize_chat_completions_url(base_url: str) -> str:
    """把 `.env` 中的 base URL 转成 ChatCompletions URL。

    允许使用者在 `.env` 中写供应商根地址、版本化根地址，或者直接写完整的
    `/chat/completions` 地址。
    """

    stripped = base_url.rstrip("/")
    if stripped.endswith("/chat/completions"):
        return stripped
    return f"{stripped}/chat/completions"


def post_json(
    url: str,
    headers: dict[str, str],
    payload: dict[str, object],
    timeout: int,
) -> dict[str, object]:
    """用标准库发送 JSON POST 请求。

    为了让项目先保持轻依赖，这里不用 requests 或供应商 SDK。以后如果接 LangChain，
    可以新增一个 LangChain 适配器而不影响业务层。
    """

    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace")
        raise LLMRequestError(f"LLM API HTTP {error.code}: {detail}") from error
    except urllib.error.URLError as error:
        raise LLMRequestError(f"LLM API 请求失败：{error.reason}") from error


def extract_chat_completion_text(response: dict[str, object]) -> str:
    """从 ChatCompletions 响应中提取助手正文。"""

    choices = response.get("choices")
    if not isinstance(choices, list) or not choices:
        raise LLMRequestError("LLM API 响应缺少 choices")
    first_choice = choices[0]
    if not isinstance(first_choice, dict):
        raise LLMRequestError("LLM API choices[0] 格式异常")
    message = first_choice.get("message")
    if not isinstance(message, dict):
        raise LLMRequestError("LLM API 响应缺少 message")
    content = message.get("content")
    if not isinstance(content, str):
        raise LLMRequestError("LLM API 响应缺少文本 content")
    return content
