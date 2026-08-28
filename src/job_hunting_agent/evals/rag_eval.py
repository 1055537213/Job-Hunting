"""RAG retrieval evaluation harness.

The evaluator is intentionally independent from pgvector and model providers.
Production checks can pass ``JobHuntingApp.search_rag`` as the search backend,
while unit tests can use deterministic in-memory results.
"""

from __future__ import annotations

import argparse
import json
import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from job_hunting_agent.app import JobHuntingApp
from job_hunting_agent.models import RAGSearchResult

DEFAULT_TOP_N = 5

SearchBackend = Callable[["RAGEvalCase", int], Sequence[object]]


@dataclass(frozen=True)
class RAGEvalThresholds:
    """套件级质量门槛，防止单条宽松阈值掩盖整体召回退化。"""

    min_case_pass_rate: float = 1.0
    min_mean_recall_at_n: float = 0.0
    min_mean_recall_at_1: float = 0.0
    min_mean_recall_at_3: float = 0.0
    min_mean_recall_at_5: float = 0.0
    min_mean_precision_at_n: float = 0.0
    min_mean_ndcg_at_n: float = 0.0
    min_mean_reciprocal_rank: float = 0.0
    max_forbidden_case_rate: float = 0.0

    def __post_init__(self) -> None:
        for name, value in asdict(self).items():
            if not 0.0 <= float(value) <= 1.0:
                raise ValueError(f"RAG eval threshold {name} must be between 0 and 1.")

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object] | None) -> RAGEvalThresholds:
        values = payload or {}
        return cls(
            min_case_pass_rate=float(values.get("min_case_pass_rate", 1.0)),
            min_mean_recall_at_n=float(
                values.get(
                    "min_mean_recall_at_n",
                    values.get("min_mean_recall_at_k", 0.0),
                )
            ),
            min_mean_recall_at_1=float(values.get("min_mean_recall_at_1", 0.0)),
            min_mean_recall_at_3=float(values.get("min_mean_recall_at_3", 0.0)),
            min_mean_recall_at_5=float(values.get("min_mean_recall_at_5", 0.0)),
            min_mean_precision_at_n=float(
                values.get(
                    "min_mean_precision_at_n",
                    values.get("min_mean_precision_at_k", 0.0),
                )
            ),
            min_mean_ndcg_at_n=float(
                values.get(
                    "min_mean_ndcg_at_n",
                    values.get("min_mean_ndcg_at_k", 0.0),
                )
            ),
            min_mean_reciprocal_rank=float(
                values.get("min_mean_reciprocal_rank", 0.0)
            ),
            max_forbidden_case_rate=float(
                values.get("max_forbidden_case_rate", 0.0)
            ),
        )


@dataclass(frozen=True)
class EvidenceRef:
    """A stable reference to a long-text source expected in RAG results."""

    long_text_id: int | None = None
    source_label: str | None = None
    entity_type: str | None = None
    entity_id: int | None = None

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object]) -> EvidenceRef:
        ref = cls(
            long_text_id=_optional_int(payload.get("long_text_id")),
            source_label=_optional_str(payload.get("source_label")),
            entity_type=_optional_str(payload.get("entity_type")),
            entity_id=_optional_int(payload.get("entity_id")),
        )
        if not ref.has_selector:
            raise ValueError("EvidenceRef must include at least one selector.")
        return ref

    @property
    def has_selector(self) -> bool:
        return any(
            value is not None
            for value in (
                self.long_text_id,
                self.source_label,
                self.entity_type,
                self.entity_id,
            )
        )

    def matches(self, hit: RetrievedEvidence) -> bool:
        if self.long_text_id is not None and hit.long_text_id != self.long_text_id:
            return False
        if self.source_label is not None and hit.source_label != self.source_label:
            return False
        if self.entity_type is not None and hit.entity_type != self.entity_type:
            return False
        return self.entity_id is None or hit.entity_id == self.entity_id


