from __future__ import annotations

import time

from job_hunting_agent.evals.rag_eval import (
    EvidenceRef,
    RAGEvalCase,
    RAGSearchObservation,
    RAGEvalThresholds,
    evaluate_rag_cases,
)
from job_hunting_agent.evals.rag_parameter_tuning import (
    RAGParameterCombination,
    RAGPerformanceRetentionTolerances,
    evaluate_rag_parameter_grid,
    format_rag_parameter_tuning_result,
)


def test_eval_override_top_n_ignores_case_default_and_records_latency() -> None:
    requested_top_n: list[int] = []
    case = RAGEvalCase(
        id="fixed-n",
        query="target",
        expected=(EvidenceRef(source_label="target"),),
        top_n=5,
    )

    def search(_case: RAGEvalCase, top_n: int) -> RAGSearchObservation:
        requested_top_n.append(top_n)
        return RAGSearchObservation(
            hits=[_hit("target")],
            stage_durations_ms={
                "retrieval_rerank": 12.5,
                "visual_reinspection": 2.5,
            },
        )

    report = evaluate_rag_cases([case], search, override_top_n=3)

    assert requested_top_n == [3]
    assert report.case_results[0].top_n == 3
    assert report.case_results[0].duration_ms >= 0
    assert report.to_dict()["p95_latency_ms"] >= 0
    assert report.stage_latency_summary["retrieval_rerank"]["p95_ms"] == 12.5


def test_parameter_tuning_selects_on_development_then_verifies_holdout() -> None:
    cases = (
        RAGEvalCase(
            id="development-easy",
            query="easy",
            expected=(EvidenceRef(source_label="easy-target"),),
            top_n=5,
            split="development",
        ),
        RAGEvalCase(
            id="development-hard",
            query="hard",
            expected=(EvidenceRef(source_label="hard-target"),),
            top_n=5,
            split="development",
        ),
        RAGEvalCase(
            id="holdout",
            query="holdout",
            expected=(EvidenceRef(source_label="holdout-target"),),
            top_n=5,
            split="holdout",
        ),
    )
    calls: list[tuple[str, int, int]] = []

    def search(case: RAGEvalCase, top_k: int, top_n: int) -> list[dict[str, object]]:
        calls.append((case.split, top_k, top_n))
        if case.id == "development-hard" and top_k < 15:
            return [_hit("noise")]
        return [_hit(f"{case.id.removeprefix('development-')}-target")]

    result = evaluate_rag_parameter_grid(
        cases,
        search,
        combinations=(
            RAGParameterCombination(10, 3),
            RAGParameterCombination(15, 3),
        ),
        baseline=RAGParameterCombination(20, 5),
        thresholds=RAGEvalThresholds(
            min_case_pass_rate=1.0,
            min_mean_recall_at_n=1.0,
        ),
    )

    assert result.recommended == RAGParameterCombination(15, 3)
    assert result.passed
    assert not result.holdout_performance_failures
    assert not next(
        trial
        for trial in result.trials
        if trial.parameters == RAGParameterCombination(10, 3)
    ).quality_retained
    first_holdout_call = next(
        index for index, call in enumerate(calls) if call[0] == "holdout"
    )
    assert all(call[0] == "development" for call in calls[:first_holdout_call])
    assert calls[first_holdout_call:] == [
        ("holdout", 20, 5),
        ("holdout", 15, 3),
    ]
    assert result.to_dict()["holdout_used_for_selection"] is False
    assert "recommended=K=15,N=3" in format_rag_parameter_tuning_result(result)


def test_parameter_tuning_rejects_holdout_latency_regression() -> None:
    cases = (
        RAGEvalCase(
            id="development",
            query="development",
            expected=(EvidenceRef(source_label="target"),),
            split="development",
        ),
        RAGEvalCase(
            id="holdout",
            query="holdout",
            expected=(EvidenceRef(source_label="target"),),
            split="holdout",
        ),
    )

    def search(case: RAGEvalCase, top_k: int, _top_n: int) -> list[dict[str, object]]:
        if case.split == "holdout" and top_k == 10:
            time.sleep(0.01)
        return [_hit("target")]

    result = evaluate_rag_parameter_grid(
        cases,
        search,
        combinations=(RAGParameterCombination(10, 3),),
        baseline=RAGParameterCombination(20, 5),
        thresholds=RAGEvalThresholds(),
        performance_tolerances=RAGPerformanceRetentionTolerances(
            mean_latency_increase_ratio=0.0,
            p95_latency_increase_ratio=0.0,
            minimum_latency_allowance_ms=1.0,
        ),
    )

    assert result.recommended == RAGParameterCombination(10, 3)
    assert not result.passed
    assert any(
        "mean_latency_ms" in failure for failure in result.holdout_performance_failures
    )


def test_parameter_tuning_repeats_interleaved_and_selects_on_core_latency() -> None:
    cases = (
        RAGEvalCase(
            id="development",
            query="development",
            expected=(EvidenceRef(source_label="target"),),
            split="development",
        ),
        RAGEvalCase(
            id="holdout",
            query="holdout",
            expected=(EvidenceRef(source_label="target"),),
            split="holdout",
        ),
    )
    calls: list[tuple[str, int]] = []

    def search(
        case: RAGEvalCase,
        top_k: int,
        _top_n: int,
    ) -> RAGSearchObservation:
        calls.append((case.split, top_k))
        return RAGSearchObservation(
            hits=[_hit("target")],
            stage_durations_ms={
                "retrieval_rerank": float(top_k),
                "visual_reinspection": 100.0,
            },
        )

    result = evaluate_rag_parameter_grid(
        cases,
        search,
        combinations=(RAGParameterCombination(10, 5),),
        baseline=RAGParameterCombination(20, 5),
        thresholds=RAGEvalThresholds(),
        measurement_repetitions=3,
    )

    assert result.measurement_repetitions == 3
    assert result.recommended == RAGParameterCombination(10, 5)
    assert (
        calls[:6]
        == [
            ("development", 10),
            ("development", 20),
        ]
        * 3
    )
    assert calls[6:] == [("holdout", 20), ("holdout", 10)] * 3
    recommended = result.recommended_trial
    assert recommended is not None
    assert recommended.measurement_repetitions == 3
    assert recommended.selection_p95_latency_ms == 10.0
    assert (
        recommended.aggregate_report.stage_latency_summary["visual_reinspection"][
            "sample_count"
        ]
        == 3
    )


def test_parameter_combination_rejects_top_n_larger_than_top_k() -> None:
    try:
        RAGParameterCombination(3, 5)
    except ValueError as error:
        assert "cannot be smaller" in str(error)
    else:  # pragma: no cover
        raise AssertionError("Top-K smaller than Top-N must be rejected")


def _hit(source_label: str) -> dict[str, object]:
    return {
        "long_text_id": 1,
        "source_label": source_label,
        "entity_type": "project_archive_file",
        "entity_id": 1,
        "content": source_label,
    }
