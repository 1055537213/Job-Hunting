"""LangChain LLM 配置测试。

当前项目已经把真实聊天模型统一切到 LangChain ChatModel，所以这里主要验证：

- `.env` 仍然是唯一配置来源。
- 第三方模型服务仍然通过 OpenAI-compatible 方式接入。
- 业务层拿到的仍然是 `LLMClient` 边界，而不是直接裸用 SDK。
"""

import pytest
from langchain_core.messages import AIMessage
from langchain_openai import ChatOpenAI

from job_hunting_agent.config import (
    load_agent_memory_settings,
    load_intent_router_settings,
    load_llm_settings,
)
from job_hunting_agent.llm import (
    LangChainLLMClient,
    build_chat_model,
    build_llm_client,
    extract_message_text,
    normalize_openai_compatible_base_url,
)


def test_load_llm_settings_reads_project_env_and_provider_options(tmp_path):
    """配置加载器能从项目 `.env` 读取模型服务及可选参数。"""

    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            [
                "JOB_AGENT_LLM_PROVIDER=siliconflow",
                "JOB_AGENT_LLM_MODEL=Qwen/Qwen3.5-35B-A3B",
                "JOB_AGENT_LLM_API_KEY=sk-test",
                "JOB_AGENT_LLM_BASE_URL=https://api.siliconflow.cn/v1",
                "JOB_AGENT_LLM_TIMEOUT_SECONDS=45",
                "JOB_AGENT_LLM_ENABLE_THINKING=true",
                "JOB_AGENT_LLM_REASONING_EFFORT=",
            ]
        ),
        encoding="utf-8",
    )

    settings = load_llm_settings(env_file, environ={})

    assert settings.provider == "siliconflow"
    assert settings.model == "Qwen/Qwen3.5-35B-A3B"
    assert settings.api_key == "sk-test"
    assert settings.base_url == "https://api.siliconflow.cn/v1"
    assert settings.timeout_seconds == 45
    assert settings.enable_thinking is True
    assert settings.reasoning_effort is None


def test_load_intent_router_settings_reads_independent_small_model(tmp_path):
    """意图路由模型可以独立于主 Agent 配置，并保持关闭时无额外调用。"""

    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            [
                "JOB_AGENT_LLM_PROVIDER=main-provider",
                "JOB_AGENT_LLM_API_KEY=sk-main",
                "JOB_AGENT_LLM_BASE_URL=https://main.example/v1",
                "JOB_AGENT_INTENT_ROUTER_ENABLED=true",
                "JOB_AGENT_INTENT_ROUTER_MODEL=small-router",
                "JOB_AGENT_INTENT_ROUTER_TIMEOUT_SECONDS=7",
                "JOB_AGENT_INTENT_ROUTER_CONFIDENCE_THRESHOLD=0.85",
                "JOB_AGENT_INTENT_ROUTER_HISTORY_MESSAGES=4",
            ]
        ),
        encoding="utf-8",
    )

    settings = load_intent_router_settings(env_file, environ={})

    assert settings.enabled is True
    assert settings.llm is not None
    assert settings.llm.model == "small-router"
    assert settings.llm.provider == "main-provider"
    assert settings.llm.api_key == "sk-main"
    assert settings.llm.base_url == "https://main.example/v1"
    assert settings.llm.timeout_seconds == 7
    assert settings.confidence_threshold == 0.85
    assert settings.history_messages == 4


def test_load_intent_router_settings_is_disabled_by_default(tmp_path):
    """没有显式启用路由器时，现有 Agent 路径保持不变。"""

    settings = load_intent_router_settings(tmp_path / ".env", environ={})

    assert settings.enabled is False
    assert settings.llm is None


def test_build_chat_model_uses_openai_compatible_langchain_model(tmp_path):
    """真实模型接入改为 LangChain ChatOpenAI，并从 `.env` 读取参数。"""

    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            [
                "JOB_AGENT_LLM_PROVIDER=siliconflow",
                "JOB_AGENT_LLM_MODEL=Qwen/Qwen3.5-35B-A3B",
                "JOB_AGENT_LLM_API_KEY=sk-from-job-agent",
                "JOB_AGENT_LLM_BASE_URL=https://api.siliconflow.cn/v1/chat/completions",
                "JOB_AGENT_LLM_ENABLE_THINKING=false",
            ]
        ),
        encoding="utf-8",
    )

    model = build_chat_model(load_llm_settings(env_file, environ={}))

    assert isinstance(model, ChatOpenAI)
    assert model.model_name == "Qwen/Qwen3.5-35B-A3B"
    assert str(model.openai_api_base) == "https://api.siliconflow.cn/v1"
    assert model.reasoning_effort is None
    assert model.use_responses_api is False
    assert model.extra_body == {"enable_thinking": False}
    assert model.streaming is True


def test_build_llm_client_returns_langchain_wrapper(tmp_path):
    """业务层仍然得到 `LLMClient.complete()` 包装，而不是直接依赖 SDK。"""

    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            [
                "JOB_AGENT_LLM_PROVIDER=deepseek",
                "JOB_AGENT_LLM_MODEL=deepseek-v4-pro",
                "JOB_AGENT_LLM_API_KEY=sk-from-job-agent",
                "JOB_AGENT_LLM_BASE_URL=https://api.deepseek.com",
            ]
        ),
        encoding="utf-8",
    )

    client = build_llm_client(load_llm_settings(env_file, environ={}))

    assert isinstance(client, LangChainLLMClient)
    assert isinstance(client.model, ChatOpenAI)
    assert client.model.model_name == "deepseek-v4-pro"