@dataclass(frozen=True)
class RetrievedEvidence:
    """Normalized RAG search hit used by the evaluator."""

    long_text_id: int
    source_label: str
    entity_type: str
    entity_id: int
    chunk_index: int = 0
    distance: float | None = None
    relevance_score: float | None = None
    content: str = ""

    @classmethod
    def from_object(cls, value: object) -> RetrievedEvidence:
        if isinstance(value, RAGSearchResult):
            return cls(
                long_text_id=value.long_text_id,
                source_label=value.source_label,
                entity_type=value.entity_type,
                entity_id=value.entity_id,
                chunk_index=value.chunk_index,
                distance=value.distance,
                relevance_score=value.relevance_score,
                content=value.content,
            )
        if isinstance(value, Mapping):
            return cls(
                long_text_id=_required_int(value, "long_text_id"),
                source_label=_required_str(value, "source_label"),
                entity_type=_required_str(value, "entity_type"),
                entity_id=_required_int(value, "entity_id"),
                chunk_index=_optional_int(value.get("chunk_index")) or 0,
                distance=_optional_float(value.get("distance")),
                relevance_score=_optional_float(value.get("relevance_score")),
                content=str(value.get("content") or ""),
            )
        return cls(
            long_text_id=int(value.long_text_id),
            source_label=str(value.source_label),
            entity_type=str(value.entity_type),
            entity_id=int(value.entity_id),
            chunk_index=int(getattr(value, "chunk_index", 0)),
            distance=_optional_float(getattr(value, "distance", None)),
            relevance_score=_optional_float(
                getattr(value, "relevance_score", None)
            ),
            content=str(getattr(value, "content", "")),
        )


@dataclass(frozen=True)
class RAGEvalCase:
    """One golden retrieval expectation."""

    id: str
    query: str
    expected: tuple[EvidenceRef, ...]
    forbidden: tuple[EvidenceRef, ...] = ()
    entity_types: tuple[str, ...] = ()
    top_n: int | None = None
    min_recall: float = 1.0
    tags: tuple[str, ...] = ()
    split: str = "development"

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object]) -> RAGEvalCase:
        case_id = _required_str(payload, "id")
        query = _required_str(payload, "query")
        expected = _load_refs(
            payload,
            refs_key="expected",
            id_key="expected_long_text_ids",
            label_key="expected_source_labels",
        )
        if not expected:
            raise ValueError(f"RAG eval case {case_id!r} must include expected refs.")
        forbidden = _load_refs(
            payload,
            refs_key="forbidden",
            id_key="forbidden_long_text_ids",
            label_key="forbidden_source_labels",
        )
        min_recall = float(payload.get("min_recall", 1.0))
        if min_recall < 0 or min_recall > 1:
            raise ValueError(f"RAG eval case {case_id!r} has invalid min_recall.")
        split = str(payload.get("split") or "development").strip()
        if split not in {"development", "holdout"}:
            raise ValueError(
                f"RAG eval case {case_id!r} split must be development or holdout."
            )
        return cls(
            id=case_id,
            query=query,
            expected=tuple(expected),
            forbidden=tuple(forbidden),
            entity_types=tuple(str(item) for item in payload.get("entity_types", []) or []),
            top_n=_optional_int(payload.get("top_n", payload.get("top_k"))),
            min_recall=min_recall,
            tags=tuple(
                dict.fromkeys(
                    str(item).strip()
                    for item in payload.get("tags", []) or []
                    if str(item).strip()
                )
            ),
            split=split,
        )


@dataclass(frozen=True)
class RAGEvalCaseResult:
    """Evaluation result for one case."""

    id: str
    query: str
    top_n: int
    expected_total: int
    expected_found: int
    recall_at_n: float
    recall_at_1: float
    recall_at_3: float
    recall_at_5: float
    precision_at_n: float
    ndcg_at_n: float
    reciprocal_rank: float
    forbidden_hit_count: int
    passed: bool
    tags: tuple[str, ...] = ()
    split: str = "development"
    hits: list[RetrievedEvidence] = field(default_factory=list)


