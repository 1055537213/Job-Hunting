"""职位匹配与排序规则。

这个模块实现决策地图 #7 的规则：硬性淘汰从严，普通差距扣分。
当前是可测试的规则版匹配器，不代表录用概率，也不会替候选人投递。
"""

from __future__ import annotations

from .job_parser import EDUCATION_ORDER
from .models import CandidateProfile, ImportedJob, MatchResult


def match_job(candidate: CandidateProfile, job: ImportedJob) -> MatchResult:
    """比较一个候选人和一个职位，返回可解释匹配结果。"""

    elimination_reasons: list[str] = []
    reasons: list[str] = []
    deductions: list[str] = []
    risks: list[str] = []
    # 60 是 MVP 的基础分：表示“未触发淘汰时，先认为有基础相关性”，
    # 再根据技能、城市、薪资、经验差距等因素加减分。
    score = 60

    # `preferred_cities` 是目标城市偏好，不是不可接受清单；不匹配只影响排序。
    if job.city and candidate.preferred_cities and job.city not in candidate.preferred_cities:
        score -= 5
        deductions.append(f"职位城市 {job.city} 不在目标城市偏好中，扣 5 分")
        risks.append("职位城市不是目标城市，需要确认是否接受")

    # 薪资硬底线来自候选人明确约束：职位薪资上限低于底线时不推荐。
    if (
        candidate.salary_floor_k is not None
        and job.salary_unit == "month"
        and job.salary_max_k is not None
        and job.salary_max_k < candidate.salary_floor_k
    ):
        elimination_reasons.append(
            f"薪资上限 {job.salary_max_k}K 低于硬底线 {candidate.salary_floor_k}K"
        )

    # 候选人明确写入 unacceptable 的条件，例如外包、长期出差，出现即淘汰。
    for unacceptable in candidate.unacceptable:
        if unacceptable and unacceptable in job.raw_text:
            elimination_reasons.append(f"包含明确不可接受条件：{unacceptable}")

    # 经验规则来自 #7：低于职位最低经验 3 年及以上直接淘汰，小于 3 年则扣分。
    if job.experience_min_years is not None:
        gap = job.experience_min_years - candidate.experience_years
        if gap >= 3:
            elimination_reasons.append(
                f"经验差距 {gap:g} 年：候选人 {candidate.experience_years:g} 年，职位最低 {job.experience_min_years:g} 年"
            )
        elif gap > 0:
            deduction = int(gap * 8)
            score -= deduction
            deductions.append(f"经验低于职位最低要求 {gap:g} 年，扣 {deduction} 分")
            risks.append("经验略低，建议用项目经历证明可迁移能力")

    # 学历规则来自 #7：候选人学历必须大于或等于职位学历要求。
    if job.education and education_rank(candidate.education) < education_rank(job.education):
        elimination_reasons.append(f"学历低于职位要求：候选人 {candidate.education}，职位要求 {job.education}")

    # 技能默认不硬淘汰：匹配技能加分，缺失技能扣分并提示风险。
    matched_skills = [skill for skill in job.skills if skill in candidate.skills]
    missing_skills = [skill for skill in job.skills if skill not in candidate.skills]
    if matched_skills:
        score += min(25, len(matched_skills) * 6)
        reasons.append("技能匹配：" + "、".join(matched_skills))
    if missing_skills:
        penalty = min(20, len(missing_skills) * 4)
        score -= penalty
        deductions.append("缺少职位技能：" + "、".join(missing_skills))
        risks.append("技能缺口会影响匹配度，需要谨慎表达")

    if job.city in candidate.preferred_cities:
        score += 5
        reasons.append(f"城市匹配：{job.city}")

    if job.salary_unit == "month" and candidate.expected_salary_k and job.salary_max_k:
        if job.salary_max_k >= candidate.expected_salary_k:
            score += 5
            reasons.append("薪资范围覆盖期望薪资")
        else:
            deductions.append("薪资上限低于期望薪资，但未低于硬底线")

    # 岗位方向先用简单文本命中演示；后续适合换成 LLM/向量相似度。
    direction_hits = [
        direction
        for direction in candidate.target_directions
        if direction and any(part in job.description_text for part in direction.split())
    ]
    if direction_hits:
        score += 5
        reasons.append("岗位方向与目标方向相关")

    eliminated = bool(elimination_reasons)
    if eliminated:
        # 已淘汰职位不进入正常排序，所以分数归零，方便 UI 或 CLI 区分展示。
        score = 0
        tier = "已淘汰"
    else:
        score = max(0, min(100, score))
        tier = tier_for(score, risks)

    return MatchResult(
        job_id=job.id,
        candidate_id=candidate.id,
        score=score,
        tier=tier,
        eliminated=eliminated,
        reasons=reasons or ["职位与候选人档案存在基础相关性"],
        elimination_reasons=elimination_reasons,
        deductions=deductions,
        risks=risks,
        uncertainty_notes=[f"字段不确定：{name}" for name in job.uncertainty_notes],
        resume_suggestions=resume_suggestions(matched_skills, missing_skills, job),
    )


def education_rank(label: str | None) -> int:
    """把学历标签映射成可比较等级。"""

    if not label:
        return 0
    return EDUCATION_ORDER.get(label, 0)


def tier_for(score: int, risks: list[str]) -> str:
    """根据匹配分数和风险数量得到推荐档位。"""

    if score >= 80 and len(risks) <= 1:
        return "强推荐"
    if score >= 65:
        return "可投递"
    if score >= 50:
        return "冲刺机会"
    return "低优先级"


def resume_suggestions(matched_skills: list[str], missing_skills: list[str], job: ImportedJob) -> list[str]:
    """生成简历优化方向。

    这里仍然遵守证据约束改写：只建议突出已确认技能，缺失技能不能编造。
    """

    suggestions = []
    if matched_skills:
        suggestions.append("简历中优先突出已确认技能：" + "、".join(matched_skills))
    if missing_skills:
        suggestions.append("不要编造缺失技能；可在学习计划或待补足项中提及：" + "、".join(missing_skills))
    if job.description_text:
        suggestions.append("根据职位描述重排项目重点，但保留证据约束改写边界")
    return suggestions