def test_legacy_thinking_setting_remains_compatible(tmp_path):
    """已有 DeepSeek 风格 `.env` 不应在升级通用模型配置后失去思考参数。"""

    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            [
                "JOB_AGENT_LLM_PROVIDER=deepseek",
                "JOB_AGENT_LLM_MODEL=deepseek-v4-pro",
                "JOB_AGENT_LLM_API_KEY=sk-from-job-agent",
                "JOB_AGENT_LLM_BASE_URL=https://api.deepseek.com",
                "JOB_AGENT_LLM_THINKING=enabled",
            ]
        ),
        encoding="utf-8",
    )

    model = build_chat_model(load_llm_settings(env_file, environ={}))

    assert model.extra_body == {"thinking": {"type": "enabled"}}


def test_custom_relay_provider_uses_openai_compatible_chat_model(tmp_path):
    """中转站可以使用自定义 provider 标签，不需要修改代码白名单。"""

    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            [
                "JOB_AGENT_LLM_PROVIDER=my-relay",
                "JOB_AGENT_LLM_MODEL=relay-model-name",
                "JOB_AGENT_LLM_API_KEY=sk-relay",
                "JOB_AGENT_LLM_BASE_URL=https://relay.example.com/v1/chat/completions",
            ]
        ),
        encoding="utf-8",
    )

    settings = load_llm_settings(env_file, environ={})
    model = build_chat_model(settings)

    assert settings.provider == "my-relay"
    assert isinstance(model, ChatOpenAI)
    assert model.model_name == "relay-model-name"
    assert str(model.openai_api_base) == "https://relay.example.com/v1"


def test_generic_openai_aliases_can_configure_local_or_relay_endpoint(tmp_path):
    """通用 OPENAI_* 键也能配置本地服务或第三方中转站。"""

    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            [
                "JOB_AGENT_LLM_PROVIDER=custom",
                "OPENAI_MODEL=custom-chat-model",
                "OPENAI_API_KEY=sk-compatible",
                "OPENAI_BASE_URL=https://gateway.example.com/v1",
            ]
        ),
        encoding="utf-8",
    )

    settings = load_llm_settings(env_file, environ={})

    assert settings.model == "custom-chat-model"
    assert settings.api_key == "sk-compatible"
    assert settings.base_url == "https://gateway.example.com/v1"


def test_normalize_openai_compatible_base_url_strips_specific_endpoints():
    """LangChain 需要根 base URL，不应保留 `/chat/completions` 这类具体端点。"""

    assert normalize_openai_compatible_base_url("https://api.deepseek.com/chat/completions") == "https://api.deepseek.com"
    assert normalize_openai_compatible_base_url("https://api.openai.com/v1/embeddings") == "https://api.openai.com/v1"


def test_extract_message_text_handles_rich_content_blocks():
    """富文本响应块也能被压平成普通文本，供业务层复用。"""

    message = AIMessage(
        content=[
            {"type": "text", "text": "第一段"},
            {"type": "output_text", "text": "第二段"},
        ]
    )

    assert extract_message_text(message) == "第一段\n第二段"


def test_llm_settings_requires_model_and_base_url_from_env(tmp_path):
    """模型名和接口地址缺失时要报错，不能回退到代码默认值。"""

    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            [
                "JOB_AGENT_LLM_PROVIDER=deepseek",
                "JOB_AGENT_LLM_API_KEY=sk-test",
            ]
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="base URL|模型名"):
        load_llm_settings(env_file, environ={})


def test_load_agent_memory_settings_reads_context_thresholds(tmp_path):
    """Agent 记忆阈值可以从 `.env` 配置，方便按模型上下文窗口调整。"""

    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            [
                "JOB_AGENT_MEMORY_ENABLED=true",
                "JOB_AGENT_MEMORY_RESTORE_HISTORY_LIMIT=80",
                "JOB_AGENT_MEMORY_RESTORE_TRIGGER_TOKENS=3000",
                "JOB_AGENT_MEMORY_RESTORE_KEEP_MESSAGES=12",
                "JOB_AGENT_MEMORY_RESTORE_SUMMARY_CHARS=1600",
                "JOB_AGENT_MEMORY_SUMMARY_TRIGGER_TOKENS=4000",
                "JOB_AGENT_MEMORY_SUMMARY_KEEP_MESSAGES=16",
                "JOB_AGENT_MEMORY_SUMMARY_TRIM_TOKENS=2000",
            ]
        ),
        encoding="utf-8",
    )

    settings = load_agent_memory_settings(env_file, environ={})

    assert settings.enabled is True
    assert settings.checkpoint_backend == "database"
    assert settings.restore_history_limit == 80
    assert settings.restore_trigger_tokens == 3000
    assert settings.restore_keep_messages == 12
    assert settings.restore_summary_chars == 1600
    assert settings.summary_trigger_tokens == 4000
    assert settings.summary_keep_messages == 16
    assert settings.summary_trim_tokens == 2000


def test_load_agent_memory_settings_accepts_process_memory_backend(tmp_path):
    """进程内记忆只能通过显式配置启用，供离线测试和本地调试使用。"""

    env_file = tmp_path / ".env"
    env_file.write_text("JOB_AGENT_MEMORY_CHECKPOINT_BACKEND=memory\n", encoding="utf-8")

    settings = load_agent_memory_settings(env_file, environ={})

    assert settings.checkpoint_backend == "memory"


def test_load_agent_memory_settings_rejects_unknown_checkpoint_backend(tmp_path):
    """未知记忆后端必须在启动配置阶段失败。"""

    env_file = tmp_path / ".env"
    env_file.write_text("JOB_AGENT_MEMORY_CHECKPOINT_BACKEND=unknown\n", encoding="utf-8")

    with pytest.raises(ValueError, match="database 或 memory"):
        load_agent_memory_settings(env_file, environ={})
