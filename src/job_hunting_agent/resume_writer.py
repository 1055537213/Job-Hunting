"""证据约束简历草稿生成器。

这个模块是第一处 LLM 使用点，但 LLM 只负责“表达”，不能新增事实。
生成流程分三步：

1. 从候选人档案、职位信息、已确认项目卡片中整理允许使用的证据。
2. 如传入 LLM，则请 LLM 基于证据改写；随后做真实性检查。
3. 如果 LLM 输出包含未确认事实，丢弃 LLM 输出并回退到规则草稿。

这样以后即使接入真实模型，也不会把模型幻觉直接保存成候选人经历。
"""

from __future__ import annotations

import re

from .job_parser import KNOWN_SKILLS
from .llm import LLMClient
from .models import CandidateProfile, ImportedJob, ProjectExperienceRecord, ResumeDraft


def build_resume_draft(
    candidate: CandidateProfile,
    job: ImportedJob,
    confirmed_project_cards: list[ProjectExperienceRecord],
    llm_client: LLMClient | None = None,
    semantic_evidence: list[str] | None = None,
    source_resume_text: str | None = None,
    allow_proficiency_upgrade: bool = False,
) -> ResumeDraft:
    """生成职位定制简历草稿。

    `confirmed_project_cards` 必须由应用服务层提前过滤，只传入候选人已经确认的
    项目卡片。`semantic_evidence` 来自 RAG 检索结果，只作为可追溯上下文，
    不能覆盖候选人档案中的结构化事实。
    """

    # 只有候选人事实和候选人主动上传的原简历可以建立新事实。RAG 结果还可能
    # 命中职位原文或同账号其它材料，因此只能帮助模型理解上下文，不能放宽校验。
    factual_evidence = collect_evidence(candidate, confirmed_project_cards)
    normalized_source_resume = (source_resume_text or "").strip()
    if normalized_source_resume:
        # 上传简历是候选人主动提供的既有陈述，可以参与本次改写，但不会反向写入档案。
        factual_evidence.append("候选人上传简历原文：\n" + normalized_source_resume[:30_000])
    contextual_evidence = list(semantic_evidence or [])
    evidence = [*factual_evidence, *contextual_evidence]
    factual_evidence_text = "\n".join(factual_evidence).lower()
    matched_skills = [
        skill
        for skill in job.skills
        if skill in candidate.skills or skill.lower() in factual_evidence_text
    ]
    missing_skills = [skill for skill in job.skills if skill not in matched_skills]
    risks = [
        f"职位要求包含未确认技能：{skill}，草稿正文未写入该技能。"
        for skill in missing_skills
    ]
    for skill in matched_skills:
        if skill not in candidate.skills:
            risks.append(f"技能 {skill} 仅来自已提供材料，熟练度仍需候选人确认。")
    if allow_proficiency_upgrade:
        # 用户可以明确要求一次性提高草稿措辞，但该选择不会反向修改事实档案。
        risks.append("本次按候选人明确要求放宽熟练度措辞，发布前仍需再次核对真实性。")
    rewrite_notes = [
        "技能熟练度按候选人档案保守表达，不自动拔高。",
        "项目卡片只使用候选人已确认摘要，待确认线索不进入正文。",
        "RAG 检索结果只作为证据上下文，不能单独证明候选人事实。",
        "上传简历只作为本次改写证据，不会自动覆盖候选人结构化档案。",
    ]

    fallback_content = (
        normalized_source_resume
        if normalized_source_resume
        else rule_based_content(candidate, job, matched_skills, confirmed_project_cards)
    )
    llm_used = llm_client is not None
    llm_discarded = False
    content = fallback_content

    if llm_client is not None:
        prompt = build_prompt(candidate, job, evidence, matched_skills, missing_skills)
        llm_content = llm_client.complete(prompt).strip()
        validation = validate_llm_output(
            llm_content,
            candidate,
            missing_skills,
            factual_evidence,
            allow_proficiency_upgrade=allow_proficiency_upgrade,
        )
        if validation:
            llm_discarded = True
            risks.append("LLM 输出已丢弃：" + "；".join(validation))
        elif llm_content:
            content = llm_content

    return ResumeDraft(
        title=f"{candidate.name} - {job.title} 职位定制简历草稿",
        content=content,
        evidence_items=evidence,
        authenticity_risks=risks,
        rewrite_notes=rewrite_notes,
        llm_used=llm_used,
        llm_discarded=llm_discarded,
    )


def collect_evidence(
    candidate: CandidateProfile,
    confirmed_project_cards: list[ProjectExperienceRecord],
) -> list[str]:
    """收集简历草稿允许使用的证据条目。"""

    evidence = [
        f"候选人姓名：{candidate.name}",
        f"最高学历：{candidate.education}",
        f"实际经验年限：{candidate.experience_years:g} 年",
    ]
    for skill, level in candidate.skills.items():
        evidence.append(f"已确认技能：{skill}（{level}）")
    for record in confirmed_project_cards:
        if record.confirmed_summary:
            evidence.append(f"已确认项目：{record.card.project_name}：{record.confirmed_summary}")
    return evidence


