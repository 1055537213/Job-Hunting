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
) -> ResumeDraft:
    """生成职位定制简历草稿。

    `confirmed_project_cards` 必须由应用服务层提前过滤，只传入候选人已经确认的
    项目卡片。待确认卡片不能进入简历草稿证据池。
    """

    evidence = collect_evidence(candidate, confirmed_project_cards)
    matched_skills = [skill for skill in job.skills if skill in candidate.skills]
    missing_skills = [skill for skill in job.skills if skill not in candidate.skills]
    risks = [
        f"职位要求包含未确认技能：{skill}，草稿正文未写入该技能。"
        for skill in missing_skills
    ]
    rewrite_notes = [
        "技能熟练度按候选人档案保守表达，不自动拔高。",
        "项目卡片只使用候选人已确认摘要，待确认线索不进入正文。",
    ]

    fallback_content = rule_based_content(candidate, job, matched_skills, confirmed_project_cards)
    llm_used = llm_client is not None
    llm_discarded = False
    content = fallback_content

    if llm_client is not None:
        prompt = build_prompt(candidate, job, evidence, matched_skills, missing_skills)
        llm_content = llm_client.complete(prompt).strip()
        validation = validate_llm_output(llm_content, candidate, missing_skills, evidence)
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

    skill_lines = [
        f"- {skill_phrase(skill, candidate.skills[skill])}"
        for skill in matched_skills
    ] or ["- 暂未发现职位技能与候选人档案中的已确认技能直接重合。"]
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
            "请输出中文简历草稿正文。",
        ]
    )


def validate_llm_output(
    text: str,
    candidate: CandidateProfile,
    missing_skills: list[str],
    evidence: list[str],
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
    for skill in KNOWN_SKILLS:
        if skill not in candidate.skills and skill.lower() in lower_text:
            note = f"包含档案外技能：{skill}"
            if note not in violations:
                violations.append(note)
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
