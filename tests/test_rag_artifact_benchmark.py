from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
import sqlalchemy as sa

from job_hunting_agent.evals.rag_artifact_benchmark import (
    GitHubArtifactBenchmarkSuite,
    PinnedGitHubArtifact,
    download_pinned_github_artifact,
    load_github_artifact_benchmark_suite,
    run_github_artifact_benchmark,
    verify_pinned_github_artifact,
)
from job_hunting_agent.evals.rag_eval import (
    EvidenceRef,
    RAGEvalCase,
    RAGEvalThresholds,
)
from job_hunting_agent.evals.rag_parameter_tuning import RAGParameterCombination


def _artifact(
    artifact_id: str,
    relative_path: str,
    content: bytes,
    *,
    expected_term: str,
) -> PinnedGitHubArtifact:
    return PinnedGitHubArtifact(
        id=artifact_id,
        industry="test",
        repository_url="https://github.com/example/project",
        commit_sha="a" * 40,
        source_path=f"fixtures/{Path(relative_path).name}",
        relative_path=relative_path,
        license_spdx="MIT",
        size_bytes=len(content),
        sha256=hashlib.sha256(content).hexdigest(),
        media_type="text/plain",
        expected_terms=(expected_term,),
    )


def test_repository_artifact_suite_is_pinned_and_cross_industry() -> None:
    suite = load_github_artifact_benchmark_suite(
        Path("evals/rag/github_artifact_suite.json")
    )

    assert len(suite.artifacts) == 12
    assert len(suite.cases) == 12
    assert {
        "industrial-engineering",
        "construction-bim",
        "healthcare",
        "accounting",
        "visual-design",
        "logistics",
    } <= {artifact.industry for artifact in suite.artifacts}
    assert all(len(artifact.commit_sha) == 40 for artifact in suite.artifacts)
    assert all(len(artifact.sha256) == 64 for artifact in suite.artifacts)
    assert all("/main/" not in artifact.raw_url for artifact in suite.artifacts)
    assert any(artifact.relative_path.endswith(".ifc") for artifact in suite.artifacts)
    assert any(artifact.require_visual_analysis for artifact in suite.artifacts)


def test_release_artifact_suite_has_hard_negatives_and_holdout() -> None:
    suite = load_github_artifact_benchmark_suite(
        Path("evals/rag/github_hard_negative_suite.json")
    )

    assert suite.benchmark_role == "release"
    assert len(suite.artifacts) >= 30
    assert len(suite.cases) >= 25
    assert len({artifact.industry for artifact in suite.artifacts}) >= 6
    assert (
        max(int(case.top_n or 0) for case in suite.cases) / len(suite.artifacts) <= 0.2
    )
    assert sum(bool(case.forbidden) for case in suite.cases) / len(suite.cases) >= 0.25
    assert (
        sum(case.split == "holdout" for case in suite.cases) / len(suite.cases) >= 0.2
    )
    assert suite.thresholds.min_mean_recall_at_1 > 0
    assert suite.thresholds.min_mean_ndcg_at_n > 0


def test_release_role_rejects_smoke_sized_suite() -> None:
    content = b"small release corpus"
    artifact = _artifact("small", "docs/small.md", content, expected_term="small")

    try:
        GitHubArtifactBenchmarkSuite(
            name="too-small-release",
            description="Must not be accepted as a release benchmark.",
            artifacts=(artifact,),
            cases=(
                RAGEvalCase(
                    id="small",
                    query="small",
                    expected=(EvidenceRef(source_label="docs/small.md"),),
                    top_n=1,
                    split="holdout",
                ),
            ),
            thresholds=RAGEvalThresholds(min_mean_recall_at_1=0.5),
            benchmark_role="release",
        )
    except ValueError as error:
        assert "30 artifacts and 25 cases" in str(error)
    else:  # pragma: no cover
        raise AssertionError("smoke-sized suites must not claim release status")


def test_visual_evaluation_requires_configured_cross_modal_embeddings() -> None:
    """视觉召回不能用只支持文字的 local-hash Embedding 冒充完整评测。"""

    content = b"visual fixture"
    suite = GitHubArtifactBenchmarkSuite(
        name="visual-mode-guard",
        description="Reject invalid visual evaluation mode.",
        artifacts=(
            _artifact(
                "visual",
                "images/current.png",
                content,
                expected_term="visual",
            ),
        ),
        cases=(
            RAGEvalCase(
                id="visual",
                query="visual",
                expected=(EvidenceRef(source_label="images/current.png"),),
                top_n=1,
            ),
        ),
    )

    with pytest.raises(ValueError, match="configured embeddings"):
        run_github_artifact_benchmark(
            suite,
            database_url="postgresql+psycopg://unused@localhost/unused",
            embedding_mode="local_hash",
            visual_mode="configured",
            fetcher=lambda _artifact: content,
        )


