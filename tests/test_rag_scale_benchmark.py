"""Isolated pgvector scale benchmark tests."""

from __future__ import annotations

import sqlalchemy as sa

from job_hunting_agent.evals.rag_scale_benchmark import (
    QueryOutcome,
    QuerySpec,
    RAGScaleBenchmarkConfig,
    _ann_index_definition,
    _neighbor_recall,
    _semantic_coverage,
    format_rag_scale_benchmark,
    run_rag_scale_benchmark,
)


def test_scale_benchmark_selects_pgvector_index_type_by_dimension() -> None:
    assert _ann_index_definition(384) == (
        "vector_hnsw",
        "vector(384)",
        "vector_cosine_ops",
    )


def test_neighbor_recall_compares_ann_ids_with_halfvec_ground_truth() -> None:
    specs = (QuerySpec(account_id=7, cluster_id=3, embedding=[1.0, 0.0]),)
    ground_truth = [
        QueryOutcome(
            latency_ms=1.0,
            rows=(
                {"id": 11, "account_id": 7, "cluster_id": 3, "distance": 0.1},
                {"id": 12, "account_id": 7, "cluster_id": 3, "distance": 0.2},
            ),
        )
    ]
    ann_outcomes = [
        QueryOutcome(
            latency_ms=1.0,
            rows=(
                {"id": 11, "account_id": 7, "cluster_id": 3, "distance": 0.1},
                {"id": 99, "account_id": 7, "cluster_id": 3, "distance": 0.2},
            ),
        )
    ]

    assert _neighbor_recall(specs, ann_outcomes, ground_truth) == 0.5


def test_semantic_coverage_tracks_queries_with_relevant_top_k_evidence() -> None:
    specs = (QuerySpec(account_id=7, cluster_id=3, embedding=[1.0, 0.0]),)
    outcomes = [
        QueryOutcome(
            latency_ms=1.0,
            rows=tuple(
                {
                    "id": item_id,
                    "account_id": 7,
                    "cluster_id": 3 if item_id < 5 else 9,
                    "distance": item_id / 10,
                }
                for item_id in range(10)
            ),
        )
    ]

    assert _semantic_coverage(specs, outcomes, required_matches=1) == 1.0
    assert _semantic_coverage(specs, outcomes, required_matches=6) == 0.0
    assert _ann_index_definition(2560) == (
        "halfvec_hnsw",
        "halfvec(2560)",
        "halfvec_cosine_ops",
    )


def test_scale_benchmark_is_isolated_and_uses_ann_index(database_url: str) -> None:
    config = RAGScaleBenchmarkConfig(
        chunk_count=640,
        dimensions=16,
        tenant_count=2,
        cluster_count=8,
        query_count=8,
        top_k=5,
        concurrency_levels=(1, 2),
        hnsw_ef_search_candidates=(40,),
        enforce_speedup=False,
        force_ann_index=True,
    )

    result = run_rag_scale_benchmark(database_url, config)

    assert result.passed is True
    assert result.ann_index_used is True
    assert result.neighbor_recall == 1.0
    assert result.exact.isolation_violations == 0
    assert all(item.errors == 0 for item in result.concurrent_ann.values())
    assert "result=PASS" in format_rag_scale_benchmark(result)

    engine = sa.create_engine(database_url)
    try:
        with engine.connect() as connection:
            remaining = connection.scalar(
                sa.text(
                    "SELECT COUNT(*) FROM pg_tables "
                    "WHERE schemaname = current_schema() "
                    "AND tablename LIKE 'rag_scale_bench_%'"
                )
            )
    finally:
        engine.dispose()
    assert remaining == 0
