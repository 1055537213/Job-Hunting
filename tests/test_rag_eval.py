from __future__ import annotations

import json

from job_hunting_agent.evals.rag_eval import (
    EvidenceRef,
    RAGEvalCase,
    RAGEvalThresholds,
    evaluate_rag_cases,
    format_rag_eval_report,
    load_rag_eval_cases,
    write_example_cases,
)
from job_hunting_agent.models import RAGSearchResult


def test_rag_eval_reports_recall_mrr_and_pass_status() -> None:
    cases = [
        RAGEvalCase(
            id="backend-project",
            query="FastAPI 后端项目经历",
            expected=(EvidenceRef(source_label="project-card:backend"),),
            forbidden=(EvidenceRef(source_label="profile-negative:frontend-only"),),
        )
    ]

    def search(_case: RAGEvalCase, _top_k: int) -> list[RAGSearchResult]:
        return [
            rag_hit(21, "resume-source", "resume", 1),
            rag_hit(22, "project-card:backend", "project_experience", 2),
        ]

    report = evaluate_rag_cases(cases, search, top_k=5)

    assert report.all_passed
    assert report.passed_count == 1
    assert report.mean_recall_at_k == 1.0
    assert report.mean_reciprocal_rank == 0.5
    assert report.forbidden_case_rate == 0.0
    assert "RAG eval: 1/1 cases passed" in format_rag_eval_report(report)


def test_rag_eval_fails_when_expected_evidence_is_missing() -> None:
    cases = [
        RAGEvalCase(
            id="missing-project",
            query="向量检索 项目经历",
            expected=(EvidenceRef(long_text_id=99),),
        )
    ]

    report = evaluate_rag_cases(
        cases,
        lambda _case, _top_k: [rag_hit(10, "unrelated", "conversation_message", 1)],
    )

    assert not report.all_passed
    assert report.case_results[0].expected_found == 0
    assert report.case_results[0].recall_at_k == 0.0
    assert report.case_results[0].reciprocal_rank == 0.0


def test_rag_eval_fails_when_forbidden_evidence_is_retrieved() -> None:
    cases = [
        RAGEvalCase(
            id="negative-skill",
            query="候选人 Python 熟练度",
            expected=(EvidenceRef(source_label="profile-skill:python"),),
            forbidden=(EvidenceRef(source_label="profile-negative:python"),),
        )
    ]

    report = evaluate_rag_cases(
        cases,
        lambda _case, _top_k: [
            rag_hit(30, "profile-skill:python", "conversation_message", 1),
            rag_hit(31, "profile-negative:python", "conversation_message", 1),
        ],
    )

    assert not report.all_passed
    assert report.forbidden_case_rate == 1.0
    assert report.case_results[0].forbidden_hit_count == 1


def test_rag_eval_applies_suite_thresholds_and_reports_tag_metrics() -> None:
    cases = [
        RAGEvalCase(
            id="industrial-parameter",
            query="泵轴公差是多少",
            expected=(EvidenceRef(source_label="drawing:shaft"),),
            tags=("industrial", "numeric"),
        ),
        RAGEvalCase(
            id="design-visual",
            query="海报的视觉层级",
            expected=(EvidenceRef(source_label="design:poster"),),
            tags=("design", "visual"),
            min_recall=0.0,
        ),
    ]

    report = evaluate_rag_cases(
        cases,
        lambda case, _top_k: (
            [rag_hit(1, "drawing:shaft", "project_archive_file", 1)]
            if case.id == "industrial-parameter"
            else [rag_hit(2, "design:noise", "project_archive_file", 1)]
        ),
        thresholds=RAGEvalThresholds(
            min_case_pass_rate=1.0,
            min_mean_recall_at_k=0.75,
            min_mean_reciprocal_rank=0.75,
            max_forbidden_case_rate=0.0,
        ),
    )

    assert not report.all_passed
    assert report.case_pass_rate == 1.0
    assert report.mean_recall_at_k == 0.5
    assert report.quality_gate_failures == (
        "mean_recall_at_k 0.500 < 0.750",
        "mean_reciprocal_rank 0.500 < 0.750",
    )
    assert report.metrics_by_tag["industrial"]["mean_recall_at_k"] == 1.0
    assert report.metrics_by_tag["visual"]["mean_recall_at_k"] == 0.0
    payload = report.to_dict()
    assert payload["thresholds"]["min_mean_recall_at_k"] == 0.75
    assert payload["quality_gate_passed"] is False


def test_rag_eval_loads_case_json_with_compact_reference_fields(tmp_path) -> None:
    path = tmp_path / "rag_cases.json"
    path.write_text(
        json.dumps(
            {
                "cases": [
                    {
                        "id": "resume-project",
                        "query": "RAG 简历项目",
                        "expected_long_text_ids": [1],
                        "expected_source_labels": ["project-card:rag"],
                        "forbidden_source_labels": ["chat:noise"],
                        "entity_types": ["project_experience"],
                        "top_k": 3,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    cases = load_rag_eval_cases(path)

    assert cases[0].id == "resume-project"
    assert cases[0].top_k == 3
    assert cases[0].entity_types == ("project_experience",)
    assert cases[0].expected == (
        EvidenceRef(long_text_id=1),
        EvidenceRef(source_label="project-card:rag"),
    )
    assert cases[0].forbidden == (EvidenceRef(source_label="chat:noise"),)


def test_rag_eval_writes_example_case_file(tmp_path) -> None:
    path = tmp_path / "rag_eval.example.json"

    write_example_cases(path)
    cases = load_rag_eval_cases(path)

    assert cases
    assert cases[0].query


def rag_hit(
    long_text_id: int,
    source_label: str,
    entity_type: str,
    entity_id: int,
) -> RAGSearchResult:
    return RAGSearchResult(
        content=f"{source_label} content",
        entity_type=entity_type,
        entity_id=entity_id,
        source_label=source_label,
        long_text_id=long_text_id,
        chunk_index=0,
        distance=0.1,
    )