def rule_based_content(
    candidate: CandidateProfile,
    job: ImportedJob,
    matched_skills: list[str],
    confirmed_project_cards: list[ProjectExperienceRecord],
) -> str:
    """生成不依赖 LLM 的安全草稿正文。

    规则草稿不追求文采，优先保证事实不越界。它也是 LLM 输出不安全时的回退结果。
    """

    skill_lines = [f"- {matched_skill_phrase(skill, candidate)}" for skill in matched_skills] or [
        "- 暂未发现职位技能与候选人事实材料中的已确认技能直接重合。"
    ]
    project_lines = []
    for record in confirmed_project_cards:
        if record.confirmed_summary:
            project_lines.append(f"- {record.card.project_name}：{record.confirmed_summary}")
    if not project_lines:
        project_lines.append("- 暂无已确认项目摘要，建议候选人先确认项目经历卡片。")

    return "\n".join(
        [
            "【求职目标】",
            f"应聘 {job.title}，结合候选人已确认经历突出与岗位相关的能力。",
            "",
            "【能力摘要】",
            f"- {candidate.education}学历，{candidate.experience_years:g} 年实际工作/项目相关经验。",
            *skill_lines,
            "",
            "【项目经历重点】",
            *project_lines,
            "",
            "【真实性边界】",
            "- 未在候选人档案或已确认项目卡片中出现的技能、成果数字和职责不写入正文。",
        ]
    )


def skill_phrase(skill: str, level: str) -> str:
    """按候选人记录的熟练度生成保守措辞。

    这里实现用户之前确认的规则：默认不把“了解/项目使用”写成“精通”。
    """

    normalized = level.strip()
    if normalized in {"精通", "熟练", "熟练掌握"}:
        return f"熟练掌握 {skill}"
    if normalized in {"熟悉", "较熟悉"}:
        return f"熟悉 {skill}"
    if normalized in {"项目使用", "项目中使用", "使用过"}:
        return f"在项目中使用过 {skill}"
    if normalized in {"了解", "学习过", "入门"}:
        return f"了解 {skill}，可结合项目继续补充证据"
    return f"{skill}（{normalized}）"


def matched_skill_phrase(skill: str, candidate: CandidateProfile) -> str:
    """为已匹配技能生成不会假设未知熟练度的简历措辞。"""

    if skill in candidate.skills:
        return skill_phrase(skill, candidate.skills[skill])
    return f"候选人材料中提及 {skill}（熟练度待确认）"


def build_prompt(
    candidate: CandidateProfile,
    job: ImportedJob,
    evidence: list[str],
    matched_skills: list[str],
    missing_skills: list[str],
) -> str:
    """构造给 LLM 的简历改写 prompt。

    Prompt 会明确列出允许使用和禁止新增的内容；但我们仍然会在输出后做检查，
    因为 prompt 约束不能替代程序边界。
    """

    return "\n".join(
        [
            "你是求职简历改写助手，只能基于【允许证据】写职位定制简历草稿。",
            "不要新增未确认技能、证书、学历、工作年限、职责范围或成果数字。",
            f"候选人：{candidate.name}",
            f"目标职位：{job.title}",
            "职位技能：" + "、".join(job.skills),
            "已匹配技能：" + "、".join(matched_skills),
            "禁止写入的未确认技能：" + "、".join(missing_skills),
            "【允许证据】",
            *evidence,
            "【RAG 使用边界】",
            "如果证据条目标注为 RAG 检索证据，只能作为已登记材料的引用上下文，不要把它改写成新的事实。",
            "请输出中文简历草稿正文。",
        ]
    )


def validate_llm_output(
    text: str,
    candidate: CandidateProfile,
    missing_skills: list[str],
    evidence: list[str],
    *,
    allow_proficiency_upgrade: bool = False,
) -> list[str]:
    """检查 LLM 输出是否越过证据边界。

    MVP 先做两类高价值检查：未确认技能、没有证据支撑的成果数字。后续可以把
    这里扩展成更完整的事实核查器。
    """

    violations: list[str] = []
    lower_text = text.lower()
    for skill in missing_skills:
        if skill.lower() in lower_text:
            violations.append(f"包含未确认技能：{skill}")
    evidence_text = "\n".join(evidence).lower()
    for skill in KNOWN_SKILLS:
        if (
            skill not in candidate.skills
            and skill.lower() not in evidence_text
            and skill.lower() in lower_text
        ):
            note = f"包含档案外技能：{skill}"
            if note not in violations:
                violations.append(note)
    if not allow_proficiency_upgrade:
        for skill, level in candidate.skills.items():
            if level.strip() in {"项目使用", "项目中使用", "使用过", "了解", "学习过", "入门"}:
                inflated_patterns = [
                    rf"(?:精通|熟练掌握|专家级)[^。；\n]{{0,12}}{re.escape(skill)}",
                    rf"{re.escape(skill)}[^。；\n]{{0,12}}(?:精通|熟练掌握|专家级)",
                ]
                if any(re.search(pattern, text, flags=re.IGNORECASE) for pattern in inflated_patterns):
                    violations.append(f"技能熟练度被拔高：{skill}（档案为{level}）")
    if contains_unsupported_metric(text, evidence):
        violations.append("包含未在证据中出现的成果数字")
    return violations


def contains_unsupported_metric(text: str, evidence: list[str]) -> bool:
    """判断文本中是否存在没有证据支撑的成果数字。

    这里不是禁止所有数字；学历、年限等数字可以来自证据。只有带明显成果动词的
    百分比/倍数/数量表达，且证据中没有同样表达时，才标为风险。
    """

    evidence_text = "\n".join(evidence)
    metric_patterns = [
        r"(?:提升|提高|降低|减少|节省|增长|优化)[^。；\n]{0,20}\d+(?:\.\d+)?\s*[%％]",
        r"\d+(?:\.\d+)?\s*(?:倍|万|千|ms|秒|分钟|小时)",
    ]
    for pattern in metric_patterns:
        for match in re.finditer(pattern, text):
            if match.group(0) not in evidence_text:
                return True
    return False
