"""应用服务层。

这个模块提供目前 MVP 的公共入口。CLI、测试或以后接入 Web/API 时，
都应该优先调用 `JobHuntingApp`，而不是直接操作存储、解析器和匹配器。
这样可以让外部接口保持简单，内部实现以后逐步替换成 LLM/向量库也更稳。
"""

from __future__ import annotations

from pathlib import Path

from .config import DEFAULT_ENV_PATH
from .conversation_ingestion import decide_conversation_ingestion
from .llm import LLMClient
from .matcher import match_job
from .models import (
    CandidateProfile,
    CandidateProfileInput,
    ConversationIngestionResult,
    ImportedJob,
    MatchResult,
    ProjectExperienceCard,
    ProjectExperienceRecord,
    RAGIndexStats,
    RAGSearchResult,
    ResumeDraftRecord,
)
from .project_analyzer import analyze_project
from .rag import RAGKnowledgeBase
from .resume_writer import build_resume_draft
from .storage import SQLiteStore


class JobHuntingApp:
    """求职助手 MVP 的门面类。

    它把 SQLite 存储、职位解析、本地项目分析、匹配规则、LLM 草稿生成和
    RAG 检索组合到一起。外部入口保持简单，内部能力可以逐步替换升级。
    """

    def __init__(self, db_path: str | Path, env_path: str | Path = DEFAULT_ENV_PATH):
        """绑定一个 SQLite 数据库路径和项目 `.env` 路径。"""

        self.store = SQLiteStore(db_path)
        self.env_path = Path(env_path)

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

    def list_candidate_profiles(self) -> list[CandidateProfile]:
        """列出候选人档案，供 Web 页面侧边栏选择。"""

        return self.store.list_candidate_profiles()

    def ingest_conversation_message(
        self,
        candidate_id: int,
        message: str,
        llm_client: LLMClient | None = None,
        rag_persist_directory: str | Path | None = None,
        auto_rebuild_rag: bool = False,
    ) -> ConversationIngestionResult:
        """自动判断并保存一条候选人对话资料。

        这是“对话即入库”的应用层入口：LLM 或规则只负责产出保存决策，
        真正的 SQLite 结构化更新、长文本写入和 RAG 增量索引都在本地代码中完成。
        这样可以保留清晰边界：SQLite 是事实源，RAG 是证据索引，LLM 是判断/表达工具。
        """

        candidate = self.store.get_candidate_profile(candidate_id)
        decision = decide_conversation_ingestion(candidate, message, llm_client)

        # 结构化字段只通过受控 patch 合并到候选人档案，避免模型直接改库。
        saved_structured_fields = self.store.update_candidate_profile(
            candidate_id,
            decision.profile_updates,
        )
        # 长文本先进入 SQLite long_texts；Chroma 只从这里同步，便于追溯来源。
        saved_long_text_ids = [
            self.store.add_long_text(
                item.entity_type,
                item.entity_id,
                item.source_label,
                item.text,
            )
            for item in decision.long_texts
            if item.text.strip()
        ]

        rag_index_stats = None
        rag_update_mode = "none"
        if auto_rebuild_rag:
            # 参数名沿用早期版本；现在对话入库的自动 RAG 刷新采用增量追加，不做全量重建。
            rag_index_stats = self.index_rag_long_texts(
                saved_long_text_ids,
                rag_persist_directory or "data/chroma",
            )
            rag_update_mode = rag_index_stats.mode

        return ConversationIngestionResult(
            candidate_id=candidate_id,
            reply=decision.reply,
            saved_structured_fields=saved_structured_fields,
            saved_long_text_ids=saved_long_text_ids,
            rag_rebuilt=False,
            rag_index_stats=rag_index_stats,
            rag_update_mode=rag_update_mode,
        )

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
        rag_persist_directory: str | Path | None = None,
        rag_query: str | None = None,
    ) -> ResumeDraftRecord:
        """为某个职位生成一版证据约束简历草稿。

        LLM 是可选表达工具；即使传入 LLM，最终草稿也会经过真实性检查。
        RAG 是可选证据上下文；即使使用 RAG，也不会覆盖候选人档案。
        """

        candidate = self.store.get_candidate_profile(candidate_id)
        job = self.store.get_job(job_id)
        confirmed_project_cards = [
            record
            for record in self.store.list_project_cards(candidate_id)
            if record.status == "已确认"
        ]
        semantic_evidence = []
        if rag_persist_directory is not None:
            query = rag_query or f"{job.title}\n{job.description_text}"
            # 简历草稿只允许检索已登记的候选人/职位/已确认项目材料；历史草稿不作为事实证据。
            semantic_evidence = [
                format_rag_evidence(result)
                for result in self.search_rag(
                    query,
                    rag_persist_directory,
                    top_k=5,
                    entity_types=["candidate_profile", "job", "project_experience_card"],
                )
            ]
        draft = build_resume_draft(candidate, job, confirmed_project_cards, llm_client, semantic_evidence)
        return self.store.save_resume_draft(candidate_id, job_id, draft)

    def list_resume_drafts(
        self,
        candidate_id: int,
        job_id: int | None = None,
    ) -> list[ResumeDraftRecord]:
        """列出候选人的职位定制简历草稿版本。"""

        return self.store.list_resume_drafts(candidate_id, job_id)

    def rebuild_rag_index(self, persist_directory: str | Path = "data/chroma") -> RAGIndexStats:
        """把 SQLite `long_texts` 全量同步到本地 Chroma RAG 索引。"""

        knowledge_base = RAGKnowledgeBase(persist_directory, env_path=self.env_path)
        return knowledge_base.rebuild(self.store.list_long_texts())

    def index_rag_long_texts(
        self,
        long_text_ids: list[int],
        persist_directory: str | Path = "data/chroma",
    ) -> RAGIndexStats:
        """把指定长文本增量追加到本地 Chroma RAG 索引。

        SQLite 仍然是长文本材料登记处；这个方法只把指定 ID 的材料同步到 Chroma，
        适合对话式自动入库后的即时检索。
        """

        knowledge_base = RAGKnowledgeBase(persist_directory, env_path=self.env_path)
        return knowledge_base.index_long_texts(self.store.get_long_texts_by_ids(long_text_ids))

    def search_rag(
        self,
        query: str,
        persist_directory: str | Path = "data/chroma",
        top_k: int = 5,
        entity_types: list[str] | None = None,
    ) -> list[RAGSearchResult]:
        """从本地 Chroma RAG 索引检索带来源的证据片段。"""

        knowledge_base = RAGKnowledgeBase(persist_directory, env_path=self.env_path)
        return knowledge_base.search(query, top_k, entity_types)


def format_rag_evidence(result: RAGSearchResult) -> str:
    """把 RAG 检索结果格式化成简历草稿可引用的证据条目。"""

    return (
        f"RAG 检索证据[{result.entity_type}#{result.entity_id}/"
        f"{result.source_label}/chunk{result.chunk_index}]：{result.content}"
    )
