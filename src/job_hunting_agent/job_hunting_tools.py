"""求职领域 Agent 工具的唯一注册位置。"""

from __future__ import annotations

from dataclasses import asdict
from functools import lru_cache
from typing import Any, cast

from pydantic import BaseModel, ConfigDict, Field

from .app import JobHuntingApp
from .deduplication import DuplicateResourceError
from .job_parser import InvalidJobTextError
from .models import BackgroundTaskRecord
from .tool_registry import (
    ToolContext,
    ToolErrorRule,
    ToolMetadata,
    ToolRegistry,
    ToolResult,
    ToolSpec,
)


class ToolArgs(BaseModel):
    """拒绝模型生成但工具未声明的额外参数。"""

    model_config = ConfigDict(extra="forbid", arbitrary_types_allowed=True)


class EmptyArgs(ToolArgs):
    """无显式参数的工具输入。"""


class IngestCandidateMessageArgs(ToolArgs):
    message: str = Field(min_length=1, description="候选人补充的资料或对话正文")
    auto_rag: bool | None = Field(default=None, description="是否同步更新 RAG 索引")


class SearchCandidateEvidenceArgs(ToolArgs):
    query: str = Field(min_length=1, description="要检索的候选人证据")
    top_n: int = Field(default=5, ge=1, le=10, description="重排后最多返回的证据数量")
    entity_types: list[str] | None = Field(default=None, description="可选的材料类型过滤")


class ImportJobFromTextArgs(ToolArgs):
    raw_text: str = Field(min_length=1, description="候选人主动提供的职位原文")
    source_url: str | None = Field(default=None, description="可选的职位来源链接")


class AnalyzeGitHubProjectArgs(ToolArgs):
    repository_url: str = Field(min_length=1, description="公开 GitHub 仓库首页链接")


class ConfirmProjectCardArgs(ToolArgs):
    record_id: int = Field(gt=0, description="待确认项目经历卡片 ID")
    confirmed_summary: str | None = Field(default=None, description="候选人确认后的项目摘要")


class CreateResumeDraftArgs(ToolArgs):
    job_id: int = Field(gt=0, description="目标职位 ID")
    use_rag: bool = Field(default=True, description="是否检索候选人证据")
    rag_query: str | None = Field(default=None, description="可选的证据检索词")


class CreateTailoredResumeArgs(ToolArgs):
    source_artifact_id: int = Field(gt=0, description="原始上传简历文件 ID")
    job_id: int = Field(gt=0, description="目标职位 ID")
    use_rag: bool = Field(default=True, description="是否检索候选人证据")
    rag_query: str | None = Field(default=None, description="可选的证据检索词")
    allow_proficiency_upgrade: bool = Field(
        default=False,
        description="候选人确认真实性风险后是否允许提高熟练度措辞",
    )


