"""Embedding 配置和真实向量适配器测试。

这些测试不访问真实网络，也不读取真实 `.env`。目标是保证 embedding 的 provider、
模型名、接口地址和 API Key 都来自 `.env`/环境变量，且未配置时会安全回退到
本地 hash embedding。
"""

import json

import pytest

from job_hunting_agent.config import load_embedding_settings
from job_hunting_agent.rag import (
    LocalHashEmbeddings,
    OpenAICompatibleEmbeddings,
    build_rag_embeddings,
)


def test_load_embedding_settings_reads_project_env(tmp_path):
    """配置加载器能从项目 `.env` 读取 embedding API 信息。"""

    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            [
                "JOB_AGENT_EMBEDDING_PROVIDER=openai_compatible",
                "JOB_AGENT_EMBEDDING_MODEL=text-embedding-3-small",
                "JOB_AGENT_EMBEDDING_API_KEY=sk-embed",
                "JOB_AGENT_EMBEDDING_BASE_URL=https://api.openai.com/v1",
                "JOB_AGENT_EMBEDDING_TIMEOUT_SECONDS=45",
                "JOB_AGENT_EMBEDDING_BATCH_SIZE=32",
                "JOB_AGENT_EMBEDDING_DIMENSIONS=256",
            ]
        ),
        encoding="utf-8",
    )

    settings = load_embedding_settings(env_file, environ={})

    assert settings is not None
    assert settings.provider == "openai_compatible"
    assert settings.model == "text-embedding-3-small"
    assert settings.api_key == "sk-embed"
    assert settings.base_url == "https://api.openai.com/v1"
    assert settings.timeout_seconds == 45
    assert settings.batch_size == 32
    assert settings.dimensions == 256


def test_load_embedding_settings_returns_none_when_not_configured(tmp_path):
    """完全没有 embedding 配置时，RAG 应回退到本地 hash embedding。"""

    env_file = tmp_path / ".env"
    env_file.write_text("JOB_AGENT_LLM_PROVIDER=deepseek\n", encoding="utf-8")

    settings = load_embedding_settings(env_file, environ={})

    assert settings is None
    assert isinstance(build_rag_embeddings(env_file), LocalHashEmbeddings)


def test_build_rag_embeddings_uses_real_provider_when_configured(tmp_path):
    """配置真实 embedding provider 后，RAG 应切换到远程向量适配器。"""

    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            [
                "JOB_AGENT_EMBEDDING_PROVIDER=openai_compatible",
                "JOB_AGENT_EMBEDDING_MODEL=text-embedding-3-small",
                "JOB_AGENT_EMBEDDING_API_KEY=sk-embed",
                "JOB_AGENT_EMBEDDING_BASE_URL=https://api.openai.com/v1",
            ]
        ),
        encoding="utf-8",
    )

    embeddings = build_rag_embeddings(env_file)

    assert isinstance(embeddings, OpenAICompatibleEmbeddings)


def test_openai_compatible_embeddings_build_request_and_parse_vectors():
    """真实 embedding 适配器按 OpenAI-compatible Embeddings 形态构造请求。"""

    captured: dict[str, object] = {}

    def fake_transport(url: str, headers: dict[str, str], payload: dict[str, object], timeout: int) -> dict[str, object]:
        """测试用传输函数：记录请求并返回模拟向量。"""

        captured["url"] = url
        captured["headers"] = headers
        captured["payload"] = payload
        captured["timeout"] = timeout
        return {
            "data": [
                {"index": 0, "embedding": [0.1, 0.2, 0.3]},
                {"index": 1, "embedding": [0.4, 0.5, 0.6]},
            ]
        }

    client = OpenAICompatibleEmbeddings(
        api_key="sk-embed",
        base_url="https://api.openai.com/v1",
        model="text-embedding-3-small",
        timeout_seconds=30,
        batch_size=16,
        dimensions=256,
        transport=fake_transport,
    )

    vectors = client.embed_documents(["职位解析", "RAG 知识库"])

    assert vectors == [
        [0.1, 0.2, 0.3],
        [0.4, 0.5, 0.6],
    ]
    assert captured["url"] == "https://api.openai.com/v1/embeddings"
    assert captured["headers"]["Authorization"] == "Bearer sk-embed"
    assert captured["timeout"] == 30
    assert captured["payload"]["model"] == "text-embedding-3-small"
    assert captured["payload"]["input"] == ["职位解析", "RAG 知识库"]
    assert captured["payload"]["encoding_format"] == "float"
    assert captured["payload"]["dimensions"] == 256
    json.dumps(captured["payload"], ensure_ascii=False)


def test_embedding_settings_require_model_and_base_url_when_provider_enabled(tmp_path):
    """启用真实 embedding provider 时，缺少模型名或地址必须报错。"""

    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            [
                "JOB_AGENT_EMBEDDING_PROVIDER=openai_compatible",
                "JOB_AGENT_EMBEDDING_API_KEY=sk-embed",
            ]
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="base URL|模型名"):
        load_embedding_settings(env_file, environ={})
