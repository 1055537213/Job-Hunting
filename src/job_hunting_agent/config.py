"""项目配置加载。

模型 API Key、base URL、模型名等容易变化且包含敏感信息的内容，都从 `.env`
或系统环境变量读取，不写死在代码里。当前项目不依赖第三方 dotenv 包，
这里实现一个小型 `.env` 解析器，足够覆盖 `KEY=value` 这类常见配置。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping


DEFAULT_ENV_PATH = Path(".env")


@dataclass(frozen=True)
class LLMSettings:
    """LLM 供应商配置。

    `api_key` 是敏感字段，只在内存中用于请求头，CLI 和日志都不应该打印它。
    `base_url` 和 `model` 来自 `.env`，方便后续从 DeepSeek 切换到其他模型。
    """

    provider: str
    model: str
    api_key: str
    base_url: str
    timeout_seconds: int = 60
    thinking: str | None = None
    reasoning_effort: str | None = None


@dataclass(frozen=True)
class EmbeddingSettings:
    """Embedding 供应商配置。

    这组配置与聊天模型配置分开保存：很多供应商提供聊天模型但不提供 embedding，
    或者两者的计费、模型名、接口地址并不相同。
    """

    provider: str
    model: str
    api_key: str
    base_url: str
    timeout_seconds: int = 60
    batch_size: int = 64
    dimensions: int | None = None


@dataclass(frozen=True)
class AgentMemorySettings:
    """Agent 对话记忆配置。

    `restore_history_limit` 控制启动恢复时最多读取多少条 SQLite 聊天记录。
    `restore_trigger_tokens` 控制恢复历史过长时何时先压缩再交给 Agent。
    `summary_trigger_tokens` 控制 LangChain 运行中何时触发自动总结。
    `summary_keep_messages` 表示总结后保留最近多少条原文消息。
    """

    enabled: bool = True
    restore_history_limit: int = 200
    restore_trigger_tokens: int = 12000
    restore_keep_messages: int = 24
    restore_summary_chars: int = 6000
    summary_trigger_tokens: int = 12000
    summary_keep_messages: int = 24
    summary_trim_tokens: int = 6000


def load_dotenv_values(env_path: str | Path = DEFAULT_ENV_PATH) -> dict[str, str]:
    """读取 `.env` 文件并返回键值字典。

    解析器支持空行、注释、`export KEY=value` 和单双引号包裹的值。它不会把值写入
    `os.environ`，避免测试或多项目运行时互相污染。
    """

    path = Path(env_path)
    if not path.exists():
        return {}

    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line.removeprefix("export ").strip()
        key, value = line.split("=", 1)
        key = key.strip()
        value = strip_env_value(value.strip())
        if key:
            values[key] = value
    return values


def strip_env_value(value: str) -> str:
    """去掉 `.env` 值外层引号，并保留内部内容。"""

    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value


def load_llm_settings(
    env_path: str | Path = DEFAULT_ENV_PATH,
    environ: Mapping[str, str] | None = None,
) -> LLMSettings:
    """从 `.env` 和系统环境变量加载 LLM 配置。

    优先级是系统环境变量高于 `.env`。为了兼容你已有学习项目的命名，本函数同时
    支持 `JOB_AGENT_LLM_*` 和 `DEEPSEEK_*` 两套键名。
    """

    file_values = load_dotenv_values(env_path)
    environment = os.environ if environ is None else environ

    def get(*keys: str, default: str | None = None) -> str | None:
        """按优先级读取多个候选键名。"""

        for key in keys:
            if key in environment and environment[key]:
                return environment[key]
            if key in file_values and file_values[key]:
                return file_values[key]
        return default

    provider = get("JOB_AGENT_LLM_PROVIDER")
    model = get("JOB_AGENT_LLM_MODEL", "DEEPSEEK_MODEL")
    api_key = get("JOB_AGENT_LLM_API_KEY", "DEEPSEEK_API_KEY")
    base_url = get("JOB_AGENT_LLM_BASE_URL", "DEEPSEEK_BASE_URL")
    timeout = int(get("JOB_AGENT_LLM_TIMEOUT_SECONDS", default="60") or 60)
    thinking = get("JOB_AGENT_LLM_THINKING")
    reasoning_effort = get("JOB_AGENT_LLM_REASONING_EFFORT")

    if not provider:
        raise ValueError("缺少 LLM provider：请在 .env 中配置 JOB_AGENT_LLM_PROVIDER")
    if not api_key:
        raise ValueError("缺少 LLM API Key：请在 .env 中配置 JOB_AGENT_LLM_API_KEY 或 DEEPSEEK_API_KEY")
    if not base_url:
        raise ValueError("缺少 LLM base URL：请在 .env 中配置 JOB_AGENT_LLM_BASE_URL 或 DEEPSEEK_BASE_URL")
    if not model:
        raise ValueError("缺少 LLM 模型名：请在 .env 中配置 JOB_AGENT_LLM_MODEL")

    return LLMSettings(
        provider=provider.lower(),
        model=model,
        api_key=api_key,
        base_url=base_url,
        timeout_seconds=timeout,
        thinking=thinking,
        reasoning_effort=reasoning_effort,
    )


def masked_llm_settings(settings: LLMSettings) -> dict[str, object]:
    """返回适合 CLI 展示的配置摘要，不泄露 API Key。"""

    return {
        "provider": settings.provider,
        "model": settings.model,
        "base_url": settings.base_url,
        "api_key_set": bool(settings.api_key),
        "timeout_seconds": settings.timeout_seconds,
        "thinking": settings.thinking,
        "reasoning_effort": settings.reasoning_effort,
    }


def load_embedding_settings(
    env_path: str | Path = DEFAULT_ENV_PATH,
    environ: Mapping[str, str] | None = None,
) -> EmbeddingSettings | None:
    """从 `.env` 和系统环境变量加载 embedding 配置。

    如果完全没有提供 embedding 配置，则返回 None，表示回退到本地 hash embedding。
    如果只配了一部分字段，则抛出异常，避免用户误以为已经启用了真实语义向量。
    """

    file_values = load_dotenv_values(env_path)
    environment = os.environ if environ is None else environ

    def get(*keys: str, default: str | None = None) -> str | None:
        """按优先级读取多个候选键名。"""

        for key in keys:
            if key in environment and environment[key]:
                return environment[key]
            if key in file_values and file_values[key]:
                return file_values[key]
        return default

    provider = get("JOB_AGENT_EMBEDDING_PROVIDER")
    model = get("JOB_AGENT_EMBEDDING_MODEL")
    api_key = get("JOB_AGENT_EMBEDDING_API_KEY", "OPENAI_API_KEY")
    base_url = get("JOB_AGENT_EMBEDDING_BASE_URL", "OPENAI_BASE_URL")
    timeout = int(get("JOB_AGENT_EMBEDDING_TIMEOUT_SECONDS", default="60") or 60)
    batch_size = int(get("JOB_AGENT_EMBEDDING_BATCH_SIZE", default="64") or 64)
    dimensions = get("JOB_AGENT_EMBEDDING_DIMENSIONS")

    if not any([provider, model, api_key, base_url, dimensions]):
        return None
    if not provider:
        raise ValueError("缺少 embedding provider：请在 .env 中配置 JOB_AGENT_EMBEDDING_PROVIDER")
    if provider.lower() in {"local", "local_hash"}:
        parsed_dimensions = int(dimensions) if dimensions else None
        return EmbeddingSettings(
            provider=provider.lower(),
            model=model or "local-hash",
            api_key="local",
            base_url="local",
            timeout_seconds=timeout,
            batch_size=batch_size,
            dimensions=parsed_dimensions,
        )
    if not model:
        raise ValueError("缺少 embedding 模型名：请在 .env 中配置 JOB_AGENT_EMBEDDING_MODEL")
    if not api_key:
        raise ValueError("缺少 embedding API Key：请在 .env 中配置 JOB_AGENT_EMBEDDING_API_KEY")
    if not base_url:
        raise ValueError("缺少 embedding base URL：请在 .env 中配置 JOB_AGENT_EMBEDDING_BASE_URL")

    return EmbeddingSettings(
        provider=provider.lower(),
        model=model,
        api_key=api_key,
        base_url=base_url,
        timeout_seconds=timeout,
        batch_size=batch_size,
        dimensions=int(dimensions) if dimensions else None,
    )


def masked_embedding_settings(settings: EmbeddingSettings | None) -> dict[str, object]:
    """返回适合 CLI / Web 展示的 embedding 配置摘要。"""

    if settings is None:
        return {"provider": "local_hash", "mode": "fallback", "configured": False}
    return {
        "provider": settings.provider,
        "model": settings.model,
        "base_url": settings.base_url,
        "api_key_set": bool(settings.api_key and settings.api_key != "local"),
        "timeout_seconds": settings.timeout_seconds,
        "batch_size": settings.batch_size,
        "dimensions": settings.dimensions,
        "configured": settings.provider not in {"local", "local_hash"},
    }


def load_agent_memory_settings(
    env_path: str | Path = DEFAULT_ENV_PATH,
    environ: Mapping[str, str] | None = None,
) -> AgentMemorySettings:
    """从 `.env` 和系统环境变量加载 Agent 记忆配置。

    这些配置不包含敏感信息；提供环境变量只是为了后续按不同模型上下文窗口调整阈值，
    不需要改业务代码。
    """

    file_values = load_dotenv_values(env_path)
    environment = os.environ if environ is None else environ

    def get(*keys: str, default: str | None = None) -> str | None:
        """按优先级读取多个候选键名。"""

        for key in keys:
            if key in environment and environment[key]:
                return environment[key]
            if key in file_values and file_values[key]:
                return file_values[key]
        return default

    enabled = parse_bool(get("JOB_AGENT_MEMORY_ENABLED", default="true"))
    return AgentMemorySettings(
        enabled=enabled,
        restore_history_limit=parse_positive_int(
            get("JOB_AGENT_MEMORY_RESTORE_HISTORY_LIMIT", default="200"),
            "JOB_AGENT_MEMORY_RESTORE_HISTORY_LIMIT",
        ),
        restore_trigger_tokens=parse_positive_int(
            get("JOB_AGENT_MEMORY_RESTORE_TRIGGER_TOKENS", default="12000"),
            "JOB_AGENT_MEMORY_RESTORE_TRIGGER_TOKENS",
        ),
        restore_keep_messages=parse_positive_int(
            get("JOB_AGENT_MEMORY_RESTORE_KEEP_MESSAGES", default="24"),
            "JOB_AGENT_MEMORY_RESTORE_KEEP_MESSAGES",
        ),
        restore_summary_chars=parse_positive_int(
            get("JOB_AGENT_MEMORY_RESTORE_SUMMARY_CHARS", default="6000"),
            "JOB_AGENT_MEMORY_RESTORE_SUMMARY_CHARS",
        ),
        summary_trigger_tokens=parse_positive_int(
            get("JOB_AGENT_MEMORY_SUMMARY_TRIGGER_TOKENS", default="12000"),
            "JOB_AGENT_MEMORY_SUMMARY_TRIGGER_TOKENS",
        ),
        summary_keep_messages=parse_positive_int(
            get("JOB_AGENT_MEMORY_SUMMARY_KEEP_MESSAGES", default="24"),
            "JOB_AGENT_MEMORY_SUMMARY_KEEP_MESSAGES",
        ),
        summary_trim_tokens=parse_positive_int(
            get("JOB_AGENT_MEMORY_SUMMARY_TRIM_TOKENS", default="6000"),
            "JOB_AGENT_MEMORY_SUMMARY_TRIM_TOKENS",
        ),
    )


def masked_agent_memory_settings(settings: AgentMemorySettings) -> dict[str, object]:
    """返回适合 Web/CLI 展示的 Agent 记忆配置。"""

    return {
        "enabled": settings.enabled,
        "restore_history_limit": settings.restore_history_limit,
        "restore_trigger_tokens": settings.restore_trigger_tokens,
        "restore_keep_messages": settings.restore_keep_messages,
        "restore_summary_chars": settings.restore_summary_chars,
        "summary_trigger_tokens": settings.summary_trigger_tokens,
        "summary_keep_messages": settings.summary_keep_messages,
        "summary_trim_tokens": settings.summary_trim_tokens,
    }


def parse_bool(value: str | None) -> bool:
    """解析常见布尔配置值。"""

    if value is None:
        return True
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"无法解析布尔配置：{value}")


def parse_positive_int(value: str | None, field_name: str) -> int:
    """解析正整数配置，并给出可读错误。"""

    try:
        parsed = int(value or "")
    except ValueError as error:
        raise ValueError(f"{field_name} 必须是正整数") from error
    if parsed <= 0:
        raise ValueError(f"{field_name} 必须大于 0")
    return parsed
