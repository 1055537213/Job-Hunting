"""通用 Embedding 与 Rerank 协议适配器测试。

所有请求都使用注入的假 transport，确保测试不会读取真实 `.env` 或访问网络。
"""

from __future__ import annotations

import pytest

from job_hunting_agent.config import (
    load_embedding_settings,
    load_rerank_settings,
    masked_rerank_settings,
)
from job_hunting_agent.model_resilience import CircuitBreaker, ModelCircuitOpenError
from job_hunting_agent.models import RAGSearchResult
from job_hunting_agent.pgvector_rag import PgVectorKnowledgeBase
from job_hunting_agent.rag import (
    EmbeddingRequestError,
    HttpReranker,
    NativeMultimodalEmbeddings,
    OpenAICompatibleEmbeddings,
    RerankRequestError,
    RerankResult,
    build_rag_embeddings,
    build_rag_retrieval_query,
    build_reranker,
    decompose_rag_query,
    filter_rag_candidates,
    rag_embedding_model_name,
    rerank_rag_results,
)


def write_native_env(path) -> None:
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
                "JOB_AGENT_RAG_RETRIEVAL_TOP_K=20",
                "JOB_AGENT_RERANK_MIN_RELEVANCE_SCORE=0.65",
                "JOB_AGENT_RERANK_RELATIVE_SCORE_THRESHOLD=0.86",
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
    assert rerank.retrieval_top_k == 20
    assert rerank.min_relevance_score == 0.65
    assert rerank.relative_score_threshold == 0.86
    assert rerank.candidate_multiplier == 4
    assert "test-rag-key" not in str(masked_rerank_settings(rerank))
    assert isinstance(build_rag_embeddings(env_file), NativeMultimodalEmbeddings)
    assert isinstance(build_reranker(env_file), HttpReranker)


def test_rerank_settings_use_tuned_default_retrieval_top_k(tmp_path):
    env_file = tmp_path / ".env"
    write_native_env(env_file)
    env_file.write_text(
        env_file.read_text(encoding="utf-8").replace(
            "JOB_AGENT_RAG_RETRIEVAL_TOP_K=20\n",
            "",
        ),
        encoding="utf-8",
    )

    rerank = load_rerank_settings(env_file, environ={})

    assert rerank is not None
    assert rerank.retrieval_top_k == 10
    assert rerank.candidate_multiplier == 2


def test_rerank_settings_keep_explicit_legacy_candidate_multiplier(tmp_path):
    env_file = tmp_path / ".env"
    write_native_env(env_file)
    env_file.write_text(
        env_file.read_text(encoding="utf-8").replace(
            "JOB_AGENT_RAG_RETRIEVAL_TOP_K=20",
            "JOB_AGENT_RERANK_CANDIDATE_MULTIPLIER=6",
        ),
        encoding="utf-8",
    )

    rerank = load_rerank_settings(env_file, environ={})

    assert rerank is not None
    assert rerank.retrieval_top_k == 30
    assert rerank.candidate_multiplier == 6


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

    def fake_transport(url, headers, payload, timeout):
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


def test_native_multimodal_embeddings_send_base64_images_as_independent_items():
    """视觉索引使用 Base64 Data URI，且不把多张图片融合成一个向量。"""

    captured: dict[str, object] = {}

    def fake_transport(url, headers, payload, timeout):
        captured.update(payload=payload)
        return {
            "output": {
                "embeddings": [
                    {"index": 1, "type": "vl", "embedding": [0.3, 0.4]},
                    {"index": 0, "type": "vl", "embedding": [0.1, 0.2]},
                ]
            },
            "usage": {"input_tokens": 2, "image_tokens": 30, "total_tokens": 32},
        }

    embeddings = NativeMultimodalEmbeddings(
        api_key="test-key",
        base_url="https://embedding.example/v1/encode",
        model="qwen3-vl-embedding",
        dimensions=1024,
        transport=fake_transport,
    )

    vectors = embeddings.embed_images(
        [(b"first-image", "image/png"), (b"second-image", "image/jpeg")]
    )

    assert vectors == [[0.1, 0.2], [0.3, 0.4]]
    payload = captured["payload"]
    assert payload["model"] == "qwen3-vl-embedding"
    assert payload["parameters"] == {"dimension": 1024}
    assert "enable_fusion" not in payload.get("parameters", {})
    contents = payload["input"]["contents"]
    assert contents[0]["image"].startswith("data:image/png;base64,")
    assert contents[1]["image"].startswith("data:image/jpeg;base64,")


