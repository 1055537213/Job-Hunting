"""职位匹配与排序规则。

这个模块实现决策地图 #7 的规则：硬性淘汰从严，普通差距扣分。
当前是可测试的规则版匹配器，不代表录用概率，也不会替候选人投递。
"""

from __future__ import annotations

import math
from collections.abc import Callable

from .city_catalog import normalize_city_list, normalize_city_name
from .job_parser import EDUCATION_ORDER
from .models import (
    CandidateProfile,
    ImportedJob,
    MatchResult,
    SkillRequirement,
    sanitize_preference_weights,
)
from .skill_normalization import skill_identity, skill_level_is_available

SKILL_PROFICIENCY_SCORE = {
    "精通": 100.0,
    "熟练": 100.0,
    "熟练掌握": 100.0,
    "熟悉": 85.0,
    "较熟悉": 85.0,
    "项目使用": 70.0,
    "项目中使用": 70.0,
    "使用过": 70.0,
    "了解": 40.0,
    "学习过": 40.0,
    "入门": 40.0,
    "待确认": 30.0,
}
DirectionScorer = Callable[[CandidateProfile, ImportedJob], float | None]


def match_job(
    candidate: CandidateProfile,
    job: ImportedJob,
    direction_scorer: DirectionScorer | None = None,
) -> MatchResult:
    """比较一个候选人和一个职位，返回可解释匹配结果。"""

    elimination_reasons: list[str] = []
    reasons: list[str] = []
    deductions: list[str] = []
    risks: list[str] = []
    dimension_scores: dict[str, float] = {}
    applied_weights: dict[str, float] = {}

    # 城市偏好是软指标：首选、其他可接受和非目标城市分别给不同标准化分数。
    preferred_cities = set(normalize_city_list(candidate.preferred_cities))
    acceptable_cities = set(normalize_city_list(candidate.acceptable_cities)) - preferred_cities
    job_city = normalize_city_name(job.city)
    if preferred_cities or acceptable_cities:
        if not job_city:
            city_score = 50.0
            risks.append("职位城市信息缺失，暂时无法判断城市偏好")
        elif job_city in preferred_cities:
            city_score = 100.0
            reasons.append(f"命中首选城市：{job_city}")
        elif job_city in acceptable_cities:
            city_score = 80.0
            reasons.append(f"命中其他可接受城市：{job_city}")
        else:
            city_score = 40.0
            deductions.append(f"职位城市 {job_city} 不在目标城市偏好（首选或其他可接受城市）中")
            risks.append("职位城市不在当前城市偏好中，需要确认是否接受")
        dimension_scores["city"] = city_score

    # 月薪现在是软指标：低于底线降分，不再直接淘汰；硬底线字段仍保留用于解释。
    if candidate.salary_floor_k is not None or candidate.expected_salary_k is not None:
        dimension_scores["salary"] = salary_score(candidate, job, deductions, risks, reasons)

    # 候选人明确写入 unacceptable 的条件，例如外包、长期出差，出现即淘汰。
    for unacceptable in candidate.unacceptable:
        if unacceptable and unacceptable in job.raw_text:
            elimination_reasons.append(f"包含明确不可接受条件：{unacceptable}")

    # 候选人在档案里显式确认“不会”的核心技能，可以触发更强淘汰。
    core_skill_names = {
        skill_identity(requirement.name)
        for requirement in (job.skill_requirements or [])
        if requirement.category == "core"
    }
    confirmed_missing_core = [
        name
        for name, level in candidate.skills.items()
        if not skill_level_is_available(level)
        and skill_identity(name) in core_skill_names
    ]
    if confirmed_missing_core:
        elimination_reasons.append("职位明确必须且候选人确认不具备：" + "、".join(confirmed_missing_core))

    # 经验规则：低于职位最低经验 3 年及以上直接淘汰，小于 3 年按比例扣分。
    if job.experience_min_years is not None:
        gap = job.experience_min_years - candidate.experience_years
        if gap >= 3:
            elimination_reasons.append(
                f"经验差距 {gap:g} 年：候选人 {candidate.experience_years:g} 年，职位最低 {job.experience_min_years:g} 年"
            )
        elif gap > 0:
            dimension_scores["experience"] = max(0.0, 100.0 - gap * 25.0)
            deductions.append(f"经验低于职位最低要求 {gap:g} 年")
            risks.append("经验略低，建议用项目经历证明可迁移能力")
        else:
            dimension_scores["experience"] = 100.0
    else:
        dimension_scores["experience"] = 50.0
        risks.append("职位未明确经验要求，经验匹配信息不完整")

    # 学历规则来自 #7：候选人学历必须大于或等于职位学历要求。
    education_confidence = float(job.field_confidence.get("education", 1.0) or 0)
    if job.education and education_confidence >= 0.5 and education_rank(candidate.education) < education_rank(job.education):
        elimination_reasons.append(f"学历低于职位要求：候选人 {candidate.education}，职位要求 {job.education}")
    elif job.education and education_confidence < 0.5:
        risks.append("职位学历字段置信度较低，暂不执行学历硬性淘汰")

    # 技能先按核心/一般/加分分组，避免职位列出大量非核心技能时把分数拉低。
    dimension_scores["skills"] = skill_score(candidate, job, reasons, deductions, risks)

    # 语义模型是可选增强；网络、协议或配置异常时回退到本地规则，不能阻断匹配。
    direction_score = None
    if direction_scorer is not None:
        try:
            direction_score = direction_scorer(candidate, job)
        except Exception:  # noqa: BLE001 - 匹配必须继续返回可解释结果。
            risks.append("语义方向模型暂时不可用，已回退关键词匹配")
    if direction_score is None:
        direction_score = direction_fallback_score(candidate, job, reasons, risks)
    elif candidate.target_directions:
        direction_score = max(0.0, min(100.0, float(direction_score)))
        reasons.append("岗位方向已使用 Embedding/Rerank 语义匹配")
    if direction_score is not None:
        dimension_scores["direction"] = direction_score

    candidate_skill_names = {
        skill_identity(name)
        for name, level in candidate.skills.items()
        if skill_level_is_available(level)
    }
    matched_skills = [skill for skill in job.skills if skill_identity(skill) in candidate_skill_names]
    missing_skills = [skill for skill in job.skills if skill_identity(skill) not in candidate_skill_names]

    eliminated = bool(elimination_reasons)
    if eliminated:
        # 已淘汰职位不进入正常排序，所以分数归零，方便 UI 或 API 区分展示。
        score = 0
        tier = "已淘汰"
    else:
        weights = sanitize_preference_weights(candidate.preference_weights)
        applied_weights = {
            name: weights[name]
            for name in dimension_scores
            if name in weights
        }
        if dimension_scores and applied_weights:
            weight_total = sum(applied_weights.values())
            score = round(
                sum(dimension_scores[name] * applied_weights[name] for name in applied_weights)
                / weight_total
            )
        else:
            # 没有任何可比较字段时保留中性基线，兼容早期职位数据。
            score = 60
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
        dimension_scores=dimension_scores,
        applied_weights=applied_weights,
    )


