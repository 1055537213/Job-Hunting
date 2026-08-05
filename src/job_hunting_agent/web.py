"""本地 Web 前端入口。

这个模块现在同时支持两种聊天路径：

- 标准 LangChain Agent 模式：Web -> JobHuntingAgent -> Tools -> JobHuntingApp
- 本地规则兜底模式：Web -> JobHuntingApp.ingest_conversation_message

之所以保留兜底，是为了测试和显式离线调用；网页与 API 默认都走标准 Agent 结构，
模型配置错误时直接返回原因，不会静默切换执行逻辑。
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import uuid
from dataclasses import asdict
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, Query, Request, Response, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .agent import JobHuntingAgent
from .app import JobHuntingApp
from .auth import (
    hash_password,
    is_session_expired,
    new_session_token,
    session_expiry,
    session_token_hash,
    utc_now,
    verify_password,
)
from .config import (
    load_agent_memory_settings,
    load_embedding_settings,
    load_rerank_settings,
    load_cookie_secure,
    load_llm_settings,
    masked_agent_memory_settings,
    masked_embedding_settings,
    masked_llm_settings,
    masked_rerank_settings,
)
from .models import AccountRecord, CandidateProfileInput, ResumeArtifactRecord
from .job_parser import InvalidJobTextError
from .llm import LLMClient, LLMRequestError
from .rag import RAGProviderRequestError
from .resume_document import MAX_RESUME_FILE_BYTES, ResumeDocumentError


STATIC_DIR = Path(__file__).with_name("web_static")
SESSION_COOKIE_NAME = "job_agent_session"
SESSION_MAX_AGE_SECONDS = 30 * 24 * 60 * 60


class NoCacheStaticFiles(StaticFiles):
    """开发期静态资源服务。

    本项目目前是本地开发型 Web 前端，JS/CSS 经常会随着教学推进修改。
    禁用浏览器缓存可以避免用户看到旧版 `app.js`，例如 Markdown 渲染修复已经写入源码，
    但浏览器仍拿旧脚本导致 `**加粗**` 原样显示。
    """

    async def get_response(self, path: str, scope):  # noqa: ANN001
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


class RegisterPayload(BaseModel):
    """普通用户注册表单。"""

    email: str
    password: str
    display_name: str | None = None


class LoginPayload(BaseModel):
    """登录表单。"""

    email: str
    password: str


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


def create_web_app(
    db_path: str | Path = "data/job_agent.db",
    env_file: str | Path = ".env",
    rag_dir: str | Path = "data/chroma",
    resume_dir: str | Path | None = None,
    chat_agent: JobHuntingAgent | None = None,
    resume_llm_client: LLMClient | None = None,
    require_auth: bool = True,
) -> FastAPI:
    """创建本地 FastAPI 应用。

    这里显式保留数据库路径、`.env` 路径、RAG 目录和可注入 Agent，目的是：

    - 生产使用时，Web 层通过 `JobHuntingAgent` 或 `JobHuntingApp` 访问业务能力；
    - 测试时，可以安全地注入临时 SQLite、临时 Chroma 和假模型；
    - Web 层自己不直接碰 SQLite 连接、RAG 向量库细节或厂商 SDK。
    """

    backend = JobHuntingApp(db_path, env_file, resume_dir=resume_dir)
    backend.initialize()
    env_path = Path(env_file)
    rag_path = Path(rag_dir)
    cookie_secure = load_cookie_secure(env_path)

    agent_error: str | None = None
    if chat_agent is None:
        try:
            chat_agent = JobHuntingAgent(backend, env_path=env_path, rag_dir=rag_path)
        except ValueError as error:
            # `.env` 没配好时，网页仍然可以以本地规则模式工作。
            agent_error = str(error)

    web_app = FastAPI(title="Job Hunting Agent Web", version="0.1.0")
    web_app.mount("/static", NoCacheStaticFiles(directory=STATIC_DIR), name="static")

    def current_account(request: Request, required: bool = True) -> AccountRecord | None:
        """从 HttpOnly Cookie 解析当前账号，并顺延 Session 闲置窗口。"""

        token = request.cookies.get(SESSION_COOKIE_NAME)
        if not token:
            if required and require_auth:
                raise HTTPException(status_code=401, detail="请先登录。")
            return None
        session = backend.store.get_auth_session_by_token_hash(session_token_hash(token))
        if (
            session is None
            or session.revoked_at is not None
            or is_session_expired(session.expires_at, session.absolute_expires_at)
        ):
            if required and require_auth:
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

    @web_app.post("/api/auth/register")
    def register(payload: RegisterPayload) -> dict[str, object]:
        """开放普通用户注册；管理员账号不通过此接口创建。"""

        email = payload.email.strip().lower()
        if "@" not in email or len(email) > 254:
            raise HTTPException(status_code=400, detail="请输入有效邮箱。")
        try:
            password_hash = hash_password(payload.password)
            account = backend.store.create_account(
                email=email,
                password_hash=password_hash,
                display_name=(payload.display_name or "").strip() or None,
                role="user",
            )
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        except sqlite3.IntegrityError as error:
            raise HTTPException(status_code=409, detail="该邮箱已经注册。") from error
        return {"account": asdict(account)}

    @web_app.post("/api/auth/login")
    def login(payload: LoginPayload, response: Response, request: Request) -> dict[str, object]:
        """验证密码并签发服务端 Session Cookie。"""

        record = backend.store.get_account_by_email(payload.email.strip().lower())
        if record is None or not verify_password(record[1], payload.password):
            raise HTTPException(status_code=401, detail="邮箱或密码错误。")
        account, _ = record
        if account.status != "active":
            raise HTTPException(status_code=403, detail="账号已被禁用。")
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
        backend.store.touch_account_login(account.id)
        return {"account": asdict(account)}

    @web_app.get("/api/auth/me")
    def auth_me(request: Request) -> dict[str, object]:
        """返回当前登录账号；未登录用于前端显示登录页。"""

        account = current_account(request, required=False)
        return {"authenticated": account is not None, "account": asdict(account) if account else None}

    @web_app.post("/api/auth/logout")
    def logout(request: Request, response: Response) -> dict[str, bool]:
        """撤销当前设备 Session 并清理 Cookie。"""

        token = request.cookies.get(SESSION_COOKIE_NAME)
        if token:
            session = backend.store.get_auth_session_by_token_hash(session_token_hash(token))
            if session is not None:
                backend.store.revoke_auth_session(session.id)
        response.delete_cookie(SESSION_COOKIE_NAME, path="/")
        return {"ok": True}

    @web_app.post("/api/auth/logout-all")
    def logout_all(request: Request, response: Response) -> dict[str, object]:
        """撤销当前账号在所有设备上的登录状态。"""

        account = current_account(request)
        assert account is not None
        count = backend.store.revoke_all_auth_sessions(account.id)
        response.delete_cookie(SESSION_COOKIE_NAME, path="/")
        return {"ok": True, "revoked_sessions": count}

    @web_app.get("/")
    def home() -> FileResponse:
        """返回单页 Web 前端。"""

        response = FileResponse(STATIC_DIR / "index.html")
        response.headers["Cache-Control"] = "no-store, max-age=0"
        return response

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
        if account is None or account.role != "admin":
            return {
                "status": "ok",
                "agent": {"configured": chat_agent is not None},
                "llm": {"configured": bool(llm_config.get("configured"))},
                "embedding": {"configured": bool(embedding_config.get("configured"))},
                "rerank": {"configured": bool(rerank_config.get("configured"))},
                "memory": {"configured": bool(memory_config.get("enabled"))},
            }
        return {
            "status": "ok",
            "db_path": str(Path(db_path)),
            "rag_dir": str(rag_path),
            "llm": llm_config,
            "embedding": embedding_config,
            "rerank": rerank_config,
            "memory": memory_config,
            "agent": {
                "configured": chat_agent is not None,
                "error": agent_error,
            },
        }

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
        candidate_id = backend.save_candidate_profile(
            CandidateProfileInput(
                name=payload.name.strip(),
                status=payload.status.strip() or "待补充",
                education=payload.education.strip() or "待补充",
                experience_years=payload.experience_years,
                skills=clean_string_dict(payload.skills),
                preferred_cities=clean_string_list(payload.preferred_cities),
                salary_floor_k=payload.salary_floor_k,
                expected_salary_k=payload.expected_salary_k,
                target_directions=clean_string_list(payload.target_directions),
                unacceptable=clean_string_list(payload.unacceptable),
            ),
            account_id=account.id if account else None,
        )
        return {"candidate_id": candidate_id, "profile": asdict(backend.get_candidate_profile(candidate_id, account_id=account.id if account else None))}

    @web_app.get("/api/profiles/{candidate_id}")
    def get_profile(candidate_id: int, request: Request) -> dict[str, object]:
        """读取某个候选人档案。"""

        account = current_account(request)
        return {"profile": asdict(get_profile_or_404(backend, candidate_id, account.id if account else None))}

    @web_app.post("/api/chat/sessions")
    def create_chat_session(payload: ChatSessionPayload, request: Request) -> dict[str, object]:
        """创建一个绑定当前账号和候选人档案的独立会话。"""

        account = current_account(request)
        account_id = account.id if account else None
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
        account_id: int | None,
        candidate_id: int,
        session_id: str,
    ) -> None:
        """验证会话是否属于当前账号和当前候选人。

        首次使用的默认会话还没有索引记录，允许成功回复时自动创建；
        已存在的会话若绑定了另一份档案则立即拒绝，避免记忆和历史串线。
        """

        if account_id is None:
            return
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

    @web_app.post("/api/chat")
    def chat(payload: ChatPayload, request: Request) -> dict[str, object]:
        """处理网页聊天消息。

        - 开启 Agent 时：执行标准 LangChain Agent 对话。
        - 关闭 Agent 时：回退到原有“对话式自动入库”规则链路。
        """

        account = current_account(request)
        account_id = account.id if account else None
        get_profile_or_404(backend, payload.candidate_id, account_id)
        user_message = payload.message.strip()
        session_id = payload.session_id or default_web_session_id(payload.candidate_id, account_id)
        validate_chat_session(account_id, payload.candidate_id, session_id)
        if not user_message:
            raise HTTPException(status_code=400, detail="消息不能为空。")

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
                    use_tool_llm=True,
                    auto_rag=payload.auto_rag,
                    account_id=account_id,
                )
            except RAGProviderRequestError as error:
                raise HTTPException(status_code=502, detail=str(error)) from error
            except Exception as error:  # noqa: BLE001 - 对 Web 层统一转成可读错误。
                raise HTTPException(status_code=502, detail=str(error)) from error
            tool_outputs = result.tool_outputs
            display_reply = format_web_chat_reply(
                mode=result.mode,
                reply=result.reply,
                used_tools=result.used_tools,
                tool_outputs=tool_outputs,
                rule_based_result=None,
            )
            save_successful_web_chat_turn(
                backend,
                payload.candidate_id,
                session_id,
                user_message,
                display_reply,
                {"mode": result.mode, "used_tools": result.used_tools, "usage": result.usage},
                account_id=account_id,
            )
            return {
                "mode": result.mode,
                "reply": result.reply,
                "used_tools": result.used_tools,
                "tool_outputs": tool_outputs,
                "usage": result.usage,
                "display_reply": display_reply,
                "profile": asdict(backend.get_candidate_profile(payload.candidate_id, account_id=account_id)),
            }

        try:
            result = backend.ingest_conversation_message(
                payload.candidate_id,
                user_message,
                llm_client=None,
                rag_persist_directory=rag_path,
                auto_rebuild_rag=payload.auto_rag,
                account_id=account_id,
            )
        except RAGProviderRequestError as error:
            raise HTTPException(status_code=502, detail=str(error)) from error
        tool_outputs = [{"tool_name": "ingest_conversation_message", "data": asdict(result)}]
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
            {"mode": "rule_based_ingestion", "used_tools": ["ingest_conversation_message"]},
            account_id=account_id,
        )
        return {
            "mode": "rule_based_ingestion",
            "reply": result.reply,
            "used_tools": ["ingest_conversation_message"],
            "tool_outputs": tool_outputs,
            "result": asdict(result),
            "display_reply": display_reply,
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
                rag_path=rag_path,
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
        account_id = account.id if account else None
        get_profile_or_404(backend, candidate_id, account_id)
        actual_session_id = session_id or default_web_session_id(candidate_id, account_id)
        validate_chat_session(account_id, candidate_id, actual_session_id)
        return {
            "candidate_id": candidate_id,
            "session_id": actual_session_id,
            "messages": [
                asdict(message)
                for message in backend.list_chat_messages(candidate_id, actual_session_id, limit, account_id=account_id)
            ],
        }

    @web_app.post("/api/jobs")
    def import_job(payload: JobPayload, request: Request) -> dict[str, object]:
        """保存候选人从 BOSS 页面主动复制回来的职位文本。"""

        account = current_account(request)
        if not payload.raw_text.strip():
            raise HTTPException(status_code=400, detail="职位文本不能为空。")
        try:
            job = backend.import_job_text(payload.raw_text, payload.source_url, account_id=account.id if account else None)
        except InvalidJobTextError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        return {"job": asdict(job)}

    @web_app.get("/api/jobs")
    def list_jobs(request: Request) -> dict[str, object]:
        """列出已经导入的职位。"""

        account = current_account(request)
        return {"jobs": [asdict(job) for job in backend.list_jobs(account_id=account.id if account else None)]}

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

    @web_app.post("/api/resumes/upload")
    async def upload_resume(
        request: Request,
        candidate_id: int = Form(...),
        file: UploadFile = File(...),
    ) -> dict[str, object]:
        """上传并解析 DOCX/PDF 简历，然后自动增量登记到当前账号的 RAG。"""

        account = current_account(request)
        account_id = account.id if account else None
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
            )
        except ResumeDocumentError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error

        rag_update = None
        rag_warning = None
        if artifact.long_text_id is not None:
            try:
                rag_update = asdict(
                    backend.index_rag_long_texts(
                        [artifact.long_text_id],
                        rag_path,
                        account_id=account_id,
                    )
                )
            except RAGProviderRequestError as error:
                # 文件与 SQLite 正文已经安全保存；索引失败单独告知，避免用户重复上传。
                rag_warning = f"简历已保存，但 RAG 增量索引失败：{error}"
        return {
            "artifact": serialize_resume_artifact(artifact),
            "rag_update": rag_update,
            "warning": rag_warning,
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

    @web_app.get("/api/resumes/{artifact_id}/download")
    def download_resume(artifact_id: int, request: Request) -> FileResponse:
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
        path = backend.resume_file_path(artifact)
        if not path.is_file():
            raise HTTPException(status_code=404, detail="简历文件已丢失，请重新上传或生成。")
        return FileResponse(
            path,
            media_type=artifact.media_type,
            filename=artifact.download_filename,
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

        active_llm = resume_llm_client
        if active_llm is None:
            request_id = uuid.uuid4().hex
            context = {
                "candidate_id": source.candidate_id,
                "account_id": account_id,
                "session_id": f"resume-web-{source.candidate_id}",
                "root_request_id": request_id,
                "rag_dir": str(rag_path),
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
                rag_persist_directory=rag_path if payload.use_rag else None,
                # 网页按钮始终采用档案熟练度；一次性放宽只允许 Agent 完成风险确认后调用。
                allow_proficiency_upgrade=False,
                account_id=account_id,
            )
        except KeyError as error:
            raise HTTPException(status_code=404, detail="职位、候选人或简历文件不存在。") from error
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        except (RAGProviderRequestError, LLMRequestError) as error:
            raise HTTPException(status_code=502, detail=str(error)) from error
        return {
            "draft": asdict(result.draft),
            "artifacts": [serialize_resume_artifact(item) for item in result.artifacts],
        }

    @web_app.get("/api/rag/search")
    def search_rag(request: Request, query: str = Query(...), top_k: int = 5) -> dict[str, object]:
        """检索本地 RAG 证据片段。"""

        account = current_account(request)
        try:
            results = backend.search_rag(query, rag_path, top_k, account_id=account.id if account else None)
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
        if payload.status == "disabled" and target.role == "admin":
            if backend.store.count_active_admins() <= 1:
                raise HTTPException(status_code=400, detail="至少需要保留一个可用管理员账号。")
        account = backend.store.update_account_status(account_id, payload.status)
        if payload.status != "active":
            backend.store.revoke_all_auth_sessions(account_id)
        return {"account": asdict(account)}

    @web_app.get("/api/admin/usage/summary")
    def admin_usage_summary(request: Request) -> dict[str, object]:
        """管理员查看全局 Token 汇总；正式账单只使用 billable_tokens。"""

        require_admin(request)
        return {
            "summary": backend.store.summarize_usage(),
            "by_account": backend.store.summarize_usage_by_account(),
        }

    @web_app.get("/api/admin/usage/events")
    def admin_usage_events(
        request: Request,
        account_id: int | None = Query(default=None),
        candidate_id: int | None = Query(default=None),
        session_id: str | None = Query(default=None),
        limit: int = Query(default=200, ge=1, le=5000),
    ) -> dict[str, object]:
        """管理员查看用量流水，保留来源、操作类型和是否可计费状态。"""

        require_admin(request)
        events = backend.store.list_usage_events(account_id, candidate_id, session_id, limit)
        return {"events": [asdict(event) for event in events]}

    return web_app


def stream_web_chat_events(
    backend: JobHuntingApp,
    chat_agent: JobHuntingAgent | None,
    payload: ChatPayload,
    user_message: str,
    session_id: str,
    rag_path: Path,
    account_id: int | None = None,
):
    """生成网页聊天 SSE 事件。

    这个函数集中处理“流式展示”和“最终落库”的顺序：只有成功拿到 final 结果后，
    才会把用户消息和助手展示文本写入聊天历史。
    """

    if payload.use_env_llm:
        assert chat_agent is not None
        try:
            # 真实模型可能会先经历一段思考/排队时间；先发状态事件，让前端立即有反馈。
            yield sse_event("status", {"content": "正在连接模型并等待首个 token..."})
            for event in chat_agent.stream_chat(
                user_message,
                candidate_id=payload.candidate_id,
                session_id=session_id,
                use_tool_llm=True,
                auto_rag=payload.auto_rag,
                account_id=account_id,
            ):
                event_type = event.get("type")
                if event_type == "token":
                    yield sse_event("token", {"content": event.get("content", "")})
                elif event_type == "tool":
                    tool_name = str(event.get("name") or "unknown_tool")
                    yield sse_event("status", {"content": f"工具完成：{tool_name}", "tool": tool_name})
                elif event_type == "final":
                    result = event["result"]
                    display_reply = format_web_chat_reply(
                        mode=result.mode,
                        reply=result.reply,
                        used_tools=result.used_tools,
                        tool_outputs=result.tool_outputs,
                        rule_based_result=None,
                    )
                    save_successful_web_chat_turn(
                        backend,
                        payload.candidate_id,
                        session_id,
                        user_message,
                        display_reply,
                        {"mode": result.mode, "used_tools": result.used_tools, "usage": result.usage},
                        account_id=account_id,
                    )
                    yield sse_event(
                        "final",
                        {
                            "mode": result.mode,
                            "reply": result.reply,
                            "used_tools": result.used_tools,
                            "tool_outputs": result.tool_outputs,
                            "usage": result.usage,
                            "display_reply": display_reply,
                            "profile": asdict(backend.get_candidate_profile(payload.candidate_id, account_id=account_id)),
                        },
                    )
        except RAGProviderRequestError as error:
            yield sse_event("error", {"detail": str(error)})
        except Exception as error:  # noqa: BLE001 - SSE 内统一返回可读错误事件。
            yield sse_event("error", {"detail": str(error)})
        return

    try:
        result = backend.ingest_conversation_message(
            payload.candidate_id,
            user_message,
            llm_client=None,
            rag_persist_directory=rag_path,
            auto_rebuild_rag=payload.auto_rag,
            account_id=account_id,
        )
    except RAGProviderRequestError as error:
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
    save_successful_web_chat_turn(
        backend,
        payload.candidate_id,
        session_id,
        user_message,
        display_reply,
        {"mode": "rule_based_ingestion", "used_tools": ["ingest_conversation_message"]},
        account_id=account_id,
    )
    # 规则兜底没有真实 token 流，这里发送一次完整文本，保持前端路径统一。
    yield sse_event("token", {"content": display_reply})
    yield sse_event(
        "final",
        {
            "mode": "rule_based_ingestion",
            "reply": result.reply,
            "used_tools": ["ingest_conversation_message"],
            "tool_outputs": tool_outputs,
            "result": result_dict,
            "display_reply": display_reply,
            "profile": asdict(backend.get_candidate_profile(payload.candidate_id, account_id=account_id)),
        },
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


def default_web_session_id(candidate_id: int, account_id: int | None = None) -> str:
    """生成网页默认会话 ID。

    目前一个候选人对应一个默认网页聊天窗口；后续如果支持多个求职主题会话，
    可以在前端传入更细的 `session_id`。
    """

    return f"account-{account_id or 'legacy'}-candidate-{candidate_id}"


def save_successful_web_chat_turn(
    backend: JobHuntingApp,
    candidate_id: int,
    session_id: str,
    user_message: str,
    assistant_message: str,
    assistant_metadata: dict[str, object],
    account_id: int | None = None,
) -> None:
    """保存一次成功网页聊天的用户消息和助手消息。

    失败的模型/API 调用不会写入历史，避免用户刷新后看到半截无效回合。
    """

    if account_id is not None:
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

    if mode == "langchain_agent":
        tool_line = f"工具：{'、'.join(used_tools)}" if used_tools else "工具：本轮未调用工具"
        tool_summary = summarize_tool_outputs_for_display(tool_outputs)
        return "\n\n".join(part for part in [reply, tool_line, tool_summary] if part)

    result = rule_based_result or {}
    saved_fields = result.get("saved_structured_fields") or []
    saved_fields_text = "、".join(str(item) for item in saved_fields) or "无结构化字段"
    saved_long_text_ids = result.get("saved_long_text_ids") or []
    long_text_ids_text = "、".join(str(item) for item in saved_long_text_ids) or "无"
    rag_line = (
        "RAG：已增量索引本次长文本"
        if result.get("rag_update_mode") == "incremental"
        else "RAG：本次未更新索引"
    )
    return f"{reply}\n\n保存字段：{saved_fields_text}\n长文本 ID：{long_text_ids_text}\n{rag_line}"


def summarize_tool_outputs_for_display(tool_outputs: list[dict[str, object]]) -> str:
    """把 Agent 工具输出压缩成网页可读摘要。

    这里只生成展示文本，不把工具输出当成新的前端事实源；事实仍以后端 SQLite 为准。
    """

    lines: list[str] = []
    for item in tool_outputs:
        data = item.get("data") if isinstance(item, dict) else None
        if not isinstance(data, dict):
            continue
        if data.get("error"):
            lines.append(f"工具错误：{data['error']}")
        saved_fields = data.get("saved_structured_fields")
        if isinstance(saved_fields, list):
            fields_text = "、".join(str(field) for field in saved_fields) or "无结构化字段"
            lines.append(f"保存字段：{fields_text}")
        saved_long_text_ids = data.get("saved_long_text_ids")
        if isinstance(saved_long_text_ids, list):
            ids_text = "、".join(str(item_id) for item_id in saved_long_text_ids) or "无"
            lines.append(f"长文本 ID：{ids_text}")
        rag_update_mode = data.get("rag_update_mode")
        if rag_update_mode:
            lines.append(
                "RAG：已增量索引本次长文本"
                if rag_update_mode == "incremental"
                else f"RAG：{rag_update_mode}"
            )
        job = data.get("job")
        if isinstance(job, dict) and job.get("title"):
            lines.append(f"导入职位：{job['title']}")
        matches = data.get("matches")
        if isinstance(matches, list) and matches:
            lines.append(f"匹配结果：共 {len(matches)} 个职位，已按推荐顺序返回。")
    return "\n".join(lines)


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

    return {
        str(key).strip(): str(value).strip() or "待确认"
        for key, value in values.items()
        if str(key).strip()
    }


def main(argv: list[str] | None = None) -> None:
    """从命令行启动本地 Web 服务。"""

    parser = argparse.ArgumentParser(prog="job-agent-web")
    parser.add_argument("--db", default="data/job_agent.db")
    parser.add_argument("--env-file", default=".env")
    parser.add_argument("--rag-dir", default="data/chroma")
    parser.add_argument("--resume-dir", default="data/resumes")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args(argv)

    import uvicorn

    uvicorn.run(
        create_web_app(
            args.db,
            args.env_file,
            args.rag_dir,
            resume_dir=args.resume_dir,
            require_auth=True,
        ),
        host=args.host,
        port=args.port,
    )


if __name__ == "__main__":
    main()
