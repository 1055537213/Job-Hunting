"""数据库无关的领域仓储方法。

本模块只保存候选人、职位、对话、简历和用量的领域读写逻辑，不创建数据库连接，
也不负责 schema 初始化。具体连接和事务由 `SQLAlchemyStore` 提供，因此 Web、
后台任务和测试都走同一条 PostgreSQL + pgvector 数据路径。
"""

from __future__ import annotations

import json
from collections.abc import Iterator, Mapping
from dataclasses import asdict, replace
from datetime import UTC, datetime
from typing import Any, Protocol, Self
from uuid import uuid4

from .city_catalog import normalize_city_list
from .deduplication import (
    DuplicateResourceError,
    candidate_profile_content_fingerprint,
    is_unique_constraint_violation,
    job_text_content_fingerprint,
    project_card_content_fingerprint,
)
from .job_parser import classify_skill_requirements, parse_job_text, validate_job_text
from .models import (
    AccountRecord,
    AdminAuditEventRecord,
    AuthSessionRecord,
    BackgroundTaskRecord,
    CandidateProfile,
    CandidateProfileInput,
    CandidateProfilePatch,
    ChatMessageRecord,
    ChatSessionRecord,
    ImportedJob,
    LongTextRecord,
    ProjectExperienceCard,
    ProjectExperienceRecord,
    ResumeArtifactRecord,
    ResumeDraft,
    ResumeDraftRecord,
    SkillRequirement,
    ToolCallTraceRecord,
    UsageEventRecord,
    sanitize_preference_weights,
)
from .skill_normalization import merge_skill_mappings, normalize_skill_mapping
from .tool_audit import tool_audit_retention_cutoff

RESUME_ARTIFACT_STATUSES = {"ready", "processing", "failed"}


class RepositoryRow(Protocol):
    """仓储方法读取的最小行接口；具体实现由 SQLAlchemy 适配层提供。"""

    def __getitem__(self, key: str) -> Any:
        ...

    def keys(self) -> list[str]:
        ...

    def __contains__(self, key: str) -> bool:
        ...

    def get(self, key: str, default: Any = None) -> Any:
        ...

    def __iter__(self) -> Iterator[str]:
        ...


class RepositoryConnection(Protocol):
    """仓储方法需要的最小事务连接接口。"""

    def __enter__(self) -> Self:
        ...

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> bool:
        ...

    def execute(self, sql: str, parameters: object = None) -> Any:
        ...


