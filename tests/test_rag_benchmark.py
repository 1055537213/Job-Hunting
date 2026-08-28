from __future__ import annotations

import json
from pathlib import Path

import pytest
import sqlalchemy as sa

from job_hunting_agent.evals.rag_benchmark import (
    RAGBenchmarkDocument,
    RAGBenchmarkSuite,
    load_rag_benchmark_suite,
    run_rag_benchmark,
)
from job_hunting_agent.evals.rag_eval import (
    EvidenceRef,
    RAGEvalCase,
    RAGEvalThresholds,
)
from job_hunting_agent.sqlalchemy_store import SQLAlchemyStore


def test_repository_golden_suite_covers_cross_industry_risks() -> None:
    suite = load_rag_benchmark_suite(Path("evals/rag/golden_suite.json"))

    assert len(suite.documents) >= 12
    assert len(suite.cases) >= 12
    assert {document.account_scope for document in suite.documents} == {
        "primary",
        "foreign",
    }
    tags = {tag for case in suite.cases for tag in case.tags}
    assert {
        "software",
        "industrial",
        "numeric",
        "table",
        "visual-summary",
        "conflict",
        "account-isolation",
    } <= tags
    assert suite.thresholds.min_mean_recall_at_n >= 0.8
    assert suite.thresholds.max_forbidden_case_rate == 0.0


def test_rag_benchmark_script_uses_isolated_suite_and_writes_report() -> None:
    script = Path("scripts/validate_rag_retrieval.ps1").read_text(encoding="utf-8")

    assert "job_hunting_agent.evals.rag_benchmark" in script
    assert "evals\\rag\\golden_suite.json" in script
    assert "data\\eval-reports" in script
    assert 'ValidateSet("configured", "local_hash")' in script
    assert "$env:PYTHONPATH" in script


def test_rag_benchmark_suite_rejects_duplicate_source_labels(tmp_path) -> None:
    path = tmp_path / "invalid-suite.json"
    path.write_text(
        json.dumps(
            {
                "name": "invalid",
                "documents": [
                    {
                        "id": "one",
                        "source_label": "duplicate",
                        "entity_type": "project_archive_file",
                        "text": "first",
                    },
                    {
                        "id": "two",
                        "source_label": "duplicate",
                        "entity_type": "project_archive_file",
                        "text": "second",
                    },
                ],
                "cases": [
                    {
                        "id": "query",
                        "query": "first",
                        "expected_source_labels": ["duplicate"],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    try:
        load_rag_benchmark_suite(path)
    except ValueError as error:
        assert "source_label" in str(error)
    else:  # pragma: no cover - failure branch documents the validation contract.
        raise AssertionError("duplicate source labels must be rejected")


def test_rag_benchmark_runs_in_isolated_accounts_and_cleans_up(database_url) -> None:
    suite = RAGBenchmarkSuite(
        name="isolated-local-hash-smoke",
        description="Test-only deterministic retrieval suite.",
        documents=(
            RAGBenchmarkDocument(
                id="primary-alpha",
                source_label="eval:primary-alpha",
                entity_type="project_archive_file",
                text="isolationgoldenalpha 当前账号的液压泵额定压力为 16 MPa。",
            ),
            RAGBenchmarkDocument(
                id="primary-beta",
                source_label="eval:primary-beta",
                entity_type="project_archive_file",
                text="isolationgoldenbeta 当前账号使用 FastAPI 构建异步接口。",
            ),
            RAGBenchmarkDocument(
                id="foreign-alpha",
                source_label="eval:foreign-alpha",
                entity_type="project_archive_file",
                text="isolationgoldenalpha 其他账号的液压泵额定压力为 25 MPa。",
                account_scope="foreign",
            ),
        ),
        cases=(
            RAGEvalCase(
                id="primary-alpha",
                query="isolationgoldenalpha",
                expected=(EvidenceRef(source_label="eval:primary-alpha"),),
                forbidden=(EvidenceRef(source_label="eval:foreign-alpha"),),
                top_n=1,
                tags=("account-isolation",),
            ),
            RAGEvalCase(
                id="primary-beta",
                query="isolationgoldenbeta",
                expected=(EvidenceRef(source_label="eval:primary-beta"),),
                top_n=1,
                tags=("software",),
            ),
        ),
        thresholds=RAGEvalThresholds(
            min_case_pass_rate=1.0,
            min_mean_recall_at_n=1.0,
            min_mean_reciprocal_rank=1.0,
            max_forbidden_case_rate=0.0,
        ),
    )
    engine = sa.create_engine(database_url)
    with engine.connect() as connection:
        account_count_before = connection.scalar(sa.text("SELECT COUNT(*) FROM accounts"))
        long_text_count_before = connection.scalar(sa.text("SELECT COUNT(*) FROM long_texts"))
        chunk_count_before = connection.scalar(sa.text("SELECT COUNT(*) FROM rag_chunks"))

    result = run_rag_benchmark(
        suite,
        database_url=database_url,
        embedding_mode="local_hash",
    )

    assert result.report.all_passed
    assert result.document_count == 3
    assert result.embedding_model.startswith("local-hash-")
    with engine.connect() as connection:
        assert connection.scalar(sa.text("SELECT COUNT(*) FROM accounts")) == account_count_before
        assert connection.scalar(sa.text("SELECT COUNT(*) FROM long_texts")) == long_text_count_before
        assert connection.scalar(sa.text("SELECT COUNT(*) FROM rag_chunks")) == chunk_count_before
    engine.dispose()


def test_rag_benchmark_cleans_first_account_when_second_account_creation_fails(
    database_url,
    monkeypatch,
) -> None:
    suite = load_rag_benchmark_suite(Path("evals/rag/golden_suite.json"))
    engine = sa.create_engine(database_url)
    with engine.connect() as connection:
        account_count_before = connection.scalar(sa.text("SELECT COUNT(*) FROM accounts"))
    original_create_account = SQLAlchemyStore.create_account
    call_count = 0

    def create_account_then_fail(self, *args, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 2:
            raise RuntimeError("simulated foreign account creation failure")
        return original_create_account(self, *args, **kwargs)

    monkeypatch.setattr(SQLAlchemyStore, "create_account", create_account_then_fail)

    with pytest.raises(RuntimeError, match="simulated foreign account"):
        run_rag_benchmark(
            suite,
            database_url=database_url,
            embedding_mode="local_hash",
        )

    with engine.connect() as connection:
        assert connection.scalar(sa.text("SELECT COUNT(*) FROM accounts")) == account_count_before
    engine.dispose()