def semantic_direction_score(
    candidate: CandidateProfile,
    job: ImportedJob,
    embeddings: object,
    reranker: object | None = None,
) -> float | None:
    """用 Embedding 与可选 Rerank 计算职位方向分。

    每个目标方向都分别比较职位标题和职位描述正文，标题贡献 30%，正文贡献
    70%。多个目标方向取最高分，避免候选人同时填写多个方向时被平均稀释。
    该函数只依赖 ``embed_documents`` 和 ``rerank`` 最小协议，便于测试替身和
    更换任意供应商适配器。
    """

    if not candidate.target_directions:
        return None
    lines = [line.strip() for line in job.description_text.splitlines() if line.strip()]
    title = (job.title or (lines[0] if lines else "")).strip()
    body = " ".join(lines[1:]).strip() or job.description_text.strip()
    if not title and not body:
        return None

    best_score: float | None = None
    for direction in candidate.target_directions:
        query = str(direction or "").strip()
        if not query:
            continue
        vectors = embeddings.embed_documents([query, title, body])
        if not isinstance(vectors, list) or len(vectors) < 3:
            continue
        title_score = normalize_similarity(cosine_similarity(vectors[0], vectors[1]))
        body_score = normalize_similarity(cosine_similarity(vectors[0], vectors[2]))

        # Rerank 的 relevance_score 可能是 0-1，也可能是 0-100；统一到 0-100
        # 后和 Embedding 各占一半，缺少分数时保留 Embedding 结果。
        if reranker is not None:
            rankings = reranker.rerank(query, [title, body], top_n=2)
            rerank_scores = {
                int(item.index): normalize_relevance(item.relevance_score)
                for item in rankings
                if getattr(item, "relevance_score", None) is not None
            }
            if 0 in rerank_scores:
                title_score = (title_score + rerank_scores[0]) / 2
            if 1 in rerank_scores:
                body_score = (body_score + rerank_scores[1]) / 2
        score = 0.3 * title_score + 0.7 * body_score
        best_score = score if best_score is None else max(best_score, score)
    return None if best_score is None else round(best_score, 2)


