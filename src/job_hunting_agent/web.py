"""本地 Web 前端入口。

这个模块现在同时支持两种聊天路径：

- 标准 LangChain Agent 模式：Web -> JobHuntingAgent -> Tools -> JobHuntingApp
- 本地规则兜底模式：Web -> JobHuntingApp.ingest_conversation_message

之所以保留兜底，是为了测试和显式离线调用；网页与 API 默认都走标准 Agent 结构，
模型配置错误时直接返回原因，不会静默切换执行逻辑。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import re
import time
import uuid
from dataclasses import asdict
from decimal import Decimal
from pathlib import Path
from urllib.parse import quote

from fastapi import (
    FastAPI,
    File,
    Form,
    HTTPException,
    Query,
    Request,
    Response,
    UploadFile,
)
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.exc import IntegrityError as SQLAlchemyIntegrityError
from starlette.concurrency import run_in_threadpool

from .account_email_outbox import AccountEmailOutboxService, redact_email
from .account_lifecycle import (
    AccountEmailSender,
    action_token_hash,
    build_account_email_sender,
    new_action_token,
)
from .admin_ledger import ADMIN_LEDGER_MAX_PAGES, ADMIN_LEDGER_PAGE_SIZE
from .agent import JobHuntingAgent
from .app import JobHuntingApp
from .auth import (
    AccountAlreadyExistsError,
    hash_password,
    is_session_expired,
    iso_utc,
    new_session_token,
    normalize_display_name,
    session_expiry,
    session_token_hash,
    utc_now,
    verify_password,
)
from .billing_projection import (
    balance_projection_to_dict,
    balance_summary_to_dict,
    project_account_balances,
)
from .concurrency_control import (
    ConcurrencyBackendUnavailable,
    ConcurrencyController,
    ConcurrencyLimitExceeded,
)
from .config import (
    DEFAULT_RAG_RERANK_TOP_N,
    MAX_RAG_RERANK_TOP_N,
    load_account_lifecycle_settings,
    load_agent_memory_settings,
    load_billing_settings,
    load_bootstrap_admin_settings,
    load_concurrency_settings,
    load_cookie_secure,
    load_database_settings,
    load_embedding_settings,
    load_file_scanning_settings,
    load_llm_settings,
    load_object_storage_settings,
    load_observability_settings,
    load_project_visual_analysis_settings,
    load_rerank_settings,
    load_task_queue_settings,
    load_web_security_settings,
    masked_account_lifecycle_settings,
    masked_agent_memory_settings,
    masked_billing_settings,
    masked_concurrency_settings,
    masked_embedding_settings,
    masked_file_scanning_settings,
    masked_llm_settings,
    masked_object_storage_settings,
    masked_project_visual_analysis_settings,
    masked_rerank_settings,
    masked_task_queue_settings,
    masked_web_security_settings,
    require_postgresql_database_url,
)
from .observability import configure_logging, configure_tracing, install_http_tracing
from .deduplication import DuplicateResourceError
from .file_scanning import FileInfectedError, FileScannerUnavailableError
from .github_project import GitHubRepositoryError
from .job_parser import InvalidJobTextError
from .job_screenshot import (
    MAX_JOB_SCREENSHOT_FILE_BYTES,
    MAX_JOB_SCREENSHOT_FILES,
    JobScreenshot,
    JobScreenshotError,
    JobScreenshotModelError,
)
from .llm import LLMClient, LLMRequestError
from .model_resilience import ModelCircuitOpenError
from .models import (
    AccountRecord,
    AdminAuditEventRecord,
    BackgroundTaskRecord,
    CandidateProfileInput,
    ResumeArtifactRecord,
    SkillRequirement,
    ToolCallTraceRecord,
)
from .project_evidence import (
    MAX_MANIFEST_FILES,
    ProjectEvidenceError,
    ProjectManifestItem,
)
from .rag import RAGProviderRequestError
from .rate_limiting import RateLimiter
from .resume_document import MAX_RESUME_FILE_BYTES, ResumeDocumentError
from .skill_normalization import normalize_skill_mapping
from .storage import AccountEmailRequestSuppressed, IdempotencyConflictError
from .task_queue import (
    AccountEmailQueue,
    BackgroundTaskQueue,
    CeleryAccountEmailQueue,
    TaskQueueError,
)
from .tool_audit import (
    background_task_tool_name,
    build_tool_trace_record,
    has_audited_steps,
    tool_step_label,
    tool_trace_title,
)
from .web_hardening import (
    CSRF_COOKIE_NAME,
    PROMETHEUS_CONTENT_TYPE,
    delete_csrf_cookie,
    format_prometheus_request_metrics,
    install_web_hardening,
    new_csrf_token,
    set_csrf_cookie,
)

STATIC_DIR = Path(__file__).with_name("web_static")
SESSION_COOKIE_NAME = "job_agent_session"
SESSION_MAX_AGE_SECONDS = 30 * 24 * 60 * 60
# Uvicorn 的重载子进程只能通过导入路径重新创建应用，因此启动参数通过这些
# 仅在进程内存在的环境变量传递，不写进用户的 .env 文件。
WEB_RELOAD_ENV_FILE_ENV = "JOB_AGENT_WEB_RELOAD_ENV_FILE"
WEB_RELOAD_RESUME_DIR_ENV = "JOB_AGENT_WEB_RELOAD_RESUME_DIR"
web_logger = logging.getLogger("job_hunting_agent.web")


def bootstrap_initial_admin(backend: JobHuntingApp, env_path: Path) -> None:
    """在空管理员集合上安全地应用一次 `.env` 引导配置。

    这不是公开注册接口：只有数据库中从未创建过管理员时才会执行。密码仅被
    AuthService 哈希后写入数据库，函数不返回也不记录原始密码。
    """

    settings = load_bootstrap_admin_settings(env_path)
    if settings is None:
        return
    if any(account.role == "admin" for account in backend.store.list_accounts()):
        return
    try:
        backend.auth.create_admin(settings.email, settings.password, settings.display_name)
    except AccountAlreadyExistsError as error:
        # 同邮箱的普通账号不能被静默提升为管理员，避免配置笔误造成权限变化。
        existing = backend.store.get_account_by_email(settings.email)
        if existing is None or existing[0].role != "admin":
            raise RuntimeError("首次管理员邮箱已被普通账号占用，请更换邮箱。") from error


class NoCacheStaticFiles(StaticFiles):
    """开发期静态资源服务。

    本项目目前是本地开发型 Web 前端，JS/CSS 经常会随着教学推进修改。
    禁用浏览器缓存可以避免用户看到旧版 `app.js`，例如 Markdown 渲染修复已经写入源码，
    但浏览器仍拿旧脚本导致 `**加粗**` 原样显示。
    """

    async def get_response(self, path: str, scope):
        """返回静态文件，并要求浏览器每次重新获取。"""

        response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = "no-store, max-age=0"
        return response


class ProfilePayload(BaseModel):
    """网页创建候选人档案时提交的数据。"""

    name: str
    status: str = "待补充"
    education: str = "待补充"
    experience_years: float = 0
    skills: dict[str, str] = Field(default_factory=dict)
    preferred_cities: list[str] = Field(default_factory=list)
    acceptable_cities: list[str] = Field(default_factory=list)
    salary_floor_k: int | None = None
    expected_salary_k: int | None = None
    target_directions: list[str] = Field(default_factory=list)
    unacceptable: list[str] = Field(default_factory=list)


class ChatPayload(BaseModel):
    """网页聊天输入。

    为了兼容已有前端字段名，这里沿用 `use_env_llm`。现在它的语义是：

    - `true`（默认）：走标准 LangChain Agent 主流程。
    - `false`：走本地规则兜底流程。
    """

    candidate_id: int
    message: str
    use_env_llm: bool = True
    auto_rag: bool = True
    session_id: str | None = None


class JobPayload(BaseModel):
    """网页导入职位文本时提交的数据。"""

    raw_text: str
    source_url: str | None = None


class JobSkillRequirementPayload(BaseModel):
    """职位已有技能的人工分类输入。"""

    name: str
    category: str = "general"
    confidence: float = 0.5
    evidence: str = ""


class JobSkillRequirementsPayload(BaseModel):
    """批量保存一个职位的技能分类。"""

    requirements: list[JobSkillRequirementPayload] = Field(default_factory=list)


class RegisterPayload(BaseModel):
    """普通用户注册表单。"""

    email: str
    password: str
    display_name: str | None = None
    accepted_terms_version: str | None = Field(default=None, max_length=64)
    accepted_privacy_version: str | None = Field(default=None, max_length=64)


class LoginPayload(BaseModel):
    """登录表单。"""

    email: str
    password: str


class AccountActionTokenPayload(BaseModel):
    """一次性邮箱操作令牌。"""

    token: str = Field(min_length=32, max_length=256)


class AccountEmailPayload(BaseModel):
    """账号邮件请求；响应不得泄露邮箱是否存在。"""

    email: str = Field(min_length=3, max_length=254)


class PasswordResetConfirmPayload(AccountActionTokenPayload):
    """密码重置确认。"""

    new_password: str = Field(min_length=8, max_length=1024)


class PasswordChangePayload(BaseModel):
    """登录用户主动修改密码。"""

    current_password: str = Field(min_length=1, max_length=1024)
    new_password: str = Field(min_length=8, max_length=1024)


class AccountDeletePayload(BaseModel):
    """账号注销的二次确认。"""

    current_password: str = Field(min_length=1, max_length=1024)
    confirmation: str = Field(min_length=1, max_length=32)


class AccountStatusPayload(BaseModel):
    """管理员启用或禁用账号时提交的状态。"""

    status: str


class ChatSessionPayload(BaseModel):
    """创建独立求职对话时提交的档案、职位和标题。"""

    candidate_id: int
    title: str = "新对话"
    job_id: int | None = None


class TailorResumePayload(BaseModel):
    """根据一份已上传简历生成职位定制文件时提交的参数。"""

    job_id: int
    use_rag: bool = True


class GitHubProjectPayload(BaseModel):
    """网页提交公开 GitHub 项目分析时需要的归属和仓库链接。"""

    candidate_id: int
    repository_url: str


class LocalProjectManifestFilePayload(BaseModel):
    """One browser-scanned file descriptor; content arrives only after selection."""

    relative_path: str = Field(min_length=1, max_length=2048)
    file_size: int = Field(ge=0)
    sha256: str | None = Field(default=None, min_length=64, max_length=64)
    media_type: str = Field(default="application/octet-stream", max_length=128)
    last_modified: int | None = Field(default=None, ge=0)


class LocalProjectManifestPayload(BaseModel):
    """Manifest-first local directory collection request."""

    model_config = ConfigDict(extra="forbid")

    candidate_id: int
    project_name: str = Field(min_length=1, max_length=256)
    files: list[LocalProjectManifestFilePayload] = Field(
        min_length=1,
        max_length=MAX_MANIFEST_FILES,
    )


class ProjectCardConfirmationPayload(BaseModel):
    """候选人确认项目卡片时可选提供的本人职责摘要。"""

    confirmed_summary: str | None = Field(default=None, max_length=20_000)
    root_request_id: str | None = Field(
        default=None,
        min_length=32,
        max_length=32,
        pattern=r"^[A-Fa-f0-9]{32}$",
    )


class BalanceRechargePayload(BaseModel):
    """个人中心余额充值表单。"""

    amount_yuan: Decimal = Field(gt=0, le=1_000_000, decimal_places=6)
    note: str | None = Field(default=None, max_length=120)
    idempotency_key: str | None = Field(default=None, min_length=16, max_length=128)


class AdminBalanceCreditPayload(BaseModel):
    """管理员为指定账号执行人工补款。"""

    amount_yuan: Decimal = Field(gt=0, le=1_000_000, decimal_places=6)
    reason: str = Field(min_length=2, max_length=500)
    idempotency_key: str = Field(min_length=16, max_length=128)


def create_web_app(
    env_file: str | Path = ".env",
    resume_dir: str | Path | None = None,
    chat_agent: JobHuntingAgent | None = None,
    resume_llm_client: LLMClient | None = None,
    database_url: str | None = None,
    task_queue: BackgroundTaskQueue | None = None,
    rate_limiter: RateLimiter | None = None,
    concurrency_controller: ConcurrencyController | None = None,
    file_scanner: object | None = None,
    account_email_sender: AccountEmailSender | None = None,
    account_email_queue: AccountEmailQueue | None = None,
) -> FastAPI:
    """创建本地 FastAPI 应用。

    这里保留测试可注入的 Agent 和模型，目的是：

    - 生产使用时，Web 层通过 `JobHuntingAgent` 或 `JobHuntingApp` 访问业务能力；
    - 测试时，可以显式注入假模型；
    - 生产入口传入 PostgreSQL URL 后，Web 层通过 SQLAlchemy 仓储访问数据；
    - Web 层自己不直接碰数据库连接、RAG 向量库细节或厂商 SDK。
    """

    backend = JobHuntingApp(
        env_path=env_file,
        resume_dir=resume_dir,
        database_url=database_url,
        task_queue=task_queue,
        concurrency_controller=concurrency_controller,
        file_scanner=file_scanner,  # type: ignore[arg-type]
    )
    backend.initialize()
    env_path = Path(env_file)
    bootstrap_initial_admin(backend, env_path)
    cookie_secure = load_cookie_secure(env_path)
    web_security_settings = load_web_security_settings(env_path)
    account_lifecycle_settings = load_account_lifecycle_settings(env_path)
    lifecycle_email_sender = account_email_sender or build_account_email_sender(
        account_lifecycle_settings
    )
    lifecycle_email_queue = account_email_queue
    if lifecycle_email_queue is None and backend.task_queue_settings.enabled:
        lifecycle_email_queue = CeleryAccountEmailQueue(backend.task_queue_settings)
    account_email_outbox = AccountEmailOutboxService(
        backend.store,
        account_lifecycle_settings,
        lifecycle_email_sender,
    )

    agent_error: str | None = None
    if chat_agent is None:
        try:
            chat_agent = JobHuntingAgent(backend, env_path=env_path)
        except ValueError as error:
            # `.env` 没配好时，网页仍然可以以本地规则模式工作。
            agent_error = str(error)

    web_app = FastAPI(title="Job Hunting Agent Web", version="0.1.0")
    web_app.state.backend = backend
    web_app.state.account_email_outbox = account_email_outbox

    @web_app.exception_handler(ConcurrencyLimitExceeded)
    async def concurrency_limit_handler(
        request: Request,
        error: ConcurrencyLimitExceeded,
    ) -> JSONResponse:
        return JSONResponse(
            status_code=429,
            content={"detail": str(error)},
            headers={"Retry-After": "1"},
        )

    @web_app.exception_handler(ConcurrencyBackendUnavailable)
    async def concurrency_backend_handler(
        request: Request,
        error: ConcurrencyBackendUnavailable,
    ) -> JSONResponse:
        return JSONResponse(
            status_code=503,
            content={"detail": "并发保护服务暂时不可用，请稍后重试。"},
            headers={"Retry-After": "1"},
        )

    @web_app.exception_handler(ModelCircuitOpenError)
    async def model_circuit_handler(
        request: Request,
        error: ModelCircuitOpenError,
    ) -> JSONResponse:
        return JSONResponse(
            status_code=503,
            content={"detail": str(error)},
            headers={"Retry-After": str(error.retry_after_seconds)},
        )

    def rate_limit_identity(request: Request) -> str:
        """把有效登录态映射为账号级限流键，并缓存已读取的 Session。"""

        host = request.client.host if request.client and request.client.host else "unknown"
        network_identity = f"ip:{host}"
        if not request.url.path.startswith("/api/"):
            return network_identity
        token = request.cookies.get(SESSION_COOKIE_NAME)
        if not token:
            return network_identity
        try:
            session = backend.store.get_auth_session_by_token_hash(
                session_token_hash(token)
            )
        except Exception as error:  # noqa: BLE001 - 数据库故障时仍保留 IP 级请求保护。
            web_logger.warning(
                "rate-limit identity fell back to network source: %s",
                type(error).__name__,
            )
            return network_identity
        if (
            session is None
            or session.revoked_at is not None
            or is_session_expired(session.expires_at, session.absolute_expires_at)
        ):
            return network_identity
        request.state.auth_session = session
        return f"account:{session.account_id}"

    install_web_hardening(
        web_app,
        settings=web_security_settings,
        session_cookie_name=SESSION_COOKIE_NAME,
        identity_resolver=rate_limit_identity,
        rate_limiter=rate_limiter,
    )
    web_app.mount("/static", NoCacheStaticFiles(directory=STATIC_DIR), name="static")
    # 截图本体不持久化，无法安全地只向 Worker 投递 task_key；因此它是一个有界的
    # 前台导入例外。实际模型调用在线程池执行，共享并发控制器负责限制图片内存占用。

    def current_account(request: Request, required: bool = True) -> AccountRecord | None:
        """从 HttpOnly Cookie 解析当前账号，并顺延 Session 闲置窗口。"""

        token = request.cookies.get(SESSION_COOKIE_NAME)
        if not token:
            if required:
                raise HTTPException(status_code=401, detail="请先登录。")
            return None
        session = getattr(request.state, "auth_session", None)
        if session is None:
            session = backend.store.get_auth_session_by_token_hash(
                session_token_hash(token)
            )
        if (
            session is None
            or session.revoked_at is not None
            or is_session_expired(session.expires_at, session.absolute_expires_at)
        ):
            if required:
                raise HTTPException(status_code=401, detail="登录状态已过期，请重新登录。")
            return None
        try:
            account = backend.store.get_account(session.account_id)
        except KeyError as error:
            raise HTTPException(status_code=401, detail="登录账号不存在。") from error
        if account.status != "active":
            raise HTTPException(status_code=403, detail="账号已被禁用。")
        idle_expiry, _ = session_expiry()
        # 绝对过期时间不变，只把闲置过期时间向后滑动。
        if idle_expiry < session.absolute_expires_at:
            backend.store.touch_auth_session(session.id, utc_now().isoformat(timespec="seconds"), idle_expiry)
        request.state.account = account
        return account

    def require_admin(request: Request) -> AccountRecord:
        """管理员接口统一检查角色。"""

        account = current_account(request)
        if account is None or account.role != "admin":
            raise HTTPException(status_code=403, detail="需要管理员权限。")
        return account

    def prune_admin_retention_window() -> None:
        """仅裁剪非财务运维记录；余额账本作为财务事实永久追加保存。"""

        backend.store.prune_usage_events_to_limit()
        backend.store.prune_tool_call_traces_to_limit()

    @web_app.get("/api/auth/config")
    def auth_config() -> dict[str, object]:
        """Expose only public registration requirements to the login page."""

        return {
            "registration_enabled": account_lifecycle_settings.registration_enabled,
            "email_verification_required": account_lifecycle_settings.email_verification_required,
            "consent_required": account_lifecycle_settings.consent_required,
            "terms_version": account_lifecycle_settings.terms_version,
            "privacy_version": account_lifecycle_settings.privacy_version,
        }

    @web_app.post("/api/auth/register")
    def register(payload: RegisterPayload, request: Request) -> dict[str, object]:
        """开放普通用户注册；管理员账号不通过此接口创建。"""

        if not account_lifecycle_settings.registration_enabled:
            raise HTTPException(status_code=403, detail="当前未开放新账号注册。")
        try:
            email = backend.auth.normalize_email(payload.email)
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        consent_rows: list[tuple[str, str]] = []
        if account_lifecycle_settings.consent_required:
            if payload.accepted_terms_version != account_lifecycle_settings.terms_version:
                raise HTTPException(status_code=400, detail="请阅读并同意当前版本的服务条款。")
            if payload.accepted_privacy_version != account_lifecycle_settings.privacy_version:
                raise HTTPException(status_code=400, detail="请阅读并同意当前版本的隐私政策。")
            consent_rows = [
                ("terms", account_lifecycle_settings.terms_version),
                ("privacy", account_lifecycle_settings.privacy_version),
            ]
        try:
            password_hash = hash_password(payload.password)
            account = backend.store.create_account(
                email=email,
                password_hash=password_hash,
                display_name=normalize_display_name(payload.display_name, payload.password),
                role="user",
                email_verified=not account_lifecycle_settings.email_verification_required,
                consents=consent_rows,
                consent_ip_address=request.client.host if request.client else None,
                consent_user_agent=request.headers.get("user-agent"),
            )
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        # PostgreSQL 唯一约束异常统一映射为同一 409 响应。
        except SQLAlchemyIntegrityError as error:
            raise HTTPException(status_code=409, detail="该邮箱已经注册。") from error
        if account_lifecycle_settings.email_verification_required:
            enqueue_account_action_email(account, "verify_email", request)
        return {
            "account": asdict(account),
            "verification_required": account_lifecycle_settings.email_verification_required,
        }

    @web_app.post("/api/auth/verify-email")
    def verify_email(payload: AccountActionTokenPayload) -> dict[str, object]:
        """Consume a one-time verification token."""

        account = backend.store.consume_email_verification_token(
            action_token_hash(payload.token)
        )
        if account is None:
            raise HTTPException(status_code=400, detail="验证链接无效或已过期。")
        return {"account": asdict(account)}

    def send_account_action_email(
        account: AccountRecord,
        purpose: str,
        request: Request,
    ) -> None:
        """兼容内部调用名称，实际只登记 Outbox 并投递低敏 ID。"""

        enqueue_account_action_email(account, purpose, request)

    def enqueue_account_action_email(
        account: AccountRecord,
        purpose: str,
        request: Request,
    ) -> None:
        """登记账号邮件；频率限制不泄露账号是否存在。"""

        try:
            record = account_email_outbox.enqueue(
                account,
                purpose,
                request.client.host if request.client else None,
            )
        except AccountEmailRequestSuppressed:
            return
        if lifecycle_email_queue is None:
            web_logger.warning(
                "account email queued without active broker: outbox_id=%s",
                record.id,
            )
            return
        try:
            lifecycle_email_queue.enqueue(record.id)
        except Exception as error:  # noqa: BLE001 - Beat recovers the pending row.
            web_logger.warning(
                "account email broker dispatch failed: outbox_id=%s error=%s",
                record.id,
                type(error).__name__,
            )

    @web_app.post("/api/auth/verification/request")
    def request_verification(payload: AccountEmailPayload, request: Request) -> dict[str, str]:
        """Resend verification without revealing whether the email is registered."""

        record = backend.store.get_account_by_email(payload.email.strip().lower())
        if record is not None and record[0].status == "active" and record[0].email_verified_at is None:
            send_account_action_email(record[0], "verify_email", request)
        return {"message": "如果账号需要验证，验证邮件已发送。"}

    @web_app.post("/api/auth/password-reset/request")
    def request_password_reset(payload: AccountEmailPayload, request: Request) -> dict[str, str]:
        """Issue a reset token while keeping account existence private."""

        record = backend.store.get_account_by_email(payload.email.strip().lower())
        if record is not None and record[0].status == "active":
            send_account_action_email(record[0], "reset_password", request)
        return {"message": "如果该邮箱已注册，密码重置邮件已发送。"}

    @web_app.post("/api/auth/password-reset/confirm")
    def confirm_password_reset(payload: PasswordResetConfirmPayload) -> dict[str, bool]:
        """Consume a reset token and revoke every existing device session."""

        try:
            password_hash = hash_password(payload.new_password)
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        account = backend.store.consume_password_reset_token(
            action_token_hash(payload.token),
            password_hash,
        )
        if account is None:
            raise HTTPException(status_code=400, detail="重置链接无效或已过期。")
        return {"ok": True}

    @web_app.post("/api/auth/login")
    def login(payload: LoginPayload, response: Response, request: Request) -> dict[str, object]:
        """验证密码并签发服务端 Session Cookie。"""

        record = backend.store.get_account_by_email(payload.email.strip().lower())
        if record is None or not verify_password(record[1], payload.password):
            raise HTTPException(status_code=401, detail="邮箱或密码错误。")
        account, _ = record
        if account.status != "active":
            raise HTTPException(status_code=403, detail="账号已被禁用。")
        if (
            account_lifecycle_settings.email_verification_required
            and account.email_verified_at is None
        ):
            raise HTTPException(status_code=403, detail="请先完成邮箱验证。")
        raw_token = new_session_token()
        now = utc_now().isoformat(timespec="seconds")
        expires_at, absolute_expires_at = session_expiry()
        backend.store.save_auth_session(
            account_id=account.id,
            token_hash=session_token_hash(raw_token),
            created_at=now,
            last_seen_at=now,
            expires_at=expires_at,
            absolute_expires_at=absolute_expires_at,
            user_agent=request.headers.get("user-agent"),
            ip_address=request.client.host if request.client else None,
        )
        response.set_cookie(
            SESSION_COOKIE_NAME,
            raw_token,
            max_age=SESSION_MAX_AGE_SECONDS,
            httponly=True,
            secure=cookie_secure,
            samesite="lax",
            path="/",
        )
        csrf_token = new_csrf_token()
        set_csrf_cookie(response, csrf_token, secure=cookie_secure)
        backend.store.touch_account_login(account.id)
        return {"account": asdict(account), "csrf_token": csrf_token}

    @web_app.get("/api/auth/me")
    def auth_me(request: Request, response: Response) -> dict[str, object]:
        """返回当前登录账号；未登录用于前端显示登录页。"""

        account = current_account(request, required=False)
        csrf_token = None
        billing_summary = None
        if account is not None:
            csrf_token = request.cookies.get(CSRF_COOKIE_NAME) or new_csrf_token()
            set_csrf_cookie(response, csrf_token, secure=cookie_secure)
            billing_summary = asdict(backend.store.get_account_balance_summary(account.id))
        return {
            "authenticated": account is not None,
            "account": asdict(account) if account else None,
            "csrf_token": csrf_token,
            "billing": billing_summary,
        }

    @web_app.post("/api/auth/logout")
    def logout(request: Request, response: Response) -> dict[str, bool]:
        """撤销当前设备 Session 并清理 Cookie。"""

        token = request.cookies.get(SESSION_COOKIE_NAME)
        if token:
            session = backend.store.get_auth_session_by_token_hash(session_token_hash(token))
            if session is not None:
                backend.store.revoke_auth_session(session.id)
        response.delete_cookie(SESSION_COOKIE_NAME, path="/")
        delete_csrf_cookie(response)
        return {"ok": True}

    @web_app.post("/api/auth/logout-all")
    def logout_all(request: Request, response: Response) -> dict[str, object]:
        """撤销当前账号在所有设备上的登录状态。"""

        account = current_account(request)
        assert account is not None
        if account.role == "admin":
            audit_event = AdminAuditEventRecord(
                id=0,
                actor_account_id=account.id,
                target_account_id=account.id,
                action="auth.logout_all_devices",
                target_type="account",
                target_id=str(account.id),
                outcome="succeeded",
                summary=f"撤销账号 #{account.id} 的全部登录会话。",
                details={},
                request_id=getattr(request.state, "request_id", None),
            )
            count = backend.store.revoke_all_auth_sessions_with_audit(account.id, audit_event)
        else:
            count = backend.store.revoke_all_auth_sessions(account.id)
        response.delete_cookie(SESSION_COOKIE_NAME, path="/")
        delete_csrf_cookie(response)
        return {"ok": True, "revoked_sessions": count}

    @web_app.post("/api/account/password")
    def change_password(
        payload: PasswordChangePayload,
        request: Request,
        response: Response,
    ) -> dict[str, bool]:
        """Change the current password and revoke all browser sessions."""

        account = current_account(request)
        assert account is not None
        _, current_hash = backend.store.get_account_with_password(account.id)
        if not verify_password(current_hash, payload.current_password):
            raise HTTPException(status_code=400, detail="当前密码错误。")
        try:
            new_hash = hash_password(payload.new_password)
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        if verify_password(current_hash, payload.new_password):
            raise HTTPException(status_code=400, detail="新密码不能与当前密码相同。")
        backend.store.update_account_password_and_revoke_sessions(account.id, new_hash)
        response.delete_cookie(SESSION_COOKIE_NAME, path="/")
        delete_csrf_cookie(response)
        return {"ok": True}

    @web_app.get("/api/account/export")
    def export_account_data(request: Request, response: Response) -> dict[str, object]:
        """Download the current account's structured portable data export."""

        account = current_account(request)
        assert account is not None
        response.headers["Content-Disposition"] = (
            f'attachment; filename="job-agent-account-{account.id}-export.json"'
        )
        response.headers["Cache-Control"] = "no-store"
        return backend.store.export_account_data(account.id)

    @web_app.post("/api/account/delete")
    def delete_account(
        payload: AccountDeletePayload,
        request: Request,
        response: Response,
    ) -> dict[str, bool]:
        """Delete job-search data and anonymize the retained financial account."""

        account = current_account(request)
        assert account is not None
        if payload.confirmation != "注销账号":
            raise HTTPException(status_code=400, detail="请输入“注销账号”完成确认。")
        _, current_hash = backend.store.get_account_with_password(account.id)
        if not verify_password(current_hash, payload.current_password):
            raise HTTPException(status_code=400, detail="当前密码错误。")
        try:
            storage_keys = backend.store.prepare_account_deletion(account.id)
        except ValueError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        try:
            for storage_key in storage_keys:
                backend.resume_files.delete(storage_key)
        except Exception as error:
            web_logger.exception("account object cleanup failed", extra={"account_id": account.id})
            try:
                backend.store.restore_account_after_failed_deletion(account.id)
            except Exception:
                web_logger.exception(
                    "account status restore failed after object cleanup error",
                    extra={"account_id": account.id},
                )
            raise HTTPException(status_code=503, detail="账号文件清理失败，请稍后重试。") from error
        backend.store.finalize_account_deletion(
            account.id,
            f"deleted-{account.id}-{uuid.uuid4().hex}@invalid.local",
            hash_password(new_action_token()),
        )
        response.delete_cookie(SESSION_COOKIE_NAME, path="/")
        delete_csrf_cookie(response)
        return {"ok": True}

    def frontend_shell() -> FileResponse:
        response = FileResponse(STATIC_DIR / "index.html")
        response.headers["Cache-Control"] = "no-store, max-age=0"
        return response

    @web_app.get("/")
    def home() -> FileResponse:
        """返回 Web 前端入口；前端会按登录状态进入登录页或工作台。"""

        return frontend_shell()

    @web_app.get("/login")
    def login_page() -> FileResponse:
        """登录和注册页面。"""

        return frontend_shell()

    @web_app.get("/workspace")
    def workspace_page() -> FileResponse:
        """求职工作台页面。"""

        return frontend_shell()

    @web_app.get("/profile")
    def profile_page() -> FileResponse:
        """个人中心页面。"""

        return frontend_shell()

    @web_app.get("/terms")
    def terms_page() -> FileResponse:
        """服务条款基线页面；正式版本仍需运营主体审核。"""

        return FileResponse(STATIC_DIR / "terms.html", headers={"Cache-Control": "no-store"})

    @web_app.get("/privacy")
    def privacy_page() -> FileResponse:
        """隐私政策基线页面；正式版本仍需运营主体审核。"""

        return FileResponse(STATIC_DIR / "privacy.html", headers={"Cache-Control": "no-store"})

    @web_app.get("/admin")
    def admin_page() -> FileResponse:
        """管理后台页面。"""

        return frontend_shell()

    @web_app.get("/api/health")
    def health(request: Request) -> dict[str, object]:
        """返回最小服务状态；详细路径和模型配置只给管理员。"""

        account = current_account(request, required=False)

        try:
            llm_config: dict[str, object] = masked_llm_settings(load_llm_settings(env_path))
        except ValueError as error:
            llm_config = {"configured": False, "error": str(error)}
        else:
            llm_config["configured"] = True
        try:
            embedding_config = masked_embedding_settings(load_embedding_settings(env_path))
        except ValueError as error:
            embedding_config = {"configured": False, "error": str(error)}
        try:
            rerank_config = masked_rerank_settings(load_rerank_settings(env_path))
        except ValueError as error:
            rerank_config = {"configured": False, "error": str(error)}
        try:
            memory_config = masked_agent_memory_settings(load_agent_memory_settings(env_path))
        except ValueError as error:
            memory_config = {"configured": False, "error": str(error)}
        try:
            file_storage_config = masked_object_storage_settings(
                load_object_storage_settings(env_path)
            )
            file_storage_config["configured"] = True
        except ValueError as error:
            file_storage_config = {"configured": False, "error": str(error)}
        try:
            task_queue_config = masked_task_queue_settings(
                load_task_queue_settings(env_path)
            )
            task_queue_config["configured"] = bool(task_queue_config.get("enabled"))
        except ValueError as error:
            task_queue_config = {"configured": False, "error": str(error)}
        try:
            web_security_config = masked_web_security_settings(load_web_security_settings(env_path))
        except ValueError as error:
            web_security_config = {"configured": False, "error": str(error)}
        try:
            account_lifecycle_config = masked_account_lifecycle_settings(
                load_account_lifecycle_settings(env_path)
            )
        except ValueError as error:
            account_lifecycle_config = {"configured": False, "error": str(error)}
        try:
            billing_config = masked_billing_settings(load_billing_settings(env_path))
        except ValueError as error:
            billing_config = {"configured": False, "error": str(error)}
        try:
            concurrency_config = masked_concurrency_settings(
                load_concurrency_settings(env_path)
            )
        except ValueError as error:
            concurrency_config = {"configured": False, "error": str(error)}
        try:
            file_scanning_config = masked_file_scanning_settings(
                load_file_scanning_settings(env_path)
            )
            file_scanning_config["configured"] = True
        except ValueError as error:
            file_scanning_config = {"configured": False, "error": str(error)}
        try:
            project_visual_config = masked_project_visual_analysis_settings(
                load_project_visual_analysis_settings(env_path)
            )
            project_visual_config["configured"] = bool(
                project_visual_config.get("enabled") and llm_config.get("configured")
            )
        except ValueError as error:
            project_visual_config = {"configured": False, "error": str(error)}
        if account is None or account.role != "admin":
            return {
                "status": "ok",
                "agent": {"configured": chat_agent is not None},
                "llm": {"configured": bool(llm_config.get("configured"))},
                "embedding": {"configured": bool(embedding_config.get("configured"))},
                "rerank": {"configured": bool(rerank_config.get("configured"))},
                "memory": {
                    "configured": bool(memory_config.get("enabled")),
                    "checkpoint_backend": memory_config.get("checkpoint_backend"),
                },
                "task_queue": {"configured": bool(task_queue_config.get("configured"))},
                "web_security": {
                    "configured": not bool(web_security_config.get("error")),
                    "security_headers_enabled": bool(
                        web_security_config.get("security_headers_enabled")
                    ),
                    "rate_limit_enabled": bool(web_security_config.get("rate_limit_enabled")),
                },
                "account_lifecycle": {
                    "configured": not bool(account_lifecycle_config.get("error")),
                    "email_verification_required": bool(
                        account_lifecycle_config.get("email_verification_required")
                    ),
                },
                "billing": {"configured": bool(billing_config.get("configured"))},
                "concurrency": {
                    "configured": not bool(concurrency_config.get("error")),
                    "enabled": bool(concurrency_config.get("enabled")),
                },
                "file_scanning": {"configured": bool(file_scanning_config.get("configured"))},
                "project_visual_analysis": {
                    "configured": bool(project_visual_config.get("configured")),
                    "enabled": bool(project_visual_config.get("enabled")),
                },
                "model_circuit": backend.model_gateway.circuit_snapshot(),
            }
        return {
            "status": "ok",
            "storage_backend": "postgresql" if backend._uses_pgvector_rag() else "test_adapter",
            "file_storage_backend": backend.file_storage_backend,
            "file_storage": file_storage_config,
            "rag_backend": "pgvector",
            "llm": llm_config,
            "embedding": embedding_config,
            "rerank": rerank_config,
            "memory": memory_config,
            "task_queue": task_queue_config,
            "web_security": web_security_config,
            "account_lifecycle": account_lifecycle_config,
            "billing": billing_config,
            "concurrency": concurrency_config,
            "file_scanning": file_scanning_config,
            "project_visual_analysis": project_visual_config,
            "model_circuit": backend.model_gateway.circuit_snapshot(),
            "agent": {
                "configured": chat_agent is not None,
                "error": agent_error,
            },
        }

    @web_app.get("/internal/metrics", include_in_schema=False)
    def prometheus_metrics() -> Response:
        """供内部 Prometheus 采集当前 Web 进程的低敏请求指标。"""

        # 并发控制器只返回固定资源名和计数，不把账号、租约 token 或请求正文带入
        # 监控系统。Worker 没有 HTTP 指标端点，因此 Web 端只暴露自身观察到的事件。
        concurrency_snapshot = getattr(
            backend.concurrency_controller,
            "metrics_snapshot",
            lambda: {"resources": {}},
        )()
        body = format_prometheus_request_metrics(
            web_app.state.request_metrics.snapshot(),
            concurrency_snapshot,
            (
                chat_agent.routing_metrics.snapshot()
                if chat_agent is not None
                else None
            ),
        )
        return Response(content=body, media_type=PROMETHEUS_CONTENT_TYPE)

    @web_app.get("/api/profiles")
    def list_profiles(request: Request) -> dict[str, object]:
        """列出候选人档案，供左侧栏选择。"""

        account = current_account(request)
        return {"profiles": [asdict(profile) for profile in backend.list_candidate_profiles(account_id=account.id if account else None)]}

    @web_app.post("/api/profiles")
    def create_profile(payload: ProfilePayload, request: Request) -> dict[str, object]:
        """创建候选人档案。"""

        account = current_account(request)
        if not payload.name.strip():
            raise HTTPException(status_code=400, detail="候选人姓名不能为空。")
        try:
            candidate_id = backend.save_candidate_profile(
                CandidateProfileInput(
                    name=payload.name.strip(),
                    status=payload.status.strip() or "待补充",
                    education=payload.education.strip() or "待补充",
                    experience_years=payload.experience_years,
                    skills=clean_string_dict(payload.skills),
                    preferred_cities=clean_string_list(payload.preferred_cities),
                    acceptable_cities=clean_string_list(payload.acceptable_cities),
                    salary_floor_k=payload.salary_floor_k,
                    expected_salary_k=payload.expected_salary_k,
                    target_directions=clean_string_list(payload.target_directions),
                    unacceptable=clean_string_list(payload.unacceptable),
                ),
                account_id=account.id if account else None,
            )
        except DuplicateResourceError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        return {"candidate_id": candidate_id, "profile": asdict(backend.get_candidate_profile(candidate_id, account_id=account.id if account else None))}

    @web_app.get("/api/profiles/{candidate_id}")
    def get_profile(candidate_id: int, request: Request) -> dict[str, object]:
        """读取某个候选人档案。"""

        account = current_account(request)
        return {"profile": asdict(get_profile_or_404(backend, candidate_id, account.id if account else None))}

    @web_app.delete("/api/profiles/{candidate_id}")
    def delete_profile(candidate_id: int, request: Request) -> dict[str, object]:
        """删除当前账号的候选人档案及其从属数据。"""

        account = current_account(request)
        try:
            result = backend.delete_candidate_profile(
                candidate_id,
                account_id=account.id if account else None,
            )
        except KeyError as error:
            raise HTTPException(status_code=404, detail="候选人档案不存在。") from error
        return {"deleted": True, **result}

    @web_app.post("/api/chat/sessions")
    def create_chat_session(payload: ChatSessionPayload, request: Request) -> dict[str, object]:
        """创建一个绑定当前账号和候选人档案的独立会话。"""

        account = current_account(request)
        assert account is not None
        account_id = account.id
        get_profile_or_404(backend, payload.candidate_id, account_id)
        if payload.job_id is not None:
            backend.store.get_job(payload.job_id, account_id=account_id)
        session_id = f"chat-{uuid.uuid4().hex}"
        record = backend.store.create_chat_session(
            session_id=session_id,
            account_id=account_id or 0,
            candidate_id=payload.candidate_id,
            title=payload.title.strip() or "新对话",
            job_id=payload.job_id,
        )
        return {"session": asdict(record)}

    def validate_chat_session(
        account_id: int,
        candidate_id: int,
        session_id: str,
    ) -> None:
        """验证会话是否属于当前账号和当前候选人。

        首次使用的默认会话还没有索引记录，允许成功回复时自动创建；
        已存在的会话若绑定了另一份档案则立即拒绝，避免记忆和历史串线。
        """

        try:
            session = backend.store.get_chat_session_by_key(session_id, account_id)
        except KeyError:
            return
        if session.candidate_id != candidate_id:
            raise HTTPException(status_code=403, detail="该会话不属于当前候选人档案。")
        if session.status != "active":
            raise HTTPException(status_code=409, detail="该会话已经归档，请新建对话。")

    @web_app.get("/api/chat/sessions")
    def list_chat_sessions(
        request: Request,
        candidate_id: int | None = Query(default=None),
        include_archived: bool = False,
    ) -> dict[str, object]:
        """列出当前账号的会话索引，默认隐藏已归档会话。"""

        account = current_account(request)
        assert account is not None
        if candidate_id is not None:
            get_profile_or_404(backend, candidate_id, account.id)
        records = backend.store.list_chat_sessions(account.id, candidate_id, include_archived)
        return {"sessions": [asdict(record) for record in records]}

    @web_app.post("/api/chat/sessions/{session_id}/archive")
    def archive_chat_session(session_id: str, request: Request) -> dict[str, object]:
        """软归档当前账号的会话，历史和用量流水仍然保留。"""

        account = current_account(request)
        assert account is not None
        try:
            record = backend.store.archive_chat_session(session_id, account.id)
        except KeyError as error:
            raise HTTPException(status_code=404, detail="会话不存在。") from error
        return {"session": asdict(record)}

    @web_app.delete("/api/chat/sessions/{session_id}")
    def delete_chat_session(session_id: str, request: Request) -> dict[str, object]:
        """永久删除当前账号的一段对话及其消息。"""

        account = current_account(request)
        assert account is not None
        try:
            result = backend.delete_chat_session(session_id, account.id)
        except KeyError as error:
            raise HTTPException(status_code=404, detail="会话不存在。") from error
        return {"deleted": True, **result}

    @web_app.post("/api/chat")
    def chat(payload: ChatPayload, request: Request) -> dict[str, object]:
        """处理网页聊天消息。

        - 开启 Agent 时：执行标准 LangChain Agent 对话。
        - 关闭 Agent 时：回退到原有“对话式自动入库”规则链路。
        """

        account = current_account(request)
        assert account is not None
        account_id = account.id
        get_profile_or_404(backend, payload.candidate_id, account_id)
        user_message = payload.message.strip()
        session_id = payload.session_id or default_web_session_id(payload.candidate_id, account_id)
        validate_chat_session(account_id, payload.candidate_id, session_id)
        if not user_message:
            raise HTTPException(status_code=400, detail="消息不能为空。")
        root_request_id = uuid.uuid4().hex
        trace_started_at = time.monotonic()

        if payload.use_env_llm:
            if chat_agent is None:
                raise HTTPException(
                    status_code=400,
                    detail=f"LangChain Agent 未就绪：{agent_error or '请检查 .env 配置'}",
                )
            try:
                result = chat_agent.chat(
                    user_message,
                    candidate_id=payload.candidate_id,
                    session_id=session_id,
                    # 顶层 Agent 已经负责理解意图和组织回复；工具层保持本地规则，
                    # 避免一次聊天因资料入库、职位分类或草稿生成而串行发起多次模型请求。
                    # 需要视觉识别或独立简历改写时，用户会通过对应的显式上传/改写接口触发。
                    use_tool_llm=False,
                    auto_rag=payload.auto_rag,
                    account_id=account_id,
                    root_request_id=root_request_id,
                )
            except ConcurrencyLimitExceeded as error:
                raise HTTPException(
                    status_code=429,
                    detail=str(error),
                    headers={"Retry-After": "1"},
                ) from error
            except ConcurrencyBackendUnavailable as error:
                raise HTTPException(
                    status_code=503,
                    detail="并发保护服务暂时不可用，请稍后重试。",
                    headers={"Retry-After": "1"},
                ) from error
            except ModelCircuitOpenError as error:
                raise HTTPException(
                    status_code=503,
                    detail=str(error),
                    headers={"Retry-After": str(error.retry_after_seconds)},
                ) from error
            except RAGProviderRequestError as error:
                raise HTTPException(status_code=502, detail=str(error)) from error
            except Exception as error:
                raise HTTPException(status_code=502, detail=str(error)) from error
            tool_outputs = result.tool_outputs
            trace = new_task_trace(root_request_id=root_request_id, source="chat")
            reconcile_task_trace(trace, tool_outputs)
            approval = approval_from_tool_outputs(tool_outputs)
            trace["approval"] = approval
            trace["status"] = task_trace_completion_status(trace, approval)
            trace["duration_ms"] = max(0, round((time.monotonic() - trace_started_at) * 1000))
            trace["finished_at"] = iso_utc() if not approval else None
            trace["updated_at"] = iso_utc()
            display_reply = format_web_chat_reply(
                mode=result.mode,
                reply=result.reply,
                used_tools=result.used_tools,
                tool_outputs=tool_outputs,
                rule_based_result=None,
            )
            task_trace_metadata = trace if has_audited_steps(trace) else None
            user_task_trace = serialize_user_task_trace(task_trace_metadata)
            save_successful_web_chat_turn(
                backend,
                payload.candidate_id,
                session_id,
                user_message,
                display_reply,
                {
                    "mode": result.mode,
                    "task_trace": user_task_trace,
                    "routing": result.routing,
                },
                account_id=account_id,
            )
            persist_tool_trace(
                backend,
                trace,
                account_id=account_id,
                candidate_id=payload.candidate_id,
                session_id=session_id,
                source="chat",
            )
            return {
                "mode": result.mode,
                "reply": result.reply,
                "display_reply": display_reply,
                "task_trace": user_task_trace,
                "background_tasks": public_background_tasks_from_tool_outputs(tool_outputs),
                "profile": asdict(backend.get_candidate_profile(payload.candidate_id, account_id=account_id)),
            }

        try:
            result = backend.ingest_conversation_message(
                payload.candidate_id,
                user_message,
                llm_client=None,
                auto_rebuild_rag=payload.auto_rag,
                account_id=account_id,
            )
        except RAGProviderRequestError as error:
            raise HTTPException(status_code=502, detail=str(error)) from error
        tool_outputs = [{"tool_name": "ingest_conversation_message", "data": asdict(result)}]
        trace = new_task_trace(root_request_id=root_request_id, source="chat")
        reconcile_task_trace(trace, tool_outputs)
        trace["status"] = task_trace_completion_status(trace, None)
        trace["duration_ms"] = max(0, round((time.monotonic() - trace_started_at) * 1000))
        trace["finished_at"] = iso_utc()
        trace["updated_at"] = iso_utc()
        display_reply = format_web_chat_reply(
            mode="rule_based_ingestion",
            reply=result.reply,
            used_tools=["ingest_conversation_message"],
            tool_outputs=tool_outputs,
            rule_based_result=asdict(result),
        )
        save_successful_web_chat_turn(
            backend,
            payload.candidate_id,
            session_id,
            user_message,
            display_reply,
            {
                "mode": "rule_based_ingestion",
                "task_trace": serialize_user_task_trace(trace),
            },
            account_id=account_id,
        )
        persist_tool_trace(
            backend,
            trace,
            account_id=account_id,
            candidate_id=payload.candidate_id,
            session_id=session_id,
            source="chat",
        )
        return {
            "mode": "rule_based_ingestion",
            "reply": result.reply,
            "display_reply": display_reply,
            "task_trace": serialize_user_task_trace(trace),
            "background_tasks": [],
            "profile": asdict(backend.get_candidate_profile(payload.candidate_id, account_id=account_id)),
        }

    @web_app.post("/api/chat/stream")
    def chat_stream(payload: ChatPayload, request: Request) -> StreamingResponse:
        """流式处理网页聊天消息。

        前端通过 fetch 读取 SSE 事件：

        - `token`：模型增量文本。
        - `status`：工具调用阶段提示。
        - `final`：完整结果，含工具摘要、档案更新和可持久化展示文本。
        - `error`：流式执行中的可读错误。
        """

        account = current_account(request)
        account_id = account.id if account else None
        get_profile_or_404(backend, payload.candidate_id, account_id)
        user_message = payload.message.strip()
        session_id = payload.session_id or default_web_session_id(payload.candidate_id, account_id)
        validate_chat_session(account_id, payload.candidate_id, session_id)
        if not user_message:
            raise HTTPException(status_code=400, detail="消息不能为空。")
        if payload.use_env_llm and chat_agent is None:
            raise HTTPException(
                status_code=400,
                detail=f"LangChain Agent 未就绪：{agent_error or '请检查 .env 配置'}",
            )

        return StreamingResponse(
            stream_web_chat_events(
                backend=backend,
                chat_agent=chat_agent,
                payload=payload,
                user_message=user_message,
                session_id=session_id,
                account_id=account_id,
            ),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-store, max-age=0",
                # 明确告诉常见代理不要缓冲 SSE；本地直连时也不会有副作用。
                "X-Accel-Buffering": "no",
            },
        )

    @web_app.get("/api/chat/history")
    def chat_history(
        request: Request,
        candidate_id: int = Query(...),
        session_id: str | None = Query(default=None),
        limit: int = Query(default=100, ge=1, le=500),
    ) -> dict[str, object]:
        """返回网页聊天历史，供页面刷新或重新打开时恢复对话。"""

        account = current_account(request)
        assert account is not None
        account_id = account.id
        get_profile_or_404(backend, candidate_id, account_id)
        actual_session_id = session_id or default_web_session_id(candidate_id, account_id)
        validate_chat_session(account_id, candidate_id, actual_session_id)
        return {
            "candidate_id": candidate_id,
            "session_id": actual_session_id,
            "messages": [
                {
                    **asdict(message),
                    "content": (
                        sanitize_web_chat_reply(message.content)
                        if message.role == "assistant"
                        else message.content
                    ),
                    "metadata": serialize_user_chat_metadata(message.metadata),
                }
                for message in backend.list_chat_messages(
                    candidate_id,
                    actual_session_id,
                    limit,
                    account_id=account_id,
                )
            ],
        }

    @web_app.post("/api/jobs")
    def import_job(payload: JobPayload, request: Request) -> dict[str, object]:
        """保存候选人从 BOSS 页面主动复制回来的职位文本。"""

        account = current_account(request)
        if not payload.raw_text.strip():
            raise HTTPException(status_code=400, detail="职位文本不能为空。")
        try:
            # 网页文本导入优先走可预测的本地审核和规则分类，不能因可选模型服务
            # 超时而阻塞用户导入职位。Agent 工具在用户允许模型时另行显式开启分类。
            job = backend.import_job_text(
                payload.raw_text,
                payload.source_url,
                account_id=account.id if account else None,
                classify_with_llm=False,
            )
        except InvalidJobTextError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        except DuplicateResourceError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        return {"job": asdict(job)}

    @web_app.post("/api/jobs/screenshots")
    async def import_job_screenshots(
        request: Request,
        screenshots: list[UploadFile] = File(...),  # noqa: B008 - FastAPI declares multipart fields this way.
        source_url: str | None = Form(default=None),
    ) -> dict[str, object]:
        """识别用户主动上传的可见职位截图，不访问来源链接。"""

        account = current_account(request)
        assert account is not None
        if len(screenshots) > MAX_JOB_SCREENSHOT_FILES:
            for screenshot in screenshots:
                await screenshot.close()
            raise HTTPException(status_code=400, detail=f"一次最多上传 {MAX_JOB_SCREENSHOT_FILES} 张职位截图。")

        try:
            screenshot_lease = await run_in_threadpool(
                backend.concurrency_controller.acquire,
                "screenshot",
                account_id=account.id,
                wait_timeout_seconds=0,
            )
        except ConcurrencyLimitExceeded as error:
            for screenshot in screenshots:
                await screenshot.close()
            raise HTTPException(
                status_code=429,
                detail=str(error),
                headers={"Retry-After": "1"},
            ) from error
        except ConcurrencyBackendUnavailable as error:
            for screenshot in screenshots:
                await screenshot.close()
            raise HTTPException(
                status_code=503,
                detail="并发保护服务暂时不可用，请稍后重试。",
                headers={"Retry-After": "1"},
            ) from error

        uploaded_screenshots: list[JobScreenshot] = []
        try:
            for screenshot in screenshots:
                # 多读一个字节即可拒绝超限文件，避免把任意大图片交给模型或长期保留。
                content = await screenshot.read(MAX_JOB_SCREENSHOT_FILE_BYTES + 1)
                uploaded_screenshots.append(
                    JobScreenshot(
                        content=content,
                        media_type=screenshot.content_type,
                    )
                )
            # `invoke()` 是同步 SDK 调用；转到线程池后，其他登录、聊天和任务轮询请求
            # 仍可由事件循环处理。Gateway 自己的超时配置限制这次前台等待时间。
            job = await run_in_threadpool(
                backend.import_job_screenshots,
                uploaded_screenshots,
                source_url.strip() if source_url and source_url.strip() else None,
                account_id=account.id,
            )
        except ModelCircuitOpenError as error:
            raise HTTPException(
                status_code=503,
                detail=str(error),
                headers={"Retry-After": str(error.retry_after_seconds)},
            ) from error
        except JobScreenshotModelError as error:
            raise HTTPException(status_code=502, detail=str(error)) from error
        except JobScreenshotError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        except InvalidJobTextError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        except DuplicateResourceError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        except FileInfectedError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        except FileScannerUnavailableError as error:
            raise HTTPException(status_code=503, detail=str(error), headers={"Retry-After": "10"}) from error
        finally:
            for screenshot in screenshots:
                await screenshot.close()
            screenshot_lease.release()
        return {
            "job": asdict(job),
            "extraction": {
                "source": "screenshot",
                "screenshot_count": len(uploaded_screenshots),
            },
        }

    @web_app.get("/api/jobs")
    def list_jobs(request: Request) -> dict[str, object]:
        """列出已经导入的职位。"""

        account = current_account(request)
        return {"jobs": [asdict(job) for job in backend.list_jobs(account_id=account.id if account else None)]}

    @web_app.put("/api/jobs/{job_id}/skill-requirements")
    def update_job_skill_requirements(
        job_id: int,
        payload: JobSkillRequirementsPayload,
        request: Request,
    ) -> dict[str, object]:
        """保存职位技能分类的人工校正，并返回更新后的职位。"""

        account = current_account(request)
        requirements = [
            SkillRequirement(
                name=item.name,
                category=item.category,
                confidence=item.confidence,
                evidence=item.evidence,
            )
            for item in payload.requirements
        ]
        try:
            job = backend.update_job_skill_requirements(
                job_id,
                requirements,
                account_id=account.id if account else None,
            )
        except KeyError as error:
            raise HTTPException(status_code=404, detail="职位不存在。") from error
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        return {"job": asdict(job)}

    @web_app.delete("/api/jobs/{job_id}")
    def delete_job(job_id: int, request: Request) -> dict[str, object]:
        """删除当前账号导入的职位及其职位相关文件。"""

        account = current_account(request)
        try:
            result = backend.delete_job(
                job_id,
                account_id=account.id if account else None,
            )
        except KeyError as error:
            raise HTTPException(status_code=404, detail="职位不存在。") from error
        return {"deleted": True, **result}

    @web_app.get("/api/matches/{candidate_id}")
    def list_matches(candidate_id: int, request: Request) -> dict[str, object]:
        """返回候选人与所有本地职位的匹配结果。"""

        account = current_account(request)
        account_id = account.id if account else None
        get_profile_or_404(backend, candidate_id, account_id)
        jobs_by_id = {job.id: job for job in backend.list_jobs(account_id=account_id)}
        matches = backend.match_all_jobs(candidate_id, account_id=account_id)
        return {
            "candidate_id": candidate_id,
            "matches": [
                {"job": asdict(jobs_by_id[match.job_id]), "match": asdict(match)}
                for match in matches
            ],
        }

    @web_app.get("/api/projects")
    def list_project_cards(
        request: Request,
        candidate_id: int = Query(...),
    ) -> dict[str, object]:
        """列出当前候选人的项目经历卡片，供网页复核与确认。"""

        account = current_account(request)
        assert account is not None
        get_profile_or_404(backend, candidate_id, account.id)
        return {
            "project_cards": [
                asdict(record)
                for record in backend.list_project_cards(candidate_id, account_id=account.id)
            ]
        }

    @web_app.delete("/api/projects/{record_id}")
    def delete_project_card(
        record_id: int,
        request: Request,
    ) -> dict[str, object]:
        """删除当前账号的一张项目经历卡片及其摘要证据。"""

        account = current_account(request)
        assert account is not None
        try:
            result = backend.delete_project_card(record_id, account_id=account.id)
        except KeyError as error:
            raise HTTPException(status_code=404, detail="项目经历卡片不存在。") from error
        return {"deleted": True, **result}

    @web_app.post("/api/projects/github")
    def analyze_github_project(
        payload: GitHubProjectPayload,
        request: Request,
    ) -> dict[str, object]:
        """登记公开 GitHub 仓库分析，并优先交由后台 Worker 处理。"""

        account = current_account(request)
        assert account is not None
        account_id = account.id
        get_profile_or_404(backend, payload.candidate_id, account_id)
        try:
            if backend.task_queue_enabled:
                request_id = uuid.uuid4().hex
                task = backend.enqueue_github_project_analysis_task(
                    repository_url=payload.repository_url,
                    account_id=account_id,
                    candidate_id=payload.candidate_id,
                    session_id=f"github-project-candidate-{payload.candidate_id}",
                    root_request_id=request_id,
                )
                record_background_task_enqueue_trace(
                    backend,
                    task=task,
                    root_request_id=request_id,
                    source="github_project_page",
                )
                return {
                    "task": serialize_background_task(task),
                    "project_card": None,
                    "processing_async": True,
                    "root_request_id": request_id,
                }
            # 纯本地测试或显式关闭队列时保留同步兼容，正式 Docker Web 默认不会走这里。
            project_card = backend.analyze_github_project_for_candidate(
                payload.candidate_id,
                payload.repository_url,
                account_id=account_id,
            )
        except GitHubRepositoryError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        except DuplicateResourceError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        except FileInfectedError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        except FileScannerUnavailableError as error:
            raise HTTPException(status_code=503, detail=str(error), headers={"Retry-After": "10"}) from error
        except TaskQueueError as error:
            raise HTTPException(status_code=503, detail=str(error)) from error
        return {
            "task": None,
            "project_card": asdict(project_card),
            "processing_async": False,
        }

    @web_app.post("/api/projects/local/manifest")
    def create_local_project_manifest(
        payload: LocalProjectManifestPayload,
        request: Request,
    ) -> dict[str, object]:
        """Accept metadata and hashes only, then return the server-side collection plan."""

        account = current_account(request)
        assert account is not None
        get_profile_or_404(backend, payload.candidate_id, account.id)
        try:
            session, files = backend.create_local_project_collection(
                payload.candidate_id,
                payload.project_name,
                [
                    ProjectManifestItem(
                        relative_path=item.relative_path,
                        file_size=item.file_size,
                        sha256=item.sha256,
                        media_type=item.media_type,
                        last_modified=item.last_modified,
                    )
                    for item in payload.files
                ],
                account_id=account.id,
            )
        except ProjectEvidenceError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        except DuplicateResourceError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        return {
            "collection": asdict(session),
            "files": [asdict(item) for item in files],
            "selected_files": [
                asdict(item) for item in files if item.selection_status == "selected"
            ],
            "resumed": any(
                item.selection_status == "analyzed" for item in files
            ) or session.uploaded_file_count > 0,
        }

    @web_app.get("/api/projects/local/{collection_id}")
    def get_local_project_collection(
        collection_id: int,
        request: Request,
    ) -> dict[str, object]:
        """Return resumable collection progress for the signed-in account."""

        account = current_account(request)
        assert account is not None
        try:
            session = backend.store.get_project_collection(collection_id, account_id=account.id)
            files = backend.store.list_project_collection_files(collection_id, account_id=account.id)
        except KeyError as error:
            raise HTTPException(status_code=404, detail="本地项目采集会话不存在。") from error
        return {"collection": asdict(session), "files": [asdict(item) for item in files]}

    @web_app.delete("/api/projects/local/{collection_id}")
    def delete_local_project_collection(
        collection_id: int,
        request: Request,
    ) -> dict[str, object]:
        """取消未完成的本地目录采集，释放其文本、视觉对象与派生索引。"""

        account = current_account(request)
        assert account is not None
        try:
            result = backend.delete_incomplete_local_project_collection(
                collection_id,
                account_id=account.id,
            )
        except KeyError as error:
            raise HTTPException(status_code=404, detail="本地项目采集会话不存在。") from error
        except ValueError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        return {"deleted": True, **result}

    @web_app.post("/api/projects/local/{collection_id}/files")
    async def upload_local_project_files(
        collection_id: int,
        request: Request,
        file_ids: str = Form(...),
        files: list[UploadFile] = File(...),  # noqa: B008 - FastAPI multipart declaration.
    ) -> dict[str, object]:
        """Verify and process one browser batch, then enqueue its incremental RAG update."""

        account = current_account(request)
        assert account is not None
        try:
            parsed_ids = json.loads(file_ids)
        except json.JSONDecodeError as error:
            raise HTTPException(status_code=400, detail="项目文件批次标识无效。") from error
        if not isinstance(parsed_ids, list) or not parsed_ids or len(parsed_ids) != len(files):
            raise HTTPException(status_code=400, detail="项目文件批次与采集计划不一致。")
        if len(files) > 5:
            raise HTTPException(status_code=400, detail="每批最多处理 5 个项目文件。")

        processed = []
        failures = []
        for raw_id, upload in zip(parsed_ids, files, strict=True):
            try:
                file_id = int(raw_id)
                planned = backend.store.get_project_collection_file(
                    file_id,
                    collection_id=collection_id,
                    account_id=account.id,
                )
                content = await upload.read(planned.file_size + 1)
                await upload.close()
                if len(content) > planned.file_size:
                    raise ValueError("项目文件大小与预扫描清单不一致。")
                result = await run_in_threadpool(
                    backend.process_local_project_collection_file,
                    collection_id,
                    file_id,
                    content,
                    account_id=account.id,
                )
                processed.append(result)
            except FileScannerUnavailableError as error:
                raise HTTPException(
                    status_code=503,
                    detail=str(error),
                    headers={"Retry-After": "10"},
                ) from error
            except (ProjectEvidenceError, FileInfectedError, ValueError, KeyError) as error:
                try:
                    failed = backend.store.fail_project_collection_file(
                        int(raw_id),
                        collection_id=collection_id,
                        account_id=account.id,
                        reason=str(error),
                    )
                    failures.append(asdict(failed))
                except (ValueError, KeyError):
                    failures.append({"id": raw_id, "selection_reason": str(error)})

        long_text_ids = [int(item.long_text_id) for item in processed if item.long_text_id]
        collection = backend.store.get_project_collection(
            collection_id,
            account_id=account.id,
        )
        visual_items = backend.store.list_visual_knowledge_items(
            account_id=account.id,
            project_collection_file_ids=[item.id for item in processed],
        )
        rag_task = None
        visual_task = None
        visual_index = None
        visual_warning = None
        if long_text_ids:
            if backend.task_queue_enabled:
                id_source = ",".join(str(item.id) for item in processed)
                rag_task = backend.enqueue_rag_index_task(
                    long_text_ids=long_text_ids,
                    account_id=account.id,
                    candidate_id=collection.candidate_id,
                    session_id=f"local-project-collection-{collection_id}",
                    root_request_id=uuid.uuid4().hex,
                    idempotency_key=(
                        "local-project-rag:"
                        f"{collection_id}:{hashlib.sha256(id_source.encode('ascii')).hexdigest()}"
                    ),
                )
            else:
                backend.index_rag_long_texts(long_text_ids, account_id=account.id)
        if visual_items and backend.visual_embedding_enabled:
            visual_id_source = ",".join(str(item.id) for item in visual_items)
            if backend.task_queue_enabled:
                visual_task = backend.enqueue_visual_index_task(
                    visual_item_ids=[item.id for item in visual_items],
                    account_id=account.id,
                    candidate_id=collection.candidate_id,
                    session_id=f"local-project-collection-{collection_id}",
                    root_request_id=uuid.uuid4().hex,
                    idempotency_key=(
                        "local-project-visual:"
                        f"{collection_id}:{hashlib.sha256(visual_id_source.encode('ascii')).hexdigest()}"
                    ),
                )
            else:
                try:
                    visual_index = asdict(
                        backend.index_visual_knowledge_items(
                            [item.id for item in visual_items],
                            account_id=account.id,
                            candidate_id=collection.candidate_id,
                            session_id=f"local-project-collection-{collection_id}",
                        )
                    )
                except (RAGProviderRequestError, ValueError) as error:
                    visual_warning = f"项目证据已保存，但视觉索引失败：{error}"
        elif visual_items:
            visual_warning = "项目视觉副本已保存；配置多模态 Embedding 后可建立图片向量。"
        return {
            "processed_files": [asdict(item) for item in processed],
            "failed_files": failures,
            "rag_task": serialize_background_task(rag_task) if rag_task is not None else None,
            "visual_task": (
                serialize_background_task(visual_task)
                if visual_task is not None
                else None
            ),
            "visual_index": visual_index,
            "visual_warning": visual_warning,
        }

    @web_app.post("/api/projects/local/{collection_id}/complete")
    def complete_local_project_collection(
        collection_id: int,
        request: Request,
    ) -> dict[str, object]:
        """Create the pending project card after the planned batches finish."""

        account = current_account(request)
        assert account is not None
        try:
            project_card = backend.complete_local_project_collection(
                collection_id,
                account_id=account.id,
            )
        except KeyError as error:
            raise HTTPException(status_code=404, detail="本地项目采集会话不存在。") from error
        except ValueError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        return {"project_card": asdict(project_card), "processing_async": False}

    @web_app.post("/api/projects/{record_id}/confirm")
    def confirm_project_card(
        record_id: int,
        payload: ProjectCardConfirmationPayload,
        request: Request,
    ) -> dict[str, object]:
        """确认当前账号的一张项目经历卡片，不回写候选人档案事实。"""

        account = current_account(request)
        assert account is not None
        try:
            pending_record = backend.store.get_project_card(record_id, account_id=account.id)
        except KeyError as error:
            raise HTTPException(status_code=404, detail="项目经历卡片不存在。") from error
        request_id = owned_or_new_root_request_id(
            backend,
            account_id=account.id,
            candidate_id=pending_record.candidate_id,
            root_request_id=payload.root_request_id,
        )
        try:
            record, rag_task = backend.confirm_project_card_and_enqueue_rag(
                record_id,
                payload.confirmed_summary.strip() if payload.confirmed_summary else None,
                account_id=account.id,
                session_id=f"project-confirmation-{record_id}",
                root_request_id=request_id,
            )
        except KeyError as error:
            raise HTTPException(status_code=404, detail="项目经历卡片不存在。") from error
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        except TaskQueueError as error:
            raise HTTPException(status_code=503, detail=str(error)) from error
        update_confirmation_tool_trace(
            backend,
            root_request_id=request_id,
            account_id=account.id,
            candidate_id=record.candidate_id,
            session_id=f"project-confirmation-{record_id}",
            record=asdict(record),
            rag_task=serialize_background_task(rag_task) if rag_task is not None else None,
        )
        return {
            "project_card": asdict(record),
            "task": serialize_background_task(rag_task) if rag_task is not None else None,
            "root_request_id": request_id,
        }

    @web_app.post("/api/resumes/upload")
    async def upload_resume(
        request: Request,
        candidate_id: int = Form(...),
        file: UploadFile = File(...),  # noqa: B008 - FastAPI declares multipart fields this way.
    ) -> dict[str, object]:
        """上传简历，并按文件类型安排同步解析、OCR 或 RAG 增量索引。"""

        account = current_account(request)
        assert account is not None
        account_id = account.id
        get_profile_or_404(backend, candidate_id, account_id)
        filename = file.filename or "resume"
        # 多读一个字节即可判断超限，避免把任意大文件一次性长期保留在内存中。
        content = await file.read(MAX_RESUME_FILE_BYTES + 1)
        try:
            artifact = backend.upload_resume_document(
                candidate_id,
                filename,
                content,
                account_id=account_id,
                # 队列模式仅把扫描 PDF 的 OCR 放到 Worker；DOCX/文字 PDF 仍立即校验并保存。
                defer_ocr=backend.task_queue_enabled,
            )
        except ResumeDocumentError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        except DuplicateResourceError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        except FileInfectedError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        except FileScannerUnavailableError as error:
            raise HTTPException(status_code=503, detail=str(error), headers={"Retry-After": "10"}) from error

        rag_update = None
        rag_warning = None
        background_task = None
        root_request_id = uuid.uuid4().hex
        workflow = "ready"
        if artifact.status == "processing":
            workflow = "ocr"
            idempotency_key = f"resume-ocr:{artifact.id}"
            try:
                background_task = backend.enqueue_resume_ocr_task(
                    artifact_id=artifact.id,
                    account_id=account_id,
                    candidate_id=candidate_id,
                    session_id=f"resume-upload-candidate-{candidate_id}",
                    root_request_id=root_request_id,
                    idempotency_key=idempotency_key,
                )
                record_background_task_enqueue_trace(
                    backend,
                    task=background_task,
                    root_request_id=root_request_id,
                    source="resume_upload",
                )
            except TaskQueueError as error:
                # 原件已保存但没有可用 Worker 时，不能继续显示“处理中”。
                artifact = backend.fail_resume_ocr_artifact(
                    artifact_id=artifact.id,
                    account_id=account_id,
                )
                rag_warning = f"简历已保存，但 OCR 后台任务投递失败：{error}"
                background_task = backend.store.get_background_task_by_idempotency(
                    account_id,
                    idempotency_key,
                )
        elif artifact.long_text_id is not None:
            if backend.task_queue_enabled:
                workflow = "rag"
                # 任务只保存长文本资源引用；正文留在 PostgreSQL/对象存储，不进入 Redis。
                idempotency_key = f"resume-rag:{artifact.id}"
                try:
                    background_task = backend.enqueue_rag_index_task(
                        long_text_ids=[artifact.long_text_id],
                        account_id=account_id,
                        candidate_id=candidate_id,
                        session_id=f"resume-upload-candidate-{candidate_id}",
                        root_request_id=root_request_id,
                        idempotency_key=idempotency_key,
                    )
                    record_background_task_enqueue_trace(
                        backend,
                        task=background_task,
                        root_request_id=root_request_id,
                        source="resume_upload",
                    )
                except TaskQueueError as error:
                    # 原文件已经保存；把投递失败作为可见警告返回，避免用户误以为文件丢失。
                    rag_warning = f"简历已保存，但 RAG 后台任务投递失败：{error}"
                    background_task = backend.store.get_background_task_by_idempotency(
                        account_id,
                        idempotency_key,
                    )
            else:
                try:
                    rag_update = asdict(
                        backend.index_rag_long_texts(
                            [artifact.long_text_id],
                            account_id=account_id,
                            candidate_id=candidate_id,
                            session_id=f"resume-upload-candidate-{candidate_id}",
                            root_request_id=root_request_id,
                        )
                    )
                except RAGProviderRequestError as error:
                    # 文件与 PostgreSQL 正文已经安全保存；索引失败单独告知，避免用户重复上传。
                    rag_warning = f"简历已保存，但 RAG 增量索引失败：{error}"
        return {
            "artifact": serialize_resume_artifact(artifact),
            "rag_update": rag_update,
            "warning": rag_warning,
            "task": serialize_background_task(background_task) if background_task is not None else None,
            "workflow": workflow,
            "processing_async": bool(background_task is not None),
            "indexing_async": workflow == "rag" and background_task is not None,
            "root_request_id": root_request_id if background_task is not None or rag_update is not None else None,
        }

    @web_app.get("/api/resumes")
    def list_resumes(
        request: Request,
        candidate_id: int = Query(...),
    ) -> dict[str, object]:
        """列出当前账号指定候选人的全部简历文件版本。"""

        account = current_account(request)
        try:
            artifacts = backend.list_resume_artifacts(
                candidate_id,
                account_id=account.id if account else None,
            )
        except KeyError as error:
            raise HTTPException(status_code=404, detail="候选人档案不存在。") from error
        return {"artifacts": [serialize_resume_artifact(item) for item in artifacts]}

    @web_app.delete("/api/resumes/{artifact_id}")
    def delete_resume(artifact_id: int, request: Request) -> dict[str, object]:
        """删除当前账号的一份原始或职位定制简历文件。"""

        account = current_account(request)
        account_id = account.id if account else None
        try:
            result = backend.delete_resume_artifact(
                artifact_id,
                account_id=account_id,
            )
        except KeyError as error:
            # 不区分“不存在”和“属于其他账号”，避免通过 ID 探测文件归属。
            raise HTTPException(status_code=404, detail="简历文件不存在。") from error
        return {
            "artifact_id": artifact_id,
            "deleted": True,
            "rag_deleted_chunks": result.get("rag_deleted_chunks", 0),
            "rag_warning": result.get("rag_warning"),
        }

    @web_app.get("/api/tasks/{task_key}")
    def get_task(task_key: str, request: Request) -> dict[str, object]:
        """返回当前账号可见的后台任务状态，不回显任务输入正文。"""

        account = current_account(request)
        try:
            task = backend.get_background_task(task_key, account_id=account.id if account else None)
        except KeyError as error:
            raise HTTPException(status_code=404, detail="后台任务不存在。") from error
        return {"task": serialize_background_task(task)}

    @web_app.get("/api/resumes/{artifact_id}/download")
    def download_resume(artifact_id: int, request: Request) -> StreamingResponse:
        """鉴权后下载原始或职位定制简历文件。"""

        account = current_account(request)
        try:
            artifact = backend.get_resume_artifact(
                artifact_id,
                account_id=account.id if account else None,
            )
        except KeyError as error:
            # 不区分“ID 不存在”和“属于其他账号”，避免泄露资源是否存在。
            raise HTTPException(status_code=404, detail="简历文件不存在。") from error
        try:
            content = backend.stream_resume_file(artifact)
        except KeyError as error:
            raise HTTPException(
                status_code=404,
                detail="简历文件已丢失，请重新上传或生成。",
            ) from error
        # 所有文件都经当前账号鉴权后由 Web 代理返回，避免公开对象存储永久链接。
        filename = quote(artifact.download_filename, safe="")
        return StreamingResponse(
            content,
            media_type=artifact.media_type,
            headers={
                "Content-Disposition": (
                    f"attachment; filename=resume; filename*=UTF-8''{filename}"
                )
            },
        )

    @web_app.post("/api/resumes/{artifact_id}/tailor")
    def tailor_resume(
        artifact_id: int,
        payload: TailorResumePayload,
        request: Request,
    ) -> dict[str, object]:
        """调用证据约束改写并生成可下载 DOCX/PDF，不覆盖上传源文件。"""

        account = current_account(request)
        account_id = account.id if account else None
        try:
            source = backend.get_resume_artifact(artifact_id, account_id=account_id)
        except KeyError as error:
            raise HTTPException(status_code=404, detail="原始简历文件不存在。") from error

        request_id = uuid.uuid4().hex
        session_id = f"resume-web-{source.candidate_id}"
        if backend.task_queue_enabled:
            try:
                task = backend.enqueue_resume_export_task(
                    source_artifact_id=source.id,
                    job_id=payload.job_id,
                    account_id=account_id or 0,
                    candidate_id=source.candidate_id,
                    use_rag=payload.use_rag,
                    session_id=session_id,
                    root_request_id=request_id,
                )
            except KeyError as error:
                raise HTTPException(status_code=404, detail="职位、候选人或简历文件不存在。") from error
            except ValueError as error:
                raise HTTPException(status_code=400, detail=str(error)) from error
            except TaskQueueError as error:
                raise HTTPException(
                    status_code=503,
                    detail="简历生成任务暂时无法排队，请稍后重试。",
                    headers={"Retry-After": "3"},
                ) from error
            task_root_request_id = task.payload.get("root_request_id")
            return {
                "processing_async": True,
                "artifact_id": source.id,
                "root_request_id": task_root_request_id or request_id,
                "task": serialize_background_task(task),
            }

        active_llm = resume_llm_client
        if active_llm is None:
            context = {
                "candidate_id": source.candidate_id,
                "account_id": account_id,
                "session_id": session_id,
                "root_request_id": request_id,
                "use_tool_llm": True,
                "default_auto_rag": True,
            }
            try:
                active_llm = backend.model_gateway.llm_client(
                    backend.model_gateway.new_call_context(
                        "resume_document_rewrite",
                        account_id=account_id,
                        candidate_id=source.candidate_id,
                        session_id=context["session_id"],
                        root_request_id=request_id,
                    )
                )
            except ValueError as error:
                raise HTTPException(status_code=400, detail=f"简历改写模型未就绪：{error}") from error

        try:
            result = backend.create_tailored_resume_from_artifact(
                candidate_id=source.candidate_id,
                source_artifact_id=source.id,
                job_id=payload.job_id,
                llm_client=active_llm,
                use_rag=payload.use_rag,
                # 网页按钮始终采用档案熟练度；一次性放宽只允许 Agent 完成风险确认后调用。
                allow_proficiency_upgrade=False,
                account_id=account_id,
                session_id=session_id,
                root_request_id=request_id,
            )
        except KeyError as error:
            raise HTTPException(status_code=404, detail="职位、候选人或简历文件不存在。") from error
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        except ModelCircuitOpenError as error:
            raise HTTPException(
                status_code=503,
                detail=str(error),
                headers={"Retry-After": str(error.retry_after_seconds)},
            ) from error
        except (RAGProviderRequestError, LLMRequestError) as error:
            raise HTTPException(status_code=502, detail=str(error)) from error
        return {
            "draft": asdict(result.draft),
            "artifacts": [serialize_resume_artifact(item) for item in result.artifacts],
        }

    @web_app.get("/api/rag/search")
    def search_rag(
        request: Request,
        query: str = Query(...),
        top_n: int = Query(DEFAULT_RAG_RERANK_TOP_N, ge=1, le=MAX_RAG_RERANK_TOP_N),
    ) -> dict[str, object]:
        """检索本地 RAG 最终 Top-N 证据片段。"""

        account = current_account(request)
        try:
            results = backend.search_rag(query, top_n=top_n, account_id=account.id if account else None)
        except RAGProviderRequestError as error:
            raise HTTPException(status_code=502, detail=str(error)) from error
        return {"query": query, "results": [asdict(result) for result in results]}

    @web_app.get("/api/admin/accounts")
    def admin_accounts(request: Request) -> dict[str, object]:
        """管理员查看账号状态，不返回密码、档案或对话正文。"""

        require_admin(request)
        return {"accounts": [asdict(account) for account in backend.store.list_accounts()]}

    @web_app.patch("/api/admin/accounts/{account_id}/status")
    def admin_account_status(
        account_id: int,
        payload: AccountStatusPayload,
        request: Request,
    ) -> dict[str, object]:
        """管理员启用或禁用账号；禁用会立即撤销其全部 Session。"""

        actor = require_admin(request)
        if payload.status not in {"active", "disabled"}:
            raise HTTPException(status_code=400, detail="状态只能是 active 或 disabled。")
        try:
            target = backend.store.get_account(account_id)
        except KeyError as error:
            raise HTTPException(status_code=404, detail="账号不存在。") from error
        if payload.status == "disabled" and target.id == actor.id:
            raise HTTPException(status_code=400, detail="不能禁用当前正在使用的管理员账号。")
        if (
            payload.status == "disabled"
            and target.role == "admin"
            and backend.store.count_active_admins() <= 1
        ):
            raise HTTPException(status_code=400, detail="至少需要保留一个可用管理员账号。")
        audit_event = AdminAuditEventRecord(
            id=0,
            actor_account_id=actor.id,
            target_account_id=target.id,
            action="account.status_updated",
            target_type="account",
            target_id=str(target.id),
            outcome="succeeded",
            summary=f"账号 #{target.id} 状态从 {target.status} 更新为 {payload.status}。",
            details={
                "previous_status": target.status,
                "next_status": payload.status,
                "target_role": target.role,
            },
            request_id=getattr(request.state, "request_id", None),
        )
        account = backend.store.update_account_status_with_audit(
            account_id,
            payload.status,
            audit_event,
            revoke_sessions=payload.status != "active",
        )
        return {"account": asdict(account)}

    @web_app.get("/api/admin/usage/summary")
    def admin_usage_summary(request: Request) -> dict[str, object]:
        """管理员查看全局 Token 和工具调用汇总。"""

        require_admin(request)
        prune_admin_retention_window()
        billing_settings = load_billing_settings(env_path)
        balance_projections, balance_summary = project_account_balances(
            backend.store.list_accounts(),
            backend.store.summarize_account_balances(),
            price_per_million_tokens_yuan=billing_settings.price_per_million_tokens_yuan,
            starting_balance_yuan=billing_settings.starting_balance_yuan,
            low_balance_threshold_yuan=billing_settings.low_balance_threshold_yuan,
        )
        return {
            "summary": backend.store.summarize_usage(),
            "by_account": backend.store.summarize_usage_by_account(),
            "tool_calls_by_account": backend.store.summarize_tool_call_traces_by_account(),
            "billing": {
                "settings": masked_billing_settings(billing_settings),
                "summary": balance_summary_to_dict(balance_summary),
                "by_account": [balance_projection_to_dict(projection) for projection in balance_projections],
            },
            "page_size": ADMIN_LEDGER_PAGE_SIZE,
            "max_pages": ADMIN_LEDGER_MAX_PAGES,
        }

    @web_app.get("/api/admin/balance/events")
    def admin_balance_events(
        request: Request,
        account_id: int | None = Query(default=None),
        limit: int = Query(default=ADMIN_LEDGER_PAGE_SIZE, ge=1, le=ADMIN_LEDGER_PAGE_SIZE),
        offset: int = Query(default=0, ge=0),
    ) -> dict[str, object]:
        """管理员分页查看账号余额与消费流水。"""

        require_admin(request)
        entries = backend.store.list_account_balance_ledger(
            account_id,
            limit=limit,
            offset=offset,
        )
        total = backend.store.count_account_balance_ledger(account_id)
        return {
            "entries": [asdict(entry) for entry in entries],
            "total": total,
            "limit": limit,
            "offset": offset,
            "page_size": ADMIN_LEDGER_PAGE_SIZE,
            "max_pages": None,
        }

    @web_app.post("/api/admin/accounts/{account_id}/balance/credit")
    def admin_credit_account_balance(
        account_id: int,
        payload: AdminBalanceCreditPayload,
        request: Request,
    ) -> dict[str, object]:
        """管理员不经过支付渠道为自己或任意账号补款。"""

        actor = require_admin(request)
        try:
            target = backend.store.get_account(account_id)
        except KeyError as error:
            raise HTTPException(status_code=404, detail="账号不存在。") from error
        clean_reason = payload.reason.strip()
        audit_event = AdminAuditEventRecord(
            id=0,
            actor_account_id=actor.id,
            target_account_id=target.id,
            action="balance.manual_credit",
            target_type="account_balance",
            target_id=str(target.id),
            outcome="succeeded",
            summary=f"管理员为账号 #{target.id} 执行人工补款。",
            details={"target_role": target.role},
            request_id=getattr(request.state, "request_id", None),
        )
        try:
            entry = backend.store.credit_account_balance_with_audit(
                target.id,
                payload.amount_yuan,
                actor_account_id=actor.id,
                reason=clean_reason,
                idempotency_key=payload.idempotency_key,
                audit_event=audit_event,
            )
        except IdempotencyConflictError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        except (PermissionError, ValueError) as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        return {
            "entry": asdict(entry),
            "summary": asdict(backend.store.get_account_balance_summary(target.id)),
        }

    @web_app.get("/api/admin/recharge/orders")
    def admin_recharge_orders(
        request: Request,
        account_id: int | None = Query(default=None),
        limit: int = Query(default=ADMIN_LEDGER_PAGE_SIZE, ge=1, le=ADMIN_LEDGER_PAGE_SIZE),
        offset: int = Query(default=0, ge=0),
    ) -> dict[str, object]:
        """管理员分页查看充值订单，便于排查用户充值问题。"""

        require_admin(request)
        if account_id is not None:
            try:
                backend.store.get_account(account_id)
            except KeyError as error:
                raise HTTPException(status_code=404, detail="账号不存在。") from error
        orders = backend.store.list_recharge_orders(account_id, limit=limit, offset=offset)
        return {
            "orders": [asdict(order) for order in orders],
            "total": backend.store.count_recharge_orders(account_id),
            "limit": limit,
            "offset": offset,
            "page_size": ADMIN_LEDGER_PAGE_SIZE,
            "max_pages": None,
        }

    @web_app.get("/api/admin/recharge/orders/{order_id}/events")
    def admin_recharge_order_events(order_id: int, request: Request) -> dict[str, object]:
        """管理员查看充值订单对应的低敏支付事件。"""

        require_admin(request)
        try:
            order = backend.store.get_recharge_order(order_id)
        except KeyError as error:
            raise HTTPException(status_code=404, detail="充值订单不存在。") from error
        return {
            "order": asdict(order),
            "events": [asdict(event) for event in backend.store.list_payment_events(order_id)],
        }

    @web_app.get("/api/me/balance")
    def my_balance(
        request: Request,
        limit: int = Query(default=ADMIN_LEDGER_PAGE_SIZE, ge=1, le=ADMIN_LEDGER_PAGE_SIZE),
        offset: int = Query(default=0, ge=0),
    ) -> dict[str, object]:
        """当前登录账号查看自己的余额与消费流水。"""

        account = current_account(request)
        summary = backend.store.get_account_balance_summary(account.id)
        entries = backend.store.list_account_balance_ledger(account.id, limit=limit, offset=offset)
        total = backend.store.count_account_balance_ledger(account.id)
        return {
            "summary": asdict(summary),
            "entries": [asdict(entry) for entry in entries],
            "total": total,
            "limit": limit,
            "offset": offset,
            "page_size": ADMIN_LEDGER_PAGE_SIZE,
            "max_pages": None,
            "settings": masked_billing_settings(load_billing_settings(env_path)),
        }

    @web_app.get("/api/me/recharge/orders")
    def my_recharge_orders(
        request: Request,
        limit: int = Query(default=ADMIN_LEDGER_PAGE_SIZE, ge=1, le=ADMIN_LEDGER_PAGE_SIZE),
        offset: int = Query(default=0, ge=0),
    ) -> dict[str, object]:
        """当前账号分页查看自己的充值订单。"""

        account = current_account(request)
        orders = backend.store.list_recharge_orders(account.id, limit=limit, offset=offset)
        return {
            "orders": [asdict(order) for order in orders],
            "total": backend.store.count_recharge_orders(account.id),
            "limit": limit,
            "offset": offset,
        }

    @web_app.post("/api/me/balance/recharge")
    def recharge_my_balance(payload: BalanceRechargePayload, request: Request) -> dict[str, object]:
        """当前账号发起一次本地模拟充值。"""

        account = current_account(request)
        if load_web_security_settings(env_path).environment == "production":
            raise HTTPException(status_code=503, detail="真实支付尚未接入，生产环境暂不开放模拟充值。")
        try:
            order, entry = backend.store.create_simulated_recharge_order(
                account.id,
                payload.amount_yuan,
                idempotency_key=payload.idempotency_key or uuid.uuid4().hex,
                description=(payload.note or "个人中心模拟充值").strip(),
            )
        except IdempotencyConflictError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        return {
            "order": asdict(order),
            "entry": asdict(entry),
            "summary": asdict(backend.store.get_account_balance_summary(account.id)),
        }

    @web_app.get("/api/admin/observability/requests")
    def admin_request_metrics(request: Request) -> dict[str, object]:
        """管理员查看当前 Web 进程的低敏请求指标。"""

        require_admin(request)
        email_records = backend.store.list_account_email_outbox(limit=20)
        return {
            "requests": web_app.state.request_metrics.snapshot(),
            "account_email": {
                "summary": backend.store.summarize_account_email_outbox(),
                "records": [
                    {
                        "id": record.id,
                        "account_id": record.account_id,
                        "purpose": record.purpose,
                        "recipient_email": redact_email(record.recipient_email),
                        "status": record.status,
                        "attempt_count": record.attempt_count,
                        "max_attempts": record.max_attempts,
                        "last_error_type": record.last_error_type,
                        "last_error_summary": record.last_error_summary,
                        "created_at": record.created_at,
                        "updated_at": record.updated_at,
                    }
                    for record in email_records
                ],
            },
        }

    @web_app.get("/api/admin/audit/events")
    def admin_audit_events(
        request: Request,
        limit: int = Query(default=50, ge=1, le=200),
        offset: int = Query(default=0, ge=0),
    ) -> dict[str, object]:
        """管理员查看最近的低敏管理操作审计事件。"""

        require_admin(request)
        events = backend.store.list_admin_audit_events(limit=limit, offset=offset)
        return {
            "events": [asdict(event) for event in events],
            "limit": limit,
            "offset": offset,
        }

    @web_app.post("/api/admin/tasks/probe")
    def admin_task_queue_probe(request: Request) -> dict[str, object]:
        """管理员登记一个无业务数据的 Worker 探针，用于运维验证。"""

        actor = require_admin(request)
        task: BackgroundTaskRecord | None = None
        try:
            task = backend.enqueue_system_probe(
                actor.id,
                audit_event=AdminAuditEventRecord(
                    id=0,
                    actor_account_id=actor.id,
                    target_account_id=None,
                    action="system.probe_enqueued",
                    target_type="background_task",
                    target_id=None,
                    outcome="succeeded",
                    summary="投递系统探针任务。",
                    details={},
                    request_id=getattr(request.state, "request_id", None),
                ),
            )
        except TaskQueueError as error:
            raise HTTPException(status_code=503, detail=str(error)) from error
        assert task is not None
        return {"task": serialize_background_task(task)}

    @web_app.get("/api/admin/usage/events")
    def admin_usage_events(
        request: Request,
        account_id: int | None = Query(default=None),
        candidate_id: int | None = Query(default=None),
        session_id: str | None = Query(default=None),
        limit: int = Query(default=ADMIN_LEDGER_PAGE_SIZE, ge=1, le=ADMIN_LEDGER_PAGE_SIZE),
        offset: int = Query(default=0, ge=0),
    ) -> dict[str, object]:
        """管理员查看用量流水，保留来源、操作类型和是否可计费状态。"""

        require_admin(request)
        prune_admin_retention_window()
        events = backend.store.list_usage_events(
            account_id,
            candidate_id,
            session_id,
            limit,
            offset,
        )
        total = backend.store.count_usage_events(account_id, candidate_id, session_id)
        return {
            "events": [asdict(event) for event in events],
            "total": total,
            "limit": limit,
            "offset": offset,
            "page_size": ADMIN_LEDGER_PAGE_SIZE,
            "max_pages": ADMIN_LEDGER_MAX_PAGES,
        }

    @web_app.get("/api/admin/tools/traces")
    def admin_tool_traces(
        request: Request,
        account_id: int | None = Query(default=None),
        limit: int = Query(default=ADMIN_LEDGER_PAGE_SIZE, ge=1, le=ADMIN_LEDGER_PAGE_SIZE),
        offset: int = Query(default=0, ge=0),
    ) -> dict[str, object]:
        """管理员分页查看最近固定窗口内的工具调用任务摘要。"""

        require_admin(request)
        prune_admin_retention_window()
        traces = backend.store.list_tool_call_traces(
            account_id,
            limit=limit,
            offset=offset,
        )
        total = backend.store.count_tool_call_traces(account_id)
        return {
            "traces": [serialize_tool_trace_summary(trace) for trace in traces],
            "total": total,
            "limit": limit,
            "offset": offset,
            "page_size": ADMIN_LEDGER_PAGE_SIZE,
            "max_pages": ADMIN_LEDGER_MAX_PAGES,
        }

    @web_app.get("/api/admin/tools/traces/{root_request_id}")
    def admin_tool_trace_detail(root_request_id: str, request: Request) -> dict[str, object]:
        """管理员按需查看某次任务的工具调用流程和安全结果摘要。"""

        require_admin(request)
        prune_admin_retention_window()
        try:
            trace = backend.store.get_tool_call_trace(
                root_request_id,
            )
        except KeyError as error:
            raise HTTPException(status_code=404, detail="工具调用记录不存在。") from error
        return {"trace": serialize_tool_trace_detail(trace)}

    install_http_tracing(
        web_app,
        settings=load_observability_settings(env_path),
        service_name="job-hunting-web",
    )
    return web_app


