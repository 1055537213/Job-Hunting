"""RAG retrieval evaluation harness.

The evaluator is intentionally independent from pgvector and model providers.
Production checks can pass ``JobHuntingApp.search_rag`` as the search backend,
while unit tests can use deterministic in-memory results.
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from job_hunting_agent.app import JobHuntingApp
from job_hunting_agent.models import RAGSearchResult

DEFAULT_TOP_K = 5

SearchBackend = Callable[["RAGEvalCase", int], Sequence[object]]


@dataclass(frozen=True)
class EvidenceRef:
    """A stable reference to a long-text source expected in RAG results."""

    long_text_id: int | None = None
    source_label: str | None = None
    entity_type: str | None = None
    entity_id: int | None = None

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object]) -> "EvidenceRef":
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

    def matches(self, hit: "RetrievedEvidence") -> bool:
        if self.long_text_id is not None and hit.long_text_id != self.long_text_id:
            return False
        if self.source_label is not None and hit.source_label != self.source_label:
            return False
        if self.entity_type is not None and hit.entity_type != self.entity_type:
            return False
        if self.entity_id is not None and hit.entity_id != self.entity_id:
            return False
        return True


@dataclass(frozen=True)
class RetrievedEvidence:
    """Normalized RAG search hit used by the evaluator."""

    long_text_id: int
    source_label: str
    entity_type: str
    entity_id: int
    chunk_index: int = 0
    distance: float | None = None
    content: str = ""

    @classmethod
    def from_object(cls, value: object) -> "RetrievedEvidence":
        if isinstance(value, RAGSearchResult):
            return cls(
                long_text_id=value.long_text_id,
                source_label=value.source_label,
                entity_type=value.entity_type,
                entity_id=value.entity_id,
                chunk_index=value.chunk_index,
                distance=value.distance,
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
                content=str(value.get("content") or ""),
            )
        return cls(
            long_text_id=int(getattr(value, "long_text_id")),
            source_label=str(getattr(value, "source_label")),
            entity_type=str(getattr(value, "entity_type")),
            entity_id=int(getattr(value, "entity_id")),
            chunk_index=int(getattr(value, "chunk_index", 0)),
            distance=_optional_float(getattr(value, "distance", None)),
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
    top_k: int | None = None
    min_recall: float = 1.0

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object]) -> "RAGEvalCase":
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
        return cls(
            id=case_id,
            query=query,
            expected=tuple(expected),
            forbidden=tuple(forbidden),
            entity_types=tuple(str(item) for item in payload.get("entity_types", []) or []),
            top_k=_optional_int(payload.get("top_k")),
            min_recall=min_recall,
        )


@dataclass(frozen=True)
class RAGEvalCaseResult:
    """Evaluation result for one case."""

    id: str
    query: str
    top_k: int
    expected_total: int
    expected_found: int
    recall_at_k: float
    reciprocal_rank: float
    forbidden_hit_count: int
    passed: bool
    hits: list[RetrievedEvidence] = field(default_factory=list)


@dataclass(frozen=True)
class RAGEvalReport:
    """Aggregated RAG evaluation metrics."""

    case_results: list[RAGEvalCaseResult]

    @property
    def case_count(self) -> int:
        return len(self.case_results)

    @property
    def passed_count(self) -> int:
        return sum(1 for result in self.case_results if result.passed)

    @property
    def all_passed(self) -> bool:
        return bool(self.case_results) and self.passed_count == self.case_count

    @property
    def mean_recall_at_k(self) -> float:
        return _mean(result.recall_at_k for result in self.case_results)

    @property
    def mean_reciprocal_rank(self) -> float:
        return _mean(result.reciprocal_rank for result in self.case_results)

    @property
    def forbidden_case_rate(self) -> float:
        return _mean(
            1.0 if result.forbidden_hit_count else 0.0
            for result in self.case_results
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "case_count": self.case_count,
            "passed_count": self.passed_count,
            "all_passed": self.all_passed,
            "mean_recall_at_k": self.mean_recall_at_k,
            "mean_reciprocal_rank": self.mean_reciprocal_rank,
            "forbidden_case_rate": self.forbidden_case_rate,
            "cases": [asdict(result) for result in self.case_results],
        }


def evaluate_rag_cases(
    cases: Sequence[RAGEvalCase],
    search_backend: SearchBackend,
    *,
    top_k: int = DEFAULT_TOP_K,
) -> RAGEvalReport:
    """Run golden retrieval cases against a RAG search backend."""

    results: list[RAGEvalCaseResult] = []
    for case in cases:
        case_top_k = max(1, int(case.top_k or top_k))
        hits = [
            RetrievedEvidence.from_object(item)
            for item in list(search_backend(case, case_top_k))[:case_top_k]
        ]
        expected_ranks = [_first_match_rank(ref, hits) for ref in case.expected]
        found_ranks = [rank for rank in expected_ranks if rank is not None]
        recall = len(found_ranks) / len(case.expected)
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
                top_k=case_top_k,
                expected_total=len(case.expected),
                expected_found=len(found_ranks),
                recall_at_k=recall,
                reciprocal_rank=reciprocal_rank,
                forbidden_hit_count=len(forbidden_hits),
                passed=recall >= case.min_recall and not forbidden_hits,
                hits=hits,
            )
        )
    return RAGEvalReport(results)


def load_rag_eval_cases(path: str | Path) -> list[RAGEvalCase]:
    """Load RAG eval cases from JSON."""

    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    raw_cases = payload.get("cases") if isinstance(payload, Mapping) else payload
    if not isinstance(raw_cases, list):
        raise ValueError("RAG eval JSON must be a list or an object with a cases list.")
    return [RAGEvalCase.from_mapping(item) for item in raw_cases]


def format_rag_eval_report(report: RAGEvalReport) -> str:
    """Format a compact human-readable report."""

    lines = [
        f"RAG eval: {report.passed_count}/{report.case_count} cases passed",
        f"mean_recall_at_k={report.mean_recall_at_k:.3f}",
        f"mrr={report.mean_reciprocal_rank:.3f}",
        f"forbidden_case_rate={report.forbidden_case_rate:.3f}",
    ]
    for result in report.case_results:
        status = "PASS" if result.passed else "FAIL"
        lines.append(
            " ".join(
                [
                    f"[{status}]",
                    result.id,
                    f"recall={result.recall_at_k:.3f}",
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
                "top_k": 5,
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
    parser.add_argument("--top-k", type=int, default=DEFAULT_TOP_K, help="Default retrieval cutoff.")
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
            lambda case, limit: app.search_rag(
                case.query,
                top_k=limit,
                entity_types=list(case.entity_types) or None,
                account_id=args.account_id,
            ),
            top_k=args.top_k,
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
        raise ValueError(f"{refs_key} must be a list.")
    for item in raw_refs:
        if not isinstance(item, Mapping):
            raise ValueError(f"{refs_key} entries must be objects.")
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