@dataclass(frozen=True)
class RAGEvalReport:
    """Aggregated RAG evaluation metrics."""

    case_results: list[RAGEvalCaseResult]
    thresholds: RAGEvalThresholds = field(default_factory=RAGEvalThresholds)

    @property
    def case_count(self) -> int:
        return len(self.case_results)

    @property
    def passed_count(self) -> int:
        return sum(1 for result in self.case_results if result.passed)

    @property
    def all_passed(self) -> bool:
        return bool(self.case_results) and not self.quality_gate_failures

    @property
    def case_pass_rate(self) -> float:
        if not self.case_count:
            return 0.0
        return self.passed_count / self.case_count

    @property
    def mean_recall_at_n(self) -> float:
        return _mean(result.recall_at_n for result in self.case_results)

    @property
    def mean_recall_at_1(self) -> float:
        return _mean(result.recall_at_1 for result in self.case_results)

    @property
    def mean_recall_at_3(self) -> float:
        return _mean(result.recall_at_3 for result in self.case_results)

    @property
    def mean_recall_at_5(self) -> float:
        return _mean(result.recall_at_5 for result in self.case_results)

    @property
    def mean_precision_at_n(self) -> float:
        return _mean(result.precision_at_n for result in self.case_results)

    @property
    def mean_ndcg_at_n(self) -> float:
        return _mean(result.ndcg_at_n for result in self.case_results)

    @property
    def mean_reciprocal_rank(self) -> float:
        return _mean(result.reciprocal_rank for result in self.case_results)

    @property
    def forbidden_case_rate(self) -> float:
        return _mean(
            1.0 if result.forbidden_hit_count else 0.0
            for result in self.case_results
        )

    @property
    def quality_gate_failures(self) -> tuple[str, ...]:
        failures: list[str] = []
        checks = (
            ("case_pass_rate", self.case_pass_rate, self.thresholds.min_case_pass_rate),
            (
                "mean_recall_at_n",
                self.mean_recall_at_n,
                self.thresholds.min_mean_recall_at_n,
            ),
            (
                "mean_recall_at_1",
                self.mean_recall_at_1,
                self.thresholds.min_mean_recall_at_1,
            ),
            (
                "mean_recall_at_3",
                self.mean_recall_at_3,
                self.thresholds.min_mean_recall_at_3,
            ),
            (
                "mean_recall_at_5",
                self.mean_recall_at_5,
                self.thresholds.min_mean_recall_at_5,
            ),
            (
                "mean_precision_at_n",
                self.mean_precision_at_n,
                self.thresholds.min_mean_precision_at_n,
            ),
            (
                "mean_ndcg_at_n",
                self.mean_ndcg_at_n,
                self.thresholds.min_mean_ndcg_at_n,
            ),
            (
                "mean_reciprocal_rank",
                self.mean_reciprocal_rank,
                self.thresholds.min_mean_reciprocal_rank,
            ),
        )
        for label, actual, minimum in checks:
            if actual < minimum:
                failures.append(f"{label} {actual:.3f} < {minimum:.3f}")
        if self.forbidden_case_rate > self.thresholds.max_forbidden_case_rate:
            failures.append(
                "forbidden_case_rate "
                f"{self.forbidden_case_rate:.3f} > "
                f"{self.thresholds.max_forbidden_case_rate:.3f}"
            )
        return tuple(failures)

    @property
    def metrics_by_tag(self) -> dict[str, dict[str, float | int]]:
        tags = sorted({tag for result in self.case_results for tag in result.tags})
        metrics: dict[str, dict[str, float | int]] = {}
        for tag in tags:
            results = [result for result in self.case_results if tag in result.tags]
            metrics[tag] = {
                "case_count": len(results),
                "passed_count": sum(1 for result in results if result.passed),
                "mean_recall_at_n": _mean(
                    result.recall_at_n for result in results
                ),
                "mean_recall_at_1": _mean(result.recall_at_1 for result in results),
                "mean_recall_at_3": _mean(result.recall_at_3 for result in results),
                "mean_recall_at_5": _mean(result.recall_at_5 for result in results),
                "mean_precision_at_n": _mean(
                    result.precision_at_n for result in results
                ),
                "mean_ndcg_at_n": _mean(result.ndcg_at_n for result in results),
                "mean_reciprocal_rank": _mean(
                    result.reciprocal_rank for result in results
                ),
                "forbidden_case_rate": _mean(
                    1.0 if result.forbidden_hit_count else 0.0
                    for result in results
                ),
            }
        return metrics

    @property
    def metrics_by_split(self) -> dict[str, dict[str, float | int]]:
        metrics: dict[str, dict[str, float | int]] = {}
        for split in sorted({result.split for result in self.case_results}):
            results = [result for result in self.case_results if result.split == split]
            metrics[split] = {
                "case_count": len(results),
                "passed_count": sum(result.passed for result in results),
                "case_pass_rate": _mean(1.0 if result.passed else 0.0 for result in results),
                "mean_recall_at_1": _mean(result.recall_at_1 for result in results),
                "mean_recall_at_3": _mean(result.recall_at_3 for result in results),
                "mean_recall_at_5": _mean(result.recall_at_5 for result in results),
                "mean_precision_at_n": _mean(
                    result.precision_at_n for result in results
                ),
                "mean_ndcg_at_n": _mean(result.ndcg_at_n for result in results),
                "mean_reciprocal_rank": _mean(
                    result.reciprocal_rank for result in results
                ),
                "forbidden_case_rate": _mean(
                    1.0 if result.forbidden_hit_count else 0.0
                    for result in results
                ),
            }
        return metrics

    def to_dict(self) -> dict[str, object]:
        return {
            "case_count": self.case_count,
            "passed_count": self.passed_count,
            "all_passed": self.all_passed,
            "case_pass_rate": self.case_pass_rate,
            "mean_recall_at_n": self.mean_recall_at_n,
            "mean_recall_at_1": self.mean_recall_at_1,
            "mean_recall_at_3": self.mean_recall_at_3,
            "mean_recall_at_5": self.mean_recall_at_5,
            "mean_precision_at_n": self.mean_precision_at_n,
            "mean_ndcg_at_n": self.mean_ndcg_at_n,
            "mean_reciprocal_rank": self.mean_reciprocal_rank,
            "forbidden_case_rate": self.forbidden_case_rate,
            "thresholds": asdict(self.thresholds),
            "quality_gate_passed": not self.quality_gate_failures,
            "quality_gate_failures": list(self.quality_gate_failures),
            "metrics_by_tag": self.metrics_by_tag,
            "metrics_by_split": self.metrics_by_split,
            "cases": [asdict(result) for result in self.case_results],
        }