def stream_web_chat_events(
    backend: JobHuntingApp,
    chat_agent: JobHuntingAgent | None,
    payload: ChatPayload,
    user_message: str,
    session_id: str,
    account_id: int,
):
    """生成网页聊天 SSE 事件。

    这个函数集中处理“流式展示”和“最终落库”的顺序：只有成功拿到 final 结果后，
    才会把用户消息和助手展示文本写入聊天历史。
    """

    root_request_id = uuid.uuid4().hex
    trace = new_task_trace(root_request_id=root_request_id, source="chat")
    trace_started_at = time.monotonic()
    yield sse_event("task_started", {"task_trace": serialize_user_task_trace(trace)})

    if payload.use_env_llm:
        assert chat_agent is not None
        try:
            # 真实模型可能会先经历一段思考/排队时间；先发状态事件，让前端立即有反馈。
            yield sse_event("status", {"content": "正在连接模型并等待首个 token..."})
            for event in chat_agent.stream_chat(
                user_message,
                candidate_id=payload.candidate_id,
                session_id=session_id,
                # 与非流式接口保持一致：LangChain Agent 使用模型回复，但工具调用不
                # 隐式叠加外部模型请求，以便控制首 token 延迟、失败面和 Token 成本。
                use_tool_llm=False,
                auto_rag=payload.auto_rag,
                account_id=account_id,
                root_request_id=root_request_id,
            ):
                event_type = event.get("type")
                if event_type == "token":
                    yield sse_event("token", {"content": event.get("content", "")})
                elif event_type == "step_started":
                    step = start_task_step(trace, str(event.get("name") or "unknown_tool"))
                    yield sse_event("step_started", {"step": serialize_user_task_step(step)})
                elif event_type == "step_completed":
                    step = complete_task_step(
                        trace,
                        str(event.get("name") or "unknown_tool"),
                        event.get("data"),
                    )
                    yield sse_event("step_completed", {"step": serialize_user_task_step(step)})
                elif event_type == "final":
                    result = event["result"]
                    reconcile_task_trace(trace, result.tool_outputs)
                    approval = approval_from_tool_outputs(result.tool_outputs)
                    trace["approval"] = approval
                    trace["status"] = task_trace_completion_status(trace, approval)
                    trace["duration_ms"] = max(0, round((time.monotonic() - trace_started_at) * 1000))
                    trace["finished_at"] = iso_utc() if not approval else None
                    trace["updated_at"] = iso_utc()
                    display_reply = format_web_chat_reply(
                        mode=result.mode,
                        reply=result.reply,
                        used_tools=result.used_tools,
                        tool_outputs=result.tool_outputs,
                        rule_based_result=None,
                    )
                    if not trace["steps"]:
                        start_task_step(trace, "compose_reply")
                        complete_task_step(trace, "compose_reply", None)
                    task_trace_metadata = trace if has_audited_steps(trace) else None
                    user_task_trace = serialize_user_task_trace(task_trace_metadata)
                    save_successful_web_chat_turn(
                        backend,
                        payload.candidate_id,
                        session_id,
                        user_message,
                        display_reply,
                        {
                            "mode": result.mode,
                            "task_trace": user_task_trace,
                            "routing": result.routing,
                        },
                        account_id=account_id,
                    )
                    persist_tool_trace(
                        backend,
                        trace,
                        account_id=account_id,
                        candidate_id=payload.candidate_id,
                        session_id=session_id,
                        source="chat",
                    )
                    if approval:
                        yield sse_event("approval_required", {"task_trace": user_task_trace})
                    else:
                        event_name = "task_failed" if trace["status"] == "failed" else "task_completed"
                        yield sse_event(event_name, {"task_trace": user_task_trace})
                    yield sse_event(
                        "final",
                        {
                            "mode": result.mode,
                            "reply": result.reply,
                            "display_reply": display_reply,
                            "task_trace": user_task_trace,
                            "background_tasks": public_background_tasks_from_tool_outputs(
                                result.tool_outputs
                            ),
                            "profile": asdict(backend.get_candidate_profile(payload.candidate_id, account_id=account_id)),
                        },
                    )
        except ModelCircuitOpenError as error:
            fail_task_trace(trace, str(error), trace_started_at)
            persist_tool_trace(
                backend,
                trace,
                account_id=account_id,
                candidate_id=payload.candidate_id,
                session_id=session_id,
                source="chat",
            )
            yield sse_event("task_failed", {"task_trace": serialize_user_task_trace(trace)})
            yield sse_event("error", {"detail": str(error), "retry_after": error.retry_after_seconds})
        except RAGProviderRequestError as error:
            fail_task_trace(trace, str(error), trace_started_at)
            persist_tool_trace(
                backend,
                trace,
                account_id=account_id,
                candidate_id=payload.candidate_id,
                session_id=session_id,
                source="chat",
            )
            yield sse_event("task_failed", {"task_trace": serialize_user_task_trace(trace)})
            yield sse_event("error", {"detail": str(error)})
        except Exception as error:  # noqa: BLE001 - SSE 内统一返回可读错误事件。
            fail_task_trace(trace, str(error), trace_started_at)
            persist_tool_trace(
                backend,
                trace,
                account_id=account_id,
                candidate_id=payload.candidate_id,
                session_id=session_id,
                source="chat",
            )
            yield sse_event("task_failed", {"task_trace": serialize_user_task_trace(trace)})
            yield sse_event("error", {"detail": str(error)})
        return

    try:
        step = start_task_step(trace, "ingest_candidate_message")
        yield sse_event("step_started", {"step": serialize_user_task_step(step)})
        result = backend.ingest_conversation_message(
            payload.candidate_id,
            user_message,
            llm_client=None,
            auto_rebuild_rag=payload.auto_rag,
            account_id=account_id,
        )
    except RAGProviderRequestError as error:
        fail_task_trace(trace, str(error), trace_started_at)
        persist_tool_trace(
            backend,
            trace,
            account_id=account_id,
            candidate_id=payload.candidate_id,
            session_id=session_id,
            source="rule_based_chat",
        )
        yield sse_event("task_failed", {"task_trace": serialize_user_task_trace(trace)})
        yield sse_event("error", {"detail": str(error)})
        return

    tool_outputs = [{"tool_name": "ingest_conversation_message", "data": asdict(result)}]
    result_dict = asdict(result)
    display_reply = format_web_chat_reply(
        mode="rule_based_ingestion",
        reply=result.reply,
        used_tools=["ingest_conversation_message"],
        tool_outputs=tool_outputs,
        rule_based_result=result_dict,
    )
    complete_task_step(trace, "ingest_candidate_message", result_dict)
    trace["status"] = "completed"
    trace["duration_ms"] = max(0, round((time.monotonic() - trace_started_at) * 1000))
    trace["finished_at"] = iso_utc()
    trace["updated_at"] = iso_utc()
    task_trace_metadata = trace if has_audited_steps(trace) else None
    user_task_trace = serialize_user_task_trace(task_trace_metadata)
    save_successful_web_chat_turn(
        backend,
        payload.candidate_id,
        session_id,
        user_message,
        display_reply,
        {
            "mode": "rule_based_ingestion",
            "task_trace": user_task_trace,
        },
        account_id=account_id,
    )
    persist_tool_trace(
        backend,
        trace,
        account_id=account_id,
        candidate_id=payload.candidate_id,
        session_id=session_id,
        source="rule_based_chat",
    )
    yield sse_event("task_completed", {"task_trace": user_task_trace})
    # 规则兜底没有真实 token 流，这里发送一次完整文本，保持前端路径统一。
    yield sse_event("token", {"content": display_reply})
    yield sse_event(
        "final",
        {
            "mode": "rule_based_ingestion",
            "reply": result.reply,
            "display_reply": display_reply,
            "task_trace": user_task_trace,
            "background_tasks": [],
            "profile": asdict(backend.get_candidate_profile(payload.candidate_id, account_id=account_id)),
        },
    )