class RepositoryStore:
    """封装领域读写逻辑；连接、事务和 schema 由具体数据库适配层负责。"""

    def connect(self) -> RepositoryConnection:
        """返回一次短生命周期事务连接。"""

        raise NotImplementedError

    def initialize(self) -> None:
        """确认数据库已经由 Alembic 管理并完成迁移。"""

        raise NotImplementedError
    # 账号、Session、会话和用量流水
    # ------------------------------------------------------------------

    def create_account(
        self,
        email: str,
        password_hash: str,
        display_name: str | None = None,
        role: str = "user",
        status: str = "active",
        must_change_password: bool = False,
    ) -> AccountRecord:
        """写入一个账号并返回不含密码的账号记录。"""

        if role not in {"user", "admin"}:
            raise ValueError("账号角色只能是 user 或 admin。")
        if status not in {"active", "disabled"}:
            raise ValueError("账号状态只能是 active 或 disabled。")
        now = now_iso()
        with self.connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO accounts (
                    email, password_hash, display_name, role, status,
                    must_change_password, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    email,
                    password_hash,
                    display_name,
                    role,
                    status,
                    bool(must_change_password),
                    now,
                    now,
                ),
            )
            account_id = int(cursor.lastrowid)
        return self.get_account(account_id)

    def get_account(self, account_id: int) -> AccountRecord:
        """按 ID 读取账号；密码哈希不会暴露给调用方。"""

        with self.connect() as conn:
            row = conn.execute("SELECT * FROM accounts WHERE id = ?", (account_id,)).fetchone()
        if row is None:
            raise KeyError(f"Account not found: {account_id}")
        return account_from_row(row)

    def get_account_with_password(self, account_id: int) -> tuple[AccountRecord, str]:
        """认证服务内部读取账号和哈希；其他业务代码不应调用此方法。"""

        with self.connect() as conn:
            row = conn.execute("SELECT * FROM accounts WHERE id = ?", (account_id,)).fetchone()
        if row is None:
            raise KeyError(f"Account not found: {account_id}")
        return account_from_row(row), str(row["password_hash"])

    def get_account_by_email(self, email: str) -> tuple[AccountRecord, str] | None:
        """按不区分大小写的邮箱读取账号和密码哈希。"""

        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM accounts WHERE LOWER(email) = LOWER(?)",
                (email,),
            ).fetchone()
        if row is None:
            return None
        return account_from_row(row), str(row["password_hash"])

    def list_accounts(self, include_disabled: bool = True) -> list[AccountRecord]:
        """列出账号，供管理员后台展示。"""

        with self.connect() as conn:
            if include_disabled:
                rows = conn.execute("SELECT * FROM accounts ORDER BY id").fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM accounts WHERE status = 'active' ORDER BY id"
                ).fetchall()
        return [account_from_row(row) for row in rows]

    def count_active_admins(self) -> int:
        """返回当前仍可登录的管理员数量。"""

        with self.connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS count FROM accounts WHERE role = 'admin' AND status = 'active'"
            ).fetchone()
        return int(row["count"])

    def touch_account_login(self, account_id: int) -> None:
        """记录最近一次成功登录时间，兼容 Web 认证门面。"""

        with self.connect() as conn:
            conn.execute(
                "UPDATE accounts SET updated_at = ? WHERE id = ?",
                (now_iso(), account_id),
            )

    def update_account_status(self, account_id: int, status: str) -> AccountRecord:
        """更新账号状态，例如管理员禁用或恢复账号。"""

        if status not in {"active", "disabled"}:
            raise ValueError("账号状态只能是 active 或 disabled。")
        with self.connect() as conn:
            cursor = conn.execute(
                "UPDATE accounts SET status = ?, updated_at = ? WHERE id = ?",
                (status, now_iso(), account_id),
            )
            if cursor.rowcount == 0:
                raise KeyError(f"Account not found: {account_id}")
        return self.get_account(account_id)

    def update_account_status_with_audit(
        self,
        account_id: int,
        status: str,
        audit_event: AdminAuditEventRecord,
        *,
        revoke_sessions: bool = False,
    ) -> AccountRecord:
        """在同一事务中更新账号状态、撤销 Session 并写入管理员审计。"""

        if status not in {"active", "disabled"}:
            raise ValueError("账号状态只能是 active 或 disabled。")
        if audit_event.target_account_id != account_id:
            raise ValueError("账号状态审计的目标账号与写入目标不一致。")
        if audit_event.actor_account_id is not None:
            self.get_account(audit_event.actor_account_id)
        self.get_account(account_id)
        changed_at = now_iso()
        with self.connect() as conn:
            cursor = conn.execute(
                "UPDATE accounts SET status = ?, updated_at = ? WHERE id = ?",
                (status, changed_at, account_id),
            )
            if cursor.rowcount == 0:
                raise KeyError(f"Account not found: {account_id}")
            if revoke_sessions:
                conn.execute(
                    """
                    UPDATE auth_sessions SET revoked_at = ?
                    WHERE account_id = ? AND revoked_at IS NULL
                    """,
                    (changed_at, account_id),
                )
            self._insert_admin_audit_event(conn, audit_event)
        return self.get_account(account_id)

    def update_account_password(
        self,
        account_id: int,
        password_hash: str,
        must_change_password: bool = False,
    ) -> AccountRecord:
        """更新密码哈希，并可清除首次登录改密标记。"""

        with self.connect() as conn:
            cursor = conn.execute(
                """
                UPDATE accounts
                SET password_hash = ?, must_change_password = ?, updated_at = ?
                WHERE id = ?
                """,
                (password_hash, bool(must_change_password), now_iso(), account_id),
            )
            if cursor.rowcount == 0:
                raise KeyError(f"Account not found: {account_id}")
        return self.get_account(account_id)

    def save_auth_session(
        self,
        account_id: int,
        token_hash: str,
        created_at: str,
        last_seen_at: str,
        expires_at: str,
        absolute_expires_at: str,
        user_agent: str | None = None,
        ip_address: str | None = None,
    ) -> AuthSessionRecord:
        """保存服务端 Session；Cookie 原文不会进入数据库。"""

        with self.connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO auth_sessions (
                    account_id, token_hash, created_at, last_seen_at, expires_at,
                    absolute_expires_at, revoked_at, user_agent, ip_address
                ) VALUES (?, ?, ?, ?, ?, ?, NULL, ?, ?)
                """,
                (
                    account_id,
                    token_hash,
                    created_at,
                    last_seen_at,
                    expires_at,
                    absolute_expires_at,
                    user_agent,
                    ip_address,
                ),
            )
            session_id = int(cursor.lastrowid)
        return self.get_auth_session(session_id)

    def get_auth_session(self, session_id: int) -> AuthSessionRecord:
        """按数据库 ID 读取 Session。"""

        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM auth_sessions WHERE id = ?", (session_id,)
            ).fetchone()
        if row is None:
            raise KeyError(f"Auth session not found: {session_id}")
        return auth_session_from_row(row)

    def get_auth_session_by_token_hash(self, token_hash: str) -> AuthSessionRecord | None:
        """按令牌摘要读取 Session。"""

        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM auth_sessions WHERE token_hash = ?", (token_hash,)
            ).fetchone()
        return auth_session_from_row(row) if row is not None else None

    def touch_auth_session(self, session_id: int, last_seen_at: str, expires_at: str) -> None:
        """滑动更新闲置过期时间，但绝不延长绝对过期时间。"""

        with self.connect() as conn:
            conn.execute(
                """
                UPDATE auth_sessions
                SET last_seen_at = ?, expires_at = ?
                WHERE id = ? AND revoked_at IS NULL
                """,
                (last_seen_at, expires_at, session_id),
            )

    def revoke_auth_session(self, session_id: int) -> None:
        """撤销一个 Session，使其立即失效。"""

        with self.connect() as conn:
            conn.execute(
                """
                UPDATE auth_sessions SET revoked_at = COALESCE(revoked_at, ?)
                WHERE id = ?
                """,
                (now_iso(), session_id),
            )

    def revoke_all_auth_sessions(self, account_id: int) -> int:
        """撤销账号的全部登录设备并返回受影响的 Session 数。"""

        with self.connect() as conn:
            cursor = conn.execute(
                """
                UPDATE auth_sessions SET revoked_at = ?
                WHERE account_id = ? AND revoked_at IS NULL
                """,
                (now_iso(), account_id),
            )
            return int(cursor.rowcount)

    def revoke_all_auth_sessions_with_audit(
        self,
        account_id: int,
        audit_event: AdminAuditEventRecord,
    ) -> int:
        """在同一事务中撤销全部 Session 并写入管理员审计。"""

        if audit_event.target_account_id != account_id:
            raise ValueError("登录会话审计的目标账号与撤销目标不一致。")
        self._validate_admin_audit_event(audit_event)
        self.get_account(account_id)
        revoked_at = now_iso()
        with self.connect() as conn:
            cursor = conn.execute(
                """
                UPDATE auth_sessions SET revoked_at = ?
                WHERE account_id = ? AND revoked_at IS NULL
                """,
                (revoked_at, account_id),
            )
            event = replace(
                audit_event,
                details={
                    **(audit_event.details or {}),
                    "revoked_sessions": int(cursor.rowcount),
                },
            )
            self._insert_admin_audit_event(conn, event)
            return int(cursor.rowcount)

    def create_chat_session(
        self,
        session_id: str,
        account_id: int,
        candidate_id: int,
        title: str,
        job_id: int | None = None,
    ) -> ChatSessionRecord:
        """创建一个账号内、绑定候选人档案的独立对话。"""

        # 同一账号可以访问其全部档案，但对话不能绑定到其他账号的档案或职位。
        self.get_candidate_profile(candidate_id, account_id=account_id)
        if job_id is not None:
            self.get_job(job_id, account_id=account_id)
        now = now_iso()
        with self.connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO chat_sessions (
                    session_id, account_id, candidate_id, job_id, title,
                    status, created_at, updated_at, archived_at
                ) VALUES (?, ?, ?, ?, ?, 'active', ?, ?, NULL)
                """,
                (session_id, account_id, candidate_id, job_id, title, now, now),
            )
            record_id = int(cursor.lastrowid)
        return self.get_chat_session(record_id)

    def get_chat_session(self, record_id: int) -> ChatSessionRecord:
        """按自增 ID 读取独立对话。"""

        with self.connect() as conn:
            row = conn.execute("SELECT * FROM chat_sessions WHERE id = ?", (record_id,)).fetchone()
        if row is None:
            raise KeyError(f"Chat session not found: {record_id}")
        return chat_session_from_row(row)

    def get_chat_session_by_key(self, session_id: str, account_id: int | None) -> ChatSessionRecord:
        """按账号和公开 Session ID读取对话，防止跨账号猜 ID。"""

        with self.connect() as conn:
            if account_id is None:
                row = conn.execute(
                    """
                    SELECT * FROM chat_sessions
                    WHERE session_id = ? AND account_id IS NULL
                    """,
                    (session_id,),
                ).fetchone()
            else:
                row = conn.execute(
                    """
                    SELECT * FROM chat_sessions
                    WHERE session_id = ? AND account_id = ?
                    """,
                    (session_id, account_id),
                ).fetchone()
        if row is None:
            raise KeyError(f"Chat session not found: {session_id}")
        return chat_session_from_row(row)

    def list_chat_sessions(
        self,
        account_id: int,
        candidate_id: int | None = None,
        include_archived: bool = False,
    ) -> list[ChatSessionRecord]:
        """列出账号内对话，可按候选人档案过滤。"""

        conditions = ["account_id = ?"]
        parameters: list[object] = [account_id]
        if candidate_id is not None:
            conditions.append("candidate_id = ?")
            parameters.append(candidate_id)
        if not include_archived:
            conditions.append("status = 'active'")
        with self.connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM chat_sessions WHERE {' AND '.join(conditions)} "
                "ORDER BY updated_at DESC, id DESC",
                tuple(parameters),
            ).fetchall()
        return [chat_session_from_row(row) for row in rows]

    def archive_chat_session(self, session_id: str, account_id: int) -> ChatSessionRecord:
        """软归档一段对话，保留历史和计费流水。"""

        now = now_iso()
        with self.connect() as conn:
            cursor = conn.execute(
                """
                UPDATE chat_sessions
                SET status = 'archived', archived_at = ?, updated_at = ?
                WHERE session_id = ? AND account_id = ?
                """,
                (now, now, session_id, account_id),
            )
            if cursor.rowcount == 0:
                raise KeyError(f"Chat session not found: {session_id}")
        return self.get_chat_session_by_key(session_id, account_id)

    def delete_chat_session(self, session_id: str, account_id: int) -> dict[str, object]:
        """永久删除当前账号的一段对话及其消息，但保留用量流水。"""

        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT id, candidate_id
                FROM chat_sessions
                WHERE session_id = ? AND account_id = ?
                """,
                (session_id, account_id),
            ).fetchone()
            if row is None:
                raise KeyError(f"Chat session not found: {session_id}")
            candidate_id = int(row["candidate_id"])
            conn.execute(
                """
                DELETE FROM chat_messages
                WHERE session_id = ? AND account_id = ? AND candidate_id = ?
                """,
                (session_id, account_id, candidate_id),
            )
            conn.execute("DELETE FROM chat_sessions WHERE id = ?", (int(row["id"]),))
        return {
            "session_id": session_id,
            "candidate_id": candidate_id,
        }

    def record_usage_event(self, event: UsageEventRecord) -> UsageEventRecord:
        """追加一条用量流水；相同 `call_id` 重复上报时保持幂等。"""

        if event.usage_source not in {"provider", "estimated", "missing", "local"}:
            raise ValueError(f"Unsupported usage source: {event.usage_source}")
        self.get_account(event.account_id)
        if event.candidate_id is not None:
            self.get_candidate_profile(event.candidate_id, account_id=event.account_id)
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO usage_events (
                    account_id, candidate_id, session_id, root_request_id, call_id,
                    provider, model, operation, input_tokens, output_tokens,
                    total_tokens, usage_source, status, attempt, provider_request_id,
                    raw_usage_json, created_at, billable, pricing_version
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (call_id) DO NOTHING
                """,
                (
                    event.account_id,
                    event.candidate_id,
                    event.session_id,
                    event.root_request_id,
                    event.call_id,
                    event.provider,
                    event.model,
                    event.operation,
                    max(0, int(event.input_tokens)),
                    max(0, int(event.output_tokens)),
                    max(0, int(event.total_tokens)),
                    event.usage_source,
                    event.status,
                    max(1, int(event.attempt)),
                    event.provider_request_id,
                    json.dumps(event.raw_usage or {}, ensure_ascii=False),
                    event.created_at,
                    # 只有供应商确认的成功用量才进入正式计费汇总；estimated/missing/local
                    # 仍会保留明细，但不会被误加到 billable_tokens。
                    bool(event.billable and event.usage_source == "provider" and event.status == "succeeded"),
                    event.pricing_version,
                ),
            )
            row = conn.execute(
                "SELECT * FROM usage_events WHERE call_id = ?", (event.call_id,)
            ).fetchone()
        if row is None:  # pragma: no cover - 仅在数据库异常时触发
            raise RuntimeError(f"Usage event was not persisted: {event.call_id}")
        return usage_event_from_row(row)

    def list_usage_events(
        self,
        account_id: int | None = None,
        candidate_id: int | None = None,
        session_id: str | None = None,
        limit: int = 200,
    ) -> list[UsageEventRecord]:
        """列出用量明细；管理员不传账号过滤时才可查看全局数据。"""

        conditions: list[str] = []
        parameters: list[object] = []
        if account_id is not None:
            conditions.append("account_id = ?")
            parameters.append(account_id)
        if candidate_id is not None:
            conditions.append("candidate_id = ?")
            parameters.append(candidate_id)
        if session_id is not None:
            conditions.append("session_id = ?")
            parameters.append(session_id)
        where = f" WHERE {' AND '.join(conditions)}" if conditions else ""
        parameters.append(max(1, min(limit, 5000)))
        with self.connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM usage_events{where} ORDER BY id DESC LIMIT ?",
                tuple(parameters),
            ).fetchall()
        return [usage_event_from_row(row) for row in rows]

    def summarize_usage(self, account_id: int | None = None) -> dict[str, int]:
        """汇总 Token；`billable_tokens` 只计算标记为可计费的流水。"""

        where = " WHERE account_id = ?" if account_id is not None else ""
        parameters = (account_id,) if account_id is not None else ()
        with self.connect() as conn:
            row = conn.execute(
                f"""
                SELECT
                    COALESCE(SUM(input_tokens), 0) AS input_tokens,
                    COALESCE(SUM(output_tokens), 0) AS output_tokens,
                    COALESCE(SUM(total_tokens), 0) AS total_tokens,
                    COALESCE(SUM(CASE WHEN billable THEN total_tokens ELSE 0 END), 0)
                        AS billable_tokens,
                    COUNT(*) AS event_count
                FROM usage_events{where}
                """,
                parameters,
            ).fetchone()
        return {key: int(row[key]) for key in row}

    def summarize_usage_by_account(self) -> list[dict[str, int]]:
        """按账号聚合 Token，供管理员查看不同计费主体的用量。"""

        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT
                    account_id,
                    COALESCE(SUM(input_tokens), 0) AS input_tokens,
                    COALESCE(SUM(output_tokens), 0) AS output_tokens,
                    COALESCE(SUM(total_tokens), 0) AS total_tokens,
                    COALESCE(SUM(CASE WHEN billable THEN total_tokens ELSE 0 END), 0)
                        AS billable_tokens,
                    COUNT(*) AS event_count
                FROM usage_events
                GROUP BY account_id
                ORDER BY account_id
                """
            ).fetchall()
        return [
            {key: int(row[key]) for key in row}
            for row in rows
        ]

    def record_tool_call_trace(self, trace: ToolCallTraceRecord) -> ToolCallTraceRecord:
        """写入或更新一次工具调用审计轨迹；同一 root_request_id 保持一条任务记录。"""

        if trace.account_id <= 0:
            raise ValueError("工具调用审计必须属于一个有效账号。")
        root_request_id = trace.root_request_id.strip()
        if not root_request_id:
            raise ValueError("工具调用审计缺少 root_request_id。")
        if trace.status not in {"running", "waiting_confirmation", "completed", "failed", "cancelled"}:
            raise ValueError(f"Unsupported tool trace status: {trace.status}")
        self.get_account(trace.account_id)
        if trace.candidate_id is not None:
            self.get_candidate_profile(trace.candidate_id, account_id=trace.account_id)
        now = now_iso()
        created_at = trace.created_at or now
        updated_at = trace.updated_at or now
        trace_payload = json.dumps(trace.trace or {}, ensure_ascii=False)
        with self.connect() as conn:
            # Beat 可能尚未在午夜完成清理。若同一链路 ID 在保留窗口外再次出现，
            # 先移除旧记录，避免 ON CONFLICT 保留过期 created_at 后被查询条件隐藏。
            existing = conn.execute(
                """
                SELECT account_id, created_at FROM tool_call_traces
                WHERE root_request_id = ?
                """,
                (root_request_id,),
            ).fetchone()
            if (
                existing is not None
                and int(existing["account_id"]) == trace.account_id
                and str(existing["created_at"]) < tool_audit_retention_cutoff()
            ):
                conn.execute(
                    """
                    DELETE FROM tool_call_traces
                    WHERE root_request_id = ? AND account_id = ?
                    """,
                    (root_request_id, trace.account_id),
                )
            conn.execute(
                """
                INSERT INTO tool_call_traces (
                    account_id, candidate_id, session_id, root_request_id,
                    title, status, source, step_count, attempt_count,
                    last_step_name, last_error_summary, trace_json,
                    created_at, started_at, finished_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (root_request_id) DO UPDATE SET
                    candidate_id = COALESCE(EXCLUDED.candidate_id, tool_call_traces.candidate_id),
                    session_id = COALESCE(EXCLUDED.session_id, tool_call_traces.session_id),
                    title = EXCLUDED.title,
                    status = EXCLUDED.status,
                    source = EXCLUDED.source,
                    step_count = EXCLUDED.step_count,
                    attempt_count = EXCLUDED.attempt_count,
                    last_step_name = EXCLUDED.last_step_name,
                    last_error_summary = EXCLUDED.last_error_summary,
                    trace_json = EXCLUDED.trace_json,
                    started_at = COALESCE(tool_call_traces.started_at, EXCLUDED.started_at),
                    finished_at = EXCLUDED.finished_at,
                    updated_at = EXCLUDED.updated_at
                WHERE tool_call_traces.account_id = EXCLUDED.account_id
                """,
                (
                    trace.account_id,
                    trace.candidate_id,
                    trace.session_id,
                    root_request_id,
                    trace.title[:256],
                    trace.status,
                    trace.source[:32],
                    max(0, int(trace.step_count)),
                    max(0, int(trace.attempt_count)),
                    trace.last_step_name[:128] if trace.last_step_name else None,
                    trim_task_error(trace.last_error_summary),
                    trace_payload,
                    created_at,
                    trace.started_at,
                    trace.finished_at,
                    updated_at,
                ),
            )
            row = conn.execute(
                """
                SELECT * FROM tool_call_traces
                WHERE root_request_id = ? AND account_id = ?
                """,
                (root_request_id, trace.account_id),
            ).fetchone()
        if row is None:
            raise ValueError("该 root_request_id 已属于其他账号，不能覆盖其工具轨迹。")
        return tool_call_trace_from_row(row)

    def get_tool_call_trace(
        self,
        root_request_id: str,
        account_id: int | None = None,
        *,
        cutoff_iso: str | None = None,
    ) -> ToolCallTraceRecord:
        """读取一条工具调用审计轨迹，并可按账号隔离。"""

        conditions = ["root_request_id = ?"]
        parameters: list[object] = [root_request_id]
        if account_id is not None:
            conditions.append("account_id = ?")
            parameters.append(account_id)
        if cutoff_iso is not None:
            conditions.append("created_at >= ?")
            parameters.append(cutoff_iso)
        with self.connect() as conn:
            row = conn.execute(
                f"""
                SELECT * FROM tool_call_traces
                WHERE {' AND '.join(conditions)}
                """,
                tuple(parameters),
            ).fetchone()
        if row is None:
            raise KeyError(f"Tool call trace not found: {root_request_id}")
        return tool_call_trace_from_row(row)

    def list_tool_call_traces(
        self,
        account_id: int | None = None,
        *,
        limit: int = 50,
        offset: int = 0,
        cutoff_iso: str | None = None,
    ) -> list[ToolCallTraceRecord]:
        """分页列出最近工具调用任务，详情由单条读取接口按需加载。"""

        conditions = ["created_at >= ?"]
        parameters: list[object] = [cutoff_iso or tool_audit_retention_cutoff()]
        if account_id is not None:
            conditions.append("account_id = ?")
            parameters.append(account_id)
        where = f" WHERE {' AND '.join(conditions)}" if conditions else ""
        parameters.extend([max(1, min(limit, 200)), max(0, int(offset))])
        with self.connect() as conn:
            rows = conn.execute(
                f"""
                SELECT * FROM tool_call_traces{where}
                ORDER BY updated_at DESC, id DESC
                LIMIT ? OFFSET ?
                """,
                tuple(parameters),
            ).fetchall()
        return [tool_call_trace_from_row(row) for row in rows]

    def count_tool_call_traces(
        self,
        account_id: int | None = None,
        *,
        cutoff_iso: str | None = None,
    ) -> int:
        """返回工具调用任务数量，供分页 UI 展示总数。"""

        conditions = ["created_at >= ?"]
        parameters: list[object] = [cutoff_iso or tool_audit_retention_cutoff()]
        if account_id is not None:
            conditions.append("account_id = ?")
            parameters.append(account_id)
        where = f" WHERE {' AND '.join(conditions)}"
        with self.connect() as conn:
            row = conn.execute(
                f"SELECT COUNT(*) AS count FROM tool_call_traces{where}",
                tuple(parameters),
            ).fetchone()
        return int(row["count"])

    def summarize_tool_call_traces_by_account(
        self,
        *,
        cutoff_iso: str | None = None,
    ) -> list[dict[str, int]]:
        """按账号聚合工具调用任务数量和失败数量。"""

        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT
                    account_id,
                    COUNT(*) AS trace_count,
                    COALESCE(SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END), 0)
                        AS failed_trace_count
                FROM tool_call_traces
                WHERE created_at >= ?
                GROUP BY account_id
                ORDER BY account_id
                """,
                (cutoff_iso or tool_audit_retention_cutoff(),),
            ).fetchall()
        return [
            {
                "account_id": int(row["account_id"]),
                "trace_count": int(row["trace_count"]),
                "failed_trace_count": int(row["failed_trace_count"]),
            }
            for row in rows
        ]

    def delete_tool_call_traces_before(self, cutoff_iso: str) -> int:
        """删除保留窗口之前的工具调用轨迹，返回删除条数。"""

        with self.connect() as conn:
            cursor = conn.execute(
                "DELETE FROM tool_call_traces WHERE created_at < ?",
                (cutoff_iso,),
            )
        return max(0, int(cursor.rowcount or 0))

    def record_admin_audit_event(self, event: AdminAuditEventRecord) -> AdminAuditEventRecord:
        """追加记录一次管理员动作，不保存正文、查询参数或密钥。"""

        self._validate_admin_audit_event(event)
        with self.connect() as conn:
            record_id = self._insert_admin_audit_event(conn, event)
            row = conn.execute(
                "SELECT * FROM admin_audit_events WHERE id = ?",
                (record_id,),
            ).fetchone()
        if row is None:  # pragma: no cover - 仅在数据库异常时触发
            raise RuntimeError("Admin audit event was not persisted.")
        return admin_audit_event_from_row(row)

    def _validate_admin_audit_event(self, event: AdminAuditEventRecord) -> None:
        """校验审计事件关联的账号，供独立和复合事务共同使用。"""

        if event.actor_account_id is not None:
            if event.actor_account_id <= 0:
                raise ValueError("管理员审计事件的操作者账号无效。")
            self.get_account(event.actor_account_id)
        if event.target_account_id is not None:
            if event.target_account_id <= 0:
                raise ValueError("管理员审计事件的目标账号无效。")
            self.get_account(event.target_account_id)

    def _insert_admin_audit_event(
        self,
        conn: RepositoryConnection,
        event: AdminAuditEventRecord,
    ) -> int:
        """在调用方现有事务中插入一条已脱敏的管理员审计事件。"""

        if event.outcome not in {"succeeded", "blocked", "failed"}:
            raise ValueError(f"Unsupported admin audit outcome: {event.outcome}")
        action = event.action.strip()[:96]
        target_type = event.target_type.strip()[:64]
        if not action:
            raise ValueError("管理员审计事件缺少动作。")
        if not target_type:
            raise ValueError("管理员审计事件缺少目标类型。")
        cursor = conn.execute(
            """
            INSERT INTO admin_audit_events (
                actor_account_id, target_account_id, action, target_type,
                target_id, outcome, summary, details_json, request_id, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event.actor_account_id,
                event.target_account_id,
                action,
                target_type,
                str(event.target_id)[:160] if event.target_id else None,
                event.outcome,
                trim_audit_text(event.summary) or "管理员操作已记录。",
                json.dumps(event.details or {}, ensure_ascii=False),
                str(event.request_id)[:128] if event.request_id else None,
                event.created_at or now_iso(),
            ),
        )
        return int(cursor.lastrowid)

    def list_admin_audit_events(
        self,
        *,
        limit: int = 50,
        offset: int = 0,
    ) -> list[AdminAuditEventRecord]:
        """按时间倒序列出最近管理员审计事件。"""

        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM admin_audit_events
                ORDER BY created_at DESC, id DESC
                LIMIT ? OFFSET ?
                """,
                (max(1, min(limit, 200)), max(0, int(offset))),
            ).fetchall()
        return [admin_audit_event_from_row(row) for row in rows]

    # 后台任务状态
    # ------------------------------------------------------------------

    def create_background_task(
        self,
        *,
        account_id: int,
        task_type: str,
        payload: Mapping[str, object] | None = None,
        candidate_id: int | None = None,
        session_id: str | None = None,
        idempotency_key: str | None = None,
        max_attempts: int = 3,
        audit_event: AdminAuditEventRecord | None = None,
    ) -> BackgroundTaskRecord:
        """创建一条待执行任务；相同账号和幂等键重复提交会复用原任务。"""

        if account_id <= 0:
            raise ValueError("后台任务必须属于一个有效账号。")
        normalized_type = task_type.strip()
        if not normalized_type:
            raise ValueError("后台任务类型不能为空。")
        if max_attempts <= 0:
            raise ValueError("后台任务最大尝试次数必须大于 0。")
        self.get_account(account_id)
        if candidate_id is not None:
            self.get_candidate_profile(candidate_id, account_id=account_id)
        if audit_event is not None:
            self._validate_admin_audit_event(audit_event)

        payload_json = json.dumps(dict(payload or {}), ensure_ascii=False)
        now = now_iso()
        task_key = uuid4().hex
        with self.connect() as conn:
            # 先复用幂等任务，避免网络重试重复创建队列消息和计费操作。
            if idempotency_key:
                existing = conn.execute(
                    """
                    SELECT * FROM background_tasks
                    WHERE account_id = ? AND idempotency_key = ?
                    """,
                    (account_id, idempotency_key),
                ).fetchone()
                if existing is not None:
                    row = existing
                else:
                    row = None
            else:
                row = None
            if row is None:
                conn.execute(
                    """
                    INSERT INTO background_tasks (
                        task_key, account_id, candidate_id, session_id, task_type,
                        status, progress, attempt, max_attempts, idempotency_key,
                        payload_json, result_json, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, 'queued', 0, 0, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT (account_id, idempotency_key) DO NOTHING
                    """,
                    (
                        task_key,
                        account_id,
                        candidate_id,
                        session_id,
                        normalized_type,
                        max_attempts,
                        idempotency_key,
                        payload_json,
                        json.dumps({}, ensure_ascii=False),
                        now,
                        now,
                    ),
                )
                row = conn.execute(
                    "SELECT * FROM background_tasks WHERE task_key = ?",
                    (task_key,),
                ).fetchone()
                if row is None and idempotency_key:
                    row = conn.execute(
                        """
                        SELECT * FROM background_tasks
                        WHERE account_id = ? AND idempotency_key = ?
                        """,
                        (account_id, idempotency_key),
                    ).fetchone()
            if row is None:  # pragma: no cover - 仅在数据库异常时触发
                raise RuntimeError("后台任务创建后无法读取任务记录。")
            record = background_task_from_row(row)
            if audit_event is not None:
                if audit_event.target_type != "background_task":
                    raise ValueError("后台任务审计的目标类型必须是 background_task。")
                event = replace(
                    audit_event,
                    target_id=record.task_key,
                    details={
                        **(audit_event.details or {}),
                        "task_key": record.task_key,
                        "task_type": record.task_type,
                    },
                )
                # 任务登记和审计写入必须随同一数据库事务提交；否则审计失败时会
                # 留下一条已经可投递的孤立任务。
                self._insert_admin_audit_event(conn, event)
        return record

    def get_background_task(
        self,
        task_key: str,
        account_id: int | None = None,
    ) -> BackgroundTaskRecord:
        """按任务键读取状态，并可按账号强制隔离。"""

        with self.connect() as conn:
            if account_id is None:
                row = conn.execute(
                    "SELECT * FROM background_tasks WHERE task_key = ?",
                    (task_key,),
                ).fetchone()
            else:
                row = conn.execute(
                    """
                    SELECT * FROM background_tasks
                    WHERE task_key = ? AND account_id = ?
                    """,
                    (task_key, account_id),
                ).fetchone()
        if row is None:
            raise KeyError(f"Background task not found: {task_key}")
        return background_task_from_row(row)

    def get_background_task_by_idempotency(
        self,
        account_id: int,
        idempotency_key: str,
    ) -> BackgroundTaskRecord | None:
        """按账号和幂等键读取既有任务，避免重复投递同一消息。"""

        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM background_tasks
                WHERE account_id = ? AND idempotency_key = ?
                """,
                (account_id, idempotency_key),
            ).fetchone()
        return background_task_from_row(row) if row is not None else None

    def list_background_tasks(
        self,
        *,
        account_id: int,
        candidate_id: int | None = None,
        status: str | None = None,
        limit: int = 100,
    ) -> list[BackgroundTaskRecord]:
        """列出当前账号的任务，默认按最近更新时间倒序。"""

        conditions = ["account_id = ?"]
        parameters: list[object] = [account_id]
        if candidate_id is not None:
            conditions.append("candidate_id = ?")
            parameters.append(candidate_id)
        if status is not None:
            conditions.append("status = ?")
            parameters.append(status)
        parameters.append(max(1, min(limit, 500)))
        with self.connect() as conn:
            rows = conn.execute(
                f"""
                SELECT * FROM background_tasks
                WHERE {' AND '.join(conditions)}
                ORDER BY updated_at DESC, id DESC
                LIMIT ?
                """,
                tuple(parameters),
            ).fetchall()
        return [background_task_from_row(row) for row in rows]

    def claim_background_task(self, task_key: str) -> BackgroundTaskRecord | None:
        """原子认领 queued 任务；没有取得执行权时返回 ``None``。

        Celery 在 Worker 丢失或 broker 重投时可能再次交付同一 ``task_key``。调用方
        只有在条件更新确实把状态从 queued 改为 running 后才能执行任务正文，避免
        两个 Worker 同时生成重复文件、项目卡片或模型调用。
        """

        now = now_iso()
        with self.connect() as conn:
            cursor = conn.execute(
                """
                UPDATE background_tasks
                SET status = 'running',
                    progress = GREATEST(progress, 1),
                    attempt = attempt + 1,
                    started_at = COALESCE(started_at, ?),
                    updated_at = ?
                WHERE task_key = ? AND status = 'queued'
                """,
                (now, now, task_key),
            )
        if cursor.rowcount == 0:
            # 记录可能已被其他 Worker 认领或已经结束；调用方读取现有状态后直接退出。
            self.get_background_task(task_key)
            return None
        return self.get_background_task(task_key)

    def retry_failed_background_task(self, task_key: str) -> BackgroundTaskRecord:
        """把失败任务恢复为 queued，让同一幂等请求可以重新投递。

        任务 payload 和 task_key 保持不变，便于审计和前端继续轮询；尝试次数归零，
        使人工重试拥有一组新的 Worker 重试预算。并发恢复只有第一次更新生效，重复
        投递仍会由 ``claim_background_task`` 的原子认领保护。
        """

        with self.connect() as conn:
            conn.execute(
                """
                UPDATE background_tasks
                SET status = 'queued', progress = 0, attempt = 0,
                    result_json = ?, error_summary = NULL,
                    started_at = NULL, finished_at = NULL, updated_at = ?
                WHERE task_key = ? AND status = 'failed'
                """,
                (json.dumps({}, ensure_ascii=False), now_iso(), task_key),
            )
        return self.get_background_task(task_key)

    def update_background_task_progress(self, task_key: str, progress: int) -> BackgroundTaskRecord:
        """更新运行中任务的进度，始终限制在 0 到 100。"""

        bounded_progress = max(0, min(100, int(progress)))
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE background_tasks
                SET progress = ?, updated_at = ?
                WHERE task_key = ? AND status = 'running'
                """,
                (bounded_progress, now_iso(), task_key),
            )
        return self.get_background_task(task_key)

    def complete_background_task(
        self,
        task_key: str,
        result: Mapping[str, object] | None = None,
    ) -> BackgroundTaskRecord:
        """把任务标记为成功，并保存不含正文的结果摘要。"""

        now = now_iso()
        with self.connect() as conn:
            cursor = conn.execute(
                """
                UPDATE background_tasks
                SET status = 'succeeded', progress = 100, result_json = ?,
                    error_summary = NULL, finished_at = ?, updated_at = ?
                WHERE task_key = ? AND status = 'running'
                """,
                (json.dumps(dict(result or {}), ensure_ascii=False), now, now, task_key),
            )
            if cursor.rowcount == 0:
                raise KeyError(f"Running background task not found: {task_key}")
        return self.get_background_task(task_key)

    def requeue_background_task(self, task_key: str, error_summary: str | None = None) -> BackgroundTaskRecord:
        """把本轮可重试的任务放回 queued 状态，保留尝试次数和错误摘要。"""

        with self.connect() as conn:
            cursor = conn.execute(
                """
                UPDATE background_tasks
                SET status = 'queued', error_summary = ?, updated_at = ?
                WHERE task_key = ? AND status = 'running'
                """,
                (trim_task_error(error_summary), now_iso(), task_key),
            )
            if cursor.rowcount == 0:
                raise KeyError(f"Running background task not found: {task_key}")
        return self.get_background_task(task_key)

    def fail_background_task(self, task_key: str, error_summary: str) -> BackgroundTaskRecord:
        """把任务标记为失败，只保存截断后的运维摘要。"""

        now = now_iso()
        with self.connect() as conn:
            cursor = conn.execute(
                """
                UPDATE background_tasks
                SET status = 'failed', error_summary = ?, finished_at = ?, updated_at = ?
                WHERE task_key = ? AND status = 'running'
                """,
                (trim_task_error(error_summary) or "后台任务执行失败。", now, now, task_key),
            )
            if cursor.rowcount == 0:
                raise KeyError(f"Running background task not found: {task_key}")
        return self.get_background_task(task_key)

    def fail_queued_background_task(
        self,
        task_key: str,
        error_summary: str,
    ) -> BackgroundTaskRecord:
        """记录消息投递失败；任务尚未进入 Worker，因此状态仍是 queued。"""

        now = now_iso()
        with self.connect() as conn:
            cursor = conn.execute(
                """
                UPDATE background_tasks
                SET status = 'failed', error_summary = ?, finished_at = ?, updated_at = ?
                WHERE task_key = ? AND status = 'queued'
                """,
                (trim_task_error(error_summary) or "后台任务投递失败。", now, now, task_key),
            )
            if cursor.rowcount == 0:
                raise KeyError(f"Queued background task not found: {task_key}")
        return self.get_background_task(task_key)

    def cancel_background_task(self, task_key: str, account_id: int) -> BackgroundTaskRecord:
        """取消尚未开始的任务；运行中的任务由后续 Worker 取消协议处理。"""

        now = now_iso()
        with self.connect() as conn:
            cursor = conn.execute(
                """
                UPDATE background_tasks
                SET status = 'cancelled', finished_at = ?, updated_at = ?
                WHERE task_key = ? AND account_id = ? AND status = 'queued'
                """,
                (now, now, task_key, account_id),
            )
            if cursor.rowcount == 0:
                # 仍然返回现有记录，让 API 区分已完成、运行中和不存在的任务。
                return self.get_background_task(task_key, account_id=account_id)
        return self.get_background_task(task_key, account_id=account_id)

    def save_candidate_profile(
        self,
        profile: CandidateProfileInput,
        account_id: int | None = None,
    ) -> int:
        """保存候选人结构化档案，返回新建档案 ID。"""

        skills = normalize_skill_mapping(profile.skills)
        preferred_cities = normalize_city_list(profile.preferred_cities)
        acceptable_cities = [
            city
            for city in normalize_city_list(profile.acceptable_cities)
            if city not in preferred_cities
        ]
        preference_weights = sanitize_preference_weights(profile.preference_weights)
        fingerprint = candidate_profile_content_fingerprint(profile)
        if self.find_candidate_profile_by_content_fingerprint(account_id, fingerprint) is not None:
            raise DuplicateResourceError("候选人档案")
        try:
            with self.connect() as conn:
                cursor = conn.execute(
                    """
                    INSERT INTO candidate_profiles (
                        account_id, name, status, education, experience_years, salary_floor_k,
                        expected_salary_k, skills_json, preferred_cities_json,
                        acceptable_cities_json, preference_weights_json,
                        target_directions_json, unacceptable_json, content_fingerprint
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        account_id,
                        profile.name,
                        profile.status,
                        profile.education,
                        profile.experience_years,
                        profile.salary_floor_k,
                        profile.expected_salary_k,
                        json.dumps(skills, ensure_ascii=False),
                        json.dumps(preferred_cities, ensure_ascii=False),
                        json.dumps(acceptable_cities, ensure_ascii=False),
                        json.dumps(preference_weights, ensure_ascii=False),
                        json.dumps(profile.target_directions, ensure_ascii=False),
                        json.dumps(profile.unacceptable, ensure_ascii=False),
                        fingerprint,
                    ),
                )
                candidate_id = int(cursor.lastrowid)
                # 同一个事务里写 long_texts，保证 PostgreSQL 外键和长文本登记原子完成。
                self._add_long_text(
                    conn,
                    "candidate_profile",
                    candidate_id,
                    "skills",
                    " ".join(skills),
                    account_id=account_id,
                    candidate_id=candidate_id,
                )
                return candidate_id
        except Exception as error:
            if is_unique_constraint_violation(
                error,
                "uq_candidate_profiles_account_content_fingerprint",
            ):
                raise DuplicateResourceError("候选人档案") from error
            raise

    def find_candidate_profile_by_content_fingerprint(
        self,
        account_id: int | None,
        fingerprint: str,
    ) -> CandidateProfile | None:
        """查找相同候选人档案，并兼容迁移前尚未回填指纹的历史记录。"""

        with self.connect() as conn:
            if account_id is None:
                row = conn.execute(
                    "SELECT * FROM candidate_profiles WHERE content_fingerprint = ?",
                    (fingerprint,),
                ).fetchone()
                candidate_rows = conn.execute("SELECT * FROM candidate_profiles").fetchall()
            else:
                row = conn.execute(
                    """
                    SELECT * FROM candidate_profiles
                    WHERE account_id = ? AND content_fingerprint = ?
                    """,
                    (account_id, fingerprint),
                ).fetchone()
                candidate_rows = conn.execute(
                    "SELECT * FROM candidate_profiles WHERE account_id = ?",
                    (account_id,),
                ).fetchall()
        if row is not None:
            return candidate_profile_from_row(row)
        # 历史档案可能在启用规范化前写入；补做一次逻辑比较，保证 Python/python
        # 之类的等价写法仍然会命中已有档案。
        for candidate_row in candidate_rows:
            profile = candidate_profile_from_row(candidate_row)
            if candidate_profile_content_fingerprint(profile) == fingerprint:
                return profile
        return None

    def get_candidate_profile(
        self,
        candidate_id: int,
        account_id: int | None = None,
    ) -> CandidateProfile:
        """按 ID 读取候选人档案，并把 JSON 字段还原成 Python 对象。"""

        with self.connect() as conn:
            if account_id is None:
                row = conn.execute(
                    "SELECT * FROM candidate_profiles WHERE id = ?",
                    (candidate_id,),
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT * FROM candidate_profiles WHERE id = ? AND account_id = ?",
                    (candidate_id, account_id),
                ).fetchone()
        if row is None:
            raise KeyError(f"Candidate profile not found: {candidate_id}")
        return candidate_profile_from_row(row)

    def list_candidate_profiles(self, account_id: int | None = None) -> list[CandidateProfile]:
        """列出所有候选人档案，供 Web 侧边栏选择使用。"""

        with self.connect() as conn:
            if account_id is None:
                rows = conn.execute("SELECT * FROM candidate_profiles ORDER BY id").fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM candidate_profiles WHERE account_id = ? ORDER BY id",
                    (account_id,),
                ).fetchall()
        return [candidate_profile_from_row(row) for row in rows]

    def update_candidate_profile(
        self,
        candidate_id: int,
        patch: CandidateProfilePatch,
        account_id: int | None = None,
    ) -> list[str]:
        """按 patch 局部更新候选人档案，返回实际更新的字段名。

        自动入库只处理消息中明确出现的字段：标量、首选城市和明确替换的求职方向覆盖，
        其他列表去重追加，技能字段按技能名合并/更新熟练度。
        """

        current = self.get_candidate_profile(candidate_id, account_id=account_id)
        updated_fields: list[str] = []

        status = current.status
        education = current.education
        experience_years = current.experience_years
        salary_floor_k = current.salary_floor_k
        expected_salary_k = current.expected_salary_k
        skills = normalize_skill_mapping(current.skills)
        preferred_cities = list(current.preferred_cities)
        acceptable_cities = list(current.acceptable_cities)
        preference_weights = sanitize_preference_weights(current.preference_weights)
        target_directions = list(current.target_directions)
        unacceptable = list(current.unacceptable)

        if patch.status:
            status = patch.status
            updated_fields.append("status")
        if patch.education:
            education = patch.education
            updated_fields.append("education")
        if patch.experience_years is not None:
            experience_years = patch.experience_years
            updated_fields.append("experience_years")
        if patch.salary_floor_k is not None:
            salary_floor_k = patch.salary_floor_k
            updated_fields.append("salary_floor_k")
        if patch.expected_salary_k is not None:
            expected_salary_k = patch.expected_salary_k
            updated_fields.append("expected_salary_k")
        if patch.skills:
            merged_skills = merge_skill_mappings(skills, patch.skills)
            if merged_skills != skills:
                skills = merged_skills
                updated_fields.append("skills")
        if patch.clear_preferred_cities:
            preferred_cities = []
            updated_fields.append("preferred_cities")
        elif patch.replace_preferred_cities or patch.preferred_cities:
            # 最新一次明确的首选城市意向覆盖旧值，而不是无限追加。
            preferred_cities = normalize_city_list(patch.preferred_cities)
            updated_fields.append("preferred_cities")
        if patch.clear_acceptable_cities:
            acceptable_cities = []
            updated_fields.append("acceptable_cities")
        if patch.acceptable_cities:
            acceptable_cities = merge_unique(
                acceptable_cities,
                normalize_city_list(patch.acceptable_cities),
            )
            updated_fields.append("acceptable_cities")
        # 同一城市不能同时属于首选和其他可接受集合；首选级别始终优先。
        disjoint_acceptable = [city for city in acceptable_cities if city not in preferred_cities]
        if disjoint_acceptable != acceptable_cities:
            acceptable_cities = disjoint_acceptable
            if "acceptable_cities" not in updated_fields:
                updated_fields.append("acceptable_cities")
        if patch.preference_weights:
            for key, value in patch.preference_weights.items():
                if key in preference_weights:
                    preference_weights[key] = sanitize_preference_weights({key: value})[key]
            updated_fields.append("preference_weights")
        if patch.target_directions:
            incoming_directions = merge_unique([], patch.target_directions)
            next_target_directions = (
                incoming_directions
                if patch.replace_target_directions
                else merge_unique(target_directions, incoming_directions)
            )
            if next_target_directions != target_directions:
                target_directions = next_target_directions
                updated_fields.append("target_directions")
        if patch.unacceptable:
            unacceptable = merge_unique(unacceptable, patch.unacceptable)
            updated_fields.append("unacceptable")

        if not updated_fields:
            return []

        updated_profile = CandidateProfileInput(
            name=current.name,
            status=status,
            education=education,
            experience_years=experience_years,
            skills=skills,
            preferred_cities=preferred_cities,
            acceptable_cities=acceptable_cities,
            salary_floor_k=salary_floor_k,
            expected_salary_k=expected_salary_k,
            target_directions=target_directions,
            unacceptable=unacceptable,
            preference_weights=preference_weights,
        )
        fingerprint = candidate_profile_content_fingerprint(updated_profile)

        try:
            with self.connect() as conn:
                conn.execute(
                """
                UPDATE candidate_profiles
                SET status = ?, education = ?, experience_years = ?,
                    salary_floor_k = ?, expected_salary_k = ?,
                    skills_json = ?, preferred_cities_json = ?,
                    acceptable_cities_json = ?, preference_weights_json = ?,
                    target_directions_json = ?, unacceptable_json = ?, content_fingerprint = ?
                WHERE id = ?
                  AND COALESCE(account_id, -1) = COALESCE(?, COALESCE(account_id, -1))
                """,
                (
                    status,
                    education,
                    experience_years,
                    salary_floor_k,
                    expected_salary_k,
                    json.dumps(skills, ensure_ascii=False),
                    json.dumps(preferred_cities, ensure_ascii=False),
                    json.dumps(acceptable_cities, ensure_ascii=False),
                    json.dumps(preference_weights, ensure_ascii=False),
                    json.dumps(target_directions, ensure_ascii=False),
                    json.dumps(unacceptable, ensure_ascii=False),
                    fingerprint,
                    candidate_id,
                    account_id,
                ),
                )
                # 记录自动更新摘要，方便 RAG 和审计追溯“这次对话改了哪些结构化字段”。
                self._add_long_text(
                    conn,
                    "candidate_profile",
                    candidate_id,
                    "conversation_structured_update",
                    "自动更新字段：" + "、".join(updated_fields),
                    account_id=account_id,
                    candidate_id=candidate_id,
                )
        except Exception as error:
            if is_unique_constraint_violation(
                error,
                "uq_candidate_profiles_account_content_fingerprint",
            ):
                raise DuplicateResourceError("候选人档案") from error
            raise
        return updated_fields

    def save_job_text(
        self,
        raw_text: str,
        source_url: str | None = None,
        account_id: int | None = None,
        llm_client=None,
        import_method: str = "text",
    ) -> ImportedJob:
        """保存一段职位原文。

        这里先调用规则解析器得到 `ImportedJob`，再同时保存原文、结构化字段和
        长文本副本。后续接入 LLM 时，可以替换 `parse_job_text` 的内部逻辑。
        """

        captured_at = now_iso()
        parsed = parse_job_text(
            raw_text,
            source_url=source_url,
            import_method=import_method,
            captured_at=captured_at,
        )
        fingerprint = job_text_content_fingerprint(parsed.raw_text)
        if self.find_job_by_content_fingerprint(account_id, fingerprint) is not None:
            raise DuplicateResourceError("职位信息")
        if llm_client is not None:
            parsed.skill_requirements = classify_skill_requirements(
                parsed.raw_text,
                parsed.skills,
                llm_client,
            )
        try:
            with self.connect() as conn:
                cursor = conn.execute(
                """
                INSERT INTO jobs (
                    account_id, raw_text, source_url, import_method, captured_at, title, city, salary_min_k, salary_max_k,
                    salary_months, salary_unit, experience_min_years,
                    experience_max_years, experience_label, education,
                    company_name, industry, company_size, skills_json,
                    skill_requirements_json, description_text, field_confidence_json,
                    uncertainty_notes_json, content_fingerprint
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    account_id,
                    parsed.raw_text,
                    parsed.source_url,
                    parsed.import_method,
                    parsed.captured_at,
                    parsed.title,
                    parsed.city,
                    parsed.salary_min_k,
                    parsed.salary_max_k,
                    parsed.salary_months,
                    parsed.salary_unit,
                    parsed.experience_min_years,
                    parsed.experience_max_years,
                    parsed.experience_label,
                    parsed.education,
                    parsed.company_name,
                    parsed.industry,
                    parsed.company_size,
                    json.dumps(parsed.skills, ensure_ascii=False),
                    json.dumps([asdict(item) for item in parsed.skill_requirements], ensure_ascii=False),
                    parsed.description_text,
                    json.dumps(parsed.field_confidence, ensure_ascii=False),
                    json.dumps(parsed.uncertainty_notes, ensure_ascii=False),
                    fingerprint,
                ),
                )
                job_id = int(cursor.lastrowid)
                # 职位描述先进入 long_texts，后续可以同步到真正的向量数据库。
                self._add_long_text(
                    conn,
                    "job",
                    job_id,
                    "description",
                    parsed.description_text,
                    account_id=account_id,
                )
        except Exception as error:
            if is_unique_constraint_violation(error, "uq_jobs_account_content_fingerprint"):
                raise DuplicateResourceError("职位信息") from error
            raise
        return self.get_job(job_id)

    def find_job_by_content_fingerprint(
        self,
        account_id: int | None,
        fingerprint: str,
    ) -> ImportedJob | None:
        """查找相同职位原文，并兼容迁移前没有内容指纹的职位。"""

        with self.connect() as conn:
            if account_id is None:
                row = conn.execute(
                    "SELECT * FROM jobs WHERE content_fingerprint = ?",
                    (fingerprint,),
                ).fetchone()
                legacy_rows = conn.execute(
                    "SELECT * FROM jobs WHERE content_fingerprint IS NULL"
                ).fetchall()
            else:
                row = conn.execute(
                    "SELECT * FROM jobs WHERE account_id = ? AND content_fingerprint = ?",
                    (account_id, fingerprint),
                ).fetchone()
                legacy_rows = conn.execute(
                    "SELECT * FROM jobs WHERE account_id = ? AND content_fingerprint IS NULL",
                    (account_id,),
                ).fetchall()
        if row is not None:
            return self._job_from_row(row)
        for legacy_row in legacy_rows:
            job = self._job_from_row(legacy_row)
            if job_text_content_fingerprint(job.raw_text) == fingerprint:
                return job
        return None

    def get_job(self, job_id: int, account_id: int | None = None) -> ImportedJob:
        """按 ID 读取标准化职位信息。"""

        with self.connect() as conn:
            if account_id is None:
                row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
            else:
                row = conn.execute(
                    "SELECT * FROM jobs WHERE id = ? AND account_id = ?",
                    (job_id, account_id),
                ).fetchone()
        if row is None:
            raise KeyError(f"Job not found: {job_id}")
        return self._job_from_row(row)

    def list_jobs(self, account_id: int | None = None) -> list[ImportedJob]:
        """按导入顺序列出所有职位。

        批量匹配需要先拿到候选人主动导入过的职位池；这里仍然只读取本地数据，
        不会访问 BOSS 直聘网站。
        """

        with self.connect() as conn:
            if account_id is None:
                rows = conn.execute("SELECT * FROM jobs ORDER BY id").fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM jobs WHERE account_id = ? ORDER BY id",
                    (account_id,),
                ).fetchall()
        jobs = [self._job_from_row(row) for row in rows]
        # 旧版本可能已经把普通聊天、项目日志或测试文本误写进 jobs 表；
        # 列表和批量匹配只暴露通过当前审核规则的记录，避免继续把脏数据当职位打分。
        return [job for job in jobs if validate_job_text(job.raw_text).is_valid]

    def update_job_skill_requirements(
        self,
        job_id: int,
        requirements: list[SkillRequirement],
        account_id: int | None = None,
    ) -> ImportedJob:
        """保存职位已有技能的人工分类，不允许凭空增加技能名称。"""

        current = self.get_job(job_id, account_id=account_id)
        allowed = {skill.lower(): skill for skill in current.skills}
        existing = {item.name.lower(): item for item in current.skill_requirements}
        normalized: dict[str, SkillRequirement] = {}
        valid_categories = {"core", "general", "bonus", "uncertain"}
        for item in requirements:
            canonical_name = allowed.get(str(item.name).strip().lower())
            if canonical_name is None:
                raise ValueError(f"技能不在职位原始技能列表中：{item.name}")
            category = str(item.category or "general").strip().lower()
            if category not in valid_categories:
                raise ValueError(f"不支持的技能分类：{item.category}")
            try:
                confidence = max(0.0, min(1.0, float(item.confidence)))
            except (TypeError, ValueError):
                confidence = 0.5
            normalized[canonical_name.lower()] = SkillRequirement(
                name=canonical_name,
                category=category,
                confidence=confidence,
                evidence=str(item.evidence or "").strip(),
            )

        # 前端可以只提交部分技能；未提交项沿用旧值，避免误删职位要求。
        merged: list[SkillRequirement] = []
        for skill in current.skills:
            key = skill.lower()
            merged.append(
                normalized.get(
                    key,
                    existing.get(
                        key,
                        SkillRequirement(name=skill, category="general", confidence=0.5),
                    ),
                )
            )
        with self.connect() as conn:
            conn.execute(
                "UPDATE jobs SET skill_requirements_json = ? WHERE id = ? "
                "AND COALESCE(account_id, -1) = COALESCE(?, COALESCE(account_id, -1))",
                (
                    json.dumps([asdict(item) for item in merged], ensure_ascii=False),
                    job_id,
                    account_id,
                ),
            )
        return self.get_job(job_id, account_id=account_id)

    def delete_candidate_profile(
        self,
        candidate_id: int,
        account_id: int | None = None,
    ) -> dict[str, object]:
        """删除候选人档案及其所有从属资料，返回需要清理的文件和 RAG 长文本 ID。"""

        self.get_candidate_profile(candidate_id, account_id=account_id)
        owner_clause = ""
        owner_parameters: tuple[object, ...] = ()
        if account_id is not None:
            owner_clause = " AND account_id = ?"
            owner_parameters = (account_id,)

        with self.connect() as conn:
            artifact_rows = conn.execute(
                f"""
                SELECT storage_key
                FROM resume_artifacts
                WHERE candidate_id = ?{owner_clause}
                """,
                (candidate_id, *owner_parameters),
            ).fetchall()
            long_text_rows = conn.execute(
                f"""
                SELECT id
                FROM long_texts
                WHERE (candidate_id = ? OR (entity_type = 'candidate_profile' AND entity_id = ?))
                {owner_clause}
                """,
                (candidate_id, candidate_id, *owner_parameters),
            ).fetchall()
            delete_owner_clause = owner_clause
            delete_owner_parameters = owner_parameters

            # 先解除简历派生文件的自引用，再按从属关系逆序删除，避免旧数据库的外键约束阻止清理。
            conn.execute(
                f"UPDATE resume_artifacts SET parent_artifact_id = NULL "
                f"WHERE candidate_id = ?{delete_owner_clause}",
                (candidate_id, *delete_owner_parameters),
            )
            conn.execute(
                f"DELETE FROM resume_artifacts WHERE candidate_id = ?{delete_owner_clause}",
                (candidate_id, *delete_owner_parameters),
            )
            conn.execute(
                f"DELETE FROM resume_drafts WHERE candidate_id = ?{delete_owner_clause}",
                (candidate_id, *delete_owner_parameters),
            )
            conn.execute(
                f"DELETE FROM project_experience_cards WHERE candidate_id = ?{delete_owner_clause}",
                (candidate_id, *delete_owner_parameters),
            )
            conn.execute(
                f"DELETE FROM chat_messages WHERE candidate_id = ?{delete_owner_clause}",
                (candidate_id, *delete_owner_parameters),
            )
            conn.execute(
                f"DELETE FROM chat_sessions WHERE candidate_id = ?{delete_owner_clause}",
                (candidate_id, *delete_owner_parameters),
            )
            conn.execute(
                f"""
                DELETE FROM long_texts
                WHERE (candidate_id = ? OR (entity_type = 'candidate_profile' AND entity_id = ?))
                {delete_owner_clause}
                """,
                (candidate_id, candidate_id, *delete_owner_parameters),
            )
            cursor = conn.execute(
                f"DELETE FROM candidate_profiles WHERE id = ?{delete_owner_clause}",
                (candidate_id, *delete_owner_parameters),
            )
            if cursor.rowcount == 0:
                raise KeyError(f"Candidate profile not found: {candidate_id}")

        return {
            "candidate_id": candidate_id,
            "storage_keys": [str(row["storage_key"]) for row in artifact_rows],
            "long_text_ids": [int(row["id"]) for row in long_text_rows],
        }

    def delete_job(self, job_id: int, account_id: int | None = None) -> dict[str, object]:
        """删除职位及其长文本、职位定制简历，并解除已有会话的职位关联。"""

        self.get_job(job_id, account_id=account_id)
        owner_clause = ""
        owner_parameters: tuple[object, ...] = ()
        if account_id is not None:
            owner_clause = " AND account_id = ?"
            owner_parameters = (account_id,)

        with self.connect() as conn:
            artifact_rows = conn.execute(
                f"SELECT storage_key FROM resume_artifacts WHERE job_id = ?{owner_clause}",
                (job_id, *owner_parameters),
            ).fetchall()
            long_text_rows = conn.execute(
                f"""
                SELECT id FROM long_texts
                WHERE entity_type = 'job' AND entity_id = ?{owner_clause}
                """,
                (job_id, *owner_parameters),
            ).fetchall()
            conn.execute(
                f"UPDATE chat_sessions SET job_id = NULL WHERE job_id = ?{owner_clause}",
                (job_id, *owner_parameters),
            )
            conn.execute(
                f"UPDATE resume_artifacts SET parent_artifact_id = NULL WHERE job_id = ?{owner_clause}",
                (job_id, *owner_parameters),
            )
            conn.execute(
                f"DELETE FROM resume_artifacts WHERE job_id = ?{owner_clause}",
                (job_id, *owner_parameters),
            )
            conn.execute(
                f"DELETE FROM resume_drafts WHERE job_id = ?{owner_clause}",
                (job_id, *owner_parameters),
            )
            conn.execute(
                f"DELETE FROM long_texts WHERE entity_type = 'job' AND entity_id = ?{owner_clause}",
                (job_id, *owner_parameters),
            )
            cursor = conn.execute(
                f"DELETE FROM jobs WHERE id = ?{owner_clause}",
                (job_id, *owner_parameters),
            )
            if cursor.rowcount == 0:
                raise KeyError(f"Job not found: {job_id}")

        return {
            "job_id": job_id,
            "storage_keys": [str(row["storage_key"]) for row in artifact_rows],
            "long_text_ids": [int(row["id"]) for row in long_text_rows],
        }

    def save_chat_message(
        self,
        candidate_id: int,
        session_id: str,
        role: str,
        content: str,
        metadata: dict[str, object] | None = None,
        account_id: int | None = None,
    ) -> ChatMessageRecord:
        """保存一条网页聊天消息。

        聊天历史用于恢复前端页面，不参与职位匹配和简历改写；如果消息中包含
        候选人事实，仍然必须由 `ingest_conversation_message` 单独判断后写入档案或 long_texts。
        """

        if role not in {"user", "assistant"}:
            raise ValueError(f"Unsupported chat role: {role}")
        now = now_iso()
        with self.connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO chat_messages (
                    account_id, candidate_id, session_id, role, content, metadata_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    account_id,
                    candidate_id,
                    session_id,
                    role,
                    content,
                    json.dumps(metadata or {}, ensure_ascii=False),
                    now,
                ),
            )
            record_id = int(cursor.lastrowid)
        return self.get_chat_message(record_id)

    def get_chat_message(self, record_id: int) -> ChatMessageRecord:
        """按 ID 读取一条聊天消息。"""

        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM chat_messages WHERE id = ?",
                (record_id,),
            ).fetchone()
        if row is None:
            raise KeyError(f"Chat message not found: {record_id}")
        return self._chat_message_from_row(row)

    def list_chat_messages(
        self,
        candidate_id: int,
        session_id: str,
        limit: int = 100,
        account_id: int | None = None,
    ) -> list[ChatMessageRecord]:
        """列出某个候选人在某个网页会话中的最近聊天记录。"""

        with self.connect() as conn:
            owner_clause = " AND account_id = ?" if account_id is not None else ""
            parameters: tuple[object, ...]
            if account_id is None:
                parameters = (candidate_id, session_id, max(1, limit))
            else:
                parameters = (candidate_id, session_id, account_id, max(1, limit))
            rows = conn.execute(
                f"""
                SELECT * FROM (
                    SELECT * FROM chat_messages
                    WHERE candidate_id = ? AND session_id = ?{owner_clause}
                    ORDER BY id DESC
                    LIMIT ?
                )
                ORDER BY id
                """,
                parameters,
            ).fetchall()
        return [self._chat_message_from_row(row) for row in rows]

    def save_project_card(
        self,
        candidate_id: int,
        card: ProjectExperienceCard,
        account_id: int | None = None,
    ) -> ProjectExperienceRecord:
        """保存一张待确认项目经历卡片。

        自动分析得到的项目线索只进入 `project_experience_cards`，不会直接写入
        `candidate_profiles.skills_json` 等已确认事实字段。
        """

        fingerprint = project_card_content_fingerprint(card)
        if self.find_project_card_by_content_fingerprint(account_id, candidate_id, fingerprint) is not None:
            raise DuplicateResourceError("项目")
        now = now_iso()
        try:
            with self.connect() as conn:
                cursor = conn.execute(
                    """
                    INSERT INTO project_experience_cards (
                        account_id, candidate_id, status, project_name, card_json,
                        confirmed_summary, created_at, confirmed_at, content_fingerprint
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        account_id,
                        candidate_id,
                        "待确认",
                        card.project_name,
                        json.dumps(asdict(card), ensure_ascii=False),
                        None,
                        now,
                        None,
                        fingerprint,
                    ),
                )
                record_id = int(cursor.lastrowid)
        except Exception as error:
            if is_unique_constraint_violation(error, "uq_project_cards_candidate_content_fingerprint"):
                raise DuplicateResourceError("项目") from error
            raise
        return self.get_project_card(record_id)

    def find_project_card_by_content_fingerprint(
        self,
        account_id: int | None,
        candidate_id: int,
        fingerprint: str,
    ) -> ProjectExperienceRecord | None:
        """查找同一候选人的相同项目卡片，并兼容迁移前未记录指纹的卡片。"""

        with self.connect() as conn:
            if account_id is None:
                row = conn.execute(
                    """
                    SELECT * FROM project_experience_cards
                    WHERE candidate_id = ? AND content_fingerprint = ?
                    """,
                    (candidate_id, fingerprint),
                ).fetchone()
                legacy_rows = conn.execute(
                    """
                    SELECT * FROM project_experience_cards
                    WHERE candidate_id = ? AND content_fingerprint IS NULL
                    """,
                    (candidate_id,),
                ).fetchall()
            else:
                row = conn.execute(
                    """
                    SELECT * FROM project_experience_cards
                    WHERE account_id = ? AND candidate_id = ? AND content_fingerprint = ?
                    """,
                    (account_id, candidate_id, fingerprint),
                ).fetchone()
                legacy_rows = conn.execute(
                    """
                    SELECT * FROM project_experience_cards
                    WHERE account_id = ? AND candidate_id = ? AND content_fingerprint IS NULL
                    """,
                    (account_id, candidate_id),
                ).fetchall()
        if row is not None:
            return self._project_card_from_row(row)
        for legacy_row in legacy_rows:
            record = self._project_card_from_row(legacy_row)
            if project_card_content_fingerprint(record.card) == fingerprint:
                return record
        return None

    def get_project_card(
        self,
        record_id: int,
        account_id: int | None = None,
    ) -> ProjectExperienceRecord:
        """按 ID 读取一张项目经历卡片记录。"""

        with self.connect() as conn:
            if account_id is None:
                row = conn.execute(
                    "SELECT * FROM project_experience_cards WHERE id = ?",
                    (record_id,),
                ).fetchone()
            else:
                row = conn.execute(
                    """
                    SELECT * FROM project_experience_cards
                    WHERE id = ? AND account_id = ?
                    """,
                    (record_id, account_id),
                ).fetchone()
        if row is None:
            raise KeyError(f"Project experience card not found: {record_id}")
        return self._project_card_from_row(row)

    def list_project_cards(
        self,
        candidate_id: int,
        account_id: int | None = None,
    ) -> list[ProjectExperienceRecord]:
        """列出某个候选人的项目经历卡片。"""

        with self.connect() as conn:
            if account_id is None:
                rows = conn.execute(
                    """
                    SELECT * FROM project_experience_cards
                    WHERE candidate_id = ?
                    ORDER BY id
                    """,
                    (candidate_id,),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT * FROM project_experience_cards
                    WHERE candidate_id = ? AND account_id = ?
                    ORDER BY id
                    """,
                    (candidate_id, account_id),
                ).fetchall()
        return [self._project_card_from_row(row) for row in rows]

    def confirm_project_card(
        self,
        record_id: int,
        confirmed_summary: str | None = None,
        account_id: int | None = None,
    ) -> ProjectExperienceRecord:
        """把项目经历卡片标记为已确认，并保存候选人的确认摘要。

        确认后的摘要会进入 `long_texts`，为后续向量检索/简历改写提供材料；
        但它仍然不会自动覆盖候选人档案中的学历、技能等结构化事实。
        """

        existing = self.get_project_card(record_id, account_id=account_id)
        if existing.status == "已确认":
            # 重复确认必须保持幂等，不能再次创建一份相同 long_texts 材料。
            return existing
        summary = str(
            confirmed_summary
            if confirmed_summary is not None
            else existing.confirmed_summary or ""
        ).strip()
        if not summary:
            raise ValueError("确认项目经历前至少确认一组属于候选人的内容。")
        confirmed_at = now_iso()
        with self.connect() as conn:
            cursor = conn.execute(
                """
                UPDATE project_experience_cards
                SET status = ?, confirmed_summary = ?, confirmed_at = ?
                WHERE id = ? AND status = '待确认'
                  AND COALESCE(account_id, -1) = COALESCE(?, COALESCE(account_id, -1))
                """,
                ("已确认", summary, confirmed_at, record_id, account_id),
            )
            if cursor.rowcount == 0:
                # 另一个请求已经完成确认；不再登记第二份相同长文本。
                return self.get_project_card(record_id, account_id=account_id)
            self._add_long_text(
                conn,
                "project_experience_card",
                record_id,
                "confirmed",
                summary,
                account_id=account_id,
                candidate_id=existing.candidate_id,
            )
        return self.get_project_card(record_id, account_id=account_id)

    def delete_project_card(
        self,
        record_id: int,
        account_id: int | None = None,
    ) -> dict[str, object]:
        """删除一张项目经历卡片及其确认摘要对应的长文本。"""

        existing = self.get_project_card(record_id, account_id=account_id)
        owner_clause = ""
        owner_parameters: tuple[object, ...] = ()
        if account_id is not None:
            owner_clause = " AND account_id = ?"
            owner_parameters = (account_id,)

        with self.connect() as conn:
            long_text_rows = conn.execute(
                f"""
                SELECT id FROM long_texts
                WHERE entity_type = 'project_experience_card' AND entity_id = ?{owner_clause}
                """,
                (record_id, *owner_parameters),
            ).fetchall()
            conn.execute(
                f"""
                DELETE FROM long_texts
                WHERE entity_type = 'project_experience_card' AND entity_id = ?{owner_clause}
                """,
                (record_id, *owner_parameters),
            )
            cursor = conn.execute(
                f"DELETE FROM project_experience_cards WHERE id = ?{owner_clause}",
                (record_id, *owner_parameters),
            )
            if cursor.rowcount == 0:
                raise KeyError(f"Project experience card not found: {record_id}")

        return {
            "project_card_id": record_id,
            "candidate_id": existing.candidate_id,
            "long_text_ids": [int(row["id"]) for row in long_text_rows],
        }

    def get_long_text_for_entity(
        self,
        entity_type: str,
        entity_id: int,
        *,
        source_label: str | None = None,
        account_id: int | None = None,
    ) -> LongTextRecord | None:
        """读取某个业务实体最近登记的长文本，供增量 RAG 任务取得来源 ID。"""

        conditions = ["entity_type = ?", "entity_id = ?"]
        parameters: list[object] = [entity_type, entity_id]
        if source_label is not None:
            conditions.append("source_label = ?")
            parameters.append(source_label)
        if account_id is not None:
            conditions.append("account_id = ?")
            parameters.append(account_id)
        with self.connect() as conn:
            row = conn.execute(
                f"""
                SELECT * FROM long_texts
                WHERE {' AND '.join(conditions)}
                ORDER BY id DESC
                LIMIT 1
                """,
                tuple(parameters),
            ).fetchone()
        return long_text_from_row(row) if row is not None else None

    def save_resume_draft(
        self,
        candidate_id: int,
        job_id: int,
        draft: ResumeDraft,
        account_id: int | None = None,
    ) -> ResumeDraftRecord:
        """保存一个职位定制简历草稿版本。

        草稿版本单独保存，不会更新候选人档案，也不会覆盖历史版本。
        """

        created_at = now_iso()
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT COALESCE(MAX(version), 0) AS latest_version
                FROM resume_drafts
                WHERE candidate_id = ? AND job_id = ?
                """,
                (candidate_id, job_id),
            ).fetchone()
            version = int(row["latest_version"]) + 1
            cursor = conn.execute(
                """
                INSERT INTO resume_drafts (
                    account_id, candidate_id, job_id, version, status, draft_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    account_id,
                    candidate_id,
                    job_id,
                    version,
                    "需候选人确认",
                    json.dumps(asdict(draft), ensure_ascii=False),
                    created_at,
                ),
            )
            record_id = int(cursor.lastrowid)
            # 草稿全文进入 long_texts，后续可以用于“比较不同版本”或向量检索，
            # 但它的 entity_type 明确标记为草稿，不会被当成档案事实。
            self._add_long_text(
                conn,
                "resume_draft",
                record_id,
                f"v{version}",
                draft.content,
                account_id=account_id,
                candidate_id=candidate_id,
            )
        return self.get_resume_draft(record_id, account_id=account_id)

    def get_resume_draft(
        self,
        record_id: int,
        account_id: int | None = None,
    ) -> ResumeDraftRecord:
        """按 ID 读取简历草稿版本，并在 Web 场景校验账号归属。"""

        with self.connect() as conn:
            if account_id is None:
                row = conn.execute(
                    "SELECT * FROM resume_drafts WHERE id = ?",
                    (record_id,),
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT * FROM resume_drafts WHERE id = ? AND account_id = ?",
                    (record_id, account_id),
                ).fetchone()
        if row is None:
            raise KeyError(f"Resume draft not found: {record_id}")
        return self._resume_draft_from_row(row)

    def list_resume_drafts(
        self,
        candidate_id: int,
        job_id: int | None = None,
        account_id: int | None = None,
    ) -> list[ResumeDraftRecord]:
        """列出候选人的简历草稿版本，可按职位过滤。"""

        with self.connect() as conn:
            if job_id is None and account_id is None:
                rows = conn.execute(
                    """
                    SELECT * FROM resume_drafts
                    WHERE candidate_id = ?
                    ORDER BY job_id, version
                    """,
                    (candidate_id,),
                ).fetchall()
            elif job_id is not None and account_id is None:
                rows = conn.execute(
                    """
                    SELECT * FROM resume_drafts
                    WHERE candidate_id = ? AND job_id = ?
                    ORDER BY version
                    """,
                    (candidate_id, job_id),
                ).fetchall()
            elif job_id is None:
                rows = conn.execute(
                    """
                    SELECT * FROM resume_drafts
                    WHERE candidate_id = ? AND account_id = ?
                    ORDER BY job_id, version
                    """,
                    (candidate_id, account_id),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT * FROM resume_drafts
                    WHERE candidate_id = ? AND job_id = ? AND account_id = ?
                    ORDER BY version
                    """,
                    (candidate_id, job_id, account_id),
                ).fetchall()
        return [self._resume_draft_from_row(row) for row in rows]

    def save_resume_artifact(
        self,
        *,
        account_id: int | None,
        candidate_id: int,
        artifact_type: str,
        original_filename: str,
        download_filename: str,
        storage_key: str,
        media_type: str,
        file_size: int,
        sha256: str,
        extraction_method: str,
        extracted_text: str,
        page_count: int | None,
        job_id: int | None = None,
        draft_id: int | None = None,
        parent_artifact_id: int | None = None,
        version: int | None = None,
        status: str = "ready",
        register_long_text: bool = False,
    ) -> ResumeArtifactRecord:
        """保存一份简历文件元数据，可选地同时登记 RAG 长文本来源。

        二进制文件应先由 `ResumeFileStore` 原子写入；调用方在本方法失败时负责
        删除刚写入的文件，避免文件系统和 PostgreSQL 元数据之间留下孤立记录。
        """

        if artifact_type not in {"source", "tailored"}:
            raise ValueError("简历文件类型只能是 source 或 tailored。")
        if status not in RESUME_ARTIFACT_STATUSES:
            raise ValueError("简历文件状态只能是 ready、processing 或 failed。")
        if artifact_type == "tailored" and status != "ready":
            raise ValueError("职位定制简历只能保存为 ready 状态。")
        if register_long_text and status != "ready":
            raise ValueError("只有解析完成的简历才能登记 RAG 长文本。")
        # 这些读取同时承担所有权校验，防止跨账号拼接候选人、职位、草稿或父文件。
        self.get_candidate_profile(candidate_id, account_id=account_id)
        if job_id is not None:
            self.get_job(job_id, account_id=account_id)
        if draft_id is not None:
            self.get_resume_draft(draft_id, account_id=account_id)
        if parent_artifact_id is not None:
            parent = self.get_resume_artifact(parent_artifact_id, account_id=account_id)
            if parent.candidate_id != candidate_id:
                raise ValueError("派生简历与源简历必须属于同一候选人。")

        fingerprint = sha256 if artifact_type == "source" else None
        if fingerprint and self.find_resume_source_by_content_fingerprint(account_id, candidate_id, fingerprint) is not None:
            raise DuplicateResourceError("简历")

        created_at = now_iso()
        try:
            with self.connect() as conn:
                actual_version = version
                if actual_version is None:
                    row = conn.execute(
                        """
                        SELECT COALESCE(MAX(version), 0) AS latest_version
                        FROM resume_artifacts
                        WHERE candidate_id = ? AND artifact_type = ?
                        """,
                        (candidate_id, artifact_type),
                    ).fetchone()
                    actual_version = int(row["latest_version"]) + 1
                cursor = conn.execute(
                    """
                    INSERT INTO resume_artifacts (
                        account_id, candidate_id, job_id, draft_id, parent_artifact_id,
                        version, artifact_type, original_filename, download_filename,
                        storage_key, media_type, file_size, sha256, extraction_method,
                        extracted_text, text_length, page_count, status, long_text_id, created_at,
                        content_fingerprint
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        account_id,
                        candidate_id,
                        job_id,
                        draft_id,
                        parent_artifact_id,
                        actual_version,
                        artifact_type,
                        original_filename,
                        download_filename,
                        storage_key,
                        media_type,
                        file_size,
                        sha256,
                        extraction_method,
                        extracted_text,
                        len(extracted_text),
                        page_count,
                        status,
                        None,
                        created_at,
                        fingerprint,
                    ),
                )
                artifact_id = int(cursor.lastrowid)
                if register_long_text:
                    long_text_id = self._add_long_text(
                        conn,
                        "resume_artifact",
                        artifact_id,
                        f"uploaded:{original_filename}",
                        extracted_text,
                        account_id=account_id,
                        candidate_id=candidate_id,
                    )
                    conn.execute(
                        "UPDATE resume_artifacts SET long_text_id = ? WHERE id = ?",
                        (long_text_id, artifact_id),
                    )
        except Exception as error:
            if is_unique_constraint_violation(error, "uq_resume_artifacts_candidate_content_fingerprint"):
                raise DuplicateResourceError("简历") from error
            raise
        return self.get_resume_artifact(artifact_id, account_id=account_id)

    def find_resume_source_by_content_fingerprint(
        self,
        account_id: int | None,
        candidate_id: int,
        fingerprint: str,
    ) -> ResumeArtifactRecord | None:
        """查找同一候选人上传的同字节简历，历史记录回退到原 SHA-256。"""

        with self.connect() as conn:
            if account_id is None:
                row = conn.execute(
                    """
                    SELECT * FROM resume_artifacts
                    WHERE candidate_id = ? AND artifact_type = 'source'
                      AND (content_fingerprint = ? OR (content_fingerprint IS NULL AND sha256 = ?))
                    """,
                    (candidate_id, fingerprint, fingerprint),
                ).fetchone()
            else:
                row = conn.execute(
                    """
                    SELECT * FROM resume_artifacts
                    WHERE account_id = ? AND candidate_id = ? AND artifact_type = 'source'
                      AND (content_fingerprint = ? OR (content_fingerprint IS NULL AND sha256 = ?))
                    """,
                    (account_id, candidate_id, fingerprint, fingerprint),
                ).fetchone()
        return self._resume_artifact_from_row(row) if row is not None else None

    def complete_resume_artifact_extraction(
        self,
        artifact_id: int,
        *,
        extraction_method: str,
        extracted_text: str,
        page_count: int | None,
        account_id: int | None = None,
    ) -> ResumeArtifactRecord:
        """把 OCR 成功结果原子写入原始简历，并登记一次可追溯长文本来源。

        Worker 可能因进程中断或消息重投再次执行；如果上一次已把正文和
        ``long_text_id`` 写完整，本方法直接返回已有结果，不会创建重复材料。
        """

        if not extracted_text.strip():
            raise ValueError("简历提取正文不能为空。")
        artifact = self.get_resume_artifact(artifact_id, account_id=account_id)
        if artifact.artifact_type != "source":
            raise ValueError("只有原始上传简历可以写入 OCR 解析结果。")

        owner_clause = ""
        owner_parameters: tuple[object, ...] = ()
        if account_id is not None:
            owner_clause = " AND account_id = ?"
            owner_parameters = (account_id,)

        with self.connect() as conn:
            row = conn.execute(
                f"SELECT * FROM resume_artifacts WHERE id = ?{owner_clause}",
                (artifact_id, *owner_parameters),
            ).fetchone()
            if row is None:
                raise KeyError(f"Resume artifact not found: {artifact_id}")
            current_status = str(row["status"])
            existing_long_text_id = row["long_text_id"]
            if current_status == "ready" and existing_long_text_id is not None:
                return self.get_resume_artifact(artifact_id, account_id=account_id)
            if current_status not in {"processing", "ready"}:
                raise ValueError("这份简历不处于可完成 OCR 的状态。")

            conn.execute(
                f"""
                UPDATE resume_artifacts
                SET extraction_method = ?, extracted_text = ?, text_length = ?,
                    page_count = ?, status = 'ready'
                WHERE id = ?{owner_clause}
                """,
                (
                    extraction_method,
                    extracted_text,
                    len(extracted_text),
                    page_count,
                    artifact_id,
                    *owner_parameters,
                ),
            )
            long_text_id = int(existing_long_text_id) if existing_long_text_id is not None else None
            if long_text_id is None:
                long_text_id = self._add_long_text(
                    conn,
                    "resume_artifact",
                    artifact_id,
                    f"uploaded:{row['original_filename']}",
                    extracted_text,
                    account_id=account_id,
                    candidate_id=int(row["candidate_id"]),
                )
                conn.execute(
                    f"UPDATE resume_artifacts SET long_text_id = ? WHERE id = ?{owner_clause}",
                    (long_text_id, artifact_id, *owner_parameters),
                )
        return self.get_resume_artifact(artifact_id, account_id=account_id)

    def fail_resume_artifact_extraction(
        self,
        artifact_id: int,
        *,
        account_id: int | None = None,
    ) -> ResumeArtifactRecord:
        """把未完成的 OCR 简历标记为失败，保留原文件供用户下载或删除。"""

        owner_clause = ""
        owner_parameters: tuple[object, ...] = ()
        if account_id is not None:
            owner_clause = " AND account_id = ?"
            owner_parameters = (account_id,)
        with self.connect() as conn:
            conn.execute(
                f"""
                UPDATE resume_artifacts
                SET extraction_method = 'ocr_failed', status = 'failed'
                WHERE id = ?{owner_clause} AND status = 'processing'
                """,
                (artifact_id, *owner_parameters),
            )
        return self.get_resume_artifact(artifact_id, account_id=account_id)

    def get_resume_artifact(
        self,
        artifact_id: int,
        account_id: int | None = None,
    ) -> ResumeArtifactRecord:
        """按 ID 读取简历文件元数据，并可强制账号过滤。"""

        with self.connect() as conn:
            if account_id is None:
                row = conn.execute(
                    "SELECT * FROM resume_artifacts WHERE id = ?",
                    (artifact_id,),
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT * FROM resume_artifacts WHERE id = ? AND account_id = ?",
                    (artifact_id, account_id),
                ).fetchone()
        if row is None:
            raise KeyError(f"Resume artifact not found: {artifact_id}")
        return self._resume_artifact_from_row(row)

    def get_resume_artifact_text(
        self,
        artifact_id: int,
        account_id: int | None = None,
    ) -> str:
        """读取简历提取正文；正文不随列表接口批量返回。"""

        with self.connect() as conn:
            if account_id is None:
                row = conn.execute(
                    "SELECT extracted_text FROM resume_artifacts WHERE id = ?",
                    (artifact_id,),
                ).fetchone()
            else:
                row = conn.execute(
                    """
                    SELECT extracted_text FROM resume_artifacts
                    WHERE id = ? AND account_id = ?
                    """,
                    (artifact_id, account_id),
                ).fetchone()
        if row is None:
            raise KeyError(f"Resume artifact not found: {artifact_id}")
        return str(row["extracted_text"])

    def list_resume_artifacts(
        self,
        candidate_id: int,
        account_id: int | None = None,
    ) -> list[ResumeArtifactRecord]:
        """按创建顺序列出候选人的原始和职位定制简历文件。"""

        with self.connect() as conn:
            if account_id is None:
                rows = conn.execute(
                    "SELECT * FROM resume_artifacts WHERE candidate_id = ? ORDER BY id",
                    (candidate_id,),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT * FROM resume_artifacts
                    WHERE candidate_id = ? AND account_id = ?
                    ORDER BY id
                    """,
                    (candidate_id, account_id),
                ).fetchall()
        return [self._resume_artifact_from_row(row) for row in rows]

    def delete_resume_artifact(
        self,
        artifact_id: int,
        account_id: int | None = None,
    ) -> dict[str, object]:
        """删除一份简历文件及其不再需要的草稿/长文本元数据。

        原始简历和职位定制简历都按单个文件删除。删除原始简历时保留已经生成的
        定制文件，只解除它们对原始文件的父级引用；删除某个定制文件时，只有当
        该草稿已经没有其他导出文件时才一并清理草稿记录。
        """

        artifact = self.get_resume_artifact(artifact_id, account_id=account_id)
        owner_clause = ""
        owner_parameters: tuple[object, ...] = ()
        if account_id is not None:
            owner_clause = " AND account_id = ?"
            owner_parameters = (account_id,)

        long_text_ids: set[int] = set()
        if artifact.long_text_id is not None:
            long_text_ids.add(int(artifact.long_text_id))

        with self.connect() as conn:
            # 兼容旧数据：即使 resume_artifacts.long_text_id 没有回填，也按实体关系寻找来源文本。
            artifact_long_text_rows = conn.execute(
                f"""
                SELECT id
                FROM long_texts
                WHERE (id = ? OR (entity_type = 'resume_artifact' AND entity_id = ?))
                {owner_clause}
                """,
                (artifact.long_text_id or -1, artifact.id, *owner_parameters),
            ).fetchall()
            long_text_ids.update(int(row["id"]) for row in artifact_long_text_rows)

            # 定制简历通常会同时生成 DOCX/PDF 两个文件；只有最后一个文件被删除时，
            # 才移除它们共享的草稿和草稿长文本。
            should_delete_draft = False
            if artifact.draft_id is not None:
                sibling_row = conn.execute(
                    f"""
                    SELECT COUNT(*) AS sibling_count
                    FROM resume_artifacts
                    WHERE draft_id = ? AND id <> ?{owner_clause}
                    """,
                    (artifact.draft_id, artifact.id, *owner_parameters),
                ).fetchone()
                should_delete_draft = int(sibling_row["sibling_count"]) == 0
                if should_delete_draft:
                    draft_long_text_rows = conn.execute(
                        f"""
                        SELECT id
                        FROM long_texts
                        WHERE entity_type = 'resume_draft' AND entity_id = ?{owner_clause}
                        """,
                        (artifact.draft_id, *owner_parameters),
                    ).fetchall()
                    long_text_ids.update(int(row["id"]) for row in draft_long_text_rows)

            # 允许删除原始文件而不破坏仍保留的定制文件。
            conn.execute(
                f"""
                UPDATE resume_artifacts
                SET parent_artifact_id = NULL
                WHERE parent_artifact_id = ?{owner_clause}
                """,
                (artifact.id, *owner_parameters),
            )
            cursor = conn.execute(
                f"DELETE FROM resume_artifacts WHERE id = ?{owner_clause}",
                (artifact.id, *owner_parameters),
            )
            if cursor.rowcount == 0:
                raise KeyError(f"Resume artifact not found: {artifact_id}")

            conn.execute(
                f"""
                DELETE FROM long_texts
                WHERE (
                    id IN ({", ".join("?" for _ in long_text_ids) or "NULL"})
                    OR (entity_type = 'resume_artifact' AND entity_id = ?)
                ){owner_clause}
                """,
                (
                    *sorted(long_text_ids),
                    artifact.id,
                    *owner_parameters,
                ),
            )

            if should_delete_draft and artifact.draft_id is not None:
                conn.execute(
                    f"DELETE FROM long_texts WHERE entity_type = 'resume_draft' AND entity_id = ?{owner_clause}",
                    (artifact.draft_id, *owner_parameters),
                )
                conn.execute(
                    f"DELETE FROM resume_drafts WHERE id = ?{owner_clause}",
                    (artifact.draft_id, *owner_parameters),
                )

        return {
            "artifact_id": artifact.id,
            "candidate_id": artifact.candidate_id,
            "storage_keys": [artifact.storage_key],
            "long_text_ids": sorted(long_text_ids),
            "draft_id": artifact.draft_id if should_delete_draft else None,
        }

    def delete_resume_artifacts(
        self,
        artifact_ids: list[int],
        account_id: int | None = None,
    ) -> None:
        """回滚未完整生成的派生文件元数据。

        该方法只用于应用层生成两个导出格式时的补偿事务；正常用户操作不删除历史版本。
        """

        if not artifact_ids:
            return
        placeholders = ", ".join("?" for _ in artifact_ids)
        parameters: list[object] = list(artifact_ids)
        owner_clause = ""
        if account_id is not None:
            owner_clause = " AND account_id = ?"
            parameters.append(account_id)
        with self.connect() as conn:
            conn.execute(
                f"DELETE FROM resume_artifacts WHERE id IN ({placeholders}){owner_clause}",
                tuple(parameters),
            )

    def _job_from_row(self, row: RepositoryRow) -> ImportedJob:
        """把 jobs 表的一行转换成 `ImportedJob`。"""

        return ImportedJob(
            id=int(row["id"]),
            raw_text=row["raw_text"],
            source_url=row["source_url"],
            import_method=row.get("import_method", "text"),
            captured_at=row.get("captured_at"),
            title=row["title"],
            city=row["city"],
            salary_min_k=row["salary_min_k"],
            salary_max_k=row["salary_max_k"],
            salary_months=row["salary_months"],
            salary_unit=row["salary_unit"],
            experience_min_years=row["experience_min_years"],
            experience_max_years=row["experience_max_years"],
            experience_label=row["experience_label"],
            education=row["education"],
            company_name=row["company_name"],
            industry=row["industry"],
            company_size=row["company_size"],
            skills=json.loads(row["skills_json"]),
            skill_requirements=[
                SkillRequirement(**item)
                for item in json.loads(row["skill_requirements_json"] or "[]")
                if isinstance(item, dict)
            ]
            if "skill_requirements_json" in row
            else [],
            description_text=row["description_text"],
            field_confidence=json.loads(row["field_confidence_json"]),
            uncertainty_notes=json.loads(row["uncertainty_notes_json"]),
        )

    def _project_card_from_row(self, row: RepositoryRow) -> ProjectExperienceRecord:
        """把项目卡片表的一行转换成领域模型。"""

        card = ProjectExperienceCard(**json.loads(row["card_json"]))
        return ProjectExperienceRecord(
            id=int(row["id"]),
            candidate_id=int(row["candidate_id"]),
            status=row["status"],
            card=card,
            confirmed_summary=row["confirmed_summary"],
            created_at=row["created_at"],
            confirmed_at=row["confirmed_at"],
        )

    def _resume_draft_from_row(self, row: RepositoryRow) -> ResumeDraftRecord:
        """把简历草稿表的一行转换成领域模型。"""

        draft = ResumeDraft(**json.loads(row["draft_json"]))
        return ResumeDraftRecord(
            id=int(row["id"]),
            candidate_id=int(row["candidate_id"]),
            job_id=int(row["job_id"]),
            version=int(row["version"]),
            status=row["status"],
            draft=draft,
            created_at=row["created_at"],
        )

    def _resume_artifact_from_row(self, row: RepositoryRow) -> ResumeArtifactRecord:
        """把简历文件表的一行转换成不携带全文的领域记录。"""

        return ResumeArtifactRecord(
            id=int(row["id"]),
            account_id=int(row["account_id"]) if row["account_id"] is not None else None,
            candidate_id=int(row["candidate_id"]),
            job_id=int(row["job_id"]) if row["job_id"] is not None else None,
            draft_id=int(row["draft_id"]) if row["draft_id"] is not None else None,
            parent_artifact_id=(
                int(row["parent_artifact_id"])
                if row["parent_artifact_id"] is not None
                else None
            ),
            version=int(row["version"]),
            artifact_type=str(row["artifact_type"]),
            original_filename=str(row["original_filename"]),
            download_filename=str(row["download_filename"]),
            storage_key=str(row["storage_key"]),
            media_type=str(row["media_type"]),
            file_size=int(row["file_size"]),
            sha256=str(row["sha256"]),
            extraction_method=str(row["extraction_method"]),
            text_length=int(row["text_length"]),
            page_count=int(row["page_count"]) if row["page_count"] is not None else None,
            status=str(row["status"]),
            long_text_id=int(row["long_text_id"]) if row["long_text_id"] is not None else None,
            created_at=str(row["created_at"]),
        )

    def _chat_message_from_row(self, row: RepositoryRow) -> ChatMessageRecord:
        """把聊天记录表的一行转换成领域模型。"""

        return ChatMessageRecord(
            id=int(row["id"]),
            candidate_id=int(row["candidate_id"]),
            session_id=row["session_id"],
            role=row["role"],
            content=row["content"],
            metadata=json.loads(row["metadata_json"]),
            created_at=row["created_at"],
        )

    def add_long_text(
        self,
        entity_type: str,
        entity_id: int,
        source_label: str,
        text: str,
        account_id: int | None = None,
        candidate_id: int | None = None,
    ) -> int:
        """公开的长文本写入方法。

        对话式入库、项目描述、简历片段、HR 对话等长文本材料都通过这个入口登记。
        返回插入 ID，方便应用层告诉用户本次保存了哪些材料。
        """

        with self.connect() as conn:
            return self._add_long_text(
                conn,
                entity_type,
                entity_id,
                source_label,
                text,
                account_id=account_id,
                candidate_id=candidate_id,
            )

    def list_long_texts(
        self,
        entity_types: list[str] | None = None,
        account_id: int | None = None,
        candidate_id: int | None = None,
    ) -> list[LongTextRecord]:
        """列出可同步到 RAG 索引的长文本材料。

        PostgreSQL 的 long_texts 是长文本来源登记处；RAG 层只从这里读取并建立派生索引。
        """

        with self.connect() as conn:
            conditions: list[str] = []
            parameters: list[object] = []
            if entity_types:
                placeholders = ", ".join("?" for _ in entity_types)
                conditions.append(f"entity_type IN ({placeholders})")
                parameters.extend(entity_types)
            if account_id is not None:
                conditions.append("account_id = ?")
                parameters.append(account_id)
            if candidate_id is not None:
                conditions.append("candidate_id = ?")
                parameters.append(candidate_id)
            where = f" WHERE {' AND '.join(conditions)}" if conditions else ""
            rows = conn.execute(
                f"SELECT * FROM long_texts{where} ORDER BY id",
                tuple(parameters),
            ).fetchall()
        return [long_text_from_row(row) for row in rows]

    def get_long_texts_by_ids(
        self,
        ids: list[int],
        account_id: int | None = None,
    ) -> list[LongTextRecord]:
        """按 ID 读取长文本材料，供增量 RAG 索引使用。

        对话式入库已经知道本次新增了哪些 `long_text_id`，因此增量索引不需要再
        读取整张 `long_texts` 表。
        """

        if not ids:
            return []
        placeholders = ", ".join("?" for _ in ids)
        with self.connect() as conn:
            parameters: list[object] = list(ids)
            owner_clause = ""
            if account_id is not None:
                owner_clause = " AND account_id = ?"
                parameters.append(account_id)
            rows = conn.execute(
                f"""
                SELECT * FROM long_texts
                WHERE id IN ({placeholders}){owner_clause}
                ORDER BY id
                """,
                tuple(parameters),
            ).fetchall()
        return [long_text_from_row(row) for row in rows]

    def _add_long_text(
        self,
        conn: RepositoryConnection,
        entity_type: str,
        entity_id: int,
        source_label: str,
        text: str,
        account_id: int | None = None,
        candidate_id: int | None = None,
    ) -> int:
        """在已有连接中写入长文本，供事务内复用。"""

        cursor = conn.execute(
            """
            INSERT INTO long_texts (
                account_id, candidate_id, entity_type, entity_id, source_label, text
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (account_id, candidate_id, entity_type, entity_id, source_label, text),
        )
        return int(cursor.lastrowid)

def now_iso() -> str:
    """返回秒级 ISO 时间字符串，用于记录本地确认时间。"""

    return datetime.now(UTC).isoformat(timespec="seconds")


def account_from_row(row: RepositoryRow) -> AccountRecord:
    """把账号行转换为不含密码的领域对象。"""

    return AccountRecord(
        id=int(row["id"]),
        email=str(row["email"]),
        display_name=row["display_name"],
        role=str(row["role"]),
        status=str(row["status"]),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
        must_change_password=bool(row["must_change_password"]),
    )


def auth_session_from_row(row: RepositoryRow) -> AuthSessionRecord:
    """把认证 Session 行转换为领域对象。"""

    return AuthSessionRecord(
        id=int(row["id"]),
        account_id=int(row["account_id"]),
        token_hash=str(row["token_hash"]),
        created_at=str(row["created_at"]),
        last_seen_at=str(row["last_seen_at"]),
        expires_at=str(row["expires_at"]),
        absolute_expires_at=str(row["absolute_expires_at"]),
        revoked_at=row["revoked_at"],
        user_agent=row["user_agent"],
        ip_address=row["ip_address"],
    )


def chat_session_from_row(row: RepositoryRow) -> ChatSessionRecord:
    """把独立对话行转换为领域对象。"""

    return ChatSessionRecord(
        id=int(row["id"]),
        session_id=str(row["session_id"]),
        # 未登录的历史领域/Web 测试记录可能没有账号归属；生产 Web
        # 始终传入正整数，旧记录在领域对象中用 0 表示“未绑定账号”。
        account_id=int(row["account_id"]) if row["account_id"] is not None else 0,
        candidate_id=int(row["candidate_id"]),
        job_id=int(row["job_id"]) if row["job_id"] is not None else None,
        title=str(row["title"]),
        status=str(row["status"]),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
        archived_at=row["archived_at"],
    )


def usage_event_from_row(row: RepositoryRow) -> UsageEventRecord:
    """把用量流水行转换为领域对象。"""

    return UsageEventRecord(
        id=int(row["id"]),
        account_id=int(row["account_id"]),
        candidate_id=int(row["candidate_id"]) if row["candidate_id"] is not None else None,
        session_id=row["session_id"],
        root_request_id=row["root_request_id"],
        call_id=str(row["call_id"]),
        provider=str(row["provider"]),
        model=str(row["model"]),
        operation=str(row["operation"]),
        input_tokens=int(row["input_tokens"]),
        output_tokens=int(row["output_tokens"]),
        total_tokens=int(row["total_tokens"]),
        usage_source=str(row["usage_source"]),
        status=str(row["status"]),
        attempt=int(row["attempt"]),
        provider_request_id=row["provider_request_id"],
        raw_usage=(json.loads(row["raw_usage_json"] or "{}") or {}),
        created_at=str(row["created_at"]),
        billable=bool(row["billable"]),
        pricing_version=row["pricing_version"],
    )


def tool_call_trace_from_row(row: RepositoryRow) -> ToolCallTraceRecord:
    """把工具调用审计行转换为领域对象。"""

    return ToolCallTraceRecord(
        id=int(row["id"]),
        account_id=int(row["account_id"]),
        candidate_id=int(row["candidate_id"]) if row["candidate_id"] is not None else None,
        session_id=row["session_id"],
        root_request_id=str(row["root_request_id"]),
        title=str(row["title"]),
        status=str(row["status"]),
        source=str(row["source"]),
        step_count=int(row["step_count"]),
        attempt_count=int(row["attempt_count"]),
        last_step_name=row["last_step_name"],
        last_error_summary=row["last_error_summary"],
        trace=_json_object(row["trace_json"]),
        created_at=str(row["created_at"]),
        started_at=row["started_at"],
        finished_at=row["finished_at"],
        updated_at=str(row["updated_at"]),
    )


def admin_audit_event_from_row(row: RepositoryRow) -> AdminAuditEventRecord:
    """把管理员审计行转换为领域对象。"""

    return AdminAuditEventRecord(
        id=int(row["id"]),
        actor_account_id=int(row["actor_account_id"]) if row["actor_account_id"] is not None else None,
        target_account_id=int(row["target_account_id"]) if row["target_account_id"] is not None else None,
        action=str(row["action"]),
        target_type=str(row["target_type"]),
        target_id=row["target_id"],
        outcome=str(row["outcome"]),
        summary=str(row["summary"]),
        details=_json_object(row["details_json"]),
        request_id=row["request_id"],
        created_at=str(row["created_at"]),
    )


def background_task_from_row(row: RepositoryRow) -> BackgroundTaskRecord:
    """把后台任务数据库行转换为领域记录。"""

    return BackgroundTaskRecord(
        id=int(row["id"]),
        task_key=str(row["task_key"]),
        account_id=int(row["account_id"]),
        candidate_id=int(row["candidate_id"]) if row["candidate_id"] is not None else None,
        session_id=row["session_id"],
        task_type=str(row["task_type"]),
        status=str(row["status"]),
        progress=int(row["progress"]),
        attempt=int(row["attempt"]),
        max_attempts=int(row["max_attempts"]),
        idempotency_key=row["idempotency_key"],
        payload=_json_object(row["payload_json"]),
        result=_json_object(row["result_json"]),
        error_summary=row["error_summary"],
        created_at=str(row["created_at"]),
        started_at=row["started_at"],
        finished_at=row["finished_at"],
        updated_at=str(row["updated_at"]),
    )


def _json_object(value: object) -> dict[str, object]:
    """兼容 SQLAlchemy JSONB 行和旧适配器返回的 JSON 字符串。"""

    if isinstance(value, Mapping):
        return {str(key): item for key, item in value.items()}
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def trim_task_error(value: str | None) -> str | None:
    """限制任务错误摘要长度，避免把上游原始响应或正文写入数据库。"""

    if not value:
        return None
    return " ".join(str(value).split())[:500]


def trim_audit_text(value: str | None) -> str | None:
    """限制审计摘要长度，避免把长正文写入后台日志。"""

    if not value:
        return None
    return " ".join(str(value).split())[:500]


def candidate_profile_from_row(row: RepositoryRow) -> CandidateProfile:
    """把 PostgreSQL 行转换为候选人档案对象。"""

    return CandidateProfile(
        id=int(row["id"]),
        name=row["name"],
        status=row["status"],
        education=row["education"],
        experience_years=float(row["experience_years"]),
        salary_floor_k=row["salary_floor_k"],
        expected_salary_k=row["expected_salary_k"],
        # 旧记录即使曾保留 Python/python 两个键，读取时也只向上层暴露一个技能。
        skills=normalize_skill_mapping(json.loads(row["skills_json"])),
        preferred_cities=normalize_city_list(json.loads(row["preferred_cities_json"] or "[]")),
        target_directions=json.loads(row["target_directions_json"]),
        unacceptable=json.loads(row["unacceptable_json"]),
        acceptable_cities=normalize_city_list(
            json.loads(row["acceptable_cities_json"] or "[]")
            if "acceptable_cities_json" in row
            else []
        ),
        preference_weights=sanitize_preference_weights(
            json.loads(row["preference_weights_json"] or "{}")
            if "preference_weights_json" in row
            else {}
        ),
    )


def long_text_from_row(row: RepositoryRow) -> LongTextRecord:
    """把数据库行转换为长文本记录。"""

    return LongTextRecord(
        id=int(row["id"]),
        entity_type=row["entity_type"],
        entity_id=int(row["entity_id"]),
        source_label=row["source_label"],
        text=row["text"],
        account_id=row.get("account_id"),
        candidate_id=row.get("candidate_id"),
    )


def project_card_index_text(card: ProjectExperienceCard) -> str:
    """把已确认项目卡片整理成一段可进入长文本检索的材料。"""

    parts = [
        card.project_name,
        "技术栈：" + "、".join(card.detected_tech_stack),
        "核心功能：" + "、".join(card.detected_core_features),
        "职责草稿：" + "；".join(card.responsibility_draft),
        "亮点草稿：" + "；".join(card.highlight_draft),
        "简历表达草稿：" + "；".join(card.resume_expression_draft),
    ]
    # 过滤空段落，避免向长文本表写入一堆无意义的空字段。
    return "\n".join(part for part in parts if part.strip())


def merge_unique(existing: list[str], incoming: list[str]) -> list[str]:
    """按原顺序合并列表并去重。"""

    merged = list(existing)
    for item in incoming:
        if item and item not in merged:
            merged.append(item)
    return merged