def evaluate_rag_cases(
    cases: Sequence[RAGEvalCase],
    search_backend: SearchBackend,
    *,
    top_n: int = DEFAULT_TOP_N,
    thresholds: RAGEvalThresholds | None = None,
) -> RAGEvalReport:
    """Run golden retrieval cases against a RAG search backend."""

    results: list[RAGEvalCaseResult] = []
    for case in cases:
        case_top_n = max(1, int(case.top_n or top_n))
        hits = [
            RetrievedEvidence.from_object(item)
            for item in list(search_backend(case, case_top_n))[:case_top_n]
        ]
        expected_ranks = [_first_match_rank(ref, hits) for ref in case.expected]
        found_ranks = [rank for rank in expected_ranks if rank is not None]
        recall = len(found_ranks) / len(case.expected)
        recall_at_1 = _recall_at_cutoff(expected_ranks, len(case.expected), 1)
        recall_at_3 = _recall_at_cutoff(expected_ranks, len(case.expected), 3)
        recall_at_5 = _recall_at_cutoff(expected_ranks, len(case.expected), 5)
        precision_at_n = len(set(found_ranks)) / case_top_n
        ndcg_at_n = _ndcg_at_n(found_ranks, len(case.expected), case_top_n)
        reciprocal_rank = 1 / min(found_ranks) if found_ranks else 0.0
        forbidden_hits = [
            hit
            for hit in hits
            if any(ref.matches(hit) for ref in case.forbidden)
        ]
        results.append(
            RAGEvalCaseResult(
                id=case.id,
                query=case.query,
                top_n=case_top_n,
                expected_total=len(case.expected),
                expected_found=len(found_ranks),
                recall_at_n=recall,
                recall_at_1=recall_at_1,
                recall_at_3=recall_at_3,
                recall_at_5=recall_at_5,
                precision_at_n=precision_at_n,
                ndcg_at_n=ndcg_at_n,
                reciprocal_rank=reciprocal_rank,
                forbidden_hit_count=len(forbidden_hits),
                passed=recall >= case.min_recall and not forbidden_hits,
                tags=case.tags,
                split=case.split,
                hits=hits,
            )
        )
    return RAGEvalReport(results, thresholds or RAGEvalThresholds())