def new_task_trace(root_request_id: str | None = None, source: str = "chat") -> dict[str, object]:
    """创建只包含用户可理解摘要的任务过程，不保存模型隐藏推理。"""

    now = iso_utc()
    return {
        "version": 1,
        "root_request_id": root_request_id or uuid.uuid4().hex,
        "title": "本次任务",
        "status": "running",
        "source": source,
        "duration_ms": None,
        "created_at": now,
        "started_at": now,
        "finished_at": None,
        "updated_at": now,
        "steps": [],
        "approval": None,
    }


def serialize_user_task_step(step: object) -> dict[str, object] | None:
    """返回折叠任务过程所需字段，不暴露内部工具名、参数或结果对象。"""

    if not isinstance(step, dict):
        return None
    return {
        "id": str(step.get("id") or ""),
        "label": str(step.get("label") or "执行任务"),
        "status": str(step.get("status") or "running"),
        "summary": str(step.get("summary") or ""),
    }


def serialize_user_task_trace(trace: object) -> dict[str, object] | None:
    """把内部审计轨迹投影成前端可展示的低敏任务过程。"""

    if not isinstance(trace, dict):
        return None
    steps = [
        serialized
        for step in trace.get("steps", [])
        if (serialized := serialize_user_task_step(step)) is not None
    ]
    approval = trace.get("approval")
    user_approval: dict[str, object] | None = None
    if isinstance(approval, dict):
        items = [
            {
                "label": str(item.get("label") or ""),
                "value": str(item.get("value") or ""),
            }
            for item in approval.get("items", [])
            if isinstance(item, dict)
        ]
        user_approval = {
            "kind": str(approval.get("kind") or ""),
            "record_id": approval.get("record_id"),
            "title": str(approval.get("title") or ""),
            "message": str(approval.get("message") or ""),
            "items": items,
            "confirm_label": str(approval.get("confirm_label") or "确认"),
            "cancel_label": str(approval.get("cancel_label") or "取消"),
            "status": str(approval.get("status") or "waiting"),
        }
    return {
        "version": int(trace.get("version") or 1),
        "root_request_id": str(trace.get("root_request_id") or ""),
        "title": str(trace.get("title") or "本次任务"),
        "status": str(trace.get("status") or "running"),
        "duration_ms": trace.get("duration_ms"),
        "steps": steps,
        "approval": user_approval,
    }


