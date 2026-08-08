"""应用服务层。

这个模块提供目前 MVP 的公共入口。Web/API、测试或以后接入后台任务时，
都应该优先调用 `JobHuntingApp`，而不是直接操作存储、解析器和匹配器。
这样可以让外部接口保持简单，内部实现以后逐步替换成 LLM/向量库也更稳。
"""

from __future__ import annotations

from pathlib import Path

from langchain_core.embeddings import Embeddings

from .auth import AuthService
from .config import (
    DEFAULT_ENV_PATH,
    load_database_settings,
    load_semantic_matching_enabled,
    require_postgresql_database_url,
)
from .conversation_ingestion import decide_conversation_ingestion
from .llm import LLMClient
from .matcher import match_job, semantic_direction_score
from .models import (
    CandidateProfile,
    CandidateProfileInput,
    ChatMessageRecord,
    ConversationIngestionResult,
    ImportedJob,
    MatchResult,
    ProjectExperienceCard,
    ProjectExperienceRecord,
    RAGIndexStats,
    RAGSearchResult,
    ResumeArtifactRecord,
    ResumeDraftRecord,
    SkillRequirement,
    TailoredResumeResult,
)
from .model_gateway import ModelGateway
from .project_analyzer import analyze_project
from .pgvector_rag import PgVectorKnowledgeBase
from .rag import Reranker
from .resume_document import (
    ResumeFileStore,
    extract_resume_document,
    media_type_for_filename,
    sanitize_download_filename,
)
from .resume_exporter import export_tailored_resume_files
from .resume_writer import build_resume_draft
from .sqlalchemy_store import SQLAlchemyStore


