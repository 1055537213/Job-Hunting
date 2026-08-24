"""应用服务层。

这个模块提供目前 MVP 的公共入口。Web/API、测试或以后接入后台任务时，
都应该优先调用 `JobHuntingApp`，而不是直接操作存储、解析器和匹配器。
这样可以让外部接口保持简单，内部实现以后逐步替换成 LLM/向量库也更稳。
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterator
from pathlib import Path

from langchain_core.embeddings import Embeddings

from .auth import AuthService
from .concurrency_control import (
    ConcurrencyController,
    build_concurrency_controller,
)
from .config import (
    DEFAULT_ENV_PATH,
    load_billing_settings,
    load_concurrency_settings,
    load_database_settings,
    load_object_storage_settings,
    load_semantic_matching_enabled,
    load_task_queue_settings,
    require_postgresql_database_url,
)
from .conversation_ingestion import decide_conversation_ingestion
from .deduplication import (
    DuplicateResourceError,
    github_project_content_fingerprint,
)
from .github_project import (
    analyze_public_github_repository,
    normalize_public_github_repository_url,
)
from .job_screenshot import JobScreenshot, JobScreenshotExtractor
from .llm import LLMClient
from .matcher import match_job, semantic_direction_score
from .model_gateway import ModelGateway
from .models import (
    AdminAuditEventRecord,
    BackgroundTaskRecord,
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
from .object_storage import ObjectNotFoundError, ObjectStorage, S3ObjectStorage
from .pgvector_rag import PgVectorKnowledgeBase
from .project_analyzer import analyze_project
from .rag import Reranker
from .resume_document import (
    PDF_EXTENSION,
    ResumeExtraction,
    ResumeFileStore,
    extract_resume_document,
    inspect_pdf_for_ocr,
    media_type_for_filename,
    sanitize_download_filename,
    supported_resume_extension,
)
from .resume_exporter import export_tailored_resume_files
from .resume_writer import build_resume_draft
from .sqlalchemy_store import SQLAlchemyStore
from .task_queue import (
    GITHUB_PROJECT_ANALYSIS_TASK_TYPE,
    RAG_INDEX_TASK_TYPE,
    RESUME_EXPORT_TASK_TYPE,
    RESUME_OCR_TASK_TYPE,
    BackgroundTaskQueue,
    CeleryTaskQueue,
    TaskQueueError,
)


class JobHuntingApp:
    """求职助手 MVP 的门面类。

    它把 PostgreSQL 存储、职位解析、项目证据分析、匹配规则、LLM 草稿生成和
    pgvector 检索组合到一起。所有入口都使用经 Alembic 管理的 SQLAlchemyStore。
    """

    def __init__(
        self,
        env_path: str | Path = DEFAULT_ENV_PATH,
        resume_dir: str | Path | None = None,
        semantic_matching: bool | None = None,
        database_url: str | None = None,
        object_storage: ObjectStorage | None = None,
        task_queue: BackgroundTaskQueue | None = None,
        concurrency_controller: ConcurrencyController | None = None,
    ):
        """绑定数据库、项目 `.env`、对象存储和可选后台任务队列。"""

        self.env_path = Path(env_path)
        resolved_database_url = database_url or require_postgresql_database_url(
            load_database_settings(self.env_path)
        )
        self.store = SQLAlchemyStore(resolved_database_url)
        self.store.configure_billing(load_billing_settings(self.env_path))
        # 直接运行 Web 或单元测试默认关闭队列；Compose 会显式开启并注入 Redis URL。
        self.task_queue_settings = load_task_queue_settings(self.env_path)
        self.task_queue = task_queue
        if self.task_queue is None and self.task_queue_settings.enabled:
            self.task_queue = CeleryTaskQueue(self.task_queue_settings)
        # Web 与后台任务都通过同一认证服务创建账号和 Session，避免重复实现密码逻辑。
        self.auth = AuthService(self.store)
        # 语义方向匹配涉及外部 Embedding/Rerank 请求，默认按 `.env` 显式开关；
        # 测试和离线模式可通过构造参数强制关闭或打开。
        self.semantic_matching_enabled = (
            load_semantic_matching_enabled(self.env_path)
            if semantic_matching is None
            else bool(semantic_matching)
        )
        self.concurrency_settings = load_concurrency_settings(self.env_path)
        self.concurrency_controller = (
            concurrency_controller
            if concurrency_controller is not None
            else build_concurrency_controller(self.concurrency_settings)
        )
        # 所有真实模型/Embedding 调用都通过内部 Gateway 构造和计量；它是惰性加载的，
        # 所以纯本地规则和离线测试不需要在创建 App 时提供 API Key。
        self.model_gateway = ModelGateway(
            self.env_path,
            usage_store=self.store,
            concurrency_controller=self.concurrency_controller,
        )
        self.job_screenshot_extractor = JobScreenshotExtractor(self.model_gateway)
        if object_storage is not None:
            # 测试或宿主机集成可以注入一个实现，业务层不关心具体厂商。
            self.resume_files = object_storage
        elif resume_dir is not None:
            # 保留显式临时目录入口，便于解析器单元测试，不作为 Docker Web 默认路径。
            self.resume_files = ResumeFileStore(resume_dir)
        else:
            storage_settings = load_object_storage_settings(self.env_path)
            if storage_settings.backend == "s3":
                assert storage_settings.endpoint_url is not None
                assert storage_settings.bucket is not None
                assert storage_settings.access_key is not None
                assert storage_settings.secret_key is not None
                self.resume_files = S3ObjectStorage(
                    endpoint_url=storage_settings.endpoint_url,
                    bucket=storage_settings.bucket,
                    access_key=storage_settings.access_key,
                    secret_key=storage_settings.secret_key,
                    region=storage_settings.region,
                    force_path_style=storage_settings.force_path_style,
                    auto_create_bucket=storage_settings.auto_create_bucket,
                )
            else:
                # local 只能通过配置显式启用，通常由测试 fixture 或临时本地集成使用。
                self.resume_files = ResumeFileStore(Path("data/resumes"))
        self.file_storage_backend = (
            "s3" if isinstance(self.resume_files, S3ObjectStorage) else "local"
        )

    def initialize(self) -> None:
        """确认数据库 schema 和配置的对象存储都可供 Web 请求使用。"""

        self.store.initialize()
        if isinstance(self.resume_files, S3ObjectStorage):
            # Web 进程在接收上传前验证 bucket，避免用户选择文件后才暴露基础设施错误。
            self.resume_files.health_check()
        if self.task_queue is not None:
            # 队列启用时启动检查直接失败，避免用户上传后才发现任务无人消费。
            self.task_queue.health_check()

    @property
    def task_queue_enabled(self) -> bool:
        """返回当前实例是否具备可投递后台任务的队列适配器。"""

        return self.task_queue is not None

    def enqueue_background_task(
        self,
        *,
        account_id: int,
        task_type: str,
        payload: dict[str, object] | None = None,
        candidate_id: int | None = None,
        session_id: str | None = None,
        idempotency_key: str | None = None,
        max_attempts: int = 3,
        audit_event: AdminAuditEventRecord | None = None,
    ) -> BackgroundTaskRecord:
        """先写 PostgreSQL 任务记录，再投递 task_key；失败时留下可审计状态。"""

        if self.task_queue is None:
            raise TaskQueueError("当前运行环境未启用后台任务队列。")
        if idempotency_key:
            existing = self.store.get_background_task_by_idempotency(
                account_id,
                idempotency_key,
            )
            if existing is not None:
                if existing.status != "failed":
                    # queued/running 已经投递，成功或取消任务也属于已完成的幂等请求。
                    return existing
                # 上一次可能只是在 Redis 投递阶段失败。恢复原任务键后重新发送，前端原有
                # 轮询地址和审计链路都保持不变。
                record = self.store.retry_failed_background_task(existing.task_key)
            else:
                record = self.store.create_background_task(
                    account_id=account_id,
                    task_type=task_type,
                    payload=payload,
                    candidate_id=candidate_id,
                    session_id=session_id,
                    idempotency_key=idempotency_key,
                    max_attempts=max_attempts,
                    audit_event=audit_event,
                )
        else:
            record = self.store.create_background_task(
                account_id=account_id,
                task_type=task_type,
                payload=payload,
                candidate_id=candidate_id,
                session_id=session_id,
                idempotency_key=idempotency_key,
                max_attempts=max_attempts,
                audit_event=audit_event,
            )
        # 幂等复用的已完成任务无需再次投递；queued 任务才需要向 Redis 发送消息。
        if record.status != "queued":
            return record
        try:
            self.task_queue.enqueue(record.task_key)
        except TaskQueueError as error:
            self.store.fail_queued_background_task(record.task_key, str(error))
            raise
        return self.store.get_background_task(record.task_key, account_id=account_id)

    def get_background_task(
        self,
        task_key: str,
        account_id: int | None = None,
    ) -> BackgroundTaskRecord:
        """读取后台任务状态，供 Web 轮询和管理员运维页面使用。"""

        return self.store.get_background_task(task_key, account_id=account_id)

    def enqueue_system_probe(
        self,
        account_id: int,
        *,
        audit_event: AdminAuditEventRecord | None = None,
    ) -> BackgroundTaskRecord:
        """登记一个不读取用户数据的 Worker 连通性探针，供管理员受控验证。"""

        return self.enqueue_background_task(
            account_id=account_id,
            task_type="system_probe",
            payload={"purpose": "admin_runtime_probe"},
            max_attempts=1,
            audit_event=audit_event,
        )

    def enqueue_rag_index_task(
        self,
        *,
        long_text_ids: list[int],
        account_id: int,
        candidate_id: int | None = None,
        session_id: str | None = None,
        root_request_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> BackgroundTaskRecord:
        """登记并投递一次长文本增量索引任务。

        队列消息只携带 ``task_key``；长文本 ID 和链路 ID 保存在 PostgreSQL
        任务 payload 中，Worker 会再次按账号过滤读取，避免把简历正文放进 Redis。
        """

        normalized_id_set: set[int] = set()
        for raw_id in long_text_ids:
            if isinstance(raw_id, bool):
                raise TypeError("RAG 增量索引任务包含无效长文本 ID。")
            if isinstance(raw_id, int):
                normalized_id = raw_id
            elif isinstance(raw_id, str) and raw_id.strip().isdigit():
                normalized_id = int(raw_id.strip())
            else:
                raise ValueError("RAG 增量索引任务包含无效长文本 ID。")
            if normalized_id <= 0:
                raise ValueError("RAG 增量索引任务包含无效长文本 ID。")
            normalized_id_set.add(normalized_id)
        normalized_ids = sorted(normalized_id_set)
        if not normalized_ids:
            raise ValueError("RAG 增量索引任务至少需要一个长文本 ID。")
        payload: dict[str, object] = {"long_text_ids": normalized_ids}
        if root_request_id:
            payload["root_request_id"] = str(root_request_id)
        return self.enqueue_background_task(
            account_id=account_id,
            task_type=RAG_INDEX_TASK_TYPE,
            payload=payload,
            candidate_id=candidate_id,
            session_id=session_id,
            idempotency_key=idempotency_key,
        )

    def enqueue_resume_ocr_task(
        self,
        *,
        artifact_id: int,
        account_id: int,
        candidate_id: int,
        session_id: str | None = None,
        root_request_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> BackgroundTaskRecord:
        """登记扫描 PDF 的 OCR 任务，确保任务只能读取当前账号的待处理原件。"""

        artifact = self.store.get_resume_artifact(artifact_id, account_id=account_id)
        if artifact.candidate_id != candidate_id:
            raise ValueError("OCR 任务中的简历不属于当前候选人。")
        if artifact.artifact_type != "source" or artifact.status != "processing":
            raise ValueError("只有待处理的原始简历可以创建 OCR 任务。")
        if artifact.media_type != "application/pdf":
            raise ValueError("OCR 任务只支持 PDF 简历。")
        payload: dict[str, object] = {"artifact_id": artifact.id}
        if root_request_id:
            payload["root_request_id"] = str(root_request_id)
        return self.enqueue_background_task(
            account_id=account_id,
            task_type=RESUME_OCR_TASK_TYPE,
            payload=payload,
            candidate_id=candidate_id,
            session_id=session_id,
            idempotency_key=idempotency_key,
        )

    def enqueue_github_project_analysis_task(
        self,
        *,
        repository_url: str,
        account_id: int,
        candidate_id: int,
        session_id: str | None = None,
        root_request_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> BackgroundTaskRecord:
        """登记公开 GitHub 仓库分析任务，不把仓库正文写入 Redis。

        URL 会在 Web/Agent 投递前再次规范化；Worker 只从 PostgreSQL 读取这个
        已验证的仓库首页地址，再通过固定 GitHub 官方端点下载受限归档。
        """

        self.store.get_candidate_profile(candidate_id, account_id=account_id)
        reference = normalize_public_github_repository_url(repository_url)
        fingerprint = github_project_content_fingerprint(reference.canonical_url)
        if self.store.find_project_card_by_content_fingerprint(account_id, candidate_id, fingerprint) is not None:
            raise DuplicateResourceError("GitHub 项目")
        payload: dict[str, object] = {"repository_url": reference.canonical_url}
        if root_request_id:
            payload["root_request_id"] = str(root_request_id)
        if idempotency_key is None:
            # 项目经历属于候选人而非整个共享账号：同一候选人不能重复排队，但账号
            # 中另一个人的档案可以独立分析相同的公开仓库。
            idempotency_key = f"github-project:{candidate_id}:{fingerprint[:40]}"
        existing = self.store.get_background_task_by_idempotency(account_id, idempotency_key)
        if existing is not None:
            if existing.status in {"succeeded", "cancelled"}:
                # 项目卡片是成功任务的事实去重依据；旧版本曾把已完成任务的幂等键
                # 永久保留，导致项目删除后仍无法重新导入同一仓库。只释放键，保留任务审计。
                self.store.release_background_task_idempotency(existing.task_key)
            elif existing.status != "failed":
                raise DuplicateResourceError("GitHub 项目")
        return self.enqueue_background_task(
            account_id=account_id,
            task_type=GITHUB_PROJECT_ANALYSIS_TASK_TYPE,
            payload=payload,
            candidate_id=candidate_id,
            session_id=session_id,
            idempotency_key=idempotency_key,
        )

    def enqueue_resume_export_task(
        self,
        *,
        source_artifact_id: int,
        job_id: int,
        account_id: int,
        candidate_id: int,
        use_rag: bool = True,
        session_id: str | None = None,
        root_request_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> BackgroundTaskRecord:
        """登记职位定制简历任务，只把受控资源 ID 放入 PostgreSQL payload。"""

        source = self.store.get_resume_artifact(source_artifact_id, account_id=account_id)
        if source.candidate_id != candidate_id or source.artifact_type != "source":
            raise ValueError("只能使用当前候选人的原始上传简历生成职位定制版本。")
        if source.status != "ready":
            raise ValueError("原始简历仍在 OCR 解析或解析失败，暂时不能生成定制版本。")
        self.store.get_job(job_id, account_id=account_id)
        if idempotency_key is None:
            idempotency_key = (
                f"resume-export:{candidate_id}:{source_artifact_id}:{job_id}:"
                f"{int(bool(use_rag))}"
            )
        payload: dict[str, object] = {
            "source_artifact_id": source_artifact_id,
            "job_id": job_id,
            "use_rag": bool(use_rag),
        }
        if root_request_id:
            payload["root_request_id"] = str(root_request_id)
        return self.enqueue_background_task(
            account_id=account_id,
            task_type=RESUME_EXPORT_TASK_TYPE,
            payload=payload,
            candidate_id=candidate_id,
            session_id=session_id,
            idempotency_key=idempotency_key,
        )

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
        classify_with_llm: bool = False,
        *,
        import_method: str = "text",
    ) -> ImportedJob:
        """导入候选人主动带回的职位原文，并保存标准化结果。

        默认使用本地规则解析和分类，避免普通网页导入、脚本调用或离线测试在没有
        明确请求时同步等待外部模型。LangChain Agent 在其工具调用中会显式传入是否
        允许模型分类；用户仍可在网页职位列表中人工修正技能重要性。
        """

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
            import_method=import_method,
        )

    def import_job_screenshots(
        self,
        screenshots: list[JobScreenshot],
        source_url: str | None = None,
        *,
        account_id: int | None = None,
    ) -> ImportedJob:
        """从用户主动上传的职位截图提取原文后，复用既有审核和去重流程。

        截图识别已经是一次同步模型调用，因此此处保留规则技能分类，避免同一导入操作
        再等待一次文本模型调用。用户仍可在职位列表中人工调整技能重要性。
        """

        raw_text = self.job_screenshot_extractor.extract(screenshots, account_id=account_id)
        return self.import_job_text(
            raw_text,
            source_url,
            account_id=account_id,
            classify_with_llm=False,
            import_method="screenshot",
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
        """分析候选人提供的本地项目目录，供未来客户端使用。"""

        return analyze_project(project_path)

    def analyze_github_project(self, repository_url: str) -> ProjectExperienceCard:
        """分析用户主动提供的公开 GitHub 仓库，返回待确认项目经历卡片。"""

        return analyze_public_github_repository(repository_url)

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

    def analyze_github_project_for_candidate(
        self,
        candidate_id: int,
        repository_url: str,
        account_id: int | None = None,
    ) -> ProjectExperienceRecord:
        """分析公开 GitHub 仓库并保存为待确认项目经历卡片。"""

        self.store.get_candidate_profile(candidate_id, account_id=account_id)
        reference = normalize_public_github_repository_url(repository_url)
        fingerprint = github_project_content_fingerprint(reference.canonical_url)
        if self.store.find_project_card_by_content_fingerprint(account_id, candidate_id, fingerprint) is not None:
            raise DuplicateResourceError("GitHub 项目")
        card = analyze_public_github_repository(reference.canonical_url)
        return self.store.save_project_card(candidate_id, card, account_id=account_id)

    def confirm_project_card(
        self,
        record_id: int,
        confirmed_summary: str | None = None,
        account_id: int | None = None,
    ) -> ProjectExperienceRecord:
        """确认一张项目经历卡片，并保存候选人确认摘要。"""

        return self.store.confirm_project_card(record_id, confirmed_summary, account_id=account_id)

    def delete_project_card(
        self,
        record_id: int,
        account_id: int | None = None,
    ) -> dict[str, object]:
        """删除一张项目经历卡片及其确认摘要，并同步回收 RAG 证据。"""

        result = self.store.delete_project_card(record_id, account_id=account_id)
        return self._finish_deletion_cleanup(result)

    def confirm_project_card_and_enqueue_rag(
        self,
        record_id: int,
        confirmed_summary: str | None = None,
        *,
        account_id: int,
        session_id: str | None = None,
        root_request_id: str | None = None,
    ) -> tuple[ProjectExperienceRecord, BackgroundTaskRecord | None]:
        """确认项目证据，并在启用 Worker 时登记对应的增量 RAG 任务。

        项目卡片仍由候选人明确确认后才进入 ``long_texts``。RAG 只是该事实源的派生
        索引；确定性幂等键保证重复点击确认不会重复调用 Embedding。
        """

        record = self.store.confirm_project_card(
            record_id,
            confirmed_summary,
            account_id=account_id,
        )
        long_text = self.store.get_long_text_for_entity(
            "project_experience_card",
            record.id,
            source_label="confirmed",
            account_id=account_id,
        )
        if long_text is None:
            raise RuntimeError("项目经历确认后没有登记可索引的长文本。")
        if self.task_queue is None:
            return record, None
        task = self.enqueue_rag_index_task(
            long_text_ids=[long_text.id],
            account_id=account_id,
            candidate_id=record.candidate_id,
            session_id=session_id,
            root_request_id=root_request_id,
            idempotency_key=f"project-rag:{record.id}",
        )
        return record, task

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
        defer_ocr: bool = False,
    ) -> ResumeArtifactRecord:
        """解析并保存候选人上传的原始 DOCX/PDF 简历。

        原文件不会被后续改写覆盖；提取正文登记为 `resume_artifact` 长文本，供调用方
        继续执行 RAG 增量索引。若 ``defer_ocr`` 为真，扫描 PDF 只完成文本层检查和
        原件保存，随后由 Worker 写入 OCR 正文；结构化档案始终不会被上传简历自动覆盖。
        """

        self.store.get_candidate_profile(candidate_id, account_id=account_id)
        clean_filename = sanitize_download_filename(filename, fallback="resume")
        extension = supported_resume_extension(clean_filename)
        # 在解析文档和写入对象存储前检查字节内容，重复上传不浪费 OCR 或磁盘空间。
        content_sha256 = hashlib.sha256(content).hexdigest()
        if self.store.find_resume_source_by_content_fingerprint(account_id, candidate_id, content_sha256) is not None:
            raise DuplicateResourceError("简历")
        pending_ocr = False

        def extract_pdf_with_lease() -> ResumeExtraction:
            """在可能触发页面渲染/OCR 的 PDF 解析期间占用图片资源额度。"""

            lease = self.concurrency_controller.acquire(
                "screenshot",
                account_id=account_id,
            )
            try:
                return extract_resume_document(clean_filename, content)
            finally:
                lease.release()

        if defer_ocr and extension == PDF_EXTENSION:
            inspection = inspect_pdf_for_ocr(content)
            pending_ocr = bool(inspection.pages_needing_ocr)
            if pending_ocr:
                extraction_method = "pending_ocr"
                extracted_text = ""
                page_count = inspection.page_count
            else:
                extraction = extract_pdf_with_lease()
                extraction_method = extraction.method
                extracted_text = extraction.text
                page_count = extraction.page_count
        else:
            extraction = (
                extract_pdf_with_lease()
                if extension == PDF_EXTENSION
                else extract_resume_document(clean_filename, content)
            )
            extraction_method = extraction.method
            extracted_text = extraction.text
            page_count = extraction.page_count
        stored = self.resume_files.save(
            account_id=account_id,
            candidate_id=candidate_id,
            filename=clean_filename,
            content=content,
            media_type=media_type_for_filename(clean_filename),
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
                extraction_method=extraction_method,
                extracted_text=extracted_text,
                page_count=page_count,
                status="processing" if pending_ocr else "ready",
                register_long_text=not pending_ocr,
            )
        except Exception:
            # 数据库保存失败时删除刚写入的文件，保持两个存储边界一致。
            self.resume_files.delete(stored.storage_key)
            raise

    def process_resume_ocr_artifact(
        self,
        *,
        artifact_id: int,
        account_id: int,
        candidate_id: int,
    ) -> ResumeArtifactRecord:
        """由 Worker 读取待处理 PDF、执行 OCR，并原子登记正文和 RAG 来源。"""

        artifact = self.store.get_resume_artifact(artifact_id, account_id=account_id)
        if artifact.candidate_id != candidate_id:
            raise ValueError("OCR 任务中的简历不属于当前候选人。")
        if artifact.artifact_type != "source" or artifact.media_type != "application/pdf":
            raise ValueError("OCR 任务只能处理原始 PDF 简历。")
        # OCR 任务重投时不重复读取或重建长文本，直接复用已经完成的事实记录。
        if artifact.status == "ready" and artifact.long_text_id is not None:
            return artifact
        if artifact.status != "processing":
            raise ValueError("这份简历当前不处于 OCR 待处理状态。")
        # Worker 也必须遵守与 Web 截图导入相同的图片/OCR并发额度；否则扩容 Worker
        # 后扫描版 PDF 会绕过 Redis 共享保护，直接把 OCR 内存和 CPU 放大到副本数。
        lease = self.concurrency_controller.acquire(
            "screenshot",
            account_id=account_id,
        )
        try:
            extraction = extract_resume_document(
                artifact.original_filename,
                self.read_resume_file(artifact),
            )
        finally:
            lease.release()
        return self.store.complete_resume_artifact_extraction(
            artifact.id,
            extraction_method=extraction.method,
            extracted_text=extraction.text,
            page_count=extraction.page_count,
            account_id=account_id,
        )

    def fail_resume_ocr_artifact(
        self,
        *,
        artifact_id: int,
        account_id: int,
    ) -> ResumeArtifactRecord:
        """在 OCR 重试耗尽后标记原件失败，保留用户可下载和删除的文件。"""

        return self.store.fail_resume_artifact_extraction(
            artifact_id,
            account_id=account_id,
        )

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

        path_for = getattr(self.resume_files, "path_for", None)
        if not callable(path_for):
            raise NotImplementedError("当前对象存储不提供本地文件路径，请使用 read_resume_file。")
        return path_for(artifact.storage_key)

    def read_resume_file(self, artifact: ResumeArtifactRecord) -> bytes:
        """按数据库中的对象键读取简历正文，统一兼容本地和 S3 存储。"""

        try:
            return self.resume_files.read(artifact.storage_key)
        except ObjectNotFoundError as error:
            raise KeyError(artifact.id) from error

    def stream_resume_file(
        self,
        artifact: ResumeArtifactRecord,
        chunk_size: int = 64 * 1024,
    ) -> Iterator[bytes]:
        """按对象存储分块读取简历，供 Web 鉴权代理避免一次性加载大文件。"""

        try:
            return self.resume_files.stream(artifact.storage_key, chunk_size)
        except ObjectNotFoundError as error:
            raise KeyError(artifact.id) from error

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
        generation_key: str | None = None,
        session_id: str | None = None,
        root_request_id: str | None = None,
    ) -> TailoredResumeResult:
        """基于上传简历和职位生成独立草稿、DOCX 与 PDF 文件版本。"""

        candidate = self.store.get_candidate_profile(candidate_id, account_id=account_id)
        job = self.store.get_job(job_id, account_id=account_id)
        source = self.store.get_resume_artifact(source_artifact_id, account_id=account_id)
        if source.candidate_id != candidate_id or source.artifact_type != "source":
            raise ValueError("只能使用当前候选人的原始上传简历生成职位定制版本。")
        if source.status != "ready":
            raise ValueError("原始简历仍在 OCR 解析或解析失败，暂时不能生成定制版本。")

        existing_draft = (
            self.store.get_resume_draft_by_generation_key(
                generation_key,
                account_id=account_id,
            )
            if generation_key
            else None
        )
        existing_generated_files = {
            media_type: artifact
            for media_type in (
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                "application/pdf",
            )
            if generation_key
            for artifact in [
                self.store.get_resume_artifact_by_generation_key(
                    f"{generation_key}:{media_type.rsplit('/', 1)[-1].replace('.', '-')}",
                    account_id=account_id,
                )
            ]
            if artifact is not None
        }
        if existing_draft is not None and len(existing_generated_files) == 2:
            return TailoredResumeResult(
                draft=existing_draft,
                artifacts=list(existing_generated_files.values()),
            )

        source_text = (
            self.store.get_resume_artifact_text(source.id, account_id=account_id)
            if existing_draft is None
            else ""
        )
        semantic_evidence: list[str] = []
        if existing_draft is None and use_rag:
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
                    candidate_id=candidate_id,
                    session_id=session_id,
                    root_request_id=root_request_id,
                )
            ]

        if existing_draft is None:
            confirmed_project_cards = [
                record
                for record in self.store.list_project_cards(candidate_id, account_id=account_id)
                if record.status == "已确认"
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
                generation_key=generation_key,
            )
        else:
            # 任务在文件写入阶段中断后，重试复用已有草稿，不再次调用模型或扣费。
            draft_record = existing_draft
            draft = existing_draft.draft
        generated_files = export_tailored_resume_files(
            candidate_name=candidate.name,
            job_title=job.title,
            draft_version=draft_record.version,
            content=draft.content,
        )

        saved_files = []
        saved_records: list[ResumeArtifactRecord] = []
        created_records: list[ResumeArtifactRecord] = []
        try:
            for generated in generated_files:
                artifact_generation_key = (
                    f"{generation_key}:{generated.media_type.rsplit('/', 1)[-1].replace('.', '-')}"
                    if generation_key
                    else None
                )
                existing_artifact = (
                    self.store.get_resume_artifact_by_generation_key(
                        artifact_generation_key,
                        account_id=account_id,
                    )
                    if artifact_generation_key
                    else None
                )
                if existing_artifact is not None:
                    saved_records.append(existing_artifact)
                    continue
                stored = self.resume_files.save(
                    account_id=account_id,
                    candidate_id=candidate_id,
                    filename=generated.filename,
                    content=generated.content,
                    media_type=generated.media_type,
                )
                saved_files.append(stored)
                saved_record = self.store.save_resume_artifact(
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
                    generation_key=artifact_generation_key,
                )
                saved_records.append(saved_record)
                if artifact_generation_key and saved_record.storage_key != stored.storage_key:
                    # 并发重试中另一 Worker 先登记了同一个文件，清理本次孤立对象。
                    self.resume_files.delete(stored.storage_key)
                    saved_files.pop()
                else:
                    created_records.append(saved_record)
        except Exception:
            # 两种导出格式视为一个业务结果；任一失败时补偿删除本批元数据和二进制。
            self.store.delete_resume_artifacts(
                [record.id for record in created_records],
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
        candidate_id: int | None = None,
        session_id: str | None = None,
        root_request_id: str | None = None,
    ) -> RAGIndexStats:
        """把指定长文本增量追加到当前 RAG 派生索引。

        PostgreSQL 是长文本材料登记处；这个方法只同步指定 ID，适合对话式自动
        入库后的即时检索，直接写入 PostgreSQL 的 pgvector 派生索引。
        """

        call_context = self.model_gateway.new_call_context(
            "embedding_index",
            account_id=account_id,
            candidate_id=candidate_id,
            session_id=session_id,
            root_request_id=root_request_id,
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
        candidate_id: int | None = None,
        session_id: str | None = None,
        root_request_id: str | None = None,
    ) -> list[RAGSearchResult]:
        """从当前 RAG 后端检索带来源、账号隔离的证据片段。"""

        call_context = self.model_gateway.new_call_context(
            "embedding_query",
            account_id=account_id,
            candidate_id=candidate_id,
            session_id=session_id,
            root_request_id=root_request_id,
        )
        rerank_context = self.model_gateway.new_call_context(
            "rerank_query",
            account_id=account_id,
            candidate_id=candidate_id,
            session_id=session_id,
            root_request_id=root_request_id,
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