def serialize_user_chat_metadata(metadata: object) -> dict[str, object]:
    """历史消息只返回恢复任务折叠区所需的安全元数据。"""

    if not isinstance(metadata, dict):
        return {}
    task_trace = serialize_user_task_trace(metadata.get("task_trace"))
    return {"task_trace": task_trace} if task_trace is not None else {}


def public_background_tasks_from_tool_outputs(
    tool_outputs: list[dict[str, object]],
) -> list[dict[str, object]]:
    """只返回前端恢复 GitHub 项目轮询需要的任务标识和状态。"""

    tasks: list[dict[str, object]] = []
    for item in tool_outputs:
        if item.get("tool_name") != "analyze_github_project_for_candidate":
            continue
        data = item.get("data")
        task = data.get("task") if isinstance(data, dict) else None
        if not isinstance(task, dict) or not task.get("task_key"):
            continue
        tasks.append(
            {
                "task_key": str(task["task_key"]),
                "task_type": str(task.get("task_type") or "github_project_analysis"),
                "status": str(task.get("status") or "queued"),
                "progress": int(task.get("progress") or 0),
            }
        )
    return tasks


def start_task_step(trace: dict[str, object], tool_name: str) -> dict[str, object]:
    """登记一个正在执行的任务步骤。"""

    steps = trace.setdefault("steps", [])
    assert isinstance(steps, list)
    now = iso_utc()
    step = {
        "id": f"step-{len(steps) + 1}",
        "name": tool_name,
        "label": tool_step_label(tool_name),
        "status": "running",
        "summary": None,
        "started_at": now,
        "finished_at": None,
        "attempts": [
            {
                "attempt": 1,
                "phase": "tool",
                "status": "running",
                "started_at": now,
                "finished_at": None,
                "summary": None,
            }
        ],
    }
    steps.append(step)
    trace["updated_at"] = now
    return step