def test_artifact_rejects_floating_commit_and_unapproved_repository() -> None:
    content = b"fixed fixture"
    digest = hashlib.sha256(content).hexdigest()

    try:
        PinnedGitHubArtifact(
            id="floating",
            industry="test",
            repository_url="https://github.com/example/project",
            commit_sha="main",
            source_path="README.md",
            relative_path="README.md",
            license_spdx="MIT",
            size_bytes=len(content),
            sha256=digest,
            media_type="text/markdown",
        )
    except ValueError as error:
        assert "40-character commit" in str(error)
    else:  # pragma: no cover
        raise AssertionError("floating GitHub refs must be rejected")

    try:
        PinnedGitHubArtifact(
            id="wrong-host",
            industry="test",
            repository_url="https://example.com/example/project",
            commit_sha="a" * 40,
            source_path="README.md",
            relative_path="README.md",
            license_spdx="MIT",
            size_bytes=len(content),
            sha256=digest,
            media_type="text/markdown",
        )
    except ValueError as error:
        assert "repository URL" in str(error)
    else:  # pragma: no cover
        raise AssertionError("non-GitHub repository hosts must be rejected")


def test_artifact_content_verification_rejects_upstream_drift() -> None:
    content = b"expected fixed content"
    artifact = _artifact("fixed", "docs/fixed.md", content, expected_term="fixed")

    assert verify_pinned_github_artifact(artifact, content) == content
    try:
        verify_pinned_github_artifact(artifact, b"unexpected drifted content")
    except ValueError as error:
        assert "size does not match" in str(error)
    else:  # pragma: no cover
        raise AssertionError("changed upstream content must be rejected")


def test_artifact_download_retries_transient_read_timeout(monkeypatch) -> None:
    """固定材料下载遇到瞬时读取超时后应重新建立请求。"""

    content = b"pinned artifact content"
    artifact = _artifact("retry", "docs/retry.md", content, expected_term="pinned")
    calls = 0

    class FakeResponse:
        headers = {"Content-Length": str(len(content))}

        def __init__(self, fail: bool) -> None:
            self.fail = fail

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            return False

        def geturl(self) -> str:
            return artifact.raw_url

        def read(self, _limit: int) -> bytes:
            if self.fail:
                raise TimeoutError("temporary read timeout")
            return content

    class FakeOpener:
        def open(self, _request, *, timeout):
            nonlocal calls
            calls += 1
            return FakeResponse(fail=calls == 1)

    monkeypatch.setattr(
        "job_hunting_agent.evals.rag_artifact_benchmark.build_opener",
        lambda *_handlers: FakeOpener(),
    )

    downloaded = download_pinned_github_artifact(
        artifact,
        timeout_seconds=0.01,
        max_attempts=2,
        retry_delay_seconds=0,
    )

    assert downloaded == content
    assert calls == 2


def test_artifact_download_does_not_retry_catalog_mismatch(monkeypatch) -> None:
    """内容长度或哈希漂移属于确定性错误，不应通过重试掩盖。"""

    content = b"pinned artifact content"
    artifact = _artifact("drift", "docs/drift.md", content, expected_term="pinned")
    calls = 0

    class FakeResponse:
        headers = {"Content-Length": str(len(content) + 1)}

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            return False

        def geturl(self) -> str:
            return artifact.raw_url

    class FakeOpener:
        def open(self, _request, *, timeout):
            nonlocal calls
            calls += 1
            return FakeResponse()

    monkeypatch.setattr(
        "job_hunting_agent.evals.rag_artifact_benchmark.build_opener",
        lambda *_handlers: FakeOpener(),
    )

    with pytest.raises(ValueError, match="content length changed"):
        download_pinned_github_artifact(
            artifact,
            max_attempts=3,
            retry_delay_seconds=0,
        )

    assert calls == 1


