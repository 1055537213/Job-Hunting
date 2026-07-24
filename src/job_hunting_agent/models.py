"""项目核心数据模型。

这里集中定义跨模块传递的数据结构。模型尽量保持“只描述业务事实”，
不混入数据库连接、命令行输入或 LLM 调用细节，方便后续替换底层实现。
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class CandidateProfileInput:
    """创建候选人档案时需要的输入数据。

    这里刻意只放结构化事实和偏好，长文本材料会通过单独的
    `long_texts` 表保存，后续可替换成向量检索索引。
    """

    name: str
    status: str
    education: str
    experience_years: float
    skills: dict[str, str]
    preferred_cities: list[str]
    salary_floor_k: int | None
    expected_salary_k: int | None
    target_directions: list[str]
    unacceptable: list[str] = field(default_factory=list)


@dataclass
class CandidateProfile(CandidateProfileInput):
    """已经写入 SQLite 后的候选人档案。

    与 `CandidateProfileInput` 相比，它多了数据库分配的 `id`。
    """

    id: int = 0


@dataclass
class ImportedJob:
    """标准化后的职位信息。

    `raw_text` 保存候选人主动导入的职位原文；其他字段是系统解析出的
    可比较结构化字段。`field_confidence` 和 `uncertainty_notes` 用来避免
    把不完整职位伪装成完整职位详情。
    """

    id: int
    raw_text: str
    source_url: str | None
    title: str
    city: str | None
    salary_min_k: int | None
    salary_max_k: int | None
    salary_months: int | None
    salary_unit: str
    experience_min_years: float | None
    experience_max_years: float | None
    experience_label: str | None
    education: str | None
    company_name: str | None
    industry: str | None
    company_size: str | None
    skills: list[str]
    description_text: str
    field_confidence: dict[str, float]
    uncertainty_notes: list[str]


@dataclass
class MatchResult:
    """职位匹配结果。

    匹配结果不是录用概率，而是可解释的推荐判断：包含分数、推荐档位、
    硬性淘汰原因、扣分项、风险和简历优化方向。
    """

    job_id: int
    candidate_id: int
    score: int
    tier: str
    eliminated: bool
    reasons: list[str]
    elimination_reasons: list[str]
    deductions: list[str]
    risks: list[str]
    uncertainty_notes: list[str]
    resume_suggestions: list[str]


@dataclass
class ProjectExperienceCard:
    """本地项目分析产出的待确认项目经历卡片。

    这张卡片不能直接写入候选人档案；它只是把项目证据材料整理成
    技术栈、功能、职责草稿和待确认问题，等待候选人确认。
    """

    card_type: str
    project_name: str
    read_files: list[str]
    skipped_summary: dict[str, int]
    detected_tech_stack: list[str]
    detected_core_features: list[str]
    responsibility_draft: list[str]
    highlight_draft: list[str]
    resume_expression_draft: list[str]
    questions_for_candidate: list[str]


@dataclass
class ProjectExperienceRecord:
    """已经保存到 SQLite 的项目经历卡片记录。

    `card` 是系统分析出的待确认内容；`status` 和 `confirmed_summary`
    记录候选人是否确认过。确认项目卡片不会反向覆盖候选人档案里的结构化事实。
    """

    id: int
    candidate_id: int
    status: str
    card: ProjectExperienceCard
    confirmed_summary: str | None
    created_at: str
    confirmed_at: str | None


@dataclass
class ResumeDraft:
    """职位定制简历草稿正文。

    草稿是给候选人编辑和确认的表达结果，不是候选人档案事实源。
    `evidence_items` 说明正文来自哪些已确认材料，`authenticity_risks`
    记录缺口、LLM 回退或可能需要人工确认的风险。
    """

    title: str
    content: str
    evidence_items: list[str]
    authenticity_risks: list[str]
    rewrite_notes: list[str]
    llm_used: bool
    llm_discarded: bool


@dataclass
class ResumeDraftRecord:
    """已经保存的职位定制简历草稿版本。

    同一候选人针对同一职位可以生成多个版本；这些版本不会反向覆盖
    候选人档案，只作为可编辑草稿保存。
    """

    id: int
    candidate_id: int
    job_id: int
    version: int
    status: str
    draft: ResumeDraft
    created_at: str