def complete_task_step(
    trace: dict[str, object],
    tool_name: str,
    data: object,
) -> dict[str, object]:
    """完成最近一个同名步骤，并只附加低敏摘要。"""

    steps = trace.setdefault("steps", [])
    assert isinstance(steps, list)
    step = next(
        (item for item in reversed(steps) if isinstance(item, dict) and item.get("name") == tool_name and item.get("status") == "running"),
        None,
    )
    if step is None:
        step = start_task_step(trace, tool_name)
    now = iso_utc()
    status = "failed" if isinstance(data, dict) and data.get("error") else "completed"
    summary = summarize_task_step(tool_name, data)
    result = summarize_tool_result(tool_name, data)
    step["status"] = status
    step["summary"] = summary
    step["result"] = result
    step["finished_at"] = now
    if isinstance(result, dict) and result.get("task_key"):
        step["background_task_key"] = result["task_key"]
    attempts = step.setdefault("attempts", [])
    if isinstance(attempts, list):
        if not attempts:
            attempts.append({"attempt": 1, "started_at": step.get("started_at")})
        attempt = attempts[-1]
        if isinstance(attempt, dict):
            attempt["status"] = status
            attempt["finished_at"] = now
            attempt["summary"] = summary
            attempt["result"] = result
    trace["updated_at"] = now
    return step


