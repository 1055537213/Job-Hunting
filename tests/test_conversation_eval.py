"""候选人档案变更回归评测测试。"""

from __future__ import annotations

import json

from job_hunting_agent.evals.conversation_eval import (
    ConversationEvalCase,
    evaluate_conversation_cases,
    load_conversation_eval_cases,
    write_example_cases,
)
from job_hunting_agent.models import CandidateProfileInput, CandidateProfilePatch
from job_hunting_agent.profile_mutation import apply_candidate_profile_patch


def test_apply_candidate_profile_patch_replaces_direction_and_keeps_skill_merge() -> None:
    """纯 patch 合并应复用现有方向替换和技能更新语义。"""

    current = CandidateProfileInput(
        name="测试候选人",
        status="待补充",
        education="本科",
        experience_years=1,
        skills={"Python": "待确认"},
        preferred_cities=["杭州"],
        salary_floor_k=10,
        expected_salary_k=15,
        target_directions=["AI Agent 应用开发"],
        unacceptable=[],
    )
    patch = CandidateProfilePatch(
        skills={"python": "精通"},
        preferred_cities=["上海"],
        acceptable_cities=["南京"],
        target_directions=["后端开发"],
        replace_target_directions=True,
    )

    updated, updated_fields = apply_candidate_profile_patch(current, patch)

    assert updated.skills["Python"] == "精通"
    assert updated.preferred_cities == ["上海"]
    assert updated.acceptable_cities == ["南京"]
    assert updated.target_directions == ["后端开发"]
    assert updated_fields == ["skills", "preferred_cities", "acceptable_cities", "target_directions"]


def test_conversation_eval_reports_profile_mutation_and_reply() -> None:
    """黄金用例评测应该直接反映规则化档案变更。"""

    case = ConversationEvalCase.from_mapping(
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
            "expected_profile": {"target_directions": ["后端开发"]},
            "expected_saved_structured_fields": ["target_directions"],
            "expected_reply_contains": ["已保存"],
        }
    )

    report = evaluate_conversation_cases([case])

    assert report.all_passed
    assert report.case_results[0].updated_fields == ["target_directions"]
    assert report.case_results[0].actual_profile["target_directions"] == ["后端开发"]


def test_conversation_eval_loads_and_writes_example_cases(tmp_path) -> None:
    """示例文件应可写可读，方便手工补充黄金用例。"""

    path = tmp_path / "conversation_eval.example.json"
    write_example_cases(path)

    cases = load_conversation_eval_cases(path)
    payload = json.loads(path.read_text(encoding="utf-8"))

    assert len(cases) == 2
    assert cases[0].id == payload["cases"][0]["id"]
    assert cases[1].expected_profile["skills"] == {"Python": "精通"}
