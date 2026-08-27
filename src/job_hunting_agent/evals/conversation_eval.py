"""候选人档案变更的回归评测。

这个评测不依赖 RAG 语料规模，也不需要真实数据库写入。
它只验证“用户消息 -> 结构化 patch -> 合并后的档案”这条规则链，
适合在改动对话入库语义、技能规范化或方向替换规则后快速回归。
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from job_hunting_agent.city_catalog import normalize_city_list
from job_hunting_agent.conversation_ingestion import decide_conversation_ingestion
from job_hunting_agent.models import (
    CandidateProfile,
    CandidateProfileInput,
    sanitize_preference_weights,
)
from job_hunting_agent.profile_mutation import apply_candidate_profile_patch
from job_hunting_agent.skill_normalization import normalize_skill_mapping


@dataclass(frozen=True)
class ConversationEvalCase:
    """一条档案入库黄金用例。"""

    id: str
    message: str
    initial_profile: CandidateProfileInput
    expected_profile: dict[str, object]
    expected_saved_structured_fields: tuple[str, ...] = ()
    expected_reply_contains: tuple[str, ...] = ()

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object]) -> ConversationEvalCase:
        case_id = _required_str(payload, "id")
        message = _required_str(payload, "message")
        initial_profile_payload = payload.get("initial_profile")
        if not isinstance(initial_profile_payload, Mapping):
            raise ValueError(  # noqa: TRY004 - preserve the eval payload contract
                f"Conversation eval case {case_id!r} must include initial_profile."
            )
        initial_profile = _candidate_profile_input_from_mapping(initial_profile_payload)
        expected_profile = payload.get("expected_profile")
        if not isinstance(expected_profile, Mapping):
            raise ValueError(  # noqa: TRY004 - preserve the eval payload contract
                f"Conversation eval case {case_id!r} must include expected_profile."
            )
        if not expected_profile:
            raise ValueError(f"Conversation eval case {case_id!r} must include expected_profile.")
        expected_saved_structured_fields = tuple(
            str(item)
            for item in payload.get("expected_saved_structured_fields", []) or []
        )
        expected_reply_contains = tuple(
            str(item)
            for item in payload.get("expected_reply_contains", []) or []
        )
        return cls(
            id=case_id,
            message=message,
            initial_profile=initial_profile,
            expected_profile=dict(expected_profile),
            expected_saved_structured_fields=expected_saved_structured_fields,
            expected_reply_contains=expected_reply_contains,
        )


@dataclass(frozen=True)
class ConversationEvalCaseResult:
    """一条黄金用例的评测结果。"""

    id: str
    message: str
    updated_fields: list[str]
    expected_field_count: int
    expected_field_match_count: int
    passed: bool
    mismatches: list[str] = field(default_factory=list)
    reply: str = ""
    actual_profile: dict[str, object] = field(default_factory=dict)

    @property
    def expected_field_match_rate(self) -> float:
        if self.expected_field_count == 0:
            return 1.0
        return self.expected_field_match_count / self.expected_field_count


@dataclass(frozen=True)
class ConversationEvalReport:
    """档案变更回归的聚合结果。"""

    case_results: list[ConversationEvalCaseResult]

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
    def mean_expected_field_match_rate(self) -> float:
        return _mean(result.expected_field_match_rate for result in self.case_results)

    def to_dict(self) -> dict[str, object]:
        return {
            "case_count": self.case_count,
            "passed_count": self.passed_count,
            "all_passed": self.all_passed,
            "mean_expected_field_match_rate": self.mean_expected_field_match_rate,
            "cases": [asdict(result) for result in self.case_results],
        }


def evaluate_conversation_cases(cases: Sequence[ConversationEvalCase]) -> ConversationEvalReport:
    """Evaluate golden conversation-ingestion cases without mutating a database."""

    results: list[ConversationEvalCaseResult] = []
    for case in cases:
        current_profile = CandidateProfile(id=0, **asdict(case.initial_profile))
        decision = decide_conversation_ingestion(current_profile, case.message)
        updated_profile, updated_fields = apply_candidate_profile_patch(
            current_profile,
            decision.profile_updates,
        )

        mismatches: list[str] = []
        matched_fields = 0
        for field_name, expected_value in case.expected_profile.items():
            actual_value = getattr(updated_profile, field_name)
            if actual_value == expected_value:
                matched_fields += 1
                continue
            mismatches.append(
                f"{field_name}: expected {expected_value!r}, got {actual_value!r}"
            )
        if case.expected_saved_structured_fields and list(updated_fields) != list(
            case.expected_saved_structured_fields
        ):
            mismatches.append(
                "saved_structured_fields: expected "
                f"{list(case.expected_saved_structured_fields)!r}, got {updated_fields!r}"
            )
        if case.expected_reply_contains:
            missing = [
                fragment
                for fragment in case.expected_reply_contains
                if fragment not in decision.reply
            ]
            if missing:
                mismatches.append(
                    f"reply is missing fragments: {missing!r}; actual reply={decision.reply!r}"
                )

        results.append(
            ConversationEvalCaseResult(
                id=case.id,
                message=case.message,
                updated_fields=updated_fields,
                expected_field_count=len(case.expected_profile),
                expected_field_match_count=matched_fields,
                passed=not mismatches,
                mismatches=mismatches,
                reply=decision.reply,
                actual_profile=asdict(updated_profile),
            )
        )
    return ConversationEvalReport(results)


def load_conversation_eval_cases(path: str | Path) -> list[ConversationEvalCase]:
    """Load conversation eval cases from JSON."""

    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    raw_cases = payload.get("cases") if isinstance(payload, Mapping) else payload
    if not isinstance(raw_cases, list):
        raise ValueError(  # noqa: TRY004 - preserve the eval payload contract
            "Conversation eval JSON must be a list or an object with a cases list."
        )
    return [ConversationEvalCase.from_mapping(item) for item in raw_cases]


def format_conversation_eval_report(report: ConversationEvalReport) -> str:
    """Format a compact human-readable report."""

    lines = [
        f"Conversation eval: {report.passed_count}/{report.case_count} cases passed",
        f"mean_expected_field_match_rate={report.mean_expected_field_match_rate:.3f}",
    ]
    for result in report.case_results:
        status = "PASS" if result.passed else "FAIL"
        suffix = f" mismatches={len(result.mismatches)}" if result.mismatches else ""
        lines.append(
            " ".join(
                [
                    f"[{status}]",
                    result.id,
                    f"field_match_rate={result.expected_field_match_rate:.3f}",
                    f"updated_fields={result.updated_fields}",
                ]
            )
            + suffix
        )
        for mismatch in result.mismatches:
            lines.append(f"    - {mismatch}")
    return "\n".join(lines)


def write_example_cases(path: str | Path) -> None:
    """Write an editable eval-case template."""

    target = Path(path)
    example = {
        "cases": [
            {
                "id": "direction-replacement",
                "message": "我想把求职方向改为后端开发。",
                "initial_profile": {
                    "name": "方向替换测试",
                    "status": "待补充",
                    "education": "本科",
                    "experience_years": 1,
                    "skills": {},
                    "preferred_cities": [],
                    "salary_floor_k": None,
                    "expected_salary_k": None,
                    "target_directions": ["AI Agent 应用开发"],
                    "unacceptable": [],
                },
                "expected_profile": {
                    "target_directions": ["后端开发"],
                },
                "expected_saved_structured_fields": ["target_directions"],
                "expected_reply_contains": ["已保存"],
            },
            {
                "id": "skill-proficiency",
                "message": "我的python熟练度是精通。",
                "initial_profile": {
                    "name": "技能熟练度测试",
                    "status": "待补充",
                    "education": "本科",
                    "experience_years": 1,
                    "skills": {"Python": "待确认"},
                    "preferred_cities": [],
                    "salary_floor_k": None,
                    "expected_salary_k": None,
                    "target_directions": [],
                    "unacceptable": [],
                },
                "expected_profile": {
                    "skills": {"Python": "精通"},
                },
                "expected_saved_structured_fields": ["skills"],
                "expected_reply_contains": ["已保存"],
            },
        ]
    }
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(example, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run conversation profile regression cases.")
    parser.add_argument("--cases", help="Path to conversation eval cases JSON.")
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    parser.add_argument("--write-example", help="Write an editable eval case template and exit.")
    args = parser.parse_args(argv)

    if args.write_example:
        write_example_cases(args.write_example)
        print(f"Wrote conversation eval example cases to {args.write_example}")
        return 0
    if not args.cases:
        parser.error("--cases is required unless --write-example is used.")

    cases = load_conversation_eval_cases(args.cases)
    report = evaluate_conversation_cases(cases)

    if args.json:
        print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
    else:
        print(format_conversation_eval_report(report))
    return 0 if report.all_passed else 1


def _candidate_profile_input_from_mapping(payload: Mapping[str, object]) -> CandidateProfileInput:
    """Load a candidate profile snapshot from JSON."""

    return CandidateProfileInput(
        name=_required_str(payload, "name"),
        status=_required_str(payload, "status"),
        education=_required_str(payload, "education"),
        experience_years=float(payload.get("experience_years", 0) or 0),
        skills=normalize_skill_mapping(_string_mapping(payload.get("skills"))),
        preferred_cities=normalize_city_list(_string_list(payload.get("preferred_cities"))),
        salary_floor_k=_optional_int(payload.get("salary_floor_k")),
        expected_salary_k=_optional_int(payload.get("expected_salary_k")),
        target_directions=_string_list(payload.get("target_directions")),
        unacceptable=_string_list(payload.get("unacceptable")),
        acceptable_cities=normalize_city_list(_string_list(payload.get("acceptable_cities"))),
        preference_weights=sanitize_preference_weights(
            _float_mapping(payload.get("preference_weights"))
        ),
    )


def _mean(values: Sequence[float] | Any) -> float:
    materialized = list(values)
    if not materialized:
        return 0.0
    return sum(materialized) / len(materialized)


def _required_str(payload: Mapping[str, object], key: str) -> str:
    value = str(payload.get(key) or "").strip()
    if not value:
        raise ValueError(f"Missing required string field: {key}")
    return value


def _optional_int(value: object) -> int | None:
    if value is None or value == "":
        return None
    return int(value)


def _string_list(value: object) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise ValueError("Expected a list of strings.")  # noqa: TRY004 - eval payload contract
    return [str(item).strip() for item in value if str(item).strip()]


def _string_mapping(value: object) -> dict[str, str]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ValueError("Expected a mapping of strings.")  # noqa: TRY004 - eval payload contract
    return {str(key): str(item) for key, item in value.items() if str(key).strip()}


def _float_mapping(value: object) -> dict[str, float]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ValueError("Expected a mapping of numbers.")  # noqa: TRY004 - eval payload contract
    return {str(key): float(item) for key, item in value.items() if str(key).strip()}


if __name__ == "__main__":  # pragma: no cover - CLI entrypoint
    raise SystemExit(main())