def test_native_multimodal_embeddings_cap_text_batches_at_provider_limit():
    """原生多模态文本批次不得超过供应商单次 20 条的限制。"""

    batch_sizes: list[int] = []

    def fake_transport(url, headers, payload, timeout):
        contents = payload["input"]["contents"]
        batch_sizes.append(len(contents))
        return {
            "output": {
                "embeddings": [
                    {"index": index, "embedding": [float(index), 1.0]}
                    for index in range(len(contents))
                ]
            }
        }

    embeddings = NativeMultimodalEmbeddings(
        api_key="test-key",
        base_url="https://embedding.example/v1/encode",
        model="qwen3-vl-embedding",
        batch_size=64,
        transport=fake_transport,
    )

    vectors = embeddings.embed_documents([f"document-{index}" for index in range(45)])

    assert batch_sizes == [20, 20, 5]
    assert len(vectors) == 45


def test_native_reranker_uses_query_documents_payload_and_preserves_indexes():
    """native 重排器仅返回候选索引，调用方可复用本地 chunk。"""

    captured: dict[str, object] = {}

    def fake_transport(url, headers, payload, timeout):
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


def test_rag_final_top_n_deduplicates_same_source_after_rerank():
    """最终 Top-N 不应被同一份材料的多个 Chunk 占满。"""

    candidates = [
        RAGSearchResult("a1", "text", 1, "source-a", 101, 0, 0.10),
        RAGSearchResult("a2", "text", 1, "source-a", 101, 1, 0.11),
        RAGSearchResult("b1", "text", 2, "source-b", 102, 0, 0.12),
        RAGSearchResult("c1", "text", 3, "source-c", 103, 0, 0.13),
    ]

    class FakeReranker:
        retrieval_top_k = 20

        def rerank(self, query, documents, top_n):
            assert query == "target"
            assert len(documents) == 4
            assert top_n == 4
            return [
                RerankResult(index=1, relevance_score=0.99),
                RerankResult(index=0, relevance_score=0.98),
                RerankResult(index=2, relevance_score=0.90),
                RerankResult(index=3, relevance_score=0.80),
            ]

    results = rerank_rag_results("target", candidates, top_n=3, reranker=FakeReranker())

    assert [result.source_label for result in results] == [
        "source-a",
        "source-b",
        "source-c",
    ]
    assert results[0].chunk_index == 1
    assert [result.relevance_score for result in results] == [0.99, 0.90, 0.80]


def test_rag_filters_explicitly_forbidden_terms_before_reranking():
    """明确的否定条件应在重排前排除来源和证据正文中的禁用对象。"""

    candidates = [
        RAGSearchResult(
            "CURRENT CAPTURE: columns and people",
            "visual",
            1,
            "construction/images/current_capture.jpg",
            101,
            -1,
            0.1,
            evidence_kind="visual",
        ),
        RAGSearchResult(
            "BASELINE: blank reference image",
            "visual",
            2,
            "construction/images/ground_east_baseline.jpg",
            102,
            -1,
            0.2,
            evidence_kind="visual",
        ),
    ]

    filtered = filter_rag_candidates(
        "只查找 CURRENT CAPTURE，不要返回空白基线图。",
        candidates,
    )

    assert [item.entity_id for item in filtered] == [1]


def test_rag_negative_alias_does_not_remove_current_image_that_mentions_baseline():
    """中文派生的 baseline 别名只匹配来源名，不能误删同时展示对照标签的正例。"""

    candidates = [
        RAGSearchResult(
            "Labels: NORTHSTAR - BASELINE and CURRENT CAPTURE; columns and diagonal beam",
            "visual",
            1,
            "construction/images/ground_east_current.jpg",
            101,
            -1,
            0.1,
            evidence_kind="visual",
        ),
        RAGSearchResult(
            "BASELINE: blank reference image",
            "visual",
            2,
            "construction/images/ground_east_baseline.jpg",
            102,
            -1,
            0.2,
            evidence_kind="visual",
        ),
    ]

    filtered = filter_rag_candidates(
        "只查找 CURRENT CAPTURE，不要返回空白基线图。",
        candidates,
    )

    assert [item.entity_id for item in filtered] == [1]


