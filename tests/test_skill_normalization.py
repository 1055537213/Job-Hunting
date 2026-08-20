"""候选人技能规范化与别名去重回归测试。"""

from __future__ import annotations

import json

from job_hunting_agent.app import JobHuntingApp
from job_hunting_agent.conversation_ingestion import (
    build_ingestion_prompt,
    decision_from_json,
    extract_skills_from_message,
)
from job_hunting_agent.deduplication import candidate_profile_content_fingerprint
from job_hunting_agent.job_parser import extract_skills
from job_hunting_agent.models import CandidateProfileInput, CandidateProfilePatch
from job_hunting_agent.skill_normalization import (
    merge_skill_mappings,
    normalize_skill_mapping,
    skill_identity,
)


def build_profile(skills: dict[str, str]) -> CandidateProfileInput:
    """构造除技能外完全相同的档案输入，便于验证内容指纹。"""

    return CandidateProfileInput(
        name="技能归一化测试",
        status="待补充",
        education="本科",
        experience_years=1,
        skills=skills,
        preferred_cities=[],
        salary_floor_k=None,
        expected_salary_k=None,
        target_directions=[],
        unacceptable=[],
    )


def test_skill_identity_handles_case_common_aliases_and_keeps_distinct_skills_separate():
    """大小写、常见别名和连接符变体应合并，C# 与 C++ 仍是不同技能。"""

    assert skill_identity("Python") == skill_identity(" python ") == skill_identity("Python 3")
    assert skill_identity("JavaScript") == skill_identity("JS")
    assert skill_identity("Go") == skill_identity("Golang")
    assert skill_identity("C#") != skill_identity("C++")
    assert normalize_skill_mapping({"Python": "待确认", "python": "熟悉"}) == {"Python": "待确认"}
    assert merge_skill_mappings({"Python": "待确认"}, {"python": "熟悉"}) == {"Python": "熟悉"}


def test_skill_mentions_use_aliases_without_matching_inside_other_skill_names():
    """技能抽取既要识别简称，也不能把较长技能名拆成无关短词。"""

    assert extract_skills("需要 JavaScript 和 Django 项目经验。") == ["JavaScript", "Django"]
    assert extract_skills("熟悉 JS，并使用 Golang 开发服务。") == ["Go", "JavaScript"]
    assert extract_skills_from_message("我熟练 JS，了解 Golang。") == {
        "Go": "了解",
        "JavaScript": "熟练",
    }


def test_skill_aliases_do_not_bypass_candidate_profile_content_deduplication():
    """仅把 Python 写成 python 或 Python 3 时，档案内容指纹必须保持一致。"""

    assert candidate_profile_content_fingerprint(build_profile({"Python": "项目使用"})) == (
        candidate_profile_content_fingerprint(build_profile({"python 3": "项目使用"}))
    )


def test_profile_storage_and_agent_ingestion_merge_aliases_without_duplicate_records(account_id):
    """网页/Agent 的所有写入最终都在仓储边界合并，历史显示也不会出现重复项。"""

    app = JobHuntingApp()
    app.initialize()
    candidate_id = app.save_candidate_profile(
        build_profile({"Python": "待确认", "python": "熟悉", "JS": "了解", "JavaScript": "熟悉"}),
        account_id=account_id,
    )

    profile = app.get_candidate_profile(candidate_id, account_id=account_id)
    assert profile.skills == {"Python": "待确认", "JavaScript": "了解"}
    assert "当前已记录技能：{'Python': '待确认'" in build_ingestion_prompt(profile, "我会 python。")

    decision = decision_from_json(
        profile,
        "我会 python。",
        json.dumps(
            {
                "reply": "已核对技能。",
                "profile_updates": {"skills": {"python": "待确认"}},
                "long_texts": [],
            },
            ensure_ascii=False,
        ),
    )
    assert decision.profile_updates.skills == {"Python": "待确认"}
    assert app.store.update_candidate_profile(
        candidate_id,
        decision.profile_updates,
        account_id=account_id,
    ) == []

    updated_fields = app.store.update_candidate_profile(
        candidate_id,
        CandidateProfilePatch(skills={"Python 3": "熟悉", "Golang": "项目使用"}),
        account_id=account_id,
    )
    assert updated_fields == ["skills"]
    profile = app.get_candidate_profile(candidate_id, account_id=account_id)
    assert profile.skills == {"Python": "熟悉", "JavaScript": "了解", "Go": "项目使用"}

    duplicate_only_fields = app.store.update_candidate_profile(
        candidate_id,
        CandidateProfilePatch(skills={"python": "熟悉"}),
        account_id=account_id,
    )
    assert duplicate_only_fields == []

    # 模拟旧版本已经写入 Python/python 两个键的档案；读取页面时必须只显示一个。
    with app.store.connect() as conn:
        conn.execute(
            "UPDATE candidate_profiles SET skills_json = ? WHERE id = ?",
            (json.dumps({"Python": "待确认", "python": "待确认"}, ensure_ascii=False), candidate_id),
        )
    assert app.get_candidate_profile(candidate_id, account_id=account_id).skills == {"Python": "待确认"}