class JobHuntingApp:
    """求职助手 MVP 的门面类。

    它把 PostgreSQL 存储、职位解析、本地项目分析、匹配规则、LLM 草稿生成和
    pgvector 检索组合到一起。所有入口都使用经 Alembic 管理的 SQLAlchemyStore。
    """

    def __init__(
        self,
        env_path: str | Path = DEFAULT_ENV_PATH,
        resume_dir: str | Path | None = None,
        semantic_matching: bool | None = None,
        database_url: str | None = None,
    ):
        """绑定数据库、项目 `.env` 和受控简历文件目录。"""

        self.env_path = Path(env_path)
        resolved_database_url = database_url or require_postgresql_database_url(
            load_database_settings(self.env_path)
        )
        self.store = SQLAlchemyStore(resolved_database_url)
        default_resume_dir = Path("data/resumes")
        # Web 与后台任务都通过同一认证服务创建账号和 Session，避免重复实现密码逻辑。
        self.auth = AuthService(self.store)
        # 语义方向匹配涉及外部 Embedding/Rerank 请求，默认按 `.env` 显式开关；
        # 测试和离线模式可通过构造参数强制关闭或打开。
        self.semantic_matching_enabled = (
            load_semantic_matching_enabled(self.env_path)
            if semantic_matching is None
            else bool(semantic_matching)
        )
        # 所有真实模型/Embedding 调用都通过内部 Gateway 构造和计量；它是惰性加载的，
        # 所以纯本地规则和离线测试不需要在创建 App 时提供 API Key。
        self.model_gateway = ModelGateway(self.env_path, usage_store=self.store)
        self.resume_files = ResumeFileStore(resume_dir or default_resume_dir)

    def initialize(self) -> None:
        """创建 MVP 需要的数据表。"""

        self.store.initialize()

    def save_candidate_profile(self, profile: CandidateProfileInput, account_id: int | None = None) -> int:
        """保存候选人档案，返回候选人 ID。"""

        return self.store.save_candidate_profile(profile, account_id=account_id)

    def get_candidate_profile(self, candidate_id: int, account_id: int | None = None) -> CandidateProfile:
        """读取候选人档案。

        Web API 和测试通过应用服务读取档案，避免越过门面类直接访问持久化实现。
        """

        return self.store.get_candidate_profile(candidate_id, account_id=account_id)

    def list_candidate_profiles(self, account_id: int | None = None) -> list[CandidateProfile]:
        """列出候选人档案，供 Web 页面侧边栏选择。"""

        return self.store.list_candidate_profiles(account_id=account_id)

    def delete_candidate_profile(
        self,
        candidate_id: int,
        account_id: int | None = None,
    ) -> dict[str, object]:
        """删除候选人档案及其从属数据，并尽量同步移除 RAG 证据。"""

        result = self.store.delete_candidate_profile(candidate_id, account_id=account_id)
        for storage_key in result.get("storage_keys", []):
            self.resume_files.delete(str(storage_key))
        return self._finish_deletion_cleanup(result)

    def delete_chat_session(self, session_id: str, account_id: int) -> dict[str, object]:
        """永久删除一段网页对话及其消息。"""

        return self.store.delete_chat_session(session_id, account_id)

    def ingest_conversation_message(
        self,
        candidate_id: int,
        message: str,
        llm_client: LLMClient | None = None,
        auto_rebuild_rag: bool = False,
        account_id: int | None = None,
    ) -> ConversationIngestionResult:
        """自动判断并保存一条候选人对话资料。

        这是“对话即入库”的应用层入口：LLM 或规则只负责产出保存决策，
        真正的结构化更新、长文本写入和 RAG 增量索引都在本地代码中完成。
        这样可以保留清晰边界：PostgreSQL 是事实源，RAG 是证据索引，LLM 是判断/表达工具。
        """

        candidate = self.store.get_candidate_profile(candidate_id, account_id=account_id)
        decision = decide_conversation_ingestion(candidate, message, llm_client)

        # 结构化字段只通过受控 patch 合并到候选人档案，避免模型直接改库。
        saved_structured_fields = self.store.update_candidate_profile(
            candidate_id,
            decision.profile_updates,
            account_id=account_id,
        )
        # 长文本先进入 PostgreSQL long_texts；当前 RAG 后端只从这里构建可追溯的派生索引。
        saved_long_text_ids = [
            self.store.add_long_text(
                item.entity_type,
                item.entity_id,
                item.source_label,
                item.text,
                account_id=account_id,
                candidate_id=candidate_id,
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
                account_id=account_id,
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

    def import_job_text(
        self,
        raw_text: str,
        source_url: str | None = None,
        account_id: int | None = None,
        classify_with_llm: bool = True,
    ) -> ImportedJob:
        """导入候选人主动带回的职位原文，并保存标准化结果。"""

        llm_client = None
        if classify_with_llm:
            try:
                # 职位技能分类是可选增强；没有 .env 或模型不可用时由规则分类兜底。
                call_context = self.model_gateway.new_call_context(
                    "job_skill_classification",
                    account_id=account_id,
                )
                llm_client = self.model_gateway.llm_client(call_context)
            except (ValueError, TypeError):
                llm_client = None
        return self.store.save_job_text(
            raw_text,
            source_url,
            account_id=account_id,
            llm_client=llm_client,
        )

    def list_jobs(self, account_id: int | None = None) -> list[ImportedJob]:
        """列出候选人已经主动导入的所有职位。"""

        return self.store.list_jobs(account_id=account_id)

    def update_job_skill_requirements(
        self,
        job_id: int,
        requirements: list[SkillRequirement],
        account_id: int | None = None,
    ) -> ImportedJob:
        """保存候选人对职位技能重要性分类的人工校正。"""

        return self.store.update_job_skill_requirements(job_id, requirements, account_id=account_id)

    def delete_job(
        self,
        job_id: int,
        account_id: int | None = None,
    ) -> dict[str, object]:
        """删除职位及其职位定制文件，并尽量同步移除 RAG 证据。"""

        result = self.store.delete_job(job_id, account_id=account_id)
        for storage_key in result.get("storage_keys", []):
            self.resume_files.delete(str(storage_key))
        return self._finish_deletion_cleanup(result)

    def _finish_deletion_cleanup(
        self,
        result: dict[str, object],
    ) -> dict[str, object]:
        """清理 RAG chunk；结构化数据删除完成时，向调用方返回可读警告。"""

        long_text_ids = [int(item_id) for item_id in result.get("long_text_ids", [])]
        result["rag_deleted_chunks"] = 0
        if not long_text_ids:
            return result
        # rag_chunks.long_text_id 使用 ON DELETE CASCADE；事实源删除成功后，PostgreSQL
        # 会在同一事务中删除派生向量，不需要再次调用 Embedding 或本地向量库。
        result["rag_cleanup"] = "database_cascade"
        return result

    def save_chat_message(
        self,
        candidate_id: int,
        session_id: str,
        role: str,
        content: str,
        metadata: dict[str, object] | None = None,
        account_id: int | None = None,
    ) -> ChatMessageRecord:
        """保存网页聊天消息，用于刷新页面后恢复对话。"""

        # 先读取候选人，避免前端给不存在的档案写聊天记录。
        self.store.get_candidate_profile(candidate_id, account_id=account_id)
        # PostgreSQL 以 chat_sessions.session_id 作为消息外键；直接调用应用层
        # 入口时也要先登记默认会话，不能依赖 Web 路由的额外初始化步骤。
        try:
            session = self.store.get_chat_session_by_key(session_id, account_id)
            if session.candidate_id != candidate_id:
                raise ValueError("该会话不属于当前候选人档案。")
            if session.status != "active":
                raise ValueError("该会话已经归档，请新建对话。")
        except KeyError:
            self.store.create_chat_session(
                session_id=session_id,
                account_id=account_id,
                candidate_id=candidate_id,
                title=content[:32] or "新对话",
            )
        return self.store.save_chat_message(candidate_id, session_id, role, content, metadata, account_id=account_id)

    def list_chat_messages(
        self,
        candidate_id: int,
        session_id: str,
        limit: int = 100,
        account_id: int | None = None,
    ) -> list[ChatMessageRecord]:
        """读取网页聊天历史，用于重新打开页面时恢复消息。"""

        self.store.get_candidate_profile(candidate_id, account_id=account_id)
        return self.store.list_chat_messages(candidate_id, session_id, limit, account_id=account_id)

    def analyze_project(self, project_path: str | Path) -> ProjectExperienceCard:
        """分析候选人提供的本地项目目录，返回待确认项目经历卡片。"""

        return analyze_project(project_path)

    def analyze_project_for_candidate(
        self,
        candidate_id: int,
        project_path: str | Path,
        account_id: int | None = None,
    ) -> ProjectExperienceRecord:
        """分析并保存某个候选人的项目经历卡片。

        这个方法会先确认候选人存在，再保存“待确认项目经历卡片”。它不会把
        自动发现的技术栈写入候选人档案，避免把线索误当成已确认事实。
        """

        self.store.get_candidate_profile(candidate_id, account_id=account_id)
        card = analyze_project(project_path)
        return self.store.save_project_card(candidate_id, card, account_id=account_id)

    def confirm_project_card(
        self,
        record_id: int,
        confirmed_summary: str | None = None,
        account_id: int | None = None,
    ) -> ProjectExperienceRecord:
        """确认一张项目经历卡片，并保存候选人确认摘要。"""

        return self.store.confirm_project_card(record_id, confirmed_summary, account_id=account_id)

    def list_project_cards(self, candidate_id: int, account_id: int | None = None) -> list[ProjectExperienceRecord]:
        """列出某个候选人的项目经历卡片。"""

        return self.store.list_project_cards(candidate_id, account_id=account_id)

    def match_job(self, candidate_id: int, job_id: int, account_id: int | None = None) -> MatchResult:
        """读取候选人和职位后，执行一次可解释职位匹配。"""

        candidate = self.store.get_candidate_profile(candidate_id, account_id=account_id)
        job = self.store.get_job(job_id, account_id=account_id)
        direction_scorer = self._build_direction_scorer(account_id=account_id)
        return match_job(candidate, job, direction_scorer=direction_scorer)

    def match_all_jobs(self, candidate_id: int, account_id: int | None = None) -> list[MatchResult]:
        """对当前本地职位池做批量匹配，并按推荐顺序返回结果。

        排序规则很朴素：未淘汰职位优先，同组内分数高的靠前。这个结果只是帮助
        候选人决定先看哪些岗位，不代表录用概率。
        """

        candidate = self.store.get_candidate_profile(candidate_id, account_id=account_id)
        direction_scorer = self._build_direction_scorer(account_id=account_id)
        matches = [
            match_job(candidate, job, direction_scorer=direction_scorer)
            for job in self.store.list_jobs(account_id=account_id)
        ]
        return sorted(matches, key=lambda result: (result.eliminated, -result.score, result.job_id))

    def _build_direction_scorer(self, account_id: int | None = None):
        """按需构造一次方向语义评分器，失败时让匹配器回退本地规则。"""

        if not self.semantic_matching_enabled:
            return None
        try:
            embedding_context = self.model_gateway.new_call_context(
                "matching_embedding",
                account_id=account_id,
            )
            rerank_context = self.model_gateway.new_call_context(
                "matching_rerank",
                account_id=account_id,
            )
            embeddings = self.model_gateway.embeddings(embedding_context)
            reranker = self.model_gateway.reranker(rerank_context)
        except Exception:  # noqa: BLE001 - 配置错误时由匹配器继续使用规则回退。
            return None

        def score(candidate: CandidateProfile, job: ImportedJob) -> float | None:
            """调用统一的 Embedding/Rerank 协议适配器。"""

            return semantic_direction_score(candidate, job, embeddings, reranker)

        return score

    def create_resume_draft(
        self,
        candidate_id: int,
        job_id: int,
        llm_client: LLMClient | None = None,
        rag_query: str | None = None,
        use_rag: bool = True,
        account_id: int | None = None,
    ) -> ResumeDraftRecord:
        """为某个职位生成一版证据约束简历草稿。

        LLM 是可选表达工具；即使传入 LLM，最终草稿也会经过真实性检查。
        RAG 是证据上下文；即使使用 RAG，也不会覆盖候选人档案。
        """

        candidate = self.store.get_candidate_profile(candidate_id, account_id=account_id)
        job = self.store.get_job(job_id, account_id=account_id)
        confirmed_project_cards = [
            record
            for record in self.store.list_project_cards(candidate_id, account_id=account_id)
            if record.status == "已确认"
        ]
        semantic_evidence: list[str] = []
        if use_rag:
            query = rag_query or f"{job.title}\n{job.description_text}"
            # 简历草稿只允许检索已登记的候选人/职位/已确认项目材料；历史草稿不作为事实证据。
            semantic_evidence = [
                format_rag_evidence(result)
                for result in self.search_rag(
                    query,
                    top_k=5,
                    entity_types=["candidate_profile", "job", "project_experience_card"],
                    account_id=account_id,
                )
            ]
        draft = build_resume_draft(candidate, job, confirmed_project_cards, llm_client, semantic_evidence)
        return self.store.save_resume_draft(candidate_id, job_id, draft, account_id=account_id)

    def list_resume_drafts(
        self,
        candidate_id: int,
        job_id: int | None = None,
        account_id: int | None = None,
    ) -> list[ResumeDraftRecord]:
        """列出候选人的职位定制简历草稿版本。"""

        return self.store.list_resume_drafts(candidate_id, job_id, account_id=account_id)

    def upload_resume_document(
        self,
        candidate_id: int,
        filename: str,
        content: bytes,
        account_id: int | None = None,
    ) -> ResumeArtifactRecord:
        """解析并保存候选人上传的原始 DOCX/PDF 简历。

        原文件不会被后续改写覆盖；提取正文登记为 `resume_artifact` 长文本，供调用方
        继续执行 RAG 增量索引。结构化档案不会从上传简历中自动覆盖。
        """

        self.store.get_candidate_profile(candidate_id, account_id=account_id)
        clean_filename = sanitize_download_filename(filename, fallback="resume")
        extraction = extract_resume_document(clean_filename, content)
        stored = self.resume_files.save(
            account_id=account_id,
            candidate_id=candidate_id,
            filename=clean_filename,
            content=content,
        )
        try:
            return self.store.save_resume_artifact(
                account_id=account_id,
                candidate_id=candidate_id,
                artifact_type="source",
                original_filename=clean_filename,
                download_filename=clean_filename,
                storage_key=stored.storage_key,
                media_type=media_type_for_filename(clean_filename),
                file_size=stored.file_size,
                sha256=stored.sha256,
                extraction_method=extraction.method,
                extracted_text=extraction.text,
                page_count=extraction.page_count,
                register_long_text=True,
            )
        except Exception:
            # 数据库保存失败时删除刚写入的文件，保持两个存储边界一致。
            self.resume_files.delete(stored.storage_key)
            raise

    def get_resume_artifact(
        self,
        artifact_id: int,
        account_id: int | None = None,
    ) -> ResumeArtifactRecord:
        """读取一份简历文件元数据，并在 Web 场景执行账号过滤。"""

        return self.store.get_resume_artifact(artifact_id, account_id=account_id)

    def list_resume_artifacts(
        self,
        candidate_id: int,
        account_id: int | None = None,
    ) -> list[ResumeArtifactRecord]:
        """列出候选人的原始和职位定制简历文件版本。"""

        # 先读候选人，确保跨账号请求不会仅仅返回一个空列表而掩盖越权访问。
        self.store.get_candidate_profile(candidate_id, account_id=account_id)
        return self.store.list_resume_artifacts(candidate_id, account_id=account_id)

    def delete_resume_artifact(
        self,
        artifact_id: int,
        account_id: int | None = None,
    ) -> dict[str, object]:
        """删除单个原始或职位定制简历，并同步清理受控文件和 RAG 证据。"""

        result = self.store.delete_resume_artifact(artifact_id, account_id=account_id)
        for storage_key in result.get("storage_keys", []):
            self.resume_files.delete(str(storage_key))
        return self._finish_deletion_cleanup(result)

    def resume_file_path(self, artifact: ResumeArtifactRecord) -> Path:
        """把受控存储键解析为文件路径，不接受数据库之外的任意路径。"""

        return self.resume_files.path_for(artifact.storage_key)

    def create_tailored_resume_from_artifact(
        self,
        *,
        candidate_id: int,
        source_artifact_id: int,
        job_id: int,
        llm_client: LLMClient | None = None,
        rag_query: str | None = None,
        use_rag: bool = True,
        allow_proficiency_upgrade: bool = False,
        account_id: int | None = None,
    ) -> TailoredResumeResult:
        """基于上传简历和职位生成独立草稿、DOCX 与 PDF 文件版本。"""

        candidate = self.store.get_candidate_profile(candidate_id, account_id=account_id)
        job = self.store.get_job(job_id, account_id=account_id)
        source = self.store.get_resume_artifact(source_artifact_id, account_id=account_id)
        if source.candidate_id != candidate_id or source.artifact_type != "source":
            raise ValueError("只能使用当前候选人的原始上传简历生成职位定制版本。")
        source_text = self.store.get_resume_artifact_text(source.id, account_id=account_id)
        confirmed_project_cards = [
            record
            for record in self.store.list_project_cards(candidate_id, account_id=account_id)
            if record.status == "已确认"
        ]
        semantic_evidence: list[str] = []
        if use_rag:
            query = rag_query or f"{job.title}\n{job.description_text}\n{source_text[:2_000]}"
            semantic_evidence = [
                format_rag_evidence(result)
                for result in self.search_rag(
                    query,
                    top_k=6,
                    entity_types=[
                        "candidate_profile",
                        "job",
                        "project_experience_card",
                        "resume_artifact",
                    ],
                    account_id=account_id,
                )
            ]

        draft = build_resume_draft(
            candidate,
            job,
            confirmed_project_cards,
            llm_client,
            semantic_evidence,
            source_resume_text=source_text,
            allow_proficiency_upgrade=allow_proficiency_upgrade,
        )
        draft_record = self.store.save_resume_draft(
            candidate_id,
            job_id,
            draft,
            account_id=account_id,
        )
        generated_files = export_tailored_resume_files(
            candidate_name=candidate.name,
            job_title=job.title,
            draft_version=draft_record.version,
            content=draft.content,
        )

        saved_files = []
        saved_records: list[ResumeArtifactRecord] = []
        try:
            for generated in generated_files:
                stored = self.resume_files.save(
                    account_id=account_id,
                    candidate_id=candidate_id,
                    filename=generated.filename,
                    content=generated.content,
                )
                saved_files.append(stored)
                saved_records.append(
                    self.store.save_resume_artifact(
                        account_id=account_id,
                        candidate_id=candidate_id,
                        job_id=job_id,
                        draft_id=draft_record.id,
                        parent_artifact_id=source.id,
                        version=draft_record.version,
                        artifact_type="tailored",
                        original_filename=source.original_filename,
                        download_filename=generated.filename,
                        storage_key=stored.storage_key,
                        media_type=generated.media_type,
                        file_size=stored.file_size,
                        sha256=stored.sha256,
                        extraction_method="generated",
                        extracted_text=draft.content,
                        page_count=None,
                        register_long_text=False,
                    )
                )
        except Exception:
            # 两种导出格式视为一个业务结果；任一失败时补偿删除本批元数据和二进制。
            self.store.delete_resume_artifacts(
                [record.id for record in saved_records],
                account_id=account_id,
            )
            for stored in saved_files:
                self.resume_files.delete(stored.storage_key)
            raise
        return TailoredResumeResult(draft=draft_record, artifacts=saved_records)

    def rebuild_rag_index(
        self,
        account_id: int | None = None,
    ) -> RAGIndexStats:
        """把长文本事实源全量同步到当前存储后端对应的 RAG 派生索引。"""

        call_context = self.model_gateway.new_call_context(
            "embedding_rebuild",
            account_id=account_id,
        )
        knowledge_base = self._rag_knowledge_base(
            embeddings=self.model_gateway.embeddings(call_context),
        )
        return knowledge_base.rebuild(self.store.list_long_texts(account_id=account_id), account_id=account_id)

    def index_rag_long_texts(
        self,
        long_text_ids: list[int],
        account_id: int | None = None,
    ) -> RAGIndexStats:
        """把指定长文本增量追加到当前 RAG 派生索引。

        PostgreSQL 是长文本材料登记处；这个方法只同步指定 ID，适合对话式自动
        入库后的即时检索，直接写入 PostgreSQL 的 pgvector 派生索引。
        """

        call_context = self.model_gateway.new_call_context(
            "embedding_index",
            account_id=account_id,
        )
        knowledge_base = self._rag_knowledge_base(
            embeddings=self.model_gateway.embeddings(call_context),
        )
        return knowledge_base.index_long_texts(self.store.get_long_texts_by_ids(long_text_ids, account_id=account_id), account_id=account_id)

    def search_rag(
        self,
        query: str,
        top_k: int = 5,
        entity_types: list[str] | None = None,
        account_id: int | None = None,
    ) -> list[RAGSearchResult]:
        """从当前 RAG 后端检索带来源、账号隔离的证据片段。"""

        call_context = self.model_gateway.new_call_context(
            "embedding_query",
            account_id=account_id,
        )
        rerank_context = self.model_gateway.new_call_context(
            "rerank_query",
            account_id=account_id,
        )
        knowledge_base = self._rag_knowledge_base(
            embeddings=self.model_gateway.embeddings(call_context),
            reranker=self.model_gateway.reranker(rerank_context),
        )
        return knowledge_base.search(query, top_k, entity_types, account_id=account_id)

    def _uses_pgvector_rag(self) -> bool:
        """返回当前仓储是否使用 PostgreSQL 方言。"""

        return self.store.engine.dialect.name == "postgresql"

    def _rag_knowledge_base(
        self,
        *,
        embeddings: Embeddings,
        reranker: Reranker | None = None,
    ) -> PgVectorKnowledgeBase:
        """创建唯一的 PostgreSQL + pgvector 知识库实例。"""

        return PgVectorKnowledgeBase(self.store.engine, embeddings=embeddings, reranker=reranker)


def format_rag_evidence(result: RAGSearchResult) -> str:
    """把 RAG 检索结果格式化成简历草稿可引用的证据条目。"""

    return (
        f"RAG 检索证据[{result.entity_type}#{result.entity_id}/"
        f"{result.source_label}/chunk{result.chunk_index}]：{result.content}"
    )