def cosine_similarity(left: object, right: object) -> float:
    """计算两个数值向量的余弦相似度，异常向量按 0 处理。"""

    try:
        left_values = [float(value) for value in left]  # type: ignore[arg-type]
        right_values = [float(value) for value in right]  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.0
    if not left_values or len(left_values) != len(right_values):
        return 0.0
    left_norm = math.sqrt(sum(value * value for value in left_values))
    right_norm = math.sqrt(sum(value * value for value in right_values))
    if left_norm == 0 or right_norm == 0:
        return 0.0
    return sum(a * b for a, b in zip(left_values, right_values)) / (left_norm * right_norm)


def normalize_similarity(value: float) -> float:
    """把余弦相似度从 [-1, 1] 映射到 [0, 100]。"""

    return max(0.0, min(100.0, (value + 1.0) * 50.0))


def normalize_relevance(value: object) -> float:
    """兼容常见 Rerank 供应商的 0-1 或 0-100 分数。"""

    try:
        score = float(value)
    except (TypeError, ValueError):
        return 0.0
    if 0.0 <= score <= 1.0:
        score *= 100.0
    return max(0.0, min(100.0, score))


def salary_score(
    candidate: CandidateProfile,
    job: ImportedJob,
    deductions: list[str],
    risks: list[str],
    reasons: list[str],
) -> float:
    """按月薪上限计算薪资维度分数。"""

    if job.salary_unit != "month" or job.salary_max_k is None:
        risks.append("职位月薪信息缺失，薪资匹配暂按中性分")
        return 50.0
    maximum = float(job.salary_max_k)
    floor = float(candidate.salary_floor_k or 0)
    expected = float(candidate.expected_salary_k or candidate.salary_floor_k or 0)
    target = max(expected, floor)
    if target <= 0:
        return 50.0
    if maximum >= target:
        reasons.append("职位月薪上限覆盖当前薪资目标")
        return 100.0
    if floor > 0 and maximum >= floor and expected > floor:
        deductions.append("职位月薪上限低于期望薪资，但达到最低接受线")
        return 60.0 + 40.0 * (maximum - floor) / (expected - floor)
    if floor > 0 and maximum < floor:
        deductions.append("职位月薪上限低于最低接受线，降低薪资匹配度")
        risks.append("薪资可能低于最低接受线，需要确认是否愿意让步")
        return max(0.0, min(59.0, 60.0 * maximum / floor))
    deductions.append("职位月薪上限低于期望薪资")
    return max(0.0, min(59.0, 100.0 * maximum / target))


