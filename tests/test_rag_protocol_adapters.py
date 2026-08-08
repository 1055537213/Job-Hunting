"""通用 Embedding 与 Rerank 协议适配器测试。

所有请求都使用注入的假 transport，确保测试不会读取真实 `.env` 或访问网络。
"""

from __future__ import annotations

from job_hunting_agent.config import (
    load_embedding_settings,
    load_rerank_settings,
    masked_rerank_settings,
)
from job_hunting_agent.rag import (
    HttpReranker,
    NativeMultimodalEmbeddings,
    RerankResult,
    OpenAICompatibleEmbeddings,
    build_rag_embeddings,
    build_reranker,
    rag_embedding_model_name,
)


def write_native_env(path) -> None:  # noqa: ANN001
    """写入不含真实密钥的 provider-native RAG 测试配置。"""

    path.write_text(
        "\n".join(
            [
                "JOB_AGENT_EMBEDDING_PROVIDER=embedding-provider",
                "JOB_AGENT_EMBEDDING_API_STYLE=native_multimodal",
                "JOB_AGENT_EMBEDDING_MODEL=embedding-model",
                "JOB_AGENT_EMBEDDING_API_KEY=test-rag-key",
                "JOB_AGENT_EMBEDDING_BASE_URL=https://embedding.example/v1/encode",
                "JOB_AGENT_RERANK_PROVIDER=rerank-provider",
                "JOB_AGENT_RERANK_API_STYLE=native",
                "JOB_AGENT_RERANK_MODEL=rerank-model",
                "JOB_AGENT_RERANK_API_KEY=test-rag-key",
                "JOB_AGENT_RERANK_BASE_URL=https://rerank.example/v1/score",
                "JOB_AGENT_RERANK_CANDIDATE_MULTIPLIER=4",
            ]
        ),
        encoding="utf-8",
    )


def test_native_settings_load_explicit_protocols_and_mask_key(tmp_path):
    """Embedding/Rerank 使用显式协议配置，摘要不泄露密钥。"""

    env_file = tmp_path / ".env"
    write_native_env(env_file)

    embedding = load_embedding_settings(env_file, environ={})
    rerank = load_rerank_settings(env_file, environ={})

    assert embedding is not None
    assert embedding.provider == "embedding-provider"
    assert embedding.api_style == "native_multimodal"
    assert embedding.model == "embedding-model"
    assert embedding.api_key == "test-rag-key"
    assert embedding.base_url == "https://embedding.example/v1/encode"
    assert rerank is not None
    assert rerank.provider == "rerank-provider"
    assert rerank.api_style == "native"
    assert rerank.model == "rerank-model"
    assert rerank.api_key == "test-rag-key"
    assert rerank.base_url == "https://rerank.example/v1/score"
    assert rerank.candidate_multiplier == 4
    assert "test-rag-key" not in str(masked_rerank_settings(rerank))
    assert isinstance(build_rag_embeddings(env_file), NativeMultimodalEmbeddings)
    assert isinstance(build_reranker(env_file), HttpReranker)


def test_embedding_identity_separates_same_named_models_from_different_endpoints():
    """相同模型名但不同供应商端点的向量不能被 pgvector 当作同一语义空间。"""

    first = OpenAICompatibleEmbeddings(
        api_key="first-secret-key",
        base_url="https://first-provider.example/v1",
        model="shared-embedding-model",
        dimensions=1024,
    )
    second = OpenAICompatibleEmbeddings(
        api_key="second-secret-key",
        base_url="https://second-provider.example/v1",
        model="shared-embedding-model",
        dimensions=1024,
    )

    first_identity = rag_embedding_model_name(first)
    second_identity = rag_embedding_model_name(second)

    assert first_identity != second_identity
    assert "secret" not in first_identity
    assert "secret" not in second_identity


