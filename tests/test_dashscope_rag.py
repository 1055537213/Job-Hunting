"""DashScope 向量与重排适配器测试。

所有请求都使用注入的假 transport，确保测试不会读取真实 `.env` 或访问网络。
"""

from __future__ import annotations

from langchain_core.documents import Document

from job_hunting_agent.config import (
    DASHSCOPE_MULTIMODAL_EMBEDDING_URL,
    DASHSCOPE_RERANK_URL,
    load_embedding_settings,
    load_rerank_settings,
    masked_rerank_settings,
)
from job_hunting_agent.rag import (
    DashScopeMultimodalEmbeddings,
    DashScopeReranker,
    LocalHashEmbeddings,
    RAGKnowledgeBase,
    RerankResult,
    build_rag_embeddings,
    build_reranker,
)


def write_dashscope_env(path) -> None:  # noqa: ANN001
    """写入不含真实密钥的 DashScope RAG 测试配置。"""

    path.write_text(
        "\n".join(
            [
                "DASHSCOPE_API_KEY=test-dashscope-key",
                "JOB_AGENT_EMBEDDING_PROVIDER=dashscope",
                "JOB_AGENT_EMBEDDING_MODEL=qwen3-vl-embedding",
                "JOB_AGENT_RERANK_PROVIDER=dashscope",
                "JOB_AGENT_RERANK_MODEL=qwen3-vl-rerank",
                "JOB_AGENT_RERANK_CANDIDATE_MULTIPLIER=4",
            ]
        ),
        encoding="utf-8",
    )


def test_dashscope_settings_share_key_and_use_documented_default_endpoints(tmp_path):
    """DashScope 可用一把环境变量密钥配置两个 RAG 模型，且摘要不泄露密钥。"""

    env_file = tmp_path / ".env"
    write_dashscope_env(env_file)

    embedding = load_embedding_settings(env_file, environ={})
    rerank = load_rerank_settings(env_file, environ={})

    assert embedding is not None
    assert embedding.provider == "dashscope"
    assert embedding.model == "qwen3-vl-embedding"
    assert embedding.api_key == "test-dashscope-key"
    assert embedding.base_url == DASHSCOPE_MULTIMODAL_EMBEDDING_URL
    assert rerank is not None
    assert rerank.provider == "dashscope"
    assert rerank.model == "qwen3-vl-rerank"
    assert rerank.api_key == "test-dashscope-key"
    assert rerank.base_url == DASHSCOPE_RERANK_URL
    assert rerank.candidate_multiplier == 4
    assert "test-dashscope-key" not in str(masked_rerank_settings(rerank))
    assert isinstance(build_rag_embeddings(env_file), DashScopeMultimodalEmbeddings)
    assert isinstance(build_reranker(env_file), DashScopeReranker)


def test_dashscope_multimodal_embeddings_use_native_payload_and_restore_input_order():
    """DashScope 文本向量请求必须使用 MultiModalEmbedding 的 input 数组格式。"""

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

    embeddings = DashScopeMultimodalEmbeddings(
        api_key="test-key",
        base_url="https://dashscope.aliyuncs.com/api/v1",
        model="qwen3-vl-embedding",
        transport=fake_transport,
    )

    vectors = embeddings.embed_documents(["候选人项目经历", "职位要求"])

    assert vectors == [[0.1, 0.2], [0.4, 0.5]]
    assert captured["url"] == DASHSCOPE_MULTIMODAL_EMBEDDING_URL
    assert captured["headers"] == {
        "Authorization": "Bearer test-key",
        "Content-Type": "application/json",
    }
    assert captured["payload"] == {
        "model": "qwen3-vl-embedding",
        "input": {
            "contents": [{"text": "候选人项目经历"}, {"text": "职位要求"}],
        },
    }


def test_dashscope_reranker_uses_query_documents_payload_and_preserves_indexes():
    """重排器仅返回候选索引，调用方可复用本地 chunk 而无需依赖正文回传。"""

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

    reranker = DashScopeReranker(
        api_key="test-key",
        base_url=DASHSCOPE_RERANK_URL,
        model="qwen3-vl-rerank",
        transport=fake_transport,
    )

    result = reranker.rerank("FastAPI 岗位要求", ["Python 项目", "FastAPI 服务"], top_n=2)

    assert result == [RerankResult(index=1, relevance_score=0.92), RerankResult(index=0, relevance_score=0.37)]
    assert captured["url"] == DASHSCOPE_RERANK_URL
    assert captured["headers"] == {
        "Authorization": "Bearer test-key",
        "Content-Type": "application/json",
    }
    assert captured["payload"] == {
        "model": "qwen3-vl-rerank",
        "input": {"query": "FastAPI 岗位要求", "documents": ["Python 项目", "FastAPI 服务"]},
        "parameters": {"return_documents": False, "top_n": 2},
    }


def test_rag_uses_rerank_order_after_expanded_vector_recall(tmp_path):
    """RAG 应先扩大向量候选池，再按 rerank 返回的索引顺序选择最终证据。"""

    class FakeVectorStore:
        def __init__(self) -> None:
            self.search_kwargs: dict[str, object] = {}

        def similarity_search_with_score(self, query, **kwargs):  # noqa: ANN001
            self.search_kwargs = kwargs
            return [
                (build_document("first", 1), 0.1),
                (build_document("second", 2), 0.2),
                (build_document("third", 3), 0.3),
            ]

    class FakeReranker:
        candidate_multiplier = 4

        def rerank(self, query, documents, top_n):  # noqa: ANN001
            assert query == "目标岗位"
            assert documents == ["first", "second", "third"]
            assert top_n == 2
            return [RerankResult(index=2, relevance_score=0.98), RerankResult(index=0, relevance_score=0.71)]

    vector_store = FakeVectorStore()
    knowledge_base = RAGKnowledgeBase(
        tmp_path / "chroma",
        embeddings=LocalHashEmbeddings(),
        reranker=FakeReranker(),
    )
    knowledge_base._vector_store = lambda: vector_store  # type: ignore[method-assign]

    results = knowledge_base.search("目标岗位", top_k=2)

    assert vector_store.search_kwargs == {"k": 8}
    assert [result.content for result in results] == ["third", "first"]


def build_document(content: str, identifier: int) -> Document:
    """构造带完整 RAG metadata 的测试候选 chunk。"""

    return Document(
        page_content=content,
        metadata={
            "entity_type": "project_experience",
            "entity_id": identifier,
            "source_label": f"source-{identifier}",
            "long_text_id": identifier,
            "chunk_index": 0,
            "account_id": 1,
        },
    )
