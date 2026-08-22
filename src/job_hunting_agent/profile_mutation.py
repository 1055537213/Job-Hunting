"""候选人档案 patch 合并的纯函数。

这层逻辑和仓储分离，方便：

- Web/Worker 复用同一套档案合并语义。
- 回归评测直接在内存里验证“消息 -> patch -> 结果档案”。
- 后续把更多档案变更规则抽成稳定的领域函数。
"""

from __future__ import annotations

from .city_catalog import normalize_city_list
from .models import CandidateProfileInput, CandidateProfilePatch, sanitize_preference_weights
from .skill_normalization import merge_skill_mappings, normalize_skill_mapping


def apply_candidate_profile_patch(
    current: CandidateProfileInput,
    patch: CandidateProfilePatch,
) -> tuple[CandidateProfileInput, list[str]]:
    """把局部 patch 合并到当前候选人档案，返回新档案和实际更新字段。"""

    updated_fields: list[str] = []

    status = current.status
    education = current.education
    experience_years = current.experience_years
    salary_floor_k = current.salary_floor_k
    expected_salary_k = current.expected_salary_k
    skills = normalize_skill_mapping(current.skills)
    preferred_cities = list(current.preferred_cities)
    acceptable_cities = list(current.acceptable_cities)
    preference_weights = sanitize_preference_weights(current.preference_weights)
    target_directions = list(current.target_directions)
    unacceptable = list(current.unacceptable)

    if patch.status:
        status = patch.status
        updated_fields.append("status")
    if patch.education:
        education = patch.education
        updated_fields.append("education")
    if patch.experience_years is not None:
        experience_years = patch.experience_years
        updated_fields.append("experience_years")
    if patch.salary_floor_k is not None:
        salary_floor_k = patch.salary_floor_k
        updated_fields.append("salary_floor_k")
    if patch.expected_salary_k is not None:
        expected_salary_k = patch.expected_salary_k
        updated_fields.append("expected_salary_k")
    if patch.skills:
        merged_skills = merge_skill_mappings(skills, patch.skills)
        if merged_skills != skills:
            skills = merged_skills
            updated_fields.append("skills")
    if patch.clear_preferred_cities:
        preferred_cities = []
        updated_fields.append("preferred_cities")
    elif patch.replace_preferred_cities or patch.preferred_cities:
        # 最新一次明确的首选城市意向覆盖旧值，而不是无限追加。
        preferred_cities = normalize_city_list(patch.preferred_cities)
        updated_fields.append("preferred_cities")
    if patch.clear_acceptable_cities:
        acceptable_cities = []
        updated_fields.append("acceptable_cities")
    if patch.acceptable_cities:
        acceptable_cities = _merge_unique(
            acceptable_cities,
            normalize_city_list(patch.acceptable_cities),
        )
        updated_fields.append("acceptable_cities")
    # 同一城市不能同时属于首选和其他可接受集合；首选级别始终优先。
    disjoint_acceptable = [city for city in acceptable_cities if city not in preferred_cities]
    if disjoint_acceptable != acceptable_cities:
        acceptable_cities = disjoint_acceptable
        if "acceptable_cities" not in updated_fields:
            updated_fields.append("acceptable_cities")
    if patch.preference_weights:
        for key, value in patch.preference_weights.items():
            if key in preference_weights:
                preference_weights[key] = sanitize_preference_weights({key: value})[key]
        updated_fields.append("preference_weights")
    if patch.target_directions:
        incoming_directions = _merge_unique([], patch.target_directions)
        next_target_directions = (
            incoming_directions
            if patch.replace_target_directions
            else _merge_unique(target_directions, incoming_directions)
        )
        if next_target_directions != target_directions:
            target_directions = next_target_directions
            updated_fields.append("target_directions")
    if patch.unacceptable:
        unacceptable = _merge_unique(unacceptable, patch.unacceptable)
        updated_fields.append("unacceptable")

    return (
        CandidateProfileInput(
            name=current.name,
            status=status,
            education=education,
            experience_years=experience_years,
            skills=skills,
            preferred_cities=preferred_cities,
            acceptable_cities=acceptable_cities,
            salary_floor_k=salary_floor_k,
            expected_salary_k=expected_salary_k,
            target_directions=target_directions,
            unacceptable=unacceptable,
            preference_weights=preference_weights,
        ),
        updated_fields,
    )


def _merge_unique(existing: list[str], incoming: list[str]) -> list[str]:
    """按出现顺序去重并保留旧值优先。"""

    merged: list[str] = []
    seen: set[str] = set()
    for value in existing + incoming:
        text = str(value).strip()
        if not text or text in seen:
            continue
        seen.add(text)
        merged.append(text)
    return merged