def test_native_multimodal_embeddings_use_native_payload_and_restore_input_order():
    """provider-native 文本向量请求使用 input.contents 并恢复输入顺序。"""

    captured: dict[str, object] = {}

    def fake_transport(url, headers, payload, timeout):  # noqa: ANN001
        captured.update(url=url, headers=headers, payload=payload, timeout=timeout)
        # 服务端可能返回乱序条目，适配器需按 text_index 还原为原输入顺序。
        return {
            "output": {
                "embeddings": [
                    {"text_index": 1, "embedding": [0.4, 0.5]},
                    {"text_index": 0, "embedding": [0.1, 0.2]},
                ]
            },
            "usage": {"input_tokens": 7, "total_tokens": 7},
        }

    embeddings = NativeMultimodalEmbeddings(
        api_key="test-key",
        base_url="https://embedding.example/v1/encode",
        model="embedding-model",
        transport=fake_transport,
    )

    vectors = embeddings.embed_documents(["候选人项目经历", "职位要求"])

    assert vectors == [[0.1, 0.2], [0.4, 0.5]]
    assert captured["url"] == "https://embedding.example/v1/encode"
    assert captured["headers"] == {
        "Authorization": "Bearer test-key",
        "Content-Type": "application/json",
    }
    assert captured["payload"] == {
        "model": "embedding-model",
        "input": {
            "contents": [{"text": "候选人项目经历"}, {"text": "职位要求"}],
        },
    }

def test_native_reranker_uses_query_documents_payload_and_preserves_indexes():
    """native 重排器仅返回候选索引，调用方可复用本地 chunk。"""

    captured: dict[str, object] = {}

    def fake_transport(url, headers, payload, timeout):  # noqa: ANN001
        captured.update(url=url, headers=headers, payload=payload, timeout=timeout)
        return {
            "output": {
                "results": [
                    {"index": 1, "relevance_score": 0.92},
                    {"index": 0, "relevance_score": 0.37},
                ]
            },
            "usage": {"input_tokens": 11, "total_tokens": 11},
        }

    reranker = HttpReranker(
        api_key="test-key",
        base_url="https://rerank.example/v1/score",
        model="rerank-model",
        api_style="native",
        transport=fake_transport,
    )

    result = reranker.rerank("FastAPI 岗位要求", ["Python 项目", "FastAPI 服务"], top_n=2)

    assert result == [RerankResult(index=1, relevance_score=0.92), RerankResult(index=0, relevance_score=0.37)]
    assert captured["url"] == "https://rerank.example/v1/score"
    assert captured["headers"] == {
        "Authorization": "Bearer test-key",
        "Content-Type": "application/json",
    }
    assert captured["payload"] == {
        "model": "rerank-model",
        "input": {"query": "FastAPI 岗位要求", "documents": ["Python 项目", "FastAPI 服务"]},
        "parameters": {"return_documents": False, "top_n": 2},
    }


def test_standard_reranker_uses_common_rerank_payload():
    """standard 协议使用常见 /rerank 请求和 results 响应。"""

    captured: dict[str, object] = {}

    def fake_transport(url, headers, payload, timeout):  # noqa: ANN001
        captured.update(url=url, headers=headers, payload=payload, timeout=timeout)
        return {"results": [{"index": 0, "score": 0.8}, {"index": 1, "score": 0.2}]}

    reranker = HttpReranker(
        api_key="test-key",
        base_url="https://rerank.example/v1",
        model="rerank-model",
        api_style="standard",
        transport=fake_transport,
    )

    result = reranker.rerank("目标岗位", ["项目 A", "项目 B"], top_n=2)

    assert result == [RerankResult(index=0, relevance_score=0.8), RerankResult(index=1, relevance_score=0.2)]
    assert captured["url"] == "https://rerank.example/v1/rerank"
    assert captured["payload"] == {
        "model": "rerank-model",
        "query": "目标岗位",
        "documents": ["项目 A", "项目 B"],
        "top_n": 2,
        "return_documents": False,
    }