def summarize_tool_result(tool_name: str, data: object) -> dict[str, object] | None:
    """为管理端保留低敏工具结果，不保存完整简历、聊天正文或 RAG 片段。"""

    if not isinstance(data, dict):
        return None
    if data.get("error"):
        return {"ok": False, "error": str(data.get("error"))}
    if tool_name == "search_candidate_evidence" and isinstance(data.get("results"), list):
        return {"ok": True, "result_count": len(data["results"])}
    if tool_name == "list_resume_artifacts_for_candidate" and isinstance(data.get("artifacts"), list):
        return {"ok": True, "artifact_count": len(data["artifacts"])}
    if tool_name == "match_all_jobs_for_candidate" and isinstance(data.get("matches"), list):
        return {"ok": True, "match_count": len(data["matches"])}
    if tool_name == "list_candidate_profiles" and isinstance(data.get("profiles"), list):
        return {"ok": True, "profile_count": len(data["profiles"])}
    if tool_name == "list_imported_jobs" and isinstance(data.get("jobs"), list):
        return {"ok": True, "job_count": len(data["jobs"])}
    task = data.get("task")
    if isinstance(task, dict):
        return {
            "ok": task.get("status") not in {"failed", "cancelled"},
            "task_key": task.get("task_key"),
            "task_type": task.get("task_type"),
            "status": task.get("status"),
            "progress": task.get("progress"),
            "error_summary": task.get("error_summary"),
        }
    job = data.get("job")
    if isinstance(job, dict):
        return {
            "ok": True,
            "job_id": job.get("id"),
            "title": job.get("title"),
            "city": job.get("city"),
            "import_method": job.get("import_method"),
        }
    if tool_name == "ingest_candidate_message":
        fields = data.get("saved_structured_fields") or []
        long_text_ids = data.get("saved_long_text_ids") or []
        return {
            "ok": True,
            "saved_structured_field_count": len(fields) if isinstance(fields, list) else 0,
            "saved_long_text_count": len(long_text_ids) if isinstance(long_text_ids, list) else 0,
            "rag_update_mode": data.get("rag_update_mode"),
        }
    project_card = data.get("project_card")
    if isinstance(project_card, dict):
        return {
            "ok": True,
            "project_card_id": project_card.get("id"),
            "status": project_card.get("status"),
        }
    draft = data.get("draft")
    if isinstance(draft, dict):
        return {
            "ok": True,
            "draft_id": draft.get("id"),
            "job_id": draft.get("job_id"),
            "version": draft.get("version"),
        }
    return {"ok": True, "result_keys": sorted(str(key) for key in data)[:12]}