def load_rag_eval_cases(path: str | Path) -> list[RAGEvalCase]:
    """Load RAG eval cases from JSON."""

    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    raw_cases = payload.get("cases") if isinstance(payload, Mapping) else payload
    if not isinstance(raw_cases, list):
        raise ValueError(  # noqa: TRY004 - preserve the eval payload contract
            "RAG eval JSON must be a list or an object with a cases list."
        )
    return [RAGEvalCase.from_mapping(item) for item in raw_cases]


def format_rag_eval_report(report: RAGEvalReport) -> str:
    """Format a compact human-readable report."""

    lines = [
        f"RAG eval: {report.passed_count}/{report.case_count} cases passed",
        f"mean_recall_at_n={report.mean_recall_at_n:.3f}",
        f"recall_at_1={report.mean_recall_at_1:.3f}",
        f"recall_at_3={report.mean_recall_at_3:.3f}",
        f"recall_at_5={report.mean_recall_at_5:.3f}",
        f"precision_at_n={report.mean_precision_at_n:.3f}",
        f"ndcg_at_n={report.mean_ndcg_at_n:.3f}",
        f"mrr={report.mean_reciprocal_rank:.3f}",
        f"forbidden_case_rate={report.forbidden_case_rate:.3f}",
        f"quality_gate={'PASS' if report.all_passed else 'FAIL'}",
    ]
    lines.extend(f"gate_failure={failure}" for failure in report.quality_gate_failures)
    for split, metrics in report.metrics_by_split.items():
        lines.append(
            " ".join(
                [
                    f"split_summary={split}",
                    f"passed={metrics['passed_count']}/{metrics['case_count']}",
                    f"pass_rate={metrics['case_pass_rate']:.3f}",
                    f"r@1={metrics['mean_recall_at_1']:.3f}",
                    f"r@3={metrics['mean_recall_at_3']:.3f}",
                    f"r@5={metrics['mean_recall_at_5']:.3f}",
                    f"precision={metrics['mean_precision_at_n']:.3f}",
                    f"ndcg={metrics['mean_ndcg_at_n']:.3f}",
                    f"mrr={metrics['mean_reciprocal_rank']:.3f}",
                    f"forbidden_rate={metrics['forbidden_case_rate']:.3f}",
                ]
            )
        )
    for result in report.case_results:
        status = "PASS" if result.passed else "FAIL"
        lines.append(
            " ".join(
                [
                    f"[{status}]",
                    result.id,
                    f"split={result.split}",
                    f"recall={result.recall_at_n:.3f}",
                    f"r@1={result.recall_at_1:.3f}",
                    f"ndcg={result.ndcg_at_n:.3f}",
                    f"rr={result.reciprocal_rank:.3f}",
                    f"forbidden_hits={result.forbidden_hit_count}",
                ]
            )
        )
    return "\n".join(lines)


