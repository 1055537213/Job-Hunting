"""LLM 配置和 DeepSeek 适配器测试。

这些测试不访问真实网络，也不读取真实 `.env`。目标是保证 API Key、base URL、
模型名都来自配置文件/环境变量，而不是写死在业务代码里。
"""

import json

import pytest

from job_hunting_agent.config import load_llm_settings
from job_hunting_agent.llm import DeepSeekChatClient, build_llm_client


def test_load_llm_settings_reads_project_env_and_deepseek_aliases(tmp_path):
    """配置加载器能从项目 `.env` 读取 DeepSeek API 信息。"""

    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            [
                "JOB_AGENT_LLM_PROVIDER=deepseek",
                "JOB_AGENT_LLM_MODEL=deepseek-v4-pro",
                "DEEPSEEK_API_KEY=sk-test",
                "DEEPSEEK_BASE_URL=https://api.deepseek.com",
                "JOB_AGENT_LLM_TIMEOUT_SECONDS=45",
                "JOB_AGENT_LLM_THINKING=enabled",
                "JOB_AGENT_LLM_REASONING_EFFORT=high",
            ]
        ),
        encoding="utf-8",
    )

    settings = load_llm_settings(env_file, environ={})

    assert settings.provider == "deepseek"
    assert settings.model == "deepseek-v4-pro"
    assert settings.api_key == "sk-test"
    assert settings.base_url == "https://api.deepseek.com"
    assert settings.timeout_seconds == 45
    assert settings.thinking == "enabled"
    assert settings.reasoning_effort == "high"


def test_deepseek_client_builds_openai_compatible_chat_request():
    """DeepSeek 适配器按 OpenAI ChatCompletions 兼容格式构造请求。"""

    captured: dict[str, object] = {}

    def fake_transport(url: str, headers: dict[str, str], payload: dict[str, object], timeout: int) -> dict[str, object]:
        """测试用传输函数：记录请求并返回模拟响应。"""

        captured["url"] = url
        captured["headers"] = headers
        captured["payload"] = payload
        captured["timeout"] = timeout
        return {"choices": [{"message": {"content": "安全草稿"}}]}

    client = DeepSeekChatClient(
        api_key="sk-test",
        base_url="https://api.deepseek.com",
        model="deepseek-v4-pro",
        timeout_seconds=30,
        thinking="enabled",
        reasoning_effort="high",
        transport=fake_transport,
    )

    result = client.complete("请生成简历草稿")

    assert result == "安全草稿"
    assert captured["url"] == "https://api.deepseek.com/chat/completions"
    assert captured["headers"]["Authorization"] == "Bearer sk-test"
    assert captured["timeout"] == 30
    assert captured["payload"]["model"] == "deepseek-v4-pro"
    assert captured["payload"]["messages"] == [{"role": "user", "content": "请生成简历草稿"}]
    assert captured["payload"]["thinking"] == {"type": "enabled"}
    assert captured["payload"]["reasoning_effort"] == "high"
    # 确认 payload 可序列化，避免真实请求时才暴露 JSON 编码问题。
    json.dumps(captured["payload"], ensure_ascii=False)


def test_build_llm_client_uses_settings_without_hardcoding_provider_details(tmp_path):
    """工厂函数根据 `.env` 配置创建真实模型适配器。"""

    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            [
                "JOB_AGENT_LLM_PROVIDER=deepseek",
                "JOB_AGENT_LLM_MODEL=deepseek-v4-pro",
                "JOB_AGENT_LLM_API_KEY=sk-from-job-agent",
                "JOB_AGENT_LLM_BASE_URL=https://api.deepseek.com/v1",
            ]
        ),
        encoding="utf-8",
    )

    client = build_llm_client(load_llm_settings(env_file, environ={}))

    assert isinstance(client, DeepSeekChatClient)
    assert client.model == "deepseek-v4-pro"
    assert client.chat_completions_url == "https://api.deepseek.com/v1/chat/completions"


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