def build_job_hunting_tool_registry(app: JobHuntingApp) -> ToolRegistry:
    """构建 Agent、轻量路由和协议 adapter 共享的工具注册表。"""

    def ingest_candidate_message(arguments: BaseModel, context: ToolContext) -> ToolResult:
        args = cast(IngestCandidateMessageArgs, arguments)
        candidate_id = context.require_candidate_id()
        llm_client = (
            app.model_gateway.llm_client(
                app.model_gateway.new_call_context(
                    "tool_llm_ingestion",
                    account_id=context.account_id,
                    candidate_id=candidate_id,
                    session_id=context.session_id,
                    root_request_id=context.root_request_id,
                )
            )
            if context.use_tool_llm
            else None
        )
        result = app.ingest_conversation_message(
            candidate_id,
            args.message,
            llm_client=llm_client,
            auto_rebuild_rag=(
                context.default_auto_rag if args.auto_rag is None else args.auto_rag
            ),
            account_id=context.account_id,
        )
        return ToolResult.success(
            {
                "candidate_id": candidate_id,
                "reply": result.reply,
                "saved_structured_fields": result.saved_structured_fields,
                "saved_long_text_ids": result.saved_long_text_ids,
                "rag_update_mode": result.rag_update_mode,
            }
        )

    def get_current_candidate_profile(arguments: BaseModel, context: ToolContext) -> ToolResult:
        del arguments
        profile = app.get_candidate_profile(
            context.require_candidate_id(),
            account_id=context.account_id,
        )
        return ToolResult.success(asdict(profile))

    def list_candidate_profiles(arguments: BaseModel, context: ToolContext) -> ToolResult:
        del arguments
        profiles = app.list_candidate_profiles(account_id=context.account_id)
        return ToolResult.success({"profiles": [asdict(profile) for profile in profiles]})

    def search_candidate_evidence(arguments: BaseModel, context: ToolContext) -> ToolResult:
        args = cast(SearchCandidateEvidenceArgs, arguments)
        results = app.search_rag(
            args.query,
            top_n=args.top_n,
            entity_types=args.entity_types,
            account_id=context.account_id,
            candidate_id=context.require_candidate_id(),
            session_id=context.session_id,
            root_request_id=context.root_request_id,
        )
        return ToolResult.success(
            {"query": args.query, "results": [asdict(item) for item in results]}
        )

    def import_job_from_text(arguments: BaseModel, context: ToolContext) -> ToolResult:
        args = cast(ImportJobFromTextArgs, arguments)
        job = app.import_job_text(
            args.raw_text,
            args.source_url,
            account_id=context.account_id,
            classify_with_llm=context.use_tool_llm,
        )
        return ToolResult.success({"job": asdict(job)})

    def list_imported_jobs(arguments: BaseModel, context: ToolContext) -> ToolResult:
        del arguments
        jobs = app.list_jobs(account_id=context.account_id)
        return ToolResult.success({"jobs": [asdict(job) for job in jobs]})

    def match_all_jobs_for_candidate(arguments: BaseModel, context: ToolContext) -> ToolResult:
        del arguments
        candidate_id = context.require_candidate_id()
        jobs_by_id = {job.id: job for job in app.list_jobs(account_id=context.account_id)}
        matches = app.match_all_jobs(candidate_id, account_id=context.account_id)
        return ToolResult.success(
            {
                "candidate_id": candidate_id,
                "matches": [
                    {
                        "job": asdict(jobs_by_id[match.job_id]),
                        "match": asdict(match),
                    }
                    for match in matches
                ],
            }
        )

    def list_project_cards_for_candidate(arguments: BaseModel, context: ToolContext) -> ToolResult:
        del arguments
        cards = app.list_project_cards(
            context.require_candidate_id(),
            account_id=context.account_id,
        )
        return ToolResult.success({"project_cards": [asdict(card) for card in cards]})

    def analyze_github_project_for_candidate(
        arguments: BaseModel,
        context: ToolContext,
    ) -> ToolResult:
        args = cast(AnalyzeGitHubProjectArgs, arguments)
        candidate_id = context.require_candidate_id()
        if app.task_queue_enabled:
            account_id = context.require_account_id("GitHub 项目分析任务缺少账号归属。")
            task = app.enqueue_github_project_analysis_task(
                repository_url=args.repository_url,
                account_id=account_id,
                candidate_id=candidate_id,
                session_id=context.session_id,
                root_request_id=context.root_request_id,
            )
            return ToolResult.queued(
                {
                    "task": background_task_tool_payload(task),
                    "message": "GitHub 项目分析任务已排队，完成后会生成待确认项目经历卡片。",
                }
            )
        record = app.analyze_github_project_for_candidate(
            candidate_id,
            args.repository_url,
            account_id=context.account_id,
        )
        return ToolResult.success(asdict(record))

    def confirm_project_card(arguments: BaseModel, context: ToolContext) -> ToolResult:
        args = cast(ConfirmProjectCardArgs, arguments)
        candidate_id = context.require_candidate_id()
        allowed_record_ids = {
            record.id
            for record in app.list_project_cards(candidate_id, account_id=context.account_id)
        }
        if args.record_id not in allowed_record_ids:
            raise ValueError(f"项目卡片 {args.record_id} 不属于当前候选人 {candidate_id}。")
        account_id = context.require_account_id("确认项目经历缺少账号归属。")
        record, rag_task = app.confirm_project_card_and_enqueue_rag(
            args.record_id,
            args.confirmed_summary,
            account_id=account_id,
            session_id=context.session_id,
            root_request_id=context.root_request_id,
        )
        return ToolResult.success(
            {
                "project_card": asdict(record),
                "task": (
                    background_task_tool_payload(rag_task)
                    if rag_task is not None
                    else None
                ),
            }
        )

    def create_resume_draft_for_job(arguments: BaseModel, context: ToolContext) -> ToolResult:
        args = cast(CreateResumeDraftArgs, arguments)
        candidate_id = context.require_candidate_id()
        llm_client = (
            app.model_gateway.llm_client(
                app.model_gateway.new_call_context(
                    "resume_rewrite",
                    account_id=context.account_id,
                    candidate_id=candidate_id,
                    session_id=context.session_id,
                    root_request_id=context.root_request_id,
                )
            )
            if context.use_tool_llm
            else None
        )
        draft = app.create_resume_draft(
            candidate_id,
            args.job_id,
            llm_client=llm_client,
            rag_query=args.rag_query,
            use_rag=args.use_rag,
            account_id=context.account_id,
        )
        return ToolResult.success(asdict(draft))

    def list_resume_artifacts_for_candidate(
        arguments: BaseModel,
        context: ToolContext,
    ) -> ToolResult:
        del arguments
        artifacts = app.list_resume_artifacts(
            context.require_candidate_id(),
            account_id=context.account_id,
        )
        return ToolResult.success(
            {"artifacts": [resume_artifact_tool_payload(item) for item in artifacts]}
        )

    def create_tailored_resume_from_upload(
        arguments: BaseModel,
        context: ToolContext,
    ) -> ToolResult:
        args = cast(CreateTailoredResumeArgs, arguments)
        candidate_id = context.require_candidate_id()
        llm_client = (
            app.model_gateway.llm_client(
                app.model_gateway.new_call_context(
                    "resume_document_rewrite",
                    account_id=context.account_id,
                    candidate_id=candidate_id,
                    session_id=context.session_id,
                    root_request_id=context.root_request_id,
                )
            )
            if context.use_tool_llm
            else None
        )
        result = app.create_tailored_resume_from_artifact(
            candidate_id=candidate_id,
            source_artifact_id=args.source_artifact_id,
            job_id=args.job_id,
            llm_client=llm_client,
            rag_query=args.rag_query,
            use_rag=args.use_rag,
            allow_proficiency_upgrade=args.allow_proficiency_upgrade,
            account_id=context.account_id,
        )
        return ToolResult.success(
            {
                "draft": asdict(result.draft),
                "artifacts": [
                    resume_artifact_tool_payload(artifact)
                    for artifact in result.artifacts
                ],
            }
        )

    return ToolRegistry(
        [
            ToolSpec(
                name="ingest_candidate_message",
                description="当候选人补充资料、技能、项目经历或 HR 对话时保存资料并按需更新 RAG。",
                args_model=IngestCandidateMessageArgs,
                handler=ingest_candidate_message,
                audit_label="整理并保存候选人资料",
                requires_candidate=True,
                trace_priority=40,
            ),
            ToolSpec(
                name="get_current_candidate_profile",
                description="读取当前候选人的结构化档案。",
                args_model=EmptyArgs,
                handler=get_current_candidate_profile,
                audit_label="读取当前候选人档案",
                read_only=True,
                direct_route=True,
                requires_candidate=True,
                timeout_seconds=3,
                direct_reply=lambda data: (
                    f"当前候选人档案是“{data.get('name') or '未命名候选人'}”，已读取完成。"
                ),
            ),
            ToolSpec(
                name="list_candidate_profiles",
                description="列出当前账号内的候选人档案。",
                args_model=EmptyArgs,
                handler=list_candidate_profiles,
                audit_label="读取候选人档案列表",
                read_only=True,
                direct_route=True,
                timeout_seconds=3,
                direct_reply=lambda data: (
                    f"当前账号共有 {len(data.get('profiles', []))} 个候选人档案。"
                ),
            ),
            ToolSpec(
                name="search_candidate_evidence",
                description="从 RAG 索引检索当前候选人的证据片段，结果不能直接当成新事实。",
                args_model=SearchCandidateEvidenceArgs,
                handler=search_candidate_evidence,
                audit_label="检索相关项目经历",
                read_only=True,
                direct_route=True,
                requires_candidate=True,
                timeout_seconds=5,
                direct_reply=lambda data: (
                    f"已检索候选人证据，找到 {len(data.get('results', []))} 条相关材料。"
                ),
            ),
            ToolSpec(
                name="import_job_from_text",
                description="解析并保存候选人主动提供的职位原文。",
                args_model=ImportJobFromTextArgs,
                handler=import_job_from_text,
                audit_label="解析职位信息",
                trace_priority=50,
                error_rules=(
                    ToolErrorRule(InvalidJobTextError, "invalid_job_text"),
                    ToolErrorRule(DuplicateResourceError, "duplicate_resource"),
                ),
            ),
            ToolSpec(
                name="list_imported_jobs",
                description="列出当前账号已经导入的职位池。",
                args_model=EmptyArgs,
                handler=list_imported_jobs,
                audit_label="读取已导入职位",
                read_only=True,
                direct_route=True,
                timeout_seconds=3,
                direct_reply=lambda data: (
                    f"当前职位池共有 {len(data.get('jobs', []))} 个已导入职位。"
                ),
            ),
            ToolSpec(
                name="match_all_jobs_for_candidate",
                description="匹配当前候选人与职位池，并返回职位详情和可解释匹配结果。",
                args_model=EmptyArgs,
                handler=match_all_jobs_for_candidate,
                audit_label="计算职位匹配结果",
                read_only=True,
                direct_route=True,
                requires_candidate=True,
                timeout_seconds=5,
                trace_priority=60,
                direct_reply=lambda data: (
                    f"已完成职位匹配，共分析 {len(data.get('matches', []))} 个职位。"
                ),
            ),
            ToolSpec(
                name="list_project_cards_for_candidate",
                description="列出当前候选人的项目经历卡片并显示确认状态。",
                args_model=EmptyArgs,
                handler=list_project_cards_for_candidate,
                audit_label="读取项目经历卡片",
                read_only=True,
                direct_route=True,
                requires_candidate=True,
                timeout_seconds=3,
                direct_reply=lambda data: (
                    f"当前共有 {len(data.get('project_cards', []))} 张项目经历卡片。"
                ),
            ),
            ToolSpec(
                name="analyze_github_project_for_candidate",
                description="分析公开 GitHub 仓库并生成待确认项目经历卡片。",
                args_model=AnalyzeGitHubProjectArgs,
                handler=analyze_github_project_for_candidate,
                audit_label="分析项目经历",
                requires_candidate=True,
                execution_mode="background",
                trace_priority=80,
                error_rules=(ToolErrorRule(DuplicateResourceError, "duplicate_resource"),),
            ),
            ToolSpec(
                name="confirm_project_card",
                description="在候选人明确确认后保存项目经历摘要，并按需更新 RAG。",
                args_model=ConfirmProjectCardArgs,
                handler=confirm_project_card,
                audit_label="确认项目经历",
                requires_candidate=True,
                requires_confirmation=True,
                trace_priority=55,
            ),
            ToolSpec(
                name="create_resume_draft_for_job",
                description="为当前候选人和目标职位生成独立简历草稿，不覆盖候选人档案。",
                args_model=CreateResumeDraftArgs,
                handler=create_resume_draft_for_job,
                audit_label="生成职位定制简历草稿",
                requires_candidate=True,
                trace_priority=90,
            ),
            ToolSpec(
                name="list_resume_artifacts_for_candidate",
                description="列出当前候选人已上传和已生成的简历文件。",
                args_model=EmptyArgs,
                handler=list_resume_artifacts_for_candidate,
                audit_label="读取已上传简历",
                read_only=True,
                requires_candidate=True,
            ),
            ToolSpec(
                name="create_tailored_resume_from_upload",
                description="基于原始上传简历和目标职位生成独立 DOCX/PDF 定制简历。",
                args_model=CreateTailoredResumeArgs,
                handler=create_tailored_resume_from_upload,
                audit_label="生成职位定制简历",
                requires_candidate=True,
                requires_confirmation=True,
                execution_mode="background",
                trace_priority=100,
            ),
        ]
    )


def resume_artifact_tool_payload(artifact: Any) -> dict[str, Any]:
    """转换简历文件记录，同时隐藏服务器存储路径和账号 ID。"""

    payload = asdict(artifact)
    payload.pop("storage_key", None)
    payload.pop("account_id", None)
    payload["download_url"] = f"/api/resumes/{artifact.id}/download"
    return payload


def background_task_tool_payload(task: BackgroundTaskRecord) -> dict[str, Any]:
    """压缩后台任务结果，不暴露账号和任务 payload。"""

    return {
        "task_key": task.task_key,
        "task_type": task.task_type,
        "status": task.status,
        "progress": task.progress,
        "attempt": task.attempt,
        "max_attempts": task.max_attempts,
        "result": task.result,
        "error_summary": task.error_summary,
    }


@lru_cache(maxsize=1)
def job_hunting_tool_catalog() -> tuple[ToolMetadata, ...]:
    """返回不含 handler 的稳定工具目录，供路由、审计和 MCP adapter 使用。"""

    metadata_only_app = cast(JobHuntingApp, object())
    registry = build_job_hunting_tool_registry(metadata_only_app)
    return tuple(spec.metadata() for spec in registry.list_specs())