def test_rag_positive_label_is_prioritized_before_top_n_cutoff():
    """正向大写标签命中的视觉证据应在最终截断前优先保留。"""

    candidates = [
        RAGSearchResult(
            "A generic construction frame with columns",
            "visual",
            1,
            "construction/images/frame_00060.jpg",
            101,
            -1,
            0.1,
            evidence_kind="visual",
        ),
        RAGSearchResult(
            "CURRENT CAPTURE with columns, diagonal beam, and a person marker",
            "visual",
            2,
            "construction/images/ground_east_current.jpg",
            102,
            -1,
            0.2,
            evidence_kind="visual",
        ),
    ]

    class FakeReranker:
        retrieval_top_k = 20

        def rerank(self, query, documents, top_n):
            return [
                RerankResult(index=0, relevance_score=0.90),
                RerankResult(index=1, relevance_score=0.80),
            ]

    results = rerank_rag_results(
        "只查找 CURRENT CAPTURE 中的柱体和现场人员标记。",
        candidates,
        top_n=1,
        reranker=FakeReranker(),
    )

    assert [item.source_label for item in results] == [
        "construction/images/ground_east_current.jpg"
    ]


def test_rag_negative_clause_uses_positive_query_for_reranking_after_filtering():
    """否定对象既不会送进重排候选，也不会污染重排查询。"""

    query = "只查找 CURRENT CAPTURE，不要返回空白基线图。"
    assert build_rag_retrieval_query(query) == "只查找 CURRENT CAPTURE"
    candidates = [
        RAGSearchResult(
            "CURRENT CAPTURE: columns and people",
            "visual",
            1,
            "construction/images/current_capture.jpg",
            101,
            -1,
            0.1,
            evidence_kind="visual",
        ),
        RAGSearchResult(
            "BASELINE: blank reference image",
            "visual",
            2,
            "construction/images/ground_east_baseline.jpg",
            102,
            -1,
            0.2,
            evidence_kind="visual",
        ),
    ]
    captured: dict[str, object] = {}

    class FakeReranker:
        retrieval_top_k = 20

        def rerank(self, query, documents, top_n):
            captured.update(query=query, documents=documents, top_n=top_n)
            return [RerankResult(index=0, relevance_score=1.0)]

    results = rerank_rag_results(query, candidates, top_n=1, reranker=FakeReranker())

    assert captured == {
        "query": "只查找 CURRENT CAPTURE",
        "documents": [
            "Source: construction/images/current_capture.jpg\n"
            "Evidence kind: visual\n"
            "CURRENT CAPTURE: columns and people"
        ],
        "top_n": 1,
    }
    assert [item.entity_id for item in results] == [1]


def test_rag_query_decomposition_only_splits_explicit_workflows():
    """只拆解明确的流程、多来源查询，普通问题保持单查询。"""

    assert decompose_rag_query(
        "完整追踪第一张销售发票从开票、付款、银行入账到对账匹配的四份证据。"
    ) == (
        "完整追踪第一张销售发票 开票",
        "完整追踪第一张销售发票 付款",
        "完整追踪第一张销售发票 银行入账",
        "完整追踪第一张销售发票 对账匹配",
    )
    assert decompose_rag_query(
        "普通查询需要 Python FastAPI 项目经历。"
    ) == ("普通查询需要 Python FastAPI 项目经历。",)