def test_artifact_benchmark_runs_project_ingestion_and_cleans_up(database_url) -> None:
    readme = b"artifactgoldenalpha pump station pressure control evidence"
    ifc = b"\n".join(
        [
            b"ISO-10303-21;",
            b"FILE_SCHEMA(('IFC4'));",
            b"#100=IFCWALL('artifactgoldenbeta',$,'East Shear Wall',$);",
            b"END-ISO-10303-21;",
        ]
    )
    artifacts = (
        _artifact(
            "software-readme",
            "software/README.md",
            readme,
            expected_term="artifactgoldenalpha",
        ),
        _artifact(
            "construction-ifc",
            "construction/model.ifc",
            ifc,
            expected_term="artifactgoldenbeta",
        ),
    )
    suite = GitHubArtifactBenchmarkSuite(
        name="test-artifact-pipeline",
        description="Deterministic local-hash artifact benchmark.",
        artifacts=artifacts,
        cases=(
            RAGEvalCase(
                id="readme",
                query="artifactgoldenalpha",
                expected=(EvidenceRef(source_label="software/README.md"),),
                top_n=1,
            ),
            RAGEvalCase(
                id="ifc",
                query="artifactgoldenbeta",
                expected=(EvidenceRef(source_label="construction/model.ifc"),),
                top_n=1,
                split="holdout",
            ),
        ),
        thresholds=RAGEvalThresholds(
            min_case_pass_rate=1.0,
            min_mean_recall_at_n=1.0,
            min_mean_reciprocal_rank=1.0,
            max_forbidden_case_rate=0.0,
        ),
    )
    content_by_id = {
        artifacts[0].id: readme,
        artifacts[1].id: ifc,
    }
    engine = sa.create_engine(database_url)
    with engine.connect() as connection:
        account_count_before = connection.scalar(
            sa.text("SELECT COUNT(*) FROM accounts")
        )
        long_text_count_before = connection.scalar(
            sa.text("SELECT COUNT(*) FROM long_texts")
        )
        chunk_count_before = connection.scalar(
            sa.text("SELECT COUNT(*) FROM rag_chunks")
        )

    result = run_github_artifact_benchmark(
        suite,
        database_url=database_url,
        embedding_mode="local_hash",
        visual_mode="disabled",
        fetcher=lambda artifact: content_by_id[artifact.id],
        parameter_grid=(RAGParameterCombination(5, 1),),
        tuning_repetitions=2,
    )

    assert result.all_passed
    assert result.extraction_pass_count == 2
    assert result.extraction_pass_rate == 1.0
    assert result.visual_mode == "disabled"
    assert result.visual_item_count == 0
    assert result.visual_indexed_count == 0
    assert result.visual_index_passed
    assert result.to_dict()["extraction_pass_rate"] == 1.0
    assert result.to_dict()["visual_index_passed"] is True
    assert result.parameter_tuning is not None
    assert result.parameter_tuning.recommended == RAGParameterCombination(5, 1)
    assert result.parameter_tuning.passed
    assert (
        result.parameter_tuning.recommended_trial.aggregate_report.stage_latency_summary[
            "retrieval_rerank"
        ]["sample_count"]
        == 2
    )
    assert {item.file_kind for item in result.extraction_results} == {
        "source_text",
        "engineering_drawing",
    }
    assert all(item.visual_error_type is None for item in result.extraction_results)
    assert (
        next(
            item
            for item in result.extraction_results
            if item.artifact_id == "construction-ifc"
        ).extraction_method
        == "engineering_text"
    )
    with engine.connect() as connection:
        assert (
            connection.scalar(sa.text("SELECT COUNT(*) FROM accounts"))
            == account_count_before
        )
        assert (
            connection.scalar(sa.text("SELECT COUNT(*) FROM long_texts"))
            == long_text_count_before
        )
        assert (
            connection.scalar(sa.text("SELECT COUNT(*) FROM rag_chunks"))
            == chunk_count_before
        )
    engine.dispose()


def test_artifact_validation_script_uses_versioned_suite() -> None:
    script = Path("scripts/validate_rag_artifacts.ps1").read_text(encoding="utf-8")

    assert "job_hunting_agent.evals.rag_artifact_benchmark" in script
    assert '"github_artifact_suite.json"' in script
    assert "data\\eval-reports" in script
    assert 'ValidateSet("smoke", "release")' in script
    assert "github_hard_negative_suite.json" in script
    assert 'ValidateSet("configured", "local_hash")' in script
    assert 'ValidateSet("configured", "disabled")' in script
    assert "$TuneParameters" in script
    assert '"--tune-parameters"' in script
    assert '"--tuning-repetitions"' in script
