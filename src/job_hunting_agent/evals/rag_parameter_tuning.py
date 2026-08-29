"""Controlled Retriever Top-K and Reranker Top-N parameter experiments."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass

from job_hunting_agent.config import MAX_RAG_RERANK_TOP_N, MAX_RAG_RETRIEVAL_TOP_K
from job_hunting_agent.evals.rag_eval import (
    RAGEvalCase,
    RAGEvalReport,
    RAGSearchObservation,
    RAGEvalThresholds,
    evaluate_rag_cases,
)

ParameterizedSearchBackend = Callable[
    [RAGEvalCase, int, int],
    Sequence[object] | RAGSearchObservation,
]


@dataclass(frozen=True, order=True)
class RAGParameterCombination:
    """One Retriever/Reranker parameter pair."""

    retrieval_top_k: int
    rerank_top_n: int

    def __post_init__(self) -> None:
        if self.retrieval_top_k <= 0 or self.rerank_top_n <= 0:
            raise ValueError("RAG tuning K and N must be positive integers.")
        if self.retrieval_top_k < self.rerank_top_n:
            raise ValueError("RAG tuning retrieval Top-K cannot be smaller than Top-N.")
        if self.retrieval_top_k > MAX_RAG_RETRIEVAL_TOP_K:
            raise ValueError(
                f"RAG tuning Top-K cannot exceed {MAX_RAG_RETRIEVAL_TOP_K}."
            )
        if self.rerank_top_n > MAX_RAG_RERANK_TOP_N:
            raise ValueError(f"RAG tuning Top-N cannot exceed {MAX_RAG_RERANK_TOP_N}.")

    @property
    def label(self) -> str:
        return f"K={self.retrieval_top_k},N={self.rerank_top_n}"


@dataclass(frozen=True)
class RAGQualityRetentionTolerances:
    """Maximum accepted quality loss against the current production baseline."""

    case_pass_rate_loss: float = 0.0
    recall_loss: float = 0.01
    ranking_loss: float = 0.02
    forbidden_case_rate_increase: float = 0.0

    def __post_init__(self) -> None:
        for name, value in asdict(self).items():
            if not 0.0 <= float(value) <= 1.0:
                raise ValueError(
                    f"RAG tuning tolerance {name} must be between 0 and 1."
                )


@dataclass(frozen=True)
class RAGPerformanceRetentionTolerances:
    """Allowed holdout latency increase before a parameter rollout is rejected."""

    mean_latency_increase_ratio: float = 0.25
    p95_latency_increase_ratio: float = 0.50
    minimum_latency_allowance_ms: float = 500.0

    def __post_init__(self) -> None:
        if self.mean_latency_increase_ratio < 0:
            raise ValueError("Mean latency increase ratio cannot be negative.")
        if self.p95_latency_increase_ratio < 0:
            raise ValueError("P95 latency increase ratio cannot be negative.")
        if self.minimum_latency_allowance_ms < 0:
            raise ValueError("Minimum latency allowance cannot be negative.")


@dataclass(frozen=True)
class RAGParameterTrial:
    """Development-set result for one parameter pair."""

    parameters: RAGParameterCombination
    report: RAGEvalReport
    quality_retained: bool
    rejection_reasons: tuple[str, ...] = ()
    repeat_reports: tuple[RAGEvalReport, ...] = ()

    @property
    def reports(self) -> tuple[RAGEvalReport, ...]:
        return (self.report, *self.repeat_reports)

    @property
    def aggregate_report(self) -> RAGEvalReport:
        return _aggregate_reports(self.reports)

    @property
    def measurement_repetitions(self) -> int:
        return len(self.reports)

    @property
    def selection_p95_latency_ms(self) -> float:
        core = self.aggregate_report.stage_latency_summary.get(
            "retrieval_rerank",
        )
        if core is not None:
            return float(core["p95_ms"])
        return self.aggregate_report.p95_latency_ms

    @property
    def estimated_retrieval_candidates(self) -> int:
        return (
            self.parameters.retrieval_top_k
            * self.report.case_count
            * self.measurement_repetitions
        )

    @property
    def estimated_context_results(self) -> int:
        return (
            self.parameters.rerank_top_n
            * self.report.case_count
            * self.measurement_repetitions
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "parameters": asdict(self.parameters),
            "quality_retained": self.quality_retained,
            "rejection_reasons": list(self.rejection_reasons),
            "measurement_repetitions": self.measurement_repetitions,
            "selection_p95_latency_ms": self.selection_p95_latency_ms,
            "estimated_retrieval_candidates": self.estimated_retrieval_candidates,
            "estimated_context_results": self.estimated_context_results,
            "report": self.report.to_dict(),
            "repeat_reports": [report.to_dict() for report in self.repeat_reports],
            "aggregate_report": self.aggregate_report.to_dict(),
        }


@dataclass(frozen=True)
class RAGParameterTuningResult:
    """Development selection plus the one-time holdout verification."""

    baseline: RAGParameterCombination
    trials: tuple[RAGParameterTrial, ...]
    recommended: RAGParameterCombination | None
    baseline_holdout_report: RAGEvalReport
    recommended_holdout_report: RAGEvalReport | None
    tolerances: RAGQualityRetentionTolerances
    performance_tolerances: RAGPerformanceRetentionTolerances
    measurement_repetitions: int
    baseline_holdout_repeat_reports: tuple[RAGEvalReport, ...] = ()
    recommended_holdout_repeat_reports: tuple[RAGEvalReport, ...] = ()

    @property
    def baseline_development_report(self) -> RAGEvalReport:
        return next(
            trial.report for trial in self.trials if trial.parameters == self.baseline
        )

    @property
    def recommended_trial(self) -> RAGParameterTrial | None:
        if self.recommended is None:
            return None
        return next(
            trial for trial in self.trials if trial.parameters == self.recommended
        )

    @property
    def baseline_holdout_reports(self) -> tuple[RAGEvalReport, ...]:
        return (
            self.baseline_holdout_report,
            *self.baseline_holdout_repeat_reports,
        )

    @property
    def recommended_holdout_reports(self) -> tuple[RAGEvalReport, ...]:
        if self.recommended_holdout_report is None:
            return ()
        return (
            self.recommended_holdout_report,
            *self.recommended_holdout_repeat_reports,
        )

    @property
    def aggregate_baseline_holdout_report(self) -> RAGEvalReport:
        return _aggregate_reports(self.baseline_holdout_reports)

    @property
    def aggregate_recommended_holdout_report(self) -> RAGEvalReport | None:
        if not self.recommended_holdout_reports:
            return None
        return _aggregate_reports(self.recommended_holdout_reports)

    @property
    def passed(self) -> bool:
        trial = self.recommended_trial
        return bool(
            trial is not None
            and trial.quality_retained
            and self.recommended_holdout_report is not None
            and all(report.all_passed for report in self.recommended_holdout_reports)
            and not self.holdout_performance_failures
        )

    @property
    def holdout_performance_failures(self) -> tuple[str, ...]:
        recommended_report = self.aggregate_recommended_holdout_report
        if recommended_report is None:
            return ("recommended holdout report is missing",)
        baseline_report = self.aggregate_baseline_holdout_report
        checks = (
            (
                "mean_latency_ms",
                recommended_report.mean_latency_ms,
                baseline_report.mean_latency_ms,
                self.performance_tolerances.mean_latency_increase_ratio,
            ),
            (
                "p95_latency_ms",
                recommended_report.p95_latency_ms,
                baseline_report.p95_latency_ms,
                self.performance_tolerances.p95_latency_increase_ratio,
            ),
        )
        failures: list[str] = []
        for label, actual, baseline, ratio in checks:
            ceiling = max(
                baseline * (1 + ratio),
                baseline + self.performance_tolerances.minimum_latency_allowance_ms,
            )
            if actual > ceiling:
                failures.append(
                    f"{label} {actual:.1f} > baseline ceiling {ceiling:.1f}"
                )
        return tuple(failures)

    def to_dict(self) -> dict[str, object]:
        return {
            "baseline": asdict(self.baseline),
            "recommended": (
                asdict(self.recommended) if self.recommended is not None else None
            ),
            "selection_split": "development",
            "holdout_used_for_selection": False,
            "passed": self.passed,
            "measurement_repetitions": self.measurement_repetitions,
            "tolerances": asdict(self.tolerances),
            "performance_tolerances": asdict(self.performance_tolerances),
            "holdout_performance_failures": list(self.holdout_performance_failures),
            "trials": [trial.to_dict() for trial in self.trials],
            "baseline_holdout_report": self.baseline_holdout_report.to_dict(),
            "baseline_holdout_repeat_reports": [
                report.to_dict() for report in self.baseline_holdout_repeat_reports
            ],
            "aggregate_baseline_holdout_report": (
                self.aggregate_baseline_holdout_report.to_dict()
            ),
            "recommended_holdout_report": (
                self.recommended_holdout_report.to_dict()
                if self.recommended_holdout_report is not None
                else None
            ),
            "recommended_holdout_repeat_reports": [
                report.to_dict() for report in self.recommended_holdout_repeat_reports
            ],
            "aggregate_recommended_holdout_report": (
                self.aggregate_recommended_holdout_report.to_dict()
                if self.aggregate_recommended_holdout_report is not None
                else None
            ),
        }


def evaluate_rag_parameter_grid(
    cases: Sequence[RAGEvalCase],
    search_backend: ParameterizedSearchBackend,
    *,
    combinations: Sequence[RAGParameterCombination],
    baseline: RAGParameterCombination,
    thresholds: RAGEvalThresholds,
    tolerances: RAGQualityRetentionTolerances | None = None,
    performance_tolerances: RAGPerformanceRetentionTolerances | None = None,
    measurement_repetitions: int = 1,
) -> RAGParameterTuningResult:
    """Select on development cases, then evaluate holdout exactly after selection."""

    if not 1 <= measurement_repetitions <= 5:
        raise ValueError("RAG tuning measurement repetitions must be between 1 and 5.")
    development_cases = tuple(case for case in cases if case.split == "development")
    holdout_cases = tuple(case for case in cases if case.split == "holdout")
    if not development_cases:
        raise ValueError("RAG parameter tuning requires development cases.")
    if not holdout_cases:
        raise ValueError("RAG parameter tuning requires holdout cases.")

    normalized = tuple(sorted({baseline, *combinations}))
    reports: dict[RAGParameterCombination, list[RAGEvalReport]] = {
        parameters: [] for parameters in normalized
    }
    for _repetition in range(measurement_repetitions):
        for parameters in normalized:
            reports[parameters].append(
                _evaluate_combination(
                    development_cases,
                    search_backend,
                    parameters,
                    thresholds,
                )
            )

    retention = tolerances or RAGQualityRetentionTolerances()
    baseline_report = reports[baseline]
    trial_items: list[RAGParameterTrial] = []
    for parameters in normalized:
        failures = tuple(
            f"repeat={index + 1}:{failure}"
            for index, (report, reference) in enumerate(
                zip(reports[parameters], baseline_report, strict=True)
            )
            for failure in _quality_retention_failures(
                report,
                reference,
                retention,
            )
        )
        trial_items.append(
            RAGParameterTrial(
                parameters=parameters,
                report=reports[parameters][0],
                quality_retained=not failures,
                rejection_reasons=failures,
                repeat_reports=tuple(reports[parameters][1:]),
            )
        )
    trials = tuple(trial_items)
    recommended = _select_recommended_parameters(trials)

    baseline_holdout_reports: list[RAGEvalReport] = []
    recommended_holdout_reports: list[RAGEvalReport] = []
    for _repetition in range(measurement_repetitions):
        baseline_holdout_reports.append(
            _evaluate_combination(
                holdout_cases,
                search_backend,
                baseline,
                thresholds,
            )
        )
        if recommended is not None and recommended != baseline:
            recommended_holdout_reports.append(
                _evaluate_combination(
                    holdout_cases,
                    search_backend,
                    recommended,
                    thresholds,
                )
            )
    if recommended == baseline:
        recommended_holdout_reports = list(baseline_holdout_reports)

    baseline_holdout = baseline_holdout_reports[0]
    recommended_holdout = (
        recommended_holdout_reports[0] if recommended_holdout_reports else None
    )
    return RAGParameterTuningResult(
        baseline=baseline,
        trials=trials,
        recommended=recommended,
        baseline_holdout_report=baseline_holdout,
        recommended_holdout_report=recommended_holdout,
        tolerances=retention,
        performance_tolerances=(
            performance_tolerances or RAGPerformanceRetentionTolerances()
        ),
        measurement_repetitions=measurement_repetitions,
        baseline_holdout_repeat_reports=tuple(baseline_holdout_reports[1:]),
        recommended_holdout_repeat_reports=tuple(recommended_holdout_reports[1:]),
    )


def format_rag_parameter_tuning_result(result: RAGParameterTuningResult) -> str:
    """Format a compact report without exposing holdout during selection."""

    lines = [
        "RAG parameter tuning: development selection, holdout verification",
        f"baseline={result.baseline.label}",
        f"measurement_repetitions={result.measurement_repetitions}",
    ]
    for trial in result.trials:
        report = trial.aggregate_report
        stage_summary = report.stage_latency_summary
        core_p95 = stage_summary.get("retrieval_rerank", {}).get("p95_ms")
        visual_p95 = stage_summary.get("visual_reinspection", {}).get("p95_ms")
        lines.append(
            " ".join(
                [
                    f"trial={trial.parameters.label}",
                    f"quality={'PASS' if trial.quality_retained else 'FAIL'}",
                    f"recall={report.mean_recall_at_n:.3f}",
                    f"r@1={report.mean_recall_at_1:.3f}",
                    f"ndcg={report.mean_ndcg_at_n:.3f}",
                    f"total_p95_ms={report.p95_latency_ms:.1f}",
                    f"core_p95_ms={float(core_p95):.1f}"
                    if core_p95 is not None
                    else "core_p95_ms=n/a",
                    f"visual_p95_ms={float(visual_p95):.1f}"
                    if visual_p95 is not None
                    else "visual_p95_ms=n/a",
                    f"candidate_budget={trial.estimated_retrieval_candidates}",
                    f"context_budget={trial.estimated_context_results}",
                ]
            )
        )
        lines.extend(
            f"trial_rejection={trial.parameters.label}:{reason}"
            for reason in trial.rejection_reasons
        )
    lines.append(
        f"recommended={result.recommended.label if result.recommended else 'none'}"
    )
    baseline_holdout = result.aggregate_baseline_holdout_report
    recommended_holdout = result.aggregate_recommended_holdout_report
    lines.append(
        "holdout_baseline="
        f"{'PASS' if all(report.all_passed for report in result.baseline_holdout_reports) else 'FAIL'} "
        f"recall={baseline_holdout.mean_recall_at_n:.3f} "
        f"p95_ms={baseline_holdout.p95_latency_ms:.1f}"
    )
    if recommended_holdout is not None:
        lines.append(
            "holdout_recommended="
            f"{'PASS' if all(report.all_passed for report in result.recommended_holdout_reports) else 'FAIL'} "
            f"recall={recommended_holdout.mean_recall_at_n:.3f} "
            f"p95_ms={recommended_holdout.p95_latency_ms:.1f}"
        )
    lines.append(
        "holdout_performance_gate="
        f"{'PASS' if not result.holdout_performance_failures else 'FAIL'}"
    )
    lines.extend(
        f"holdout_performance_failure={failure}"
        for failure in result.holdout_performance_failures
    )
    return "\n".join(lines)


def _evaluate_combination(
    cases: Sequence[RAGEvalCase],
    search_backend: ParameterizedSearchBackend,
    parameters: RAGParameterCombination,
    thresholds: RAGEvalThresholds,
) -> RAGEvalReport:
    return evaluate_rag_cases(
        cases,
        lambda case, top_n: search_backend(
            case,
            parameters.retrieval_top_k,
            top_n,
        ),
        override_top_n=parameters.rerank_top_n,
        thresholds=thresholds,
    )


def _quality_retention_failures(
    report: RAGEvalReport,
    baseline: RAGEvalReport,
    tolerances: RAGQualityRetentionTolerances,
) -> tuple[str, ...]:
    failures = list(report.quality_gate_failures)
    comparisons = (
        (
            "case_pass_rate",
            report.case_pass_rate,
            baseline.case_pass_rate,
            tolerances.case_pass_rate_loss,
        ),
        (
            "mean_recall_at_n",
            report.mean_recall_at_n,
            baseline.mean_recall_at_n,
            tolerances.recall_loss,
        ),
        (
            "mean_recall_at_1",
            report.mean_recall_at_1,
            baseline.mean_recall_at_1,
            tolerances.recall_loss,
        ),
        (
            "mean_recall_at_3",
            report.mean_recall_at_3,
            baseline.mean_recall_at_3,
            tolerances.recall_loss,
        ),
        (
            "mean_recall_at_5",
            report.mean_recall_at_5,
            baseline.mean_recall_at_5,
            tolerances.recall_loss,
        ),
        (
            "mean_ndcg_at_n",
            report.mean_ndcg_at_n,
            baseline.mean_ndcg_at_n,
            tolerances.ranking_loss,
        ),
        (
            "mean_reciprocal_rank",
            report.mean_reciprocal_rank,
            baseline.mean_reciprocal_rank,
            tolerances.ranking_loss,
        ),
    )
    for label, actual, reference, accepted_loss in comparisons:
        minimum = reference - accepted_loss
        if actual < minimum:
            failures.append(f"{label} {actual:.3f} < baseline floor {minimum:.3f}")
    maximum_forbidden_rate = (
        baseline.forbidden_case_rate + tolerances.forbidden_case_rate_increase
    )
    if report.forbidden_case_rate > maximum_forbidden_rate:
        failures.append(
            "forbidden_case_rate "
            f"{report.forbidden_case_rate:.3f} > baseline ceiling "
            f"{maximum_forbidden_rate:.3f}"
        )
    return tuple(dict.fromkeys(failures))


def _aggregate_reports(reports: Sequence[RAGEvalReport]) -> RAGEvalReport:
    if not reports:
        raise ValueError("Cannot aggregate an empty RAG report sequence.")
    return RAGEvalReport(
        [case_result for report in reports for case_result in report.case_results],
        reports[0].thresholds,
    )


def _select_recommended_parameters(
    trials: Sequence[RAGParameterTrial],
) -> RAGParameterCombination | None:
    eligible = [trial for trial in trials if trial.quality_retained]
    if not eligible:
        return None

    fastest_p95 = min(trial.selection_p95_latency_ms for trial in eligible)
    # Repeated samples still contain provider jitter. Treat core retrieval
    # results within 15% plus 5 ms as equivalent, then prefer smaller budgets.
    latency_ceiling = fastest_p95 * 1.15 + 5.0
    near_fastest = [
        trial for trial in eligible if trial.selection_p95_latency_ms <= latency_ceiling
    ]
    selected = min(
        near_fastest,
        key=lambda trial: (
            trial.parameters.retrieval_top_k,
            trial.parameters.rerank_top_n,
            trial.selection_p95_latency_ms,
        ),
    )
    return selected.parameters
