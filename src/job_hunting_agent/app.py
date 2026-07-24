"""应用服务层。

这个模块提供目前 MVP 的公共入口。CLI、测试或以后接入 Web/API 时，
都应该优先调用 `JobHuntingApp`，而不是直接操作存储、解析器和匹配器。
这样可以让外部接口保持简单，内部实现以后逐步替换成 LLM/向量库也更稳。
"""

from __future__ import annotations

from pathlib import Path

from .llm import LLMClient
from .matcher import match_job
from .models import (
    CandidateProfile,
    CandidateProfileInput,
    ImportedJob,
    MatchResult,
    ProjectExperienceCard,
    ProjectExperienceRecord,
    ResumeDraftRecord,
)
from .project_analyzer import analyze_project
from .resume_writer import build_resume_draft
from .storage import SQLiteStore


class JobHuntingApp:
    """求职助手 MVP 的门面类。

    它把 SQLite 存储、职位解析、本地项目分析和匹配规则组合到一起。
    目前没有接入 LLM，所有能力都是本地规则和标准库实现。
    """

    def __init__(self, db_path: str | Path):
        """绑定一个 SQLite 数据库路径。"""

        self.store = SQLiteStore(db_path)

    def initialize(self) -> None:
        """创建 MVP 需要的数据表。"""

        self.store.initialize()

    def save_candidate_profile(self, profile: CandidateProfileInput) -> int:
        """保存候选人档案，返回候选人 ID。"""

        return self.store.save_candidate_profile(profile)

    def get_candidate_profile(self, candidate_id: int) -> CandidateProfile:
        """读取候选人档案。

        CLI 和测试需要通过应用服务读取档案，避免越过门面类直接访问 SQLite。
        """

        return self.store.get_candidate_profile(candidate_id)

    def import_job_text(self, raw_text: str, source_url: str | None = None) -> ImportedJob:
        """导入候选人主动带回的职位原文，并保存标准化结果。"""

        return self.store.save_job_text(raw_text, source_url)

    def list_jobs(self) -> list[ImportedJob]:
        """列出候选人已经主动导入的所有职位。"""

        return self.store.list_jobs()

    def analyze_project(self, project_path: str | Path) -> ProjectExperienceCard:
        """分析候选人提供的本地项目目录，返回待确认项目经历卡片。"""

        return analyze_project(project_path)

    def analyze_project_for_candidate(
        self,
        candidate_id: int,
        project_path: str | Path,
    ) -> ProjectExperienceRecord:
        """分析并保存某个候选人的项目经历卡片。

        这个方法会先确认候选人存在，再保存“待确认项目经历卡片”。它不会把
        自动发现的技术栈写入候选人档案，避免把线索误当成已确认事实。
        """

        self.store.get_candidate_profile(candidate_id)
        card = analyze_project(project_path)
        return self.store.save_project_card(candidate_id, card)

    def confirm_project_card(
        self,
        record_id: int,
        confirmed_summary: str | None = None,
    ) -> ProjectExperienceRecord:
        """确认一张项目经历卡片，并保存候选人确认摘要。"""

        return self.store.confirm_project_card(record_id, confirmed_summary)

    def list_project_cards(self, candidate_id: int) -> list[ProjectExperienceRecord]:
        """列出某个候选人的项目经历卡片。"""

        return self.store.list_project_cards(candidate_id)

    def match_job(self, candidate_id: int, job_id: int) -> MatchResult:
        """读取候选人和职位后，执行一次可解释职位匹配。"""

        candidate = self.store.get_candidate_profile(candidate_id)
        job = self.store.get_job(job_id)
        return match_job(candidate, job)

    def match_all_jobs(self, candidate_id: int) -> list[MatchResult]:
        """对当前本地职位池做批量匹配，并按推荐顺序返回结果。

        排序规则很朴素：未淘汰职位优先，同组内分数高的靠前。这个结果只是帮助
        候选人决定先看哪些岗位，不代表录用概率。
        """

        candidate = self.store.get_candidate_profile(candidate_id)
        matches = [match_job(candidate, job) for job in self.store.list_jobs()]
        return sorted(matches, key=lambda result: (result.eliminated, -result.score, result.job_id))

    def create_resume_draft(
        self,
        candidate_id: int,
        job_id: int,
        llm_client: LLMClient | None = None,
    ) -> ResumeDraftRecord:
        """为某个职位生成一版证据约束简历草稿。

        LLM 是可选表达工具；即使传入 LLM，最终草稿也会经过真实性检查。
        生成结果保存为职位定制草稿版本，不会覆盖候选人档案。
        """

        candidate = self.store.get_candidate_profile(candidate_id)
        job = self.store.get_job(job_id)
        confirmed_project_cards = [
            record
            for record in self.store.list_project_cards(candidate_id)
            if record.status == "已确认"
        ]
        draft = build_resume_draft(candidate, job, confirmed_project_cards, llm_client)
        return self.store.save_resume_draft(candidate_id, job_id, draft)

    def list_resume_drafts(
        self,
        candidate_id: int,
        job_id: int | None = None,
    ) -> list[ResumeDraftRecord]:
        """列出候选人的职位定制简历草稿版本。"""

        return self.store.list_resume_drafts(candidate_id, job_id)