def write_example_cases(path: str | Path) -> None:
    """Write an editable eval-case template."""

    target = Path(path)
    example = {
        "cases": [
            {
                "id": "backend-project-experience",
                "query": "Python FastAPI 后端项目经历",
                "expected_long_text_ids": [101],
                "expected_source_labels": ["project-card:backend-agent"],
                "forbidden_source_labels": ["profile-negative:frontend-only"],
                "entity_types": ["project_experience", "conversation_message"],
                "top_n": 5,
                "min_recall": 1.0,
            }
        ]
    }
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(example, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run RAG retrieval golden-case evaluation.")
    parser.add_argument("--cases", help="Path to RAG eval cases JSON.")
    parser.add_argument("--env-file", default=".env", help="Environment file used by JobHuntingApp.")
    parser.add_argument("--database-url", help="Override PostgreSQL database URL.")
    parser.add_argument("--account-id", type=int, help="Restrict retrieval to one account.")
    parser.add_argument(
        "--top-n",
        type=int,
        default=DEFAULT_TOP_N,
        help="Default final reranked result count.",
    )
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    parser.add_argument("--write-example", help="Write an editable eval case template and exit.")
    args = parser.parse_args(argv)

    if args.write_example:
        write_example_cases(args.write_example)
        print(f"Wrote RAG eval example cases to {args.write_example}")
        return 0
    if not args.cases:
        parser.error("--cases is required unless --write-example is used.")

    cases = load_rag_eval_cases(args.cases)
    app = JobHuntingApp(env_path=args.env_file, database_url=args.database_url)
    app.initialize()
    try:
        report = evaluate_rag_cases(
            cases,
            lambda case, top_n: app.search_rag(
                case.query,
                top_n=top_n,
                entity_types=list(case.entity_types) or None,
                account_id=args.account_id,
            ),
            top_n=args.top_n,
        )
    finally:
        app.store.close()

    if args.json:
        print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
    else:
        print(format_rag_eval_report(report))
    return 0 if report.all_passed else 1


def _first_match_rank(ref: EvidenceRef, hits: Sequence[RetrievedEvidence]) -> int | None:
    for index, hit in enumerate(hits, start=1):
        if ref.matches(hit):
            return index
    return None


def _recall_at_cutoff(
    ranks: Sequence[int | None],
    expected_total: int,
    cutoff: int,
) -> float:
    if expected_total <= 0:
        return 0.0
    return sum(rank is not None and rank <= cutoff for rank in ranks) / expected_total


def _ndcg_at_n(found_ranks: Sequence[int], expected_total: int, top_n: int) -> float:
    if expected_total <= 0 or top_n <= 0:
        return 0.0
    unique_ranks = sorted({rank for rank in found_ranks if rank <= top_n})
    dcg = sum(1.0 / math.log2(rank + 1) for rank in unique_ranks)
    ideal_count = min(expected_total, top_n)
    ideal_dcg = sum(1.0 / math.log2(rank + 1) for rank in range(1, ideal_count + 1))
    return min(1.0, dcg / ideal_dcg) if ideal_dcg else 0.0


def _load_refs(
    payload: Mapping[str, object],
    *,
    refs_key: str,
    id_key: str,
    label_key: str,
) -> list[EvidenceRef]:
    refs: list[EvidenceRef] = []
    raw_refs = payload.get(refs_key) or []
    if not isinstance(raw_refs, list):
        raise ValueError(f"{refs_key} must be a list.")  # noqa: TRY004 - eval payload contract
    for item in raw_refs:
        if not isinstance(item, Mapping):
            raise ValueError(  # noqa: TRY004 - preserve the eval payload contract
                f"{refs_key} entries must be objects."
            )
        refs.append(EvidenceRef.from_mapping(item))
    for long_text_id in payload.get(id_key, []) or []:
        refs.append(EvidenceRef(long_text_id=int(long_text_id)))
    for source_label in payload.get(label_key, []) or []:
        refs.append(EvidenceRef(source_label=str(source_label)))
    return refs


def _mean(values: Sequence[float] | Any) -> float:
    materialized = list(values)
    if not materialized:
        return 0.0
    return sum(materialized) / len(materialized)


def _required_int(payload: Mapping[str, object], key: str) -> int:
    if key not in payload:
        raise ValueError(f"Missing required integer field: {key}")
    return int(payload[key])


def _optional_int(value: object) -> int | None:
    if value is None or value == "":
        return None
    return int(value)


def _required_str(payload: Mapping[str, object], key: str) -> str:
    value = str(payload.get(key) or "").strip()
    if not value:
        raise ValueError(f"Missing required string field: {key}")
    return value


def _optional_str(value: object) -> str | None:
    text = str(value or "").strip()
    return text or None


def _optional_float(value: object) -> float | None:
    if value is None or value == "":
        return None
    return float(value)


if __name__ == "__main__":
    raise SystemExit(main())