def test_pgvector_search_merges_decomposed_candidates_before_one_final_rerank(monkeypatch):
    """多步骤查询应分别召回，再把合并结果统一交给一次最终重排。"""

    knowledge_base = object.__new__(PgVectorKnowledgeBase)
    captured: dict[str, object] = {}
    calls: list[tuple[str, int, bool]] = []

    class FakeReranker:
        retrieval_top_k = 20

        def rerank(self, query, documents, top_n):
            captured.update(query=query, documents=documents, top_n=top_n)
            return [
                RerankResult(index=index, relevance_score=1.0 - index / 10)
                for index in range(len(documents))
            ]

    knowledge_base.reranker = FakeReranker()

    def fake_search_single(self, query, top_n, entity_types=None, account_id=None,
                           candidate_id=None, retrieval_top_k=None, *, _rerank=True):
        calls.append((query, top_n, _rerank))
        stage = len(calls)
        return [
            RAGSearchResult(
                f"evidence-{stage}",
                "text",
                stage,
                f"stage-{stage}",
                100 + stage,
                0,
                0.1,
            )
        ]

    monkeypatch.setattr(PgVectorKnowledgeBase, "_search_single", fake_search_single)

    results = knowledge_base.search(
        "完整追踪第一张销售发票从开票、付款、银行入账到对账匹配的四份证据。",
        top_n=4,
    )

    assert calls == [
            ("完整追踪第一张销售发票 开票", 2, True),
            ("完整追踪第一张销售发票 付款", 2, True),
            ("完整追踪第一张销售发票 银行入账", 2, True),
            ("完整追踪第一张销售发票 对账匹配", 2, True),
    ]
    assert captured["query"] == "完整追踪第一张销售发票从开票、付款、银行入账到对账匹配的四份证据"
    assert captured["top_n"] == 4
    assert captured["documents"] == [
        "Source: stage-1\nEvidence kind: text\nevidence-1",
        "Source: stage-2\nEvidence kind: text\nevidence-2",
        "Source: stage-3\nEvidence kind: text\nevidence-3",
        "Source: stage-4\nEvidence kind: text\nevidence-4",
    ]
    assert [item.source_label for item in results] == [
        "stage-1",
        "stage-2",
        "stage-3",
        "stage-4",
    ]


def test_rag_numeric_consistency_moves_wrong_measurement_after_matching_one():
    """同一对象的错误数值不能排在匹配查询数值之前。"""

    candidates = [
        RAGSearchResult(
            "泵轴直径 53.69 mm",
            "text",
            1,
            "drawing-wrong",
            101,
            0,
            0.1,
        ),
        RAGSearchResult(
            "泵轴直径 65.53 mm",
            "text",
            2,
            "drawing-correct",
            102,
            0,
            0.2,
        ),
    ]

    class FakeReranker:
        retrieval_top_k = 20

        def rerank(self, query, documents, top_n):
            return [
                RerankResult(index=0, relevance_score=0.99),
                RerankResult(index=1, relevance_score=0.98),
            ]

    results = rerank_rag_results(
        "泵轴直径 65.53 mm",
        candidates,
        top_n=2,
        reranker=FakeReranker(),
    )

    assert [item.source_label for item in results] == [
        "drawing-correct",
        "drawing-wrong",
    ]


def test_rag_confidence_filter_removes_relative_hard_negatives():
    """最终 Top-N 是上限；明显低于首条证据的硬负样本不应继续进入上下文。"""

    candidates = [
        RAGSearchResult("目标证据", "text", 1, "expected", 101, 0, 0.1),
        RAGSearchResult("相似但错误", "text", 2, "hard-negative", 102, 0, 0.2),
        RAGSearchResult("无关内容", "text", 3, "unrelated", 103, 0, 0.3),
    ]

    class FakeReranker:
        retrieval_top_k = 20
        min_relevance_score = 0.65
        relative_score_threshold = 0.85

        def rerank(self, query, documents, top_n):
            return [
                RerankResult(index=0, relevance_score=0.90),
                RerankResult(index=1, relevance_score=0.70),
                RerankResult(index=2, relevance_score=0.40),
            ]

    results = rerank_rag_results(
        "目标查询",
        candidates,
        top_n=3,
        reranker=FakeReranker(),
    )

    assert [item.source_label for item in results] == ["expected"]


def test_rag_identity_anchors_remove_partial_match_hard_negative():
    """单一目标已有完整英文锚点命中时，不保留只覆盖一半名称的相似图纸。"""

    candidates = [
        RAGSearchResult(
            "Wind turbine pitch drive RotaHub with Support Ring and EndCap",
            "text",
            1,
            "industrial/drawings/ASM479942.pdf",
            101,
            0,
            0.1,
        ),
        RAGSearchResult(
            "Spindle assembly RotaHub with Ring and EndCap",
            "text",
            2,
            "industrial/drawings/SP-MT33969129T05.pdf",
            102,
            0,
            0.2,
        ),
    ]

    class FakeReranker:
        retrieval_top_k = 20
        min_relevance_score = 0.65
        relative_score_threshold = 0.85

        def rerank(self, query, documents, top_n):
            return [
                RerankResult(index=0, relevance_score=0.90),
                RerankResult(index=1, relevance_score=0.78),
            ]

    results = rerank_rag_results(
        "风力变桨 RotaHub 的 Support Ring 和 EndCap 属于哪份图？",
        candidates,
        top_n=2,
        reranker=FakeReranker(),
    )

    assert [item.source_label for item in results] == [
        "industrial/drawings/ASM479942.pdf"
    ]