def summarize_task_step(tool_name: str, data: object) -> str | None:
    """将工具结果压缩成任务行摘要，避免把完整候选人材料塞进聊天 UI。"""

    if not isinstance(data, dict):
        return None
    if data.get("error"):
        return f"{data['error']}"
    if tool_name == "search_candidate_evidence" and isinstance(data.get("results"), list):
        return f"找到 {len(data['results'])} 条相关证据"
    if tool_name == "list_resume_artifacts_for_candidate" and isinstance(data.get("artifacts"), list):
        return f"读取到 {len(data['artifacts'])} 份简历"
    if tool_name == "match_all_jobs_for_candidate" and isinstance(data.get("matches"), list):
        return f"完成 {len(data['matches'])} 个职位的匹配"
    if tool_name == "list_candidate_profiles" and isinstance(data.get("profiles"), list):
        return f"找到 {len(data['profiles'])} 份档案"
    if tool_name == "list_imported_jobs" and isinstance(data.get("jobs"), list):
        return f"读取到 {len(data['jobs'])} 个职位"
    task = data.get("task")
    if isinstance(task, dict) and task.get("task_key"):
        return "任务已排队，完成后继续更新"
    job = data.get("job")
    if isinstance(job, dict) and job.get("title"):
        return f"已识别职位：{job['title']}"
    if tool_name == "ingest_candidate_message":
        fields = data.get("saved_structured_fields") or []
        return f"已保存 {len(fields)} 项结构化资料" if fields else "已完成资料整理"
    return None


def reconcile_task_trace(trace: dict[str, object], tool_outputs: list[dict[str, object]]) -> None:
    """用最终工具摘要补齐模型未显式报告开始事件的步骤。"""

    steps = trace.setdefault("steps", [])
    assert isinstance(steps, list)
    matched_step_ids: set[str] = set()
    tool_names: list[str] = []
    for item in tool_outputs:
        if not isinstance(item, dict):
            continue
        tool_name = str(item.get("tool_name") or "unknown_tool")
        trace_data = tool_output_trace_data(item)
        tool_names.append(tool_name)
        existing = next(
            (
                step
                for step in steps
                if isinstance(step, dict)
                and step.get("name") == tool_name
                and str(step.get("id")) not in matched_step_ids
            ),
            None,
        )
        if existing is None:
            existing = complete_task_step(trace, tool_name, trace_data)
        else:
            now = iso_utc()
            status = (
                "failed"
                if isinstance(trace_data, dict) and trace_data.get("error")
                else "completed"
            )
            summary = summarize_task_step(tool_name, trace_data)
            result = summarize_tool_result(tool_name, trace_data)
            existing["status"] = status
            existing["summary"] = summary
            existing["result"] = result
            existing["finished_at"] = now
            if isinstance(result, dict) and result.get("task_key"):
                existing["background_task_key"] = result["task_key"]
            attempts = existing.setdefault("attempts", [])
            if isinstance(attempts, list):
                if not attempts:
                    attempts.append({"attempt": 1, "started_at": existing.get("started_at")})
                attempt = attempts[-1]
                if isinstance(attempt, dict):
                    attempt["status"] = status
                    attempt["finished_at"] = now
                    attempt["summary"] = summary
                    attempt["result"] = result
        matched_step_ids.add(str(existing.get("id")))
    trace["title"] = tool_trace_title(tool_names)
    trace["updated_at"] = iso_utc()


def tool_output_trace_data(item: dict[str, object]) -> object:
    """把新旧工具错误格式统一成轨迹层已有的低敏 ``data.error`` 结构。"""

    data = item.get("data")
    if str(item.get("status") or "success") not in {"error", "rejected", "failed"}:
        return data
    if isinstance(data, dict) and data.get("error"):
        return data
    error = item.get("error")
    if isinstance(error, dict):
        message = str(error.get("message") or error.get("code") or "工具调用失败。")
    else:
        message = str(error or "工具调用失败。")
    return {"error": message}


def task_trace_completion_status(
    trace: dict[str, object],
    approval: dict[str, object] | None,
) -> str:
    """根据确认状态和真实步骤结果确定任务最终状态。"""

    if approval is not None:
        return "waiting_confirmation"
    steps = trace.get("steps")
    if isinstance(steps, list) and any(
        isinstance(step, dict) and step.get("status") == "failed"
        for step in steps
    ):
        return "failed"
    return "completed"