def skill_score(
    candidate: CandidateProfile,
    job: ImportedJob,
    reasons: list[str],
    deductions: list[str],
    risks: list[str],
) -> float:
    """计算核心/一般/加分技能的分层得分。"""

    requirements = job.skill_requirements or [
        SkillRequirement(name=skill, category="general", confidence=0.5)
        for skill in job.skills
    ]
    if not requirements:
        risks.append("职位未明确技能要求，技能匹配信息不完整")
        return 50.0
    candidate_skills = {skill_identity(name): level for name, level in candidate.skills.items()}
    grouped: dict[str, list[tuple[SkillRequirement, float]]] = {
        "core": [],
        "general": [],
        "bonus": [],
        "uncertain": [],
    }
    matched: list[str] = []
    missing_core: list[str] = []
    missing_general: list[str] = []
    for requirement in requirements:
        # 旧数据库或人工编辑数据可能含有未知分类；按不确定技能处理，
        # 避免异常数据直接中断整批职位匹配。
        category = requirement.category if requirement.category in grouped else "uncertain"
        level = candidate_skills.get(skill_identity(requirement.name))
        is_confirmed_missing = level is None or not skill_level_is_available(level)
        if is_confirmed_missing:
            if category == "uncertain":
                # 不确定技能只提示或少扣分：完全不参与分数计算，保留不确定风险即可。
                risks.append("职位存在不确定技能要求：" + requirement.name)
                continue
            value = 0.0
            if category == "core":
                missing_core.append(requirement.name)
            elif category == "general":
                missing_general.append(requirement.name)
        else:
            value = proficiency_score(level)
            matched.append(requirement.name)
        grouped[category].append((requirement, value))
    group_weights = {"core": 0.7, "general": 0.2, "bonus": 0.1, "uncertain": 0.2}
    group_scores: dict[str, float] = {}
    for category, items in grouped.items():
        if not items:
            continue
        if category == "bonus":
            matched_values = [value for _, value in items if value > 0]
            # 加分技能缺失不扣分：没有命中时完全不进入该组的分母；
            # 只有候选人确实掌握时才贡献正向分数。
            if not matched_values:
                continue
            group_scores[category] = max(matched_values)
        else:
            group_scores[category] = sum(value for _, value in items) / len(items)
    active_weight_total = sum(group_weights[key] for key in group_scores)
    result = (
        sum(group_scores[key] * group_weights[key] for key in group_scores) / active_weight_total
        if active_weight_total
        else 50.0
    )
    if matched:
        reasons.append("技能匹配：" + "、".join(matched))
    if missing_core:
        deductions.append("缺少核心技能：" + "、".join(missing_core))
        risks.append("核心技能存在缺口，需要用项目证据证明可迁移能力")
    if missing_general:
        deductions.append("缺少一般技能：" + "、".join(missing_general))
        risks.append("存在一般技能缺口，但不会单独淘汰职位")
    return result


def proficiency_score(level: str) -> float:
    """把候选人技能熟练度映射到匹配分。"""

    normalized = str(level or "").strip()
    return SKILL_PROFICIENCY_SCORE.get(normalized, 50.0)


def direction_fallback_score(
    candidate: CandidateProfile,
    job: ImportedJob,
    reasons: list[str],
    risks: list[str],
) -> float | None:
    """方向模型不可用时的关键词兜底分。

    职位描述正文占 70%，职位标题占 30%。这样标题相似但职责完全不同的岗位
    不会被误判为满分，同时正文中明确的工作内容仍然是主要依据。
    """

    if not candidate.target_directions:
        return None
    description_lines = [line.strip() for line in job.description_text.splitlines() if line.strip()]
    title_text = (job.title or (description_lines[0] if description_lines else "")).lower()
    # 解析器的 description_text 保留了职位全文，第一行通常就是 title；去掉它
    # 后再比较正文，避免标题关键词同时被重复计入 70% 的正文权重。
    body_text = " ".join(description_lines[1:]).lower()
    if not body_text:
        body_text = (job.description_text or "").lower()
    if not body_text:
        risks.append("职位描述缺失，方向匹配信息不完整")
        return 50.0

    best_score = 0.0
    best_direction = ""
    title_hit = False
    body_hit = False
    for direction in candidate.target_directions:
        if not direction:
            continue
        parts = [part.lower() for part in direction.split() if part.strip()]
        if not parts:
            continue
        current_title_hit = any(part in title_text for part in parts)
        current_body_hit = any(part in body_text for part in parts)
        current_score = (30.0 if current_title_hit else 0.0) + (70.0 if current_body_hit else 0.0)
        if current_score > best_score:
            best_score = current_score
            best_direction = direction
            title_hit = current_title_hit
            body_hit = current_body_hit
    if best_score:
        reasons.append(
            f"岗位方向与目标方向相关：{best_direction}（标题 {30 if title_hit else 0}% + 正文 {70 if body_hit else 0}%）"
        )
        return best_score
    risks.append("职位方向与当前目标方向的关键词相关性较弱")
    return 40.0


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