def test_rag_confidence_filter_abstains_when_every_candidate_is_weak():
    """没有候选达到最低可信线时应返回空结果，不把最相似项伪装成答案。"""

    candidates = [
        RAGSearchResult("弱候选 A", "text", 1, "weak-a", 101, 0, 0.1),
        RAGSearchResult("弱候选 B", "text", 2, "weak-b", 102, 0, 0.2),
    ]

    class FakeReranker:
        retrieval_top_k = 20
        min_relevance_score = 0.65
        relative_score_threshold = 0.85

        def rerank(self, query, documents, top_n):
            return [
                RerankResult(index=0, relevance_score=0.56),
                RerankResult(index=1, relevance_score=0.52),
            ]

    assert rerank_rag_results(
        "没有可靠证据的问题",
        candidates,
        top_n=2,
        reranker=FakeReranker(),
    ) == []


def test_rag_confidence_filter_preserves_decomposed_stage_count():
    """流程查询即使阶段分数有差异，也应保留每个阶段需要的结果数量。"""

    candidates = [
        RAGSearchResult(f"阶段 {index}", "text", index, f"stage-{index}", 100 + index, 0, 0.1)
        for index in range(1, 5)
    ]

    class FakeReranker:
        retrieval_top_k = 20
        min_relevance_score = 0.65
        relative_score_threshold = 0.85

        def rerank(self, query, documents, top_n):
            return [
                RerankResult(index=0, relevance_score=0.90),
                RerankResult(index=1, relevance_score=0.80),
                RerankResult(index=2, relevance_score=0.70),
                RerankResult(index=3, relevance_score=0.66),
            ]

    results = rerank_rag_results(
        "完整追踪发票从开票、付款、银行入账到对账匹配的四份证据。",
        candidates,
        top_n=4,
        reranker=FakeReranker(),
    )

    assert [item.source_label for item in results] == [
        "stage-1",
        "stage-2",
        "stage-3",
        "stage-4",
    ]


def test_standard_reranker_uses_common_rerank_payload():
    """standard 协议使用常见 /rerank 请求和 results 响应。"""

    captured: dict[str, object] = {}

    def fake_transport(url, headers, payload, timeout):
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


def test_embedding_circuit_breaker_fails_fast_after_transient_provider_error():
    """Embedding 连续 503 后应快速拒绝新请求，避免重复等待上游超时。"""

    breaker = CircuitBreaker(failure_threshold=1, recovery_seconds=10)
    calls = 0

    def failing_transport(url, headers, payload, timeout):
        nonlocal calls
        calls += 1
        raise EmbeddingRequestError("embedding unavailable", status_code=503)

    embeddings = OpenAICompatibleEmbeddings(
        api_key="test-key",
        base_url="https://embedding.example/v1",
        model="embedding-model",
        transport=failing_transport,
        max_retries=0,
        circuit_breaker=breaker,
    )

    with pytest.raises(EmbeddingRequestError):
        embeddings.embed_query("职位要求")
    with pytest.raises(ModelCircuitOpenError):
        embeddings.embed_query("候选人经历")

    assert calls == 1
    assert breaker.snapshot().state == "open"


def test_rerank_auth_error_does_not_open_circuit_or_retry():
    """Rerank 鉴权失败不是临时故障，不应重试或触发熔断。"""

    breaker = CircuitBreaker(failure_threshold=1, recovery_seconds=10)
    calls = 0

    def unauthorized_transport(url, headers, payload, timeout):
        nonlocal calls
        calls += 1
        raise RerankRequestError("invalid api key", status_code=401)

    reranker = HttpReranker(
        api_key="test-key",
        base_url="https://rerank.example/v1",
        model="rerank-model",
        transport=unauthorized_transport,
        max_retries=2,
        circuit_breaker=breaker,
    )

    with pytest.raises(RerankRequestError):
        reranker.rerank("目标岗位", ["项目经历"], top_n=1)

    assert calls == 1
    assert breaker.snapshot().state == "closed"