def fail_task_trace(trace: dict[str, object], detail: str, started_at: float) -> None:
    """标记任务失败并保留失败步骤，便于前端显示可读错误。"""

    steps = trace.setdefault("steps", [])
    assert isinstance(steps, list)
    for step in steps:
        if isinstance(step, dict) and step.get("status") == "running":
            step["status"] = "failed"
            step["summary"] = detail
            step["result"] = {"ok": False, "error": detail}
            step["finished_at"] = iso_utc()
            attempts = step.setdefault("attempts", [])
            if isinstance(attempts, list):
                if not attempts:
                    attempts.append({"attempt": 1, "started_at": step.get("started_at")})
                attempt = attempts[-1]
                if isinstance(attempt, dict):
                    attempt["status"] = "failed"
                    attempt["finished_at"] = step["finished_at"]
                    attempt["summary"] = detail
                    attempt["result"] = {"ok": False, "error": detail}
    trace["status"] = "failed"
    trace["duration_ms"] = max(0, round((time.monotonic() - started_at) * 1000))
    trace["finished_at"] = iso_utc()
    trace["updated_at"] = iso_utc()


def approval_from_tool_outputs(tool_outputs: list[dict[str, object]]) -> dict[str, object] | None:
    """为已安全落到“待确认”状态的项目卡片生成确认区。"""

    for item in tool_outputs:
        data = item.get("data") if isinstance(item, dict) else None
        card = data.get("project_card") if isinstance(data, dict) else None
        if card is None and isinstance(data, dict) and data.get("status") == "待确认":
            card = data
        if not isinstance(card, dict) or card.get("status") != "待确认":
            continue
        card_data = card.get("card") if isinstance(card.get("card"), dict) else {}
        return {
            "kind": "project_card_confirmation",
            "record_id": card.get("id"),
            "title": "等待确认项目经历",
            "message": "确认后，这段项目摘要才会作为后续简历和匹配的可引用证据。",
            "items": [
                {
                    "label": "项目",
                    "value": str(card_data.get("project_name") or "未命名项目"),
                },
                {
                    "label": "摘要",
                    "value": str(card_data.get("summary") or "暂无摘要"),
                },
            ],
            "confirm_label": "确认使用",
            "cancel_label": "暂不使用",
            "status": "waiting",
        }
    return None


def persist_tool_trace(
    backend: JobHuntingApp,
    trace: dict[str, object],
    *,
    account_id: int,
    candidate_id: int | None,
    session_id: str | None,
    source: str,
) -> None:
    """只在存在真实工具步骤时，把任务轨迹写入管理端审计表。"""

    if not has_audited_steps(trace):
        return
    backend.store.record_tool_call_trace(
        build_tool_trace_record(
            trace,
            account_id=account_id,
            candidate_id=candidate_id,
            session_id=session_id,
            source=source,
        )
    )


def load_or_new_tool_trace(
    backend: JobHuntingApp,
    *,
    root_request_id: str,
    account_id: int,
    source: str,
) -> dict[str, object]:
    """读取既有审计轨迹；不存在时创建一条新的非 SSE 轨迹。"""

    try:
        return dict(
            backend.store.get_tool_call_trace(
                root_request_id,
                account_id=account_id,
            ).trace
        )
    except KeyError:
        return new_task_trace(root_request_id=root_request_id, source=source)


def owned_or_new_root_request_id(
    backend: JobHuntingApp,
    *,
    account_id: int,
    candidate_id: int,
    root_request_id: str | None,
) -> str:
    """只续接当前账号和候选人已有轨迹，否则生成新的不可猜测 ID。"""

    value = str(root_request_id or "").strip()
    if not value:
        return uuid.uuid4().hex
    try:
        existing = backend.store.get_tool_call_trace(
            value,
            account_id=account_id,
        )
    except KeyError:
        return uuid.uuid4().hex
    if existing.candidate_id not in {None, candidate_id}:
        return uuid.uuid4().hex
    return value


def update_confirmation_tool_trace(
    backend: JobHuntingApp,
    *,
    root_request_id: str,
    account_id: int,
    candidate_id: int,
    session_id: str,
    record: dict[str, object],
    rag_task: dict[str, object] | None,
) -> None:
    """把候选人确认项目卡片的工具动作写入审计轨迹。"""

    trace = load_or_new_tool_trace(
        backend,
        root_request_id=root_request_id,
        account_id=account_id,
        source="project_confirmation",
    )
    trace["source"] = trace.get("source") or "project_confirmation"
    steps = trace.setdefault("steps", [])
    existing = next(
        (
            item
            for item in reversed(steps)
            if isinstance(item, dict) and item.get("name") == "confirm_project_card"
        ),
        None,
    )
    if isinstance(existing, dict):
        existing["status"] = "running"
        existing["started_at"] = existing.get("started_at") or iso_utc()
    else:
        start_task_step(trace, "confirm_project_card")
    complete_task_step(
        trace,
        "confirm_project_card",
        {
            "project_card": record,
            "task": rag_task,
        },
    )
    trace["title"] = tool_trace_title(
        [str(item.get("name") or "") for item in trace.get("steps", []) if isinstance(item, dict)]
    )
    trace["status"] = "running" if rag_task is not None else "completed"
    trace["finished_at"] = None if rag_task is not None else iso_utc()
    trace["updated_at"] = iso_utc()
    persist_tool_trace(
        backend,
        trace,
        account_id=account_id,
        candidate_id=candidate_id,
        session_id=session_id,
        source="project_confirmation",
    )


def record_background_task_enqueue_trace(
    backend: JobHuntingApp,
    *,
    task: BackgroundTaskRecord,
    root_request_id: str,
    source: str,
) -> None:
    """把页面直接触发的后台任务排队状态写入审计轨迹。"""

    trace = load_or_new_tool_trace(
        backend,
        root_request_id=root_request_id,
        account_id=task.account_id,
        source=source,
    )
    tool_name = background_task_tool_name(task.task_type)
    trace["title"] = trace.get("title") or tool_trace_title([tool_name])
    trace["source"] = source
    step = start_task_step(trace, tool_name)
    queued_result = {
        "ok": True,
        "task_key": task.task_key,
        "task_type": task.task_type,
        "status": task.status,
        "progress": task.progress,
    }
    step["summary"] = "任务已排队，完成后继续更新"
    step["result"] = queued_result
    step["background_task_key"] = task.task_key
    for attempt in step.get("attempts", []):
        if isinstance(attempt, dict):
            attempt["summary"] = step["summary"]
            attempt["result"] = queued_result
    trace["status"] = "running"
    trace["finished_at"] = None
    trace["updated_at"] = iso_utc()
    persist_tool_trace(
        backend,
        trace,
        account_id=task.account_id,
        candidate_id=task.candidate_id,
        session_id=task.session_id,
        source=source,
    )


def sse_event(event: str, data: dict[str, object]) -> str:
    """把事件编码成 Server-Sent Events 文本块。"""

    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def serialize_resume_artifact(artifact: ResumeArtifactRecord) -> dict[str, object]:
    """返回前端需要的文件元数据，不暴露服务器内部存储键。"""

    payload = asdict(artifact)
    payload.pop("storage_key", None)
    payload.pop("account_id", None)
    payload["download_url"] = f"/api/resumes/{artifact.id}/download"
    return payload


def serialize_background_task(task: BackgroundTaskRecord) -> dict[str, object]:
    """返回任务进度和摘要，不把 payload 或账号归属泄露给前端。"""

    return {
        "task_key": task.task_key,
        "task_type": task.task_type,
        "status": task.status,
        "progress": task.progress,
        "attempt": task.attempt,
        "max_attempts": task.max_attempts,
        "result": task.result,
        "error_summary": task.error_summary,
        "created_at": task.created_at,
        "started_at": task.started_at,
        "finished_at": task.finished_at,
        "updated_at": task.updated_at,
    }


def serialize_tool_trace_summary(trace: ToolCallTraceRecord) -> dict[str, object]:
    """返回管理端列表所需的工具调用任务摘要。"""

    return {
        "id": trace.id,
        "account_id": trace.account_id,
        "candidate_id": trace.candidate_id,
        "session_id": trace.session_id,
        "root_request_id": trace.root_request_id,
        "title": trace.title,
        "status": trace.status,
        "source": trace.source,
        "step_count": trace.step_count,
        "attempt_count": trace.attempt_count,
        "last_step_name": trace.last_step_name,
        "last_error_summary": trace.last_error_summary,
        "created_at": trace.created_at,
        "started_at": trace.started_at,
        "finished_at": trace.finished_at,
        "updated_at": trace.updated_at,
    }


def serialize_tool_trace_detail(trace: ToolCallTraceRecord) -> dict[str, object]:
    """返回管理端详情所需的完整工具调用轨迹。"""

    payload = serialize_tool_trace_summary(trace)
    payload["trace"] = trace.trace
    return payload


def default_web_session_id(candidate_id: int, account_id: int) -> str:
    """生成网页默认会话 ID。

    目前一个候选人对应一个默认网页聊天窗口；后续如果支持多个求职主题会话，
    可以在前端传入更细的 `session_id`。
    """

    return f"account-{account_id}-candidate-{candidate_id}"


def save_successful_web_chat_turn(
    backend: JobHuntingApp,
    candidate_id: int,
    session_id: str,
    user_message: str,
    assistant_message: str,
    assistant_metadata: dict[str, object],
    account_id: int,
) -> None:
    """保存一次成功网页聊天的用户消息和助手消息。

    失败的模型/API 调用不会写入历史，避免用户刷新后看到半截无效回合。
    """

    # 首次使用默认会话时自动登记会话索引；显式创建的会话则复用已有记录。
    try:
        session = backend.store.get_chat_session_by_key(session_id, account_id)
        if session.candidate_id != candidate_id:
            raise HTTPException(status_code=403, detail="该会话不属于当前候选人档案。")
        if session.status != "active":
            raise HTTPException(status_code=409, detail="该会话已经归档，请新建对话。")
    except KeyError:
        backend.store.create_chat_session(
            session_id=session_id,
            account_id=account_id,
            candidate_id=candidate_id,
            title=user_message[:32] or "新对话",
        )

    backend.save_chat_message(
        candidate_id,
        session_id,
        "user",
        user_message,
        {"source": "web_composer"},
        account_id=account_id,
    )
    backend.save_chat_message(
        candidate_id,
        session_id,
        "assistant",
        assistant_message,
        assistant_metadata,
        account_id=account_id,
    )


def format_web_chat_reply(
    mode: str,
    reply: str,
    used_tools: list[str],
    tool_outputs: list[dict[str, object]],
    rule_based_result: dict[str, object] | None,
) -> str:
    """整理网页最终展示给用户的助手回复。

    前端仍然保留兜底格式化函数，但后端也返回并持久化同一份展示文本，
    这样刷新页面恢复历史时，用户看到的内容和当时聊天窗口里的内容一致。
    """

    # 工具调用、保存字段和 RAG 状态只进入任务追踪与管理员审计，不进入聊天气泡。
    # 保留参数是为了兼容已有调用方，但展示文本唯一来源是 Agent 自然语言回复。
    return sanitize_web_chat_reply(reply)


def sanitize_web_chat_reply(value: object) -> str:
    """移除旧版本已经拼入聊天消息的内部执行元数据。

    新消息由 `format_web_chat_reply` 直接阻止元数据进入；这里同时用于历史读取，
    让旧数据库记录在用户界面恢复时也不会暴露工具名、字段名、内部 ID 或 RAG 状态。
    """

    text = str(value or "").replace("\r\n", "\n").strip()
    if not text:
        return ""
    lines = text.splitlines()
    for index, line in enumerate(lines):
        if not is_legacy_chat_metadata_line(line):
            continue
        if all(
            not remaining.strip() or is_legacy_chat_metadata_line(remaining)
            for remaining in lines[index:]
        ):
            return "\n".join(lines[:index]).rstrip()
    return text


def is_legacy_chat_metadata_line(value: str) -> bool:
    """只识别旧版确切生成过的尾部元数据行。"""

    line = value.strip()
    return any(
        re.fullmatch(pattern, line)
        for pattern in (
            r"工具：.+",
            r"工具错误：.+",
            r"保存字段：.+",
            r"长文本 ID：(?:无|\d+(?:[、,，]\s*\d+)*)",
            r"RAG：.+",
            r"导入职位：.+",
            r"匹配结果：共 \d+ 个职位，已按推荐顺序返回。",
            r"GitHub 项目分析：任务已排队，完成后会生成待确认项目卡片。",
        )
    )


def get_profile_or_404(backend: JobHuntingApp, candidate_id: int, account_id: int | None = None):
    """读取候选人档案；不存在时转换为 Web 友好的 404。"""

    try:
        return backend.get_candidate_profile(candidate_id, account_id=account_id)
    except KeyError as error:
        raise HTTPException(status_code=404, detail=f"候选人不存在：{candidate_id}") from error


def clean_string_list(values: list[str]) -> list[str]:
    """清理网页表单中的字符串列表。"""

    return [str(value).strip() for value in values if str(value).strip()]


def clean_string_dict(values: dict[str, str]) -> dict[str, str]:
    """清理网页表单中的技能字典。"""

    return normalize_skill_mapping({
        str(key).strip(): str(value).strip() or "待确认"
        for key, value in values.items()
        if str(key).strip()
    })


def create_reloadable_web_app() -> FastAPI:
    """为 Uvicorn 重载子进程重新创建认证 Web 应用。

    不能把已经创建好的 ``FastAPI`` 对象传给 ``uvicorn.run(..., reload=True)``，
    否则子进程无法在源码变化后重新导入模块。这里通过启动父进程写入的运行路径
    重建应用，同时避免把运行路径混入用户维护的模型配置文件。
    """

    env_file = os.environ.get(WEB_RELOAD_ENV_FILE_ENV, ".env")
    observability_settings = load_observability_settings(env_file)
    configure_logging(observability_settings, service_name="job-hunting-web")
    configure_tracing(observability_settings, service_name="job-hunting-web")
    database_settings = load_database_settings(env_file)
    try:
        database_url = require_postgresql_database_url(database_settings)
    except ValueError as error:
        raise RuntimeError(str(error)) from error
    return create_web_app(
        env_file=env_file,
        resume_dir=os.environ.get(WEB_RELOAD_RESUME_DIR_ENV) or None,
        database_url=database_url,
    )


def configure_reload_runtime(args: argparse.Namespace) -> None:
    """把启动参数路径传给 Uvicorn 重载子进程，保持重启前后的数据位置一致。"""

    os.environ[WEB_RELOAD_ENV_FILE_ENV] = str(args.env_file)
    if args.resume_dir:
        os.environ[WEB_RELOAD_RESUME_DIR_ENV] = str(args.resume_dir)
    else:
        os.environ.pop(WEB_RELOAD_RESUME_DIR_ENV, None)


def main(argv: list[str] | None = None) -> None:
    """从命令行启动本地 Web 服务。"""

    parser = argparse.ArgumentParser(prog="job-agent-web")
    parser.add_argument("--env-file", default=".env")
    parser.add_argument(
        "--resume-dir",
        default=None,
        help="仅用于本地兼容测试的文件目录；Docker 默认使用对象存储。",
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    # 只供本地开发覆盖配置使用；生产镜像应保持固定版本，不能自动加载宿主机源码。
    parser.add_argument("--reload", action="store_true", help="监听源码变化并自动重启开发服务")
    parser.add_argument(
        "--reload-dir",
        action="append",
        default=[],
        help="需要监听的源码目录；可重复传入多个目录",
    )
    args = parser.parse_args(argv)

    observability_settings = load_observability_settings(args.env_file)
    configure_logging(observability_settings, service_name="job-hunting-web")
    configure_tracing(observability_settings, service_name="job-hunting-web")

    try:
        database_url = require_postgresql_database_url(load_database_settings(args.env_file))
    except ValueError as error:
        raise SystemExit(str(error)) from error

    import uvicorn

    if args.reload:
        configure_reload_runtime(args)
        reload_dirs = args.reload_dir or [str(Path(__file__).resolve().parents[1])]
        uvicorn.run(
            "job_hunting_agent.web:create_reloadable_web_app",
            factory=True,
            host=args.host,
            port=args.port,
            reload=True,
            reload_dirs=reload_dirs,
            log_config=None,
            access_log=False,
        )
        return

    uvicorn.run(
        create_web_app(
            env_file=args.env_file,
            resume_dir=args.resume_dir,
            database_url=database_url,
        ),
        host=args.host,
        port=args.port,
        log_config=None,
        access_log=False,
    )


if __name__ == "__main__":
    main()
