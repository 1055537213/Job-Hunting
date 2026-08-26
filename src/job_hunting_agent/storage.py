"""数据库无关的领域仓储方法。

本模块只保存候选人、职位、对话、简历和用量的领域读写逻辑，不创建数据库连接，
也不负责 schema 初始化。具体连接和事务由 `SQLAlchemyStore` 提供，因此 Web、
后台任务和测试都走同一条 PostgreSQL + pgvector 数据路径。
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator, Mapping
from dataclasses import asdict, replace
from datetime import UTC, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any, Protocol, Self
from uuid import uuid4

from .admin_ledger import ADMIN_LEDGER_MAX_RECORDS, ADMIN_LEDGER_PAGE_SIZE
from .city_catalog import normalize_city_list
from .config import BillingSettings
from .deduplication import (
    DuplicateResourceError,
    candidate_profile_content_fingerprint,
    is_unique_constraint_violation,
    job_text_content_fingerprint,
    project_card_content_fingerprint,
)
from .job_parser import classify_skill_requirements, parse_job_text, validate_job_text
from .models import (
    AccountBalanceSummary,
    AccountRecord,
    AdminAuditEventRecord,
    AuthSessionRecord,
    BackgroundTaskRecord,
    BalanceLedgerRecord,
    CandidateProfile,
    CandidateProfileInput,
    CandidateProfilePatch,
    ChatMessageRecord,
    ChatSessionRecord,
    ImportedJob,
    KnowledgeAssetRecord,
    KnowledgeAssetVersionRecord,
    LongTextRecord,
    PaymentEventRecord,
    ProjectArchiveFileRecord,
    ProjectArchiveImportRecord,
    ProjectCollectionFileRecord,
    ProjectCollectionSessionRecord,
    ProjectExperienceCard,
    ProjectExperienceRecord,
    RechargeOrderRecord,
    ResumeArtifactRecord,
    ResumeDraft,
    ResumeDraftRecord,
    SkillRequirement,
    ToolCallTraceRecord,
    UsageEventRecord,
    VisualKnowledgeItemRecord,
    sanitize_preference_weights,
)
from .profile_mutation import apply_candidate_profile_patch
from .skill_normalization import normalize_skill_mapping

RESUME_ARTIFACT_STATUSES = {"ready", "processing", "failed", "scanning", "quarantined"}
KNOWLEDGE_ASSET_LIFECYCLE_STATUSES = {"active", "archived"}
KNOWLEDGE_ASSET_PROCESSING_STATUSES = {
    "uploaded",
    "scanning",
    "processing",
    "ready",
    "quarantined",
    "failed",
}
KNOWLEDGE_ASSET_SCAN_STATUSES = {"pending", "clean", "infected", "error", "not_required"}
PROJECT_ARCHIVE_IMPORT_STATUSES = {"uploaded", "processing", "ready", "failed", "quarantined"}
PROJECT_COLLECTION_STATUSES = {"planned", "uploading", "processing", "ready", "failed", "cancelled"}
INSUFFICIENT_BALANCE_MESSAGE = "余额不足，请先充值后重试。"


class InsufficientBalanceError(ValueError):
    """账号余额不足，当前模型调用不能开始。"""

    def __init__(self, message: str = INSUFFICIENT_BALANCE_MESSAGE) -> None:
        super().__init__(message)


class IdempotencyConflictError(ValueError):
    """同一个幂等键被用于不同的资金操作。"""


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

    def __init__(self) -> None:
        """初始化默认计费参数。"""

        self._billing_settings = BillingSettings()

    def connect(self) -> RepositoryConnection:
        """返回一次短生命周期事务连接。"""

        raise NotImplementedError

    def initialize(self) -> None:
        """确认数据库已经由 Alembic 管理并完成迁移。"""

        raise NotImplementedError

    def configure_billing(self, settings: BillingSettings) -> None:
        """覆盖默认计费参数。"""

        self._billing_settings = settings

    def billing_settings(self) -> BillingSettings:
        """返回当前计费参数。"""

        return self._billing_settings
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
        email_verified: bool = True,
        consents: list[tuple[str, str]] | None = None,
        consent_ip_address: str | None = None,
        consent_user_agent: str | None = None,
    ) -> AccountRecord:
        """写入一个账号并返回不含密码的账号记录。"""

        if role not in {"user", "admin"}:
            raise ValueError("账号角色只能是 user 或 admin。")
        if status not in {"active", "disabled"}:
            raise ValueError("账号状态只能是 active 或 disabled。")
        now = now_iso()
        consent_rows = consents or []
        for document_type, version in consent_rows:
            if document_type not in {"terms", "privacy"} or not version.strip():
                raise ValueError("协议同意记录无效。")
        with self.connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO accounts (
                    email, password_hash, display_name, role, status,
                    must_change_password, email_verified_at, deleted_at,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, NULL, ?, ?)
                """,
                (
                    email,
                    password_hash,
                    display_name,
                    role,
                    status,
                    bool(must_change_password),
                    now if email_verified else None,
                    now,
                    now,
                ),
            )
            account_id = int(cursor.lastrowid)
            self._ensure_account_billing_row(conn, account_id)
            for document_type, version in consent_rows:
                conn.execute(
                    """
                    INSERT INTO account_consents (
                        account_id, document_type, version, accepted_at,
                        ip_address, user_agent
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        account_id,
                        document_type,
                        version.strip(),
                        now,
                        consent_ip_address,
                        consent_user_agent,
                    ),
                )
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
                """
                SELECT * FROM accounts
                WHERE LOWER(email) = LOWER(?) AND deleted_at IS NULL
                """,
                (email,),
            ).fetchone()
        if row is None:
            return None
        return account_from_row(row), str(row["password_hash"])

    def list_accounts(self, include_disabled: bool = True) -> list[AccountRecord]:
        """列出账号，供管理员后台展示。"""

        with self.connect() as conn:
            if include_disabled:
                rows = conn.execute(
                    "SELECT * FROM accounts WHERE deleted_at IS NULL ORDER BY id"
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT * FROM accounts
                    WHERE status = 'active' AND deleted_at IS NULL
                    ORDER BY id
                    """
                ).fetchall()
        return [account_from_row(row) for row in rows]

    def count_active_admins(self) -> int:
        """返回当前仍可登录的管理员数量。"""

        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT COUNT(*) AS count FROM accounts
                WHERE role = 'admin' AND status = 'active' AND deleted_at IS NULL
                """
            ).fetchone()
        return int(row["count"])

    def save_account_action_token(
        self,
        account_id: int,
        purpose: str,
        token_hash: str,
        expires_at: str,
        requested_ip: str | None = None,
    ) -> None:
        """Replace outstanding tokens for one purpose and persist only the digest."""

        if purpose not in {"verify_email", "reset_password"}:
            raise ValueError("账号操作令牌类型无效。")
        created_at = now_iso()
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE account_action_tokens SET consumed_at = ?
                WHERE account_id = ? AND purpose = ? AND consumed_at IS NULL
                """,
                (created_at, account_id, purpose),
            )
            conn.execute(
                """
                INSERT INTO account_action_tokens (
                    account_id, purpose, token_hash, expires_at,
                    consumed_at, created_at, requested_ip
                ) VALUES (?, ?, ?, ?, NULL, ?, ?)
                """,
                (account_id, purpose, token_hash, expires_at, created_at, requested_ip),
            )

    def consume_email_verification_token(self, token_hash: str) -> AccountRecord | None:
        """Consume one live verification token and mark its account verified atomically."""

        consumed_at = now_iso()
        account_id: int | None = None
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT id, account_id FROM account_action_tokens
                WHERE token_hash = ? AND purpose = 'verify_email'
                  AND consumed_at IS NULL AND expires_at > ?
                """,
                (token_hash, consumed_at),
            ).fetchone()
            if row is None:
                return None
            cursor = conn.execute(
                """
                UPDATE account_action_tokens SET consumed_at = ?
                WHERE id = ? AND consumed_at IS NULL AND expires_at > ?
                """,
                (consumed_at, int(row["id"]), consumed_at),
            )
            if cursor.rowcount != 1:
                return None
            account_id = int(row["account_id"])
            conn.execute(
                """
                UPDATE accounts
                SET email_verified_at = COALESCE(email_verified_at, ?), updated_at = ?
                WHERE id = ? AND deleted_at IS NULL
                """,
                (consumed_at, consumed_at, account_id),
            )
        return self.get_account(account_id)

    def consume_password_reset_token(
        self,
        token_hash: str,
        password_hash: str,
    ) -> AccountRecord | None:
        """Atomically consume a reset token, change the password, and revoke sessions."""

        consumed_at = now_iso()
        account_id: int | None = None
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT id, account_id FROM account_action_tokens
                WHERE token_hash = ? AND purpose = 'reset_password'
                  AND consumed_at IS NULL AND expires_at > ?
                """,
                (token_hash, consumed_at),
            ).fetchone()
            if row is None:
                return None
            cursor = conn.execute(
                """
                UPDATE account_action_tokens SET consumed_at = ?
                WHERE id = ? AND consumed_at IS NULL AND expires_at > ?
                """,
                (consumed_at, int(row["id"]), consumed_at),
            )
            if cursor.rowcount != 1:
                return None
            account_id = int(row["account_id"])
            account_cursor = conn.execute(
                """
                UPDATE accounts
                SET password_hash = ?, must_change_password = FALSE,
                    email_verified_at = COALESCE(email_verified_at, ?), updated_at = ?
                WHERE id = ? AND deleted_at IS NULL
                """,
                (password_hash, consumed_at, consumed_at, account_id),
            )
            if account_cursor.rowcount != 1:
                return None
            conn.execute(
                """
                UPDATE auth_sessions SET revoked_at = ?
                WHERE account_id = ? AND revoked_at IS NULL
                """,
                (consumed_at, account_id),
            )
            conn.execute(
                """
                UPDATE account_action_tokens SET consumed_at = ?
                WHERE account_id = ? AND consumed_at IS NULL
                """,
                (consumed_at, account_id),
            )
        return self.get_account(account_id)

    def list_account_consents(self, account_id: int) -> list[dict[str, object]]:
        """List immutable agreement acceptances for account export and support."""

        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT document_type, version, accepted_at
                FROM account_consents WHERE account_id = ?
                ORDER BY accepted_at, id
                """,
                (account_id,),
            ).fetchall()
        return [
            {
                "document_type": str(row["document_type"]),
                "version": str(row["version"]),
                "accepted_at": str(row["accepted_at"]),
            }
            for row in rows
        ]

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

    def update_account_password_and_revoke_sessions(
        self,
        account_id: int,
        password_hash: str,
    ) -> AccountRecord:
        """Change a password and invalidate every existing browser session atomically."""

        changed_at = now_iso()
        with self.connect() as conn:
            cursor = conn.execute(
                """
                UPDATE accounts
                SET password_hash = ?, must_change_password = FALSE, updated_at = ?
                WHERE id = ? AND deleted_at IS NULL
                """,
                (password_hash, changed_at, account_id),
            )
            if cursor.rowcount != 1:
                raise KeyError(f"Account not found: {account_id}")
            conn.execute(
                """
                UPDATE auth_sessions SET revoked_at = ?
                WHERE account_id = ? AND revoked_at IS NULL
                """,
                (changed_at, account_id),
            )
            conn.execute(
                """
                UPDATE account_action_tokens SET consumed_at = ?
                WHERE account_id = ? AND consumed_at IS NULL
                """,
                (changed_at, account_id),
            )
        return self.get_account(account_id)

    def export_account_data(self, account_id: int) -> dict[str, object]:
        """Return a structured account export without password, session, or token secrets."""

        account = self.get_account(account_id)
        if account.deleted_at is not None:
            raise ValueError("已注销账号不能再次导出数据。")
        direct_tables = (
            "candidate_profiles",
            "jobs",
            "long_texts",
            "chat_sessions",
            "chat_messages",
            "project_experience_cards",
            "resume_drafts",
            "knowledge_assets",
            "project_archive_imports",
            "project_collection_sessions",
            "visual_knowledge_items",
            "resume_artifacts",
            "usage_events",
            "tool_call_traces",
            "background_tasks",
            "account_balance_ledger",
            "recharge_orders",
        )

        def row_dict(row: RepositoryRow) -> dict[str, object]:
            excluded = {"password_hash", "token_hash", "embedding"}
            return {
                key: row[key]
                for key in row.keys()
                if key not in excluded
            }

        exported: dict[str, object] = {
            "exported_at": now_iso(),
            "account": asdict(account),
            "consents": self.list_account_consents(account_id),
        }
        with self.connect() as conn:
            for table_name in direct_tables:
                rows = conn.execute(
                    f"SELECT * FROM {table_name} WHERE account_id = ? ORDER BY id",  # noqa: S608 - fixed allowlist
                    (account_id,),
                ).fetchall()
                exported[table_name] = [row_dict(row) for row in rows]
            exported["account_balance"] = row_dict(
                conn.execute(
                    "SELECT * FROM account_balances WHERE account_id = ?",
                    (account_id,),
                ).fetchone()
            )
            version_rows = conn.execute(
                """
                SELECT versions.* FROM knowledge_asset_versions AS versions
                JOIN knowledge_assets AS assets ON assets.id = versions.asset_id
                WHERE assets.account_id = ? ORDER BY versions.id
                """,
                (account_id,),
            ).fetchall()
            exported["knowledge_asset_versions"] = [row_dict(row) for row in version_rows]
            archive_file_rows = conn.execute(
                """
                SELECT files.* FROM project_archive_files AS files
                JOIN project_archive_imports AS imports
                  ON imports.id = files.project_archive_id
                WHERE imports.account_id = ? ORDER BY files.id
                """,
                (account_id,),
            ).fetchall()
            exported["project_archive_files"] = [row_dict(row) for row in archive_file_rows]
            collection_file_rows = conn.execute(
                """
                SELECT files.* FROM project_collection_files AS files
                JOIN project_collection_sessions AS sessions
                  ON sessions.id = files.collection_id
                WHERE sessions.account_id = ? ORDER BY files.id
                """,
                (account_id,),
            ).fetchall()
            exported["project_collection_files"] = [
                row_dict(row) for row in collection_file_rows
            ]
        return exported

    def prepare_account_deletion(self, account_id: int) -> list[str]:
        """Disable an account, reject active work, and return owned object keys."""

        account = self.get_account(account_id)
        if account.role == "admin":
            raise ValueError("管理员账号不能通过个人中心自助注销。")
        if account.deleted_at is not None:
            raise ValueError("账号已经注销。")
        changed_at = now_iso()
        with self.connect() as conn:
            running = conn.execute(
                """
                SELECT COUNT(*) AS count FROM background_tasks
                WHERE account_id = ? AND status = 'running'
                """,
                (account_id,),
            ).fetchone()
            if running is not None and int(running["count"]) > 0:
                raise ValueError("仍有后台任务正在执行，请等待任务结束后再注销。")
            key_rows = conn.execute(
                """
                SELECT versions.storage_key FROM knowledge_asset_versions AS versions
                JOIN knowledge_assets AS assets ON assets.id = versions.asset_id
                WHERE assets.account_id = ?
                UNION
                SELECT files.storage_key FROM project_collection_files AS files
                JOIN project_collection_sessions AS sessions ON sessions.id = files.collection_id
                WHERE sessions.account_id = ? AND files.storage_key IS NOT NULL
                UNION
                SELECT storage_key FROM visual_knowledge_items WHERE account_id = ?
                UNION
                SELECT storage_key FROM resume_artifacts WHERE account_id = ?
                """,
                (account_id, account_id, account_id, account_id),
            ).fetchall()
            conn.execute(
                "UPDATE accounts SET status = 'disabled', updated_at = ? WHERE id = ?",
                (changed_at, account_id),
            )
            conn.execute(
                """
                UPDATE auth_sessions SET revoked_at = ?
                WHERE account_id = ? AND revoked_at IS NULL
                """,
                (changed_at, account_id),
            )
            conn.execute(
                """
                UPDATE background_tasks
                SET status = 'cancelled', finished_at = ?, updated_at = ?
                WHERE account_id = ? AND status = 'queued'
                """,
                (changed_at, changed_at, account_id),
            )
        return sorted({str(row["storage_key"]) for row in key_rows if row["storage_key"]})

    def restore_account_after_failed_deletion(self, account_id: int) -> None:
        """Re-enable an account when object cleanup fails before database deletion."""

        with self.connect() as conn:
            cursor = conn.execute(
                """
                UPDATE accounts SET status = 'active', updated_at = ?
                WHERE id = ? AND deleted_at IS NULL AND status = 'disabled'
                """,
                (now_iso(), account_id),
            )
            if cursor.rowcount != 1:
                raise KeyError(f"Account not found or already deleted: {account_id}")

    def finalize_account_deletion(
        self,
        account_id: int,
        anonymized_email: str,
        unusable_password_hash: str,
    ) -> AccountRecord:
        """Delete personal workload data and retain anonymized financial facts."""

        deleted_at = now_iso()
        with self.connect() as conn:
            running = conn.execute(
                """
                SELECT COUNT(*) AS count FROM background_tasks
                WHERE account_id = ? AND status = 'running'
                """,
                (account_id,),
            ).fetchone()
            if running is not None and int(running["count"]) > 0:
                raise ValueError("注销过程中出现新的后台任务，请稍后重试。")
            # Child tables not directly removed by the candidate/profile cascades.
            for table_name in (
                "rag_chunks",
                "visual_knowledge_items",
                "resume_artifacts",
                "background_tasks",
                "tool_call_traces",
                "usage_events",
                "chat_messages",
                "chat_sessions",
                "resume_drafts",
                "project_archive_imports",
                "project_collection_sessions",
                "project_experience_cards",
                "knowledge_assets",
                "long_texts",
                "jobs",
                "candidate_profiles",
                "auth_sessions",
                "account_action_tokens",
            ):
                conn.execute(
                    f"DELETE FROM {table_name} WHERE account_id = ?",  # noqa: S608 - fixed allowlist
                    (account_id,),
                )
            conn.execute(
                """
                UPDATE account_consents
                SET ip_address = NULL, user_agent = NULL
                WHERE account_id = ?
                """,
                (account_id,),
            )
            conn.execute(
                """
                UPDATE accounts
                SET email = ?, password_hash = ?, display_name = NULL,
                    status = 'disabled', must_change_password = FALSE,
                    email_verified_at = NULL, deleted_at = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    anonymized_email,
                    unusable_password_hash,
                    deleted_at,
                    deleted_at,
                    account_id,
                ),
            )
        return self.get_account(account_id)

    def assert_account_can_spend(self, account_id: int) -> None:
        """确认账号仍可进行模型调用；余额停用或手动禁用时抛出错误。"""

        self.get_account(account_id)
        summary = self.get_account_balance_summary(account_id)
        if summary.state == "suspended":
            account = self.get_account(account_id)
            if account.status != "active":
                raise ValueError("账号已停用，请联系管理员。")
            raise InsufficientBalanceError()

    def get_account_balance_summary(self, account_id: int) -> AccountBalanceSummary:
        """读取单个账号的余额与消费汇总，必要时自动补初始化余额。"""

        account = self.get_account(account_id)
        with self.connect() as conn:
            self._ensure_account_billing_row(conn, account_id)
            row = conn.execute(
                """
                SELECT
                    account_id,
                    balance_micro_yuan,
                    total_recharge_micro_yuan,
                    total_consumed_micro_yuan,
                    low_balance_threshold_micro_yuan
                FROM account_balances
                WHERE account_id = ?
                """,
                (account_id,),
            ).fetchone()
            ledger_count = conn.execute(
                "SELECT COUNT(*) AS count FROM account_balance_ledger WHERE account_id = ?",
                (account_id,),
            ).fetchone()
        if row is None:
            raise KeyError(f"Account balance not found: {account_id}")
        return AccountBalanceSummary(
            account_id=account_id,
            balance_micro_yuan=int(row["balance_micro_yuan"]),
            total_recharge_micro_yuan=int(row["total_recharge_micro_yuan"]),
            total_consumed_micro_yuan=int(row["total_consumed_micro_yuan"]),
            ledger_entry_count=int(ledger_count["count"]),
            low_balance_threshold_micro_yuan=int(row["low_balance_threshold_micro_yuan"]),
            state=self._balance_state_value(
                int(row["balance_micro_yuan"]),
                int(row["low_balance_threshold_micro_yuan"]),
                account.status,
            ),
            state_label=self._balance_state_label(
                int(row["balance_micro_yuan"]),
                int(row["low_balance_threshold_micro_yuan"]),
                account.status,
            ),
        )

    def get_account_balance_row(self, account_id: int) -> dict[str, object]:
        """读取单个账号的原始余额行和派生状态。"""

        summary = self.get_account_balance_summary(account_id)
        account = self.get_account(account_id)
        return {
            "account_id": summary.account_id,
            "balance_micro_yuan": summary.balance_micro_yuan,
            "total_recharge_micro_yuan": summary.total_recharge_micro_yuan,
            "total_consumed_micro_yuan": summary.total_consumed_micro_yuan,
            "ledger_entry_count": summary.ledger_entry_count,
            "low_balance_threshold_micro_yuan": summary.low_balance_threshold_micro_yuan,
            "state": self._balance_state_value(
                summary.balance_micro_yuan,
                summary.low_balance_threshold_micro_yuan,
                account.status,
            ),
            "state_label": self._balance_state_label(
                summary.balance_micro_yuan,
                summary.low_balance_threshold_micro_yuan,
                account.status,
            ),
        }

    def summarize_account_balances(self) -> list[dict[str, object]]:
        """按账号汇总余额、充值和消费情况。"""

        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT
                    b.account_id,
                    b.balance_micro_yuan,
                    b.total_recharge_micro_yuan,
                    b.total_consumed_micro_yuan,
                    b.low_balance_threshold_micro_yuan,
                    COALESCE(l.ledger_entry_count, 0) AS ledger_entry_count
                FROM account_balances b
                LEFT JOIN (
                    SELECT account_id, COUNT(*) AS ledger_entry_count
                    FROM account_balance_ledger
                    GROUP BY account_id
                ) AS l ON l.account_id = b.account_id
                ORDER BY b.account_id
                """
            ).fetchall()
        results: list[dict[str, object]] = []
        for row in rows:
            account = self.get_account(int(row["account_id"]))
            balance_micro_yuan = int(row["balance_micro_yuan"])
            low_balance_threshold_micro_yuan = int(row["low_balance_threshold_micro_yuan"])
            results.append(
                {
                    "account_id": int(row["account_id"]),
                    "balance_micro_yuan": balance_micro_yuan,
                    "total_recharge_micro_yuan": int(row["total_recharge_micro_yuan"]),
                    "total_consumed_micro_yuan": int(row["total_consumed_micro_yuan"]),
                    "ledger_entry_count": int(row["ledger_entry_count"]),
                    "low_balance_threshold_micro_yuan": low_balance_threshold_micro_yuan,
                    "state": self._balance_state_value(
                        balance_micro_yuan,
                        low_balance_threshold_micro_yuan,
                        account.status,
                    ),
                    "state_label": self._balance_state_label(
                        balance_micro_yuan,
                        low_balance_threshold_micro_yuan,
                        account.status,
                    ),
                }
            )
        return results

    def create_simulated_recharge_order(
        self,
        account_id: int,
        amount_yuan: float | Decimal,
        *,
        idempotency_key: str,
        description: str = "个人中心模拟充值",
    ) -> tuple[RechargeOrderRecord, BalanceLedgerRecord]:
        """开发环境创建并立即结算模拟订单，完整经过订单和支付事件链路。"""

        account = self.get_account(account_id)
        amount_micro_yuan = self._positive_money_amount(amount_yuan)
        key = self._validated_idempotency_key(idempotency_key)
        clean_description = description.strip()[:500] or "个人中心模拟充值"
        with self.connect() as conn:
            self._ensure_account_billing_row(conn, account_id)
            balance_row = self._lock_account_billing_row(conn, account_id)
            if balance_row is None:
                raise KeyError(f"Account balance not found: {account_id}")
            existing_order_row = conn.execute(
                """
                SELECT * FROM recharge_orders
                WHERE account_id = ? AND idempotency_key = ?
                """,
                (account_id, key),
            ).fetchone()
            if existing_order_row is not None:
                if (
                    int(existing_order_row["amount_micro_yuan"]) != amount_micro_yuan
                    or str(existing_order_row["payment_provider"]) != "simulated"
                ):
                    raise IdempotencyConflictError("该幂等键已用于另一笔充值请求。")
                ledger_row = conn.execute(
                    "SELECT * FROM account_balance_ledger WHERE recharge_order_id = ?",
                    (int(existing_order_row["id"]),),
                ).fetchone()
                if ledger_row is None or str(existing_order_row["status"]) != "paid":
                    raise RuntimeError("模拟充值订单尚未完成，请稍后重试。")
                return (
                    self._recharge_order_from_row(existing_order_row),
                    self._balance_ledger_from_row(ledger_row),
                )

            now = now_iso()
            order_number = f"recharge-{uuid4().hex}"
            order_cursor = conn.execute(
                """
                INSERT INTO recharge_orders (
                    order_number, account_id, created_by_account_id, amount_micro_yuan,
                    status, payment_provider, provider_order_id, idempotency_key,
                    description, failure_reason, details_json, created_at, updated_at,
                    paid_at, cancelled_at, refunded_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    order_number,
                    account_id,
                    account.id,
                    amount_micro_yuan,
                    "pending",
                    "simulated",
                    order_number,
                    key,
                    clean_description,
                    None,
                    json.dumps({"environment": "development"}, ensure_ascii=False),
                    now,
                    now,
                    None,
                    None,
                    None,
                ),
            )
            order_id = order_cursor.lastrowid
            if order_id is None:
                raise RuntimeError("模拟充值订单创建失败。")
            before = int(balance_row["balance_micro_yuan"])
            after = before + amount_micro_yuan
            ledger_reference = f"recharge-order:{order_number}"
            ledger_id = self._insert_balance_ledger_entry(
                conn,
                account_id=account_id,
                entry_kind="recharge",
                amount_micro_yuan=amount_micro_yuan,
                balance_before_micro_yuan=before,
                balance_after_micro_yuan=after,
                token_count=None,
                price_per_million_tokens_yuan=None,
                source_reference=ledger_reference,
                summary=clean_description,
                details={"payment_provider": "simulated", "order_number": order_number},
                created_at=now,
                recharge_order_id=order_id,
            )
            if ledger_id is None:
                raise RuntimeError("模拟充值流水创建失败。")
            conn.execute(
                """
                UPDATE account_balances
                SET balance_micro_yuan = ?, total_recharge_micro_yuan = total_recharge_micro_yuan + ?,
                    updated_at = ?
                WHERE account_id = ?
                """,
                (after, amount_micro_yuan, now, account_id),
            )
            conn.execute(
                """
                UPDATE recharge_orders
                SET status = 'paid', paid_at = ?, updated_at = ?
                WHERE id = ?
                """,
                (now, now, order_id),
            )
            payload_hash = hashlib.sha256(
                f"{order_number}:{amount_micro_yuan}:paid".encode()
            ).hexdigest()
            conn.execute(
                """
                INSERT INTO payment_events (
                    recharge_order_id, payment_provider, provider_event_id, event_type,
                    processing_status, signature_valid, payload_sha256, error_summary,
                    details_json, received_at, processed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    order_id,
                    "simulated",
                    f"simulated:{order_number}",
                    "payment.succeeded",
                    "processed",
                    True,
                    payload_hash,
                    None,
                    json.dumps({"simulation": True}, ensure_ascii=False),
                    now,
                    now,
                ),
            )
            order_row = conn.execute(
                "SELECT * FROM recharge_orders WHERE id = ?",
                (order_id,),
            ).fetchone()
            ledger_row = conn.execute(
                "SELECT * FROM account_balance_ledger WHERE id = ?",
                (ledger_id,),
            ).fetchone()
        if order_row is None or ledger_row is None:
            raise RuntimeError("模拟充值结算结果读取失败。")
        return self._recharge_order_from_row(order_row), self._balance_ledger_from_row(ledger_row)

    def credit_account_balance_with_audit(
        self,
        account_id: int,
        amount_yuan: float | Decimal,
        *,
        actor_account_id: int,
        reason: str,
        idempotency_key: str,
        audit_event: AdminAuditEventRecord,
    ) -> BalanceLedgerRecord:
        """管理员人工补款；余额、流水和审计必须在同一事务提交。"""

        actor = self.get_account(actor_account_id)
        if actor.role != "admin":
            raise PermissionError("只有管理员可以执行人工补款。")
        self.get_account(account_id)
        if audit_event.actor_account_id != actor_account_id or audit_event.target_account_id != account_id:
            raise ValueError("管理员补款审计事件与操作账号不一致。")
        self._validate_admin_audit_event(audit_event)
        amount_micro_yuan = self._positive_money_amount(amount_yuan)
        clean_reason = reason.strip()
        if len(clean_reason) < 2:
            raise ValueError("管理员补款原因至少需要 2 个字符。")
        if len(clean_reason) > 500:
            raise ValueError("管理员补款原因不能超过 500 个字符。")
        key = self._validated_idempotency_key(idempotency_key)
        ledger_reference = f"admin-credit:{actor_account_id}:{key}"
        with self.connect() as conn:
            self._ensure_account_billing_row(conn, account_id)
            balance_row = self._lock_account_billing_row(conn, account_id)
            if balance_row is None:
                raise KeyError(f"Account balance not found: {account_id}")
            before = int(balance_row["balance_micro_yuan"])
            after = before + amount_micro_yuan
            now = now_iso()
            ledger_id = self._insert_balance_ledger_entry(
                conn,
                account_id=account_id,
                entry_kind="adjustment",
                amount_micro_yuan=amount_micro_yuan,
                balance_before_micro_yuan=before,
                balance_after_micro_yuan=after,
                token_count=None,
                price_per_million_tokens_yuan=None,
                source_reference=ledger_reference,
                summary="管理员人工补款",
                details={"reason": clean_reason, "adjustment_type": "manual_credit"},
                created_at=now,
                operator_account_id=actor_account_id,
            )
            if ledger_id is None:
                existing_row = conn.execute(
                    "SELECT * FROM account_balance_ledger WHERE source_reference = ?",
                    (ledger_reference,),
                ).fetchone()
                if existing_row is None:
                    raise RuntimeError("管理员补款幂等流水读取失败。")
                if (
                    int(existing_row["account_id"]) != account_id
                    or int(existing_row["amount_micro_yuan"]) != amount_micro_yuan
                    or existing_row.get("operator_account_id") != actor_account_id
                    or _json_object(existing_row["details_json"]).get("reason") != clean_reason
                ):
                    raise IdempotencyConflictError("该幂等键已用于另一笔管理员补款。")
                return self._balance_ledger_from_row(existing_row)
            conn.execute(
                """
                UPDATE account_balances
                SET balance_micro_yuan = ?, updated_at = ?
                WHERE account_id = ?
                """,
                (after, now, account_id),
            )
            event = replace(
                audit_event,
                details={
                    **(audit_event.details or {}),
                    "amount_micro_yuan": amount_micro_yuan,
                    "balance_before_micro_yuan": before,
                    "balance_after_micro_yuan": after,
                    "reason": clean_reason,
                    "ledger_reference": ledger_reference,
                },
            )
            self._insert_admin_audit_event(conn, event)
            row = conn.execute(
                "SELECT * FROM account_balance_ledger WHERE id = ?",
                (ledger_id,),
            ).fetchone()
        if row is None:
            raise RuntimeError("管理员补款流水写入失败。")
        return self._balance_ledger_from_row(row)

    def list_recharge_orders(
        self,
        account_id: int | None = None,
        *,
        limit: int = 100,
        offset: int = 0,
    ) -> list[RechargeOrderRecord]:
        """分页读取充值订单；管理员补款不会出现在这里。"""

        conditions: list[str] = []
        parameters: list[object] = []
        if account_id is not None:
            conditions.append("account_id = ?")
            parameters.append(account_id)
        where = f" WHERE {' AND '.join(conditions)}" if conditions else ""
        parameters.extend([max(1, min(int(limit), ADMIN_LEDGER_PAGE_SIZE)), max(0, int(offset))])
        with self.connect() as conn:
            rows = conn.execute(
                f"""
                SELECT * FROM recharge_orders{where}
                ORDER BY created_at DESC, id DESC
                LIMIT ? OFFSET ?
                """,
                tuple(parameters),
            ).fetchall()
        return [self._recharge_order_from_row(row) for row in rows]

    def count_recharge_orders(self, account_id: int | None = None) -> int:
        """返回充值订单数量。"""

        where = " WHERE account_id = ?" if account_id is not None else ""
        parameters: tuple[object, ...] = (account_id,) if account_id is not None else ()
        with self.connect() as conn:
            row = conn.execute(
                f"SELECT COUNT(*) AS count FROM recharge_orders{where}",
                parameters,
            ).fetchone()
        return int(row["count"])

    def get_recharge_order(self, order_id: int) -> RechargeOrderRecord:
        """按内部 ID 读取一笔充值订单。"""

        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM recharge_orders WHERE id = ?",
                (order_id,),
            ).fetchone()
        if row is None:
            raise KeyError(f"Recharge order not found: {order_id}")
        return self._recharge_order_from_row(row)

    def list_payment_events(
        self,
        recharge_order_id: int,
    ) -> list[PaymentEventRecord]:
        """读取一笔充值订单的低敏支付事件，供后台排障。"""

        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM payment_events
                WHERE recharge_order_id = ?
                ORDER BY received_at, id
                """,
                (recharge_order_id,),
            ).fetchall()
        return [self._payment_event_from_row(row) for row in rows]

    def list_account_balance_ledger(
        self,
        account_id: int | None = None,
        *,
        limit: int = 100,
        offset: int = 0,
    ) -> list[BalanceLedgerRecord]:
        """分页列出余额流水。"""

        conditions: list[str] = []
        parameters: list[object] = []
        if account_id is not None:
            conditions.append("account_id = ?")
            parameters.append(account_id)
        where = f" WHERE {' AND '.join(conditions)}" if conditions else ""
        parameters.extend([max(1, min(limit, ADMIN_LEDGER_PAGE_SIZE)), max(0, int(offset))])
        with self.connect() as conn:
            rows = conn.execute(
                f"""
                SELECT * FROM account_balance_ledger{where}
                ORDER BY created_at DESC, id DESC
                LIMIT ? OFFSET ?
                """,
                tuple(parameters),
            ).fetchall()
        return [self._balance_ledger_from_row(row) for row in rows]

    def count_account_balance_ledger(
        self,
        account_id: int | None = None,
    ) -> int:
        """返回余额流水数量。"""

        conditions: list[str] = []
        parameters: list[object] = []
        if account_id is not None:
            conditions.append("account_id = ?")
            parameters.append(account_id)
        where = f" WHERE {' AND '.join(conditions)}" if conditions else ""
        with self.connect() as conn:
            row = conn.execute(
                f"SELECT COUNT(*) AS count FROM account_balance_ledger{where}",
                tuple(parameters),
            ).fetchone()
        return int(row["count"])

    def summarize_balance_ledger_by_account(self) -> list[dict[str, int]]:
        """按账号汇总余额流水数量与金额。"""

        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT
                    account_id,
                    COALESCE(SUM(CASE WHEN entry_kind = 'recharge' THEN amount_micro_yuan ELSE 0 END), 0)
                        AS total_recharge_micro_yuan,
                    COALESCE(SUM(CASE WHEN entry_kind = 'consumption' THEN -amount_micro_yuan ELSE 0 END), 0)
                        AS total_consumed_micro_yuan,
                    COUNT(*) AS entry_count
                FROM account_balance_ledger
                GROUP BY account_id
                ORDER BY account_id
                """
            ).fetchall()
        return [{key: int(row[key]) for key in row} for row in rows]

    def _ensure_account_billing_row(self, conn: RepositoryConnection, account_id: int) -> None:
        """确保账号拥有余额摘要行；首次创建时会写入初始化余额流水。"""

        row = conn.execute(
            "SELECT * FROM account_balances WHERE account_id = ?",
            (account_id,),
        ).fetchone()
        settings = self.billing_settings()
        threshold_micro_yuan = self._yuan_to_micro_yuan(settings.low_balance_threshold_yuan)
        if row is None:
            starting_balance_micro_yuan = self._yuan_to_micro_yuan(settings.starting_balance_yuan)
            now = now_iso()
            conn.execute(
                """
                INSERT INTO account_balances (
                    account_id, balance_micro_yuan, total_recharge_micro_yuan,
                    total_consumed_micro_yuan, low_balance_threshold_micro_yuan,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                RETURNING account_id
                """,
                (
                    account_id,
                    starting_balance_micro_yuan,
                    starting_balance_micro_yuan,
                    0,
                    threshold_micro_yuan,
                    now,
                    now,
                ),
            )
            if starting_balance_micro_yuan > 0:
                self._insert_balance_ledger_entry(
                    conn,
                    account_id=account_id,
                    entry_kind="initial_credit",
                    amount_micro_yuan=starting_balance_micro_yuan,
                    balance_before_micro_yuan=0,
                    balance_after_micro_yuan=starting_balance_micro_yuan,
                    token_count=None,
                    price_per_million_tokens_yuan=settings.price_per_million_tokens_yuan,
                    source_reference=f"initial-credit:{account_id}",
                    summary="系统初始化余额",
                    details={"initial_balance_yuan": settings.starting_balance_yuan},
                    created_at=now,
                )
            return
        if int(row["low_balance_threshold_micro_yuan"]) != threshold_micro_yuan:
            conn.execute(
                """
                UPDATE account_balances
                SET low_balance_threshold_micro_yuan = ?, updated_at = ?
                WHERE account_id = ?
                """,
                (threshold_micro_yuan, now_iso(), account_id),
            )

    def _lock_account_billing_row(
        self,
        conn: RepositoryConnection,
        account_id: int,
    ) -> RepositoryRow | None:
        """按账号锁定余额摘要行。"""

        return conn.execute(
            "SELECT * FROM account_balances WHERE account_id = ? FOR UPDATE",
            (account_id,),
        ).fetchone()

    def _insert_balance_ledger_entry(
        self,
        conn: RepositoryConnection,
        *,
        account_id: int,
        entry_kind: str,
        amount_micro_yuan: int,
        balance_before_micro_yuan: int,
        balance_after_micro_yuan: int,
        token_count: int | None,
        price_per_million_tokens_yuan: float | None,
        source_reference: str | None,
        summary: str,
        details: dict[str, object] | None,
        created_at: str,
        operator_account_id: int | None = None,
        recharge_order_id: int | None = None,
    ) -> int | None:
        """在已有事务连接里写入一条余额流水。"""

        cursor = conn.execute(
            """
            INSERT INTO account_balance_ledger (
                account_id, entry_kind, amount_micro_yuan, balance_before_micro_yuan,
                balance_after_micro_yuan, token_count, price_per_million_tokens_yuan,
                operator_account_id, recharge_order_id, source_reference, summary,
                details_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (source_reference) DO NOTHING
            """,
            (
                account_id,
                entry_kind,
                amount_micro_yuan,
                balance_before_micro_yuan,
                balance_after_micro_yuan,
                token_count,
                price_per_million_tokens_yuan,
                operator_account_id,
                recharge_order_id,
                source_reference,
                summary,
                json.dumps(details or {}, ensure_ascii=False),
                created_at,
            ),
        )
        return cursor.lastrowid

    def _record_balance_consumption(
        self,
        conn: RepositoryConnection,
        *,
        account_id: int,
        call_id: str,
        operation: str,
        total_tokens: int,
        created_at: str,
    ) -> None:
        """把一次真实模型调用换算成余额扣减流水。"""

        settings = self.billing_settings()
        if total_tokens <= 0:
            return
        balance_row = self._lock_account_billing_row(conn, account_id)
        if balance_row is None:
            self._ensure_account_billing_row(conn, account_id)
            balance_row = self._lock_account_billing_row(conn, account_id)
        if balance_row is None:
            raise KeyError(f"Account balance not found: {account_id}")
        existing_ledger = conn.execute(
            """
            SELECT account_id, entry_kind FROM account_balance_ledger
            WHERE source_reference = ?
            """,
            (call_id,),
        ).fetchone()
        if existing_ledger is not None:
            if (
                int(existing_ledger["account_id"]) != account_id
                or str(existing_ledger["entry_kind"]) != "consumption"
            ):
                raise IdempotencyConflictError("该模型调用标识已用于另一笔余额流水。")
            return
        before = int(balance_row["balance_micro_yuan"])
        price = settings.price_per_million_tokens_yuan
        cost_micro_yuan = round(total_tokens * price)
        after = before - cost_micro_yuan
        now = created_at
        conn.execute(
            """
            UPDATE account_balances
            SET balance_micro_yuan = ?, total_consumed_micro_yuan = total_consumed_micro_yuan + ?,
                updated_at = ?
            WHERE account_id = ?
            """,
            (after, cost_micro_yuan, now, account_id),
        )
        ledger_id = self._insert_balance_ledger_entry(
            conn,
            account_id=account_id,
            entry_kind="consumption",
            amount_micro_yuan=-cost_micro_yuan,
            balance_before_micro_yuan=before,
            balance_after_micro_yuan=after,
            token_count=total_tokens,
            price_per_million_tokens_yuan=price,
            source_reference=call_id,
            summary=f"{operation} 扣费",
            details={
                "call_id": call_id,
                "operation": operation,
                "total_tokens": total_tokens,
                "price_per_million_tokens_yuan": price,
                "consumption_micro_yuan": cost_micro_yuan,
            },
            created_at=now,
        )
        if ledger_id is None:  # pragma: no cover - 账号余额行锁下不应再发生并发冲突
            raise RuntimeError("模型用量扣费流水写入失败。")

    def _balance_state_value(
        self,
        balance_micro_yuan: int,
        low_balance_threshold_micro_yuan: int,
        account_status: str,
    ) -> str:
        """把余额和账号状态压缩成前端稳定使用的状态值。"""

        if account_status != "active":
            return "suspended"
        if balance_micro_yuan <= 0:
            return "suspended"
        if balance_micro_yuan <= low_balance_threshold_micro_yuan:
            return "low_balance"
        return "balance"

    def _balance_state_label(
        self,
        balance_micro_yuan: int,
        low_balance_threshold_micro_yuan: int,
        account_status: str,
    ) -> str:
        """把余额状态转成更适合页面显示的中文标签。"""

        state = self._balance_state_value(
            balance_micro_yuan,
            low_balance_threshold_micro_yuan,
            account_status,
        )
        return {
            "balance": "余额",
            "low_balance": "低余额",
            "suspended": "停用",
        }[state]

    def _yuan_to_micro_yuan(self, value: float | Decimal) -> int:
        """把元换算成微元，便于余额和计费统一存储。"""

        try:
            amount = Decimal(str(value))
        except (InvalidOperation, ValueError) as error:
            raise ValueError("金额格式无效。") from error
        if not amount.is_finite():
            raise ValueError("金额格式无效。")
        return int((amount * Decimal(1000000)).quantize(Decimal(1), rounding=ROUND_HALF_UP))

    def _positive_money_amount(self, value: float | Decimal) -> int:
        """把正数金额转换成微元，并限制单笔金额以避免误操作。"""

        amount_micro_yuan = self._yuan_to_micro_yuan(value)
        if amount_micro_yuan <= 0:
            raise ValueError("充值金额必须大于 0。")
        if amount_micro_yuan > 1_000_000_000_000:
            raise ValueError("单笔充值金额不能超过 100 万元。")
        return amount_micro_yuan

    def _validated_idempotency_key(self, value: str) -> str:
        """校验资金操作幂等键，避免空键或不可控长文本进入唯一索引。"""

        key = str(value or "").strip()
        if len(key) < 16 or len(key) > 128:
            raise ValueError("充值幂等键长度必须在 16 到 128 个字符之间。")
        if not all(character.isalnum() or character in "-_:" for character in key):
            raise ValueError("充值幂等键只能包含字母、数字、短横线、下划线和冒号。")
        return key

    def _balance_ledger_from_row(self, row: RepositoryRow) -> BalanceLedgerRecord:
        """把余额流水行转换成领域对象。"""

        return BalanceLedgerRecord(
            id=int(row["id"]),
            account_id=int(row["account_id"]),
            entry_kind=str(row["entry_kind"]),
            amount_micro_yuan=int(row["amount_micro_yuan"]),
            balance_before_micro_yuan=int(row["balance_before_micro_yuan"]),
            balance_after_micro_yuan=int(row["balance_after_micro_yuan"]),
            token_count=int(row["token_count"]) if row["token_count"] is not None else None,
            price_per_million_tokens_yuan=(
                float(row["price_per_million_tokens_yuan"])
                if row["price_per_million_tokens_yuan"] is not None
                else None
            ),
            operator_account_id=(
                int(row["operator_account_id"])
                if row.get("operator_account_id") is not None
                else None
            ),
            recharge_order_id=(
                int(row["recharge_order_id"])
                if row.get("recharge_order_id") is not None
                else None
            ),
            source_reference=row["source_reference"],
            summary=str(row["summary"]),
            details=_json_object(row["details_json"]),
            created_at=str(row["created_at"]),
        )

    def _recharge_order_from_row(self, row: RepositoryRow) -> RechargeOrderRecord:
        """把充值订单行转换为领域对象。"""

        return RechargeOrderRecord(
            id=int(row["id"]),
            order_number=str(row["order_number"]),
            account_id=int(row["account_id"]),
            created_by_account_id=(
                int(row["created_by_account_id"])
                if row["created_by_account_id"] is not None
                else None
            ),
            amount_micro_yuan=int(row["amount_micro_yuan"]),
            status=str(row["status"]),
            payment_provider=str(row["payment_provider"]),
            provider_order_id=row["provider_order_id"],
            idempotency_key=str(row["idempotency_key"]),
            description=str(row["description"]),
            failure_reason=row["failure_reason"],
            details=_json_object(row["details_json"]),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
            paid_at=str(row["paid_at"]) if row["paid_at"] is not None else None,
            cancelled_at=(
                str(row["cancelled_at"])
                if row["cancelled_at"] is not None
                else None
            ),
            refunded_at=str(row["refunded_at"]) if row["refunded_at"] is not None else None,
        )

    def _payment_event_from_row(self, row: RepositoryRow) -> PaymentEventRecord:
        """把支付事件行转换为领域对象。"""

        return PaymentEventRecord(
            id=int(row["id"]),
            recharge_order_id=int(row["recharge_order_id"]),
            payment_provider=str(row["payment_provider"]),
            provider_event_id=str(row["provider_event_id"]),
            event_type=str(row["event_type"]),
            processing_status=str(row["processing_status"]),
            signature_valid=bool(row["signature_valid"]),
            payload_sha256=str(row["payload_sha256"]),
            error_summary=row["error_summary"],
            details=_json_object(row["details_json"]),
            received_at=str(row["received_at"]),
            processed_at=str(row["processed_at"]) if row["processed_at"] is not None else None,
        )

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
            cursor = conn.execute(
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
                "SELECT * FROM usage_events WHERE call_id = ?",
                (event.call_id,),
            ).fetchone()
            if row is not None and cursor.lastrowid is None:
                requested_identity = (
                    event.account_id,
                    event.candidate_id,
                    event.session_id,
                    event.root_request_id,
                    event.provider,
                    event.model,
                    event.operation,
                    max(0, int(event.input_tokens)),
                    max(0, int(event.output_tokens)),
                    max(0, int(event.total_tokens)),
                    event.usage_source,
                    event.status,
                    bool(event.billable and event.usage_source == "provider" and event.status == "succeeded"),
                    event.pricing_version,
                )
                stored_identity = (
                    int(row["account_id"]),
                    int(row["candidate_id"]) if row["candidate_id"] is not None else None,
                    row["session_id"],
                    row["root_request_id"],
                    str(row["provider"]),
                    str(row["model"]),
                    str(row["operation"]),
                    int(row["input_tokens"]),
                    int(row["output_tokens"]),
                    int(row["total_tokens"]),
                    str(row["usage_source"]),
                    str(row["status"]),
                    bool(row["billable"]),
                    row["pricing_version"],
                )
                if stored_identity != requested_identity:
                    raise IdempotencyConflictError("该模型调用标识已用于另一笔用量记录。")
            if (
                row is not None
                and bool(row["billable"])
                and row["usage_source"] == "provider"
                and row["status"] == "succeeded"
            ):
                self._record_balance_consumption(
                    conn,
                    account_id=int(row["account_id"]),
                    call_id=str(row["call_id"]),
                    operation=str(row["operation"]),
                    total_tokens=int(row["total_tokens"]),
                    created_at=str(row["created_at"]),
                )
            self._prune_usage_events_for_account(
                conn,
                event.account_id,
                max_records=ADMIN_LEDGER_MAX_RECORDS,
            )
        if row is None:  # pragma: no cover - 仅在数据库异常时触发
            raise RuntimeError(f"Usage event was not persisted: {event.call_id}")
        return usage_event_from_row(row)

    def _prune_usage_events_for_account(
        self,
        conn: RepositoryConnection,
        account_id: int,
        *,
        max_records: int = ADMIN_LEDGER_MAX_RECORDS,
    ) -> int:
        """保留某个账号最近的固定页数 Token 记录。"""

        retain = max(1, int(max_records))
        cursor = conn.execute(
            """
            DELETE FROM usage_events
            WHERE account_id = ?
              AND id NOT IN (
                  SELECT id
                  FROM usage_events
                  WHERE account_id = ?
                  ORDER BY created_at DESC, id DESC
                  LIMIT ?
              )
            """,
            (account_id, account_id, retain),
        )
        return max(0, int(cursor.rowcount or 0))

    def prune_usage_events_to_limit(
        self,
        account_id: int | None = None,
        *,
        max_records: int = ADMIN_LEDGER_MAX_RECORDS,
    ) -> int:
        """删除超出分页保留窗口的 Token 记录，返回删除条数。"""

        retain = max(1, int(max_records))
        with self.connect() as conn:
            if account_id is not None:
                return self._prune_usage_events_for_account(
                    conn,
                    int(account_id),
                    max_records=retain,
                )
            rows = conn.execute("SELECT DISTINCT account_id FROM usage_events").fetchall()
            deleted = 0
            for row in rows:
                deleted += self._prune_usage_events_for_account(
                    conn,
                    int(row["account_id"]),
                    max_records=retain,
                )
            return deleted

    def list_usage_events(
        self,
        account_id: int | None = None,
        candidate_id: int | None = None,
        session_id: str | None = None,
        limit: int = 100,
        offset: int = 0,
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
        parameters.extend([max(1, min(limit, 100)), max(0, int(offset))])
        with self.connect() as conn:
            rows = conn.execute(
                f"""
                SELECT * FROM usage_events{where}
                ORDER BY created_at DESC, id DESC
                LIMIT ? OFFSET ?
                """,
                tuple(parameters),
            ).fetchall()
        return [usage_event_from_row(row) for row in rows]

    def count_usage_events(
        self,
        account_id: int | None = None,
        candidate_id: int | None = None,
        session_id: str | None = None,
    ) -> int:
        """返回用量流水数量，供分页 UI 展示总页数。"""

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
        with self.connect() as conn:
            row = conn.execute(
                f"SELECT COUNT(*) AS count FROM usage_events{where}",
                tuple(parameters),
            ).fetchone()
        return int(row["count"])

    def summarize_usage(
        self,
        account_id: int | None = None,
    ) -> dict[str, int]:
        """汇总 Token；`billable_tokens` 只计算标记为可计费的流水。"""

        conditions: list[str] = []
        parameters: list[object] = []
        if account_id is not None:
            conditions.append("account_id = ?")
            parameters.append(account_id)
        where = f" WHERE {' AND '.join(conditions)}" if conditions else ""
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
                tuple(parameters),
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
                """,
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
            self._prune_tool_call_traces_for_account(
                conn,
                trace.account_id,
                max_records=ADMIN_LEDGER_MAX_RECORDS,
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

    def _prune_tool_call_traces_for_account(
        self,
        conn: RepositoryConnection,
        account_id: int,
        *,
        max_records: int = ADMIN_LEDGER_MAX_RECORDS,
    ) -> int:
        """保留某个账号最近的固定页数工具调用记录。"""

        retain = max(1, int(max_records))
        cursor = conn.execute(
            """
            DELETE FROM tool_call_traces
            WHERE account_id = ?
              AND id NOT IN (
                  SELECT id
                  FROM tool_call_traces
                  WHERE account_id = ?
                  ORDER BY updated_at DESC, id DESC
                  LIMIT ?
              )
            """,
            (account_id, account_id, retain),
        )
        return max(0, int(cursor.rowcount or 0))

    def prune_tool_call_traces_to_limit(
        self,
        account_id: int | None = None,
        *,
        max_records: int = ADMIN_LEDGER_MAX_RECORDS,
    ) -> int:
        """删除超出分页保留窗口的工具调用记录，返回删除条数。"""

        retain = max(1, int(max_records))
        with self.connect() as conn:
            if account_id is not None:
                return self._prune_tool_call_traces_for_account(
                    conn,
                    int(account_id),
                    max_records=retain,
                )
            rows = conn.execute("SELECT DISTINCT account_id FROM tool_call_traces").fetchall()
            deleted = 0
            for row in rows:
                deleted += self._prune_tool_call_traces_for_account(
                    conn,
                    int(row["account_id"]),
                    max_records=retain,
                )
            return deleted

    def get_tool_call_trace(
        self,
        root_request_id: str,
        account_id: int | None = None,
    ) -> ToolCallTraceRecord:
        """读取一条工具调用审计轨迹，并可按账号隔离。"""

        conditions = ["root_request_id = ?"]
        parameters: list[object] = [root_request_id]
        if account_id is not None:
            conditions.append("account_id = ?")
            parameters.append(account_id)
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
        limit: int = 100,
        offset: int = 0,
    ) -> list[ToolCallTraceRecord]:
        """分页列出最近工具调用任务，详情由单条读取接口按需加载。"""

        conditions: list[str] = []
        parameters: list[object] = []
        if account_id is not None:
            conditions.append("account_id = ?")
            parameters.append(account_id)
        where = f" WHERE {' AND '.join(conditions)}" if conditions else ""
        parameters.extend([max(1, min(limit, 100)), max(0, int(offset))])
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
    ) -> int:
        """返回工具调用任务数量，供分页 UI 展示总数。"""

        conditions: list[str] = []
        parameters: list[object] = []
        if account_id is not None:
            conditions.append("account_id = ?")
            parameters.append(account_id)
        where = f" WHERE {' AND '.join(conditions)}" if conditions else ""
        with self.connect() as conn:
            row = conn.execute(
                f"SELECT COUNT(*) AS count FROM tool_call_traces{where}",
                tuple(parameters),
            ).fetchone()
        return int(row["count"])

    def summarize_tool_call_traces_by_account(self) -> list[dict[str, int]]:
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
                GROUP BY account_id
                ORDER BY account_id
                """
            ).fetchall()
        return [
            {
                "account_id": int(row["account_id"]),
                "trace_count": int(row["trace_count"]),
                "failed_trace_count": int(row["failed_trace_count"]),
            }
            for row in rows
        ]

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
        *,
        clear_idempotency_key: bool = False,
    ) -> BackgroundTaskRecord:
        """把任务标记为成功，并保存不含正文的结果摘要。"""

        now = now_iso()
        idempotency_assignment = "idempotency_key = NULL,\n                    " if clear_idempotency_key else ""
        with self.connect() as conn:
            cursor = conn.execute(
                f"""
                UPDATE background_tasks
                SET status = 'succeeded', progress = 100, result_json = ?,
                    {idempotency_assignment}error_summary = NULL, finished_at = ?, updated_at = ?
                WHERE task_key = ? AND status = 'running'
                """,
                (json.dumps(dict(result or {}), ensure_ascii=False), now, now, task_key),
            )
            if cursor.rowcount == 0:
                raise KeyError(f"Running background task not found: {task_key}")
        return self.get_background_task(task_key)

    def recover_stale_background_tasks(
        self,
        *,
        stale_after_seconds: int,
    ) -> list[BackgroundTaskRecord]:
        """回收 Worker 失联的 running 任务，并按剩余次数重新排队或失败。

        Celery 的 late acknowledgement 能在 Worker 丢失后重新投递消息，但数据库中的
        状态可能已经被认领为 ``running``。超过硬超时后的任务不会再被普通认领逻辑接受，
        因此由 Beat 定期依据 ``updated_at`` 做一次数据库原子回收。
        """

        if stale_after_seconds <= 0:
            raise ValueError("后台任务失联回收时间必须大于 0 秒。")
        cutoff = (
            datetime.now(UTC) - timedelta(seconds=stale_after_seconds)
        ).isoformat(timespec="seconds")
        now = now_iso()
        recovered: list[BackgroundTaskRecord] = []
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM background_tasks
                WHERE status = 'running' AND updated_at < ?
                ORDER BY updated_at ASC, id ASC
                """,
                (cutoff,),
            ).fetchall()
            for row in rows:
                task_key = str(row["task_key"])
                attempt = int(row["attempt"] or 0)
                max_attempts = int(row["max_attempts"] or 1)
                if attempt >= max_attempts:
                    cursor = conn.execute(
                        """
                        UPDATE background_tasks
                        SET status = 'failed',
                            error_summary = ?,
                            finished_at = ?,
                            updated_at = ?
                        WHERE task_key = ? AND status = 'running'
                        """,
                        (
                            "Worker 执行超时或进程失联，已达到最大重试次数。",
                            now,
                            now,
                            task_key,
                        ),
                    )
                else:
                    cursor = conn.execute(
                        """
                        UPDATE background_tasks
                        SET status = 'queued',
                            progress = 0,
                            error_summary = ?,
                            started_at = NULL,
                            finished_at = NULL,
                            updated_at = ?
                        WHERE task_key = ? AND status = 'running'
                        """,
                        (
                            "Worker 执行超时或进程失联，任务已重新排队。",
                            now,
                            task_key,
                        ),
                    )
                # 多个 Beat 可能同时运行；只有真正把状态从 running 改掉的实例
                # 才拥有后续投递权，避免同一失联任务被重复放回队列。
                if cursor.rowcount != 1:
                    continue
                refreshed = conn.execute(
                    "SELECT * FROM background_tasks WHERE task_key = ?",
                    (task_key,),
                ).fetchone()
                if refreshed is not None:
                    recovered.append(background_task_from_row(refreshed))
        return recovered

    def release_background_task_idempotency(self, task_key: str) -> None:
        """释放已结束任务的幂等键，但保留任务记录供审计和排查。"""

        with self.connect() as conn:
            conn.execute(
                """
                UPDATE background_tasks
                SET idempotency_key = NULL, updated_at = ?
                WHERE task_key = ? AND status IN ('succeeded', 'cancelled')
                """,
                (now_iso(), task_key),
            )

    def requeue_background_task(self, task_key: str, error_summary: str | None = None) -> BackgroundTaskRecord:
        """把本轮可重试的任务放回 queued 状态，保留尝试次数和错误摘要。"""

        with self.connect() as conn:
            cursor = conn.execute(
                """
                UPDATE background_tasks
                SET status = 'queued', error_summary = ?,
                    started_at = NULL, finished_at = NULL, updated_at = ?
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
        updated_profile, updated_fields = apply_candidate_profile_patch(current, patch)

        if not updated_fields:
            return []
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
                    updated_profile.status,
                    updated_profile.education,
                    updated_profile.experience_years,
                    updated_profile.salary_floor_k,
                    updated_profile.expected_salary_k,
                    json.dumps(updated_profile.skills, ensure_ascii=False),
                    json.dumps(updated_profile.preferred_cities, ensure_ascii=False),
                    json.dumps(updated_profile.acceptable_cities, ensure_ascii=False),
                    json.dumps(updated_profile.preference_weights, ensure_ascii=False),
                    json.dumps(updated_profile.target_directions, ensure_ascii=False),
                    json.dumps(updated_profile.unacceptable, ensure_ascii=False),
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
            archive_owner_clause = ""
            if account_id is not None:
                archive_owner_clause = " AND import.account_id = ?"
            archive_rows = conn.execute(
                f"""
                SELECT version.storage_key
                FROM project_archive_imports AS import
                JOIN knowledge_asset_versions AS version
                  ON version.id = import.knowledge_asset_version_id
                WHERE import.candidate_id = ?{archive_owner_clause}
                """,
                (candidate_id, *owner_parameters),
            ).fetchall()
            collection_owner_clause = ""
            if account_id is not None:
                collection_owner_clause = " AND session.account_id = ?"
            collection_rows = conn.execute(
                f"""
                SELECT file.storage_key
                FROM project_collection_files AS file
                JOIN project_collection_sessions AS session ON session.id = file.collection_id
                WHERE session.candidate_id = ?{collection_owner_clause}
                  AND file.storage_key IS NOT NULL
                """,
                (candidate_id, *owner_parameters),
            ).fetchall()
            visual_rows = conn.execute(
                f"""
                SELECT storage_key
                FROM visual_knowledge_items
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
            "storage_keys": [
                str(row["storage_key"])
                for row in [*artifact_rows, *archive_rows, *collection_rows, *visual_rows]
            ],
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
        """删除项目卡片及其全部来源版本、长文本、视觉对象和派生索引。"""

        existing = self.get_project_card(record_id, account_id=account_id)
        owner_clause = ""
        owner_parameters: tuple[object, ...] = ()
        if account_id is not None:
            owner_clause = " AND account_id = ?"
            owner_parameters = (account_id,)
        archive_owner_clause = ""
        if account_id is not None:
            archive_owner_clause = " AND import.account_id = ?"
        collection_owner_clause = archive_owner_clause.replace("import.", "session.")

        with self.connect() as conn:
            archive_rows = conn.execute(
                f"""
                SELECT import.knowledge_asset_id, version.storage_key
                FROM project_archive_imports AS import
                JOIN knowledge_asset_versions AS version
                  ON version.id = import.knowledge_asset_version_id
                WHERE import.project_card_id = ?{archive_owner_clause}
                """,
                (record_id, *owner_parameters),
            ).fetchall()
            archive_evidence_rows = conn.execute(
                f"""
                SELECT text.id
                FROM long_texts AS text
                JOIN project_archive_files AS file
                  ON text.entity_type = 'project_archive_file' AND text.entity_id = file.id
                JOIN project_archive_imports AS import ON import.id = file.project_archive_id
                WHERE import.project_card_id = ?{archive_owner_clause}
                """,
                (record_id, *owner_parameters),
            ).fetchall()
            archive_visual_rows = conn.execute(
                f"""
                SELECT visual.storage_key
                FROM visual_knowledge_items AS visual
                JOIN project_archive_files AS file ON file.id = visual.project_archive_file_id
                JOIN project_archive_imports AS import ON import.id = file.project_archive_id
                WHERE import.project_card_id = ?{archive_owner_clause}
                """,
                (record_id, *owner_parameters),
            ).fetchall()
            collection_rows = conn.execute(
                f"""
                SELECT session.id
                FROM project_collection_sessions AS session
                WHERE session.project_card_id = ?{collection_owner_clause}
                """,
                (record_id, *owner_parameters),
            ).fetchall()
            collection_file_rows = conn.execute(
                f"""
                SELECT file.long_text_id, file.storage_key, file.knowledge_asset_id
                FROM project_collection_files AS file
                JOIN project_collection_sessions AS session ON session.id = file.collection_id
                WHERE session.project_card_id = ?{collection_owner_clause}
                """,
                (record_id, *owner_parameters),
            ).fetchall()
            collection_visual_rows = conn.execute(
                f"""
                SELECT visual.storage_key
                FROM visual_knowledge_items AS visual
                JOIN project_collection_files AS file ON file.id = visual.project_collection_file_id
                JOIN project_collection_sessions AS session ON session.id = file.collection_id
                WHERE session.project_card_id = ?{collection_owner_clause}
                """,
                (record_id, *owner_parameters),
            ).fetchall()
            card_long_text_rows = conn.execute(
                f"""
                SELECT id FROM long_texts
                WHERE entity_type = 'project_experience_card' AND entity_id = ?{owner_clause}
                """,
                (record_id, *owner_parameters),
            ).fetchall()
            long_text_ids = {
                int(row["id"])
                for row in [*card_long_text_rows, *archive_evidence_rows]
            }
            long_text_ids.update(
                int(row["long_text_id"])
                for row in collection_file_rows
                if row["long_text_id"] is not None
            )
            if long_text_ids:
                conn.execute(
                    f"DELETE FROM long_texts WHERE id IN ({', '.join('?' for _ in long_text_ids)})",
                    tuple(sorted(long_text_ids)),
                )
            for archive_row in archive_rows:
                conn.execute(
                    "DELETE FROM knowledge_assets WHERE id = ?",
                    (int(archive_row["knowledge_asset_id"]),),
                )
            for collection_row in collection_rows:
                conn.execute(
                    "DELETE FROM project_collection_sessions WHERE id = ?",
                    (int(collection_row["id"]),),
                )
            for row in collection_file_rows:
                if row["knowledge_asset_id"] is not None:
                    conn.execute(
                        "DELETE FROM knowledge_assets WHERE id = ?",
                        (int(row["knowledge_asset_id"]),),
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
            "long_text_ids": sorted(long_text_ids),
            "storage_keys": [
                *[str(row["storage_key"]) for row in archive_rows],
                *[str(row["storage_key"]) for row in archive_visual_rows],
                *[
                    str(row["storage_key"])
                    for row in collection_file_rows
                    if row["storage_key"] is not None
                ],
                *[str(row["storage_key"]) for row in collection_visual_rows],
            ],
        }

    def register_project_archive_import(
        self,
        *,
        account_id: int,
        candidate_id: int,
        original_filename: str,
        storage_key: str,
        file_size: int,
        sha256: str,
        scan_engine: str | None,
        source_type: str = "uploaded_project_archive",
        source_url: str | None = None,
        source_ref: str | None = None,
    ) -> ProjectArchiveImportRecord:
        """原子登记项目 ZIP 的知识资产、首个版本和项目导入业务记录。"""

        self.get_candidate_profile(candidate_id, account_id=account_id)
        normalized_sha = str(sha256 or "").strip().lower()
        if len(normalized_sha) != 64 or any(char not in "0123456789abcdef" for char in normalized_sha):
            raise ValueError("项目 ZIP 摘要格式无效。")
        if self.find_project_archive_import_by_fingerprint(
            account_id=account_id,
            candidate_id=candidate_id,
            content_fingerprint=normalized_sha,
        ) is not None:
            raise DuplicateResourceError("项目压缩包")

        created_at = now_iso()
        try:
            with self.connect() as conn:
                asset_cursor = conn.execute(
                    """
                    INSERT INTO knowledge_assets (
                        account_id, candidate_id, asset_kind, title, lifecycle_status,
                        metadata_json, created_at, updated_at
                    ) VALUES (?, ?, 'project_archive', ?, 'active', ?, ?, ?)
                    """,
                    (
                        account_id,
                        candidate_id,
                        str(original_filename)[:512],
                        json.dumps({"business_source": "project_archive_imports"}, ensure_ascii=False),
                        created_at,
                        created_at,
                    ),
                )
                asset_id = int(asset_cursor.lastrowid)
                version_id = self._insert_knowledge_asset_version(
                    conn,
                    asset_id=asset_id,
                    version_number=1,
                    original_filename=original_filename,
                    storage_key=storage_key,
                    media_type="application/zip",
                    file_size=file_size,
                    sha256=normalized_sha,
                    source_kind="upload",
                    source_url=source_url,
                    processing_status="uploaded",
                    scan_status="clean",
                    scan_engine=scan_engine,
                    scan_reason=None,
                    revision_label=source_ref,
                    metadata={"project_source_type": source_type},
                    created_at=created_at,
                )
                import_cursor = conn.execute(
                    """
                    INSERT INTO project_archive_imports (
                        account_id, candidate_id, knowledge_asset_id,
                        knowledge_asset_version_id, project_card_id, source_type,
                        source_url, source_ref, original_filename, content_fingerprint,
                        status, error_summary, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, 'uploaded', NULL, ?, ?)
                    """,
                    (
                        account_id,
                        candidate_id,
                        asset_id,
                        version_id,
                        source_type,
                        source_url,
                        source_ref,
                        str(original_filename)[:512],
                        normalized_sha,
                        created_at,
                        created_at,
                    ),
                )
                import_id = int(import_cursor.lastrowid)
        except Exception as error:
            if is_unique_constraint_violation(error, "uq_project_archive_candidate_content"):
                raise DuplicateResourceError("项目压缩包") from error
            raise
        return self.get_project_archive_import(import_id, account_id=account_id)

    def get_project_archive_import(
        self,
        import_id: int,
        *,
        account_id: int,
    ) -> ProjectArchiveImportRecord:
        """读取当前账号的一次项目整包导入。"""

        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM project_archive_imports WHERE id = ? AND account_id = ?",
                (import_id, account_id),
            ).fetchone()
        if row is None:
            raise KeyError(f"Project archive import not found: {import_id}")
        return self._project_archive_import_from_row(row)

    def find_project_archive_import_by_fingerprint(
        self,
        *,
        account_id: int,
        candidate_id: int,
        content_fingerprint: str,
    ) -> ProjectArchiveImportRecord | None:
        """按候选人和 ZIP 内容摘要查找重复项目包。"""

        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM project_archive_imports
                WHERE account_id = ? AND candidate_id = ? AND content_fingerprint = ?
                """,
                (account_id, candidate_id, str(content_fingerprint).lower()),
            ).fetchone()
        return self._project_archive_import_from_row(row) if row is not None else None

    def find_project_archive_import_by_project_card(
        self,
        project_card_id: int,
        *,
        account_id: int,
    ) -> ProjectArchiveImportRecord | None:
        """Find the persisted source archive behind one generated project card."""

        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM project_archive_imports
                WHERE project_card_id = ? AND account_id = ?
                ORDER BY id DESC
                LIMIT 1
                """,
                (project_card_id, account_id),
            ).fetchone()
        return self._project_archive_import_from_row(row) if row is not None else None

    def mark_project_archive_import(
        self,
        import_id: int,
        *,
        account_id: int,
        status: str,
        error_summary: str | None = None,
    ) -> ProjectArchiveImportRecord:
        """同步更新项目导入和知识资产版本的处理状态。"""

        if status not in PROJECT_ARCHIVE_IMPORT_STATUSES:
            raise ValueError(f"不支持的项目导入状态：{status}")
        existing = self.get_project_archive_import(import_id, account_id=account_id)
        updated_at = now_iso()
        with self.connect() as conn:
            cursor = conn.execute(
                """
                UPDATE project_archive_imports
                SET status = ?, error_summary = ?, updated_at = ?
                WHERE id = ? AND account_id = ?
                """,
                (
                    status,
                    str(error_summary)[:2000] if error_summary else None,
                    updated_at,
                    import_id,
                    account_id,
                ),
            )
            if cursor.rowcount == 0:
                raise KeyError(f"Project archive import not found: {import_id}")
            conn.execute(
                "UPDATE knowledge_asset_versions SET processing_status = ? WHERE id = ?",
                (status, existing.knowledge_asset_version_id),
            )
            conn.execute(
                "UPDATE knowledge_assets SET updated_at = ? WHERE id = ?",
                (updated_at, existing.knowledge_asset_id),
            )
        return self.get_project_archive_import(import_id, account_id=account_id)

    def complete_project_archive_import(
        self,
        import_id: int,
        *,
        account_id: int,
        project_card_id: int,
        files: list[Mapping[str, object]],
        evidence: list[Mapping[str, object]] | None = None,
        visual_items: list[Mapping[str, object]] | None = None,
    ) -> ProjectArchiveImportRecord:
        """原子保存文件清单、文本证据、视觉资产和待确认项目卡片关联。"""

        existing = self.get_project_archive_import(import_id, account_id=account_id)
        card = self.get_project_card(project_card_id, account_id=account_id)
        if card.candidate_id != existing.candidate_id:
            raise ValueError("项目卡片与项目 ZIP 不属于同一候选人。")
        evidence_by_path = {
            str(item["relative_path"]): item
            for item in (evidence or [])
        }
        updated_at = now_iso()
        with self.connect() as conn:
            conn.execute(
                """
                DELETE FROM long_texts
                WHERE entity_type = 'project_archive_file'
                  AND entity_id IN (
                    SELECT id FROM project_archive_files WHERE project_archive_id = ?
                  )
                """,
                (import_id,),
            )
            conn.execute(
                "DELETE FROM project_archive_files WHERE project_archive_id = ?",
                (import_id,),
            )
            source_rows: dict[str, tuple[int, int | None]] = {}
            for item in files:
                metadata = dict(item.get("metadata") or {})
                relative_path = str(item["relative_path"])
                evidence_item = evidence_by_path.get(relative_path)
                cursor = conn.execute(
                    """
                    INSERT INTO project_archive_files (
                        project_archive_id, relative_path, file_kind, media_type,
                        file_size, compressed_size, sha256, analysis_status,
                        skip_reason, long_text_id, extraction_method, text_length,
                        metadata_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?)
                    """,
                    (
                        import_id,
                        str(item["relative_path"])[:2048],
                        str(item["file_kind"])[:64],
                        str(item["media_type"])[:128],
                        int(item["file_size"]),
                        int(item["compressed_size"]),
                        str(item["sha256"]) if item.get("sha256") else None,
                        str(item["analysis_status"])[:32],
                        str(item["skip_reason"])[:128] if item.get("skip_reason") else None,
                        (
                            str(evidence_item.get("extraction_method"))[:64]
                            if evidence_item is not None
                            else None
                        ),
                        len(str(evidence_item.get("text") or "")) if evidence_item else 0,
                        json.dumps(metadata, ensure_ascii=False),
                    ),
                )
                file_id = int(cursor.lastrowid)
                long_text_id: int | None = None
                if evidence_item is not None and str(evidence_item.get("text") or "").strip():
                    long_text_id = self._add_long_text(
                        conn,
                        "project_archive_file",
                        file_id,
                        str(item["relative_path"])[:256],
                        str(evidence_item["text"]),
                        account_id=account_id,
                        candidate_id=existing.candidate_id,
                    )
                    conn.execute(
                        "UPDATE project_archive_files SET long_text_id = ? WHERE id = ?",
                        (long_text_id, file_id),
                    )
                source_rows[relative_path] = (file_id, long_text_id)
            for visual_item in visual_items or []:
                relative_path = str(visual_item.get("relative_path") or "")
                source_row = source_rows.get(relative_path)
                if source_row is None:
                    raise ValueError("视觉知识项引用了不存在的项目文件。")
                self._insert_visual_knowledge_item(
                    conn,
                    visual_item,
                    account_id=account_id,
                    candidate_id=existing.candidate_id,
                    project_archive_file_id=source_row[0],
                    long_text_id=source_row[1],
                    updated_at=updated_at,
                )
            cursor = conn.execute(
                """
                UPDATE project_archive_imports
                SET project_card_id = ?, status = 'ready', error_summary = NULL, updated_at = ?
                WHERE id = ? AND account_id = ?
                """,
                (project_card_id, updated_at, import_id, account_id),
            )
            if cursor.rowcount == 0:
                raise KeyError(f"Project archive import not found: {import_id}")
            conn.execute(
                "UPDATE knowledge_asset_versions SET processing_status = 'ready' WHERE id = ?",
                (existing.knowledge_asset_version_id,),
            )
            conn.execute(
                "UPDATE knowledge_assets SET updated_at = ? WHERE id = ?",
                (updated_at, existing.knowledge_asset_id),
            )
        return self.get_project_archive_import(import_id, account_id=account_id)

    def list_project_archive_files(
        self,
        import_id: int,
        *,
        account_id: int,
    ) -> list[ProjectArchiveFileRecord]:
        """列出当前账号项目 ZIP 的文件路由清单。"""

        self.get_project_archive_import(import_id, account_id=account_id)
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM project_archive_files
                WHERE project_archive_id = ?
                ORDER BY id
                """,
                (import_id,),
            ).fetchall()
        return [self._project_archive_file_from_row(row) for row in rows]

    def create_project_collection(
        self,
        *,
        account_id: int,
        candidate_id: int,
        project_name: str,
        manifest_fingerprint: str,
        files: list[Mapping[str, object]],
        source_type: str = "local_directory_collection",
    ) -> ProjectCollectionSessionRecord:
        """Persist a backend-approved local directory manifest as one collection session."""

        self.get_candidate_profile(candidate_id, account_id=account_id)
        normalized_name = str(project_name or "").strip()[:256]
        if not normalized_name:
            raise ValueError("项目名称不能为空。")
        normalized_fingerprint = str(manifest_fingerprint or "").strip().lower()
        if len(normalized_fingerprint) != 64:
            raise ValueError("项目清单摘要无效。")
        existing = self.find_project_collection_by_manifest(
            account_id=account_id,
            candidate_id=candidate_id,
            manifest_fingerprint=normalized_fingerprint,
        )
        if existing is not None:
            return self.resume_project_collection(
                existing.id,
                account_id=account_id,
                project_name=normalized_name,
            )
        selected = [item for item in files if item.get("selection_status") == "selected"]
        created_at = now_iso()
        try:
            with self.connect() as conn:
                cursor = conn.execute(
                    """
                    INSERT INTO project_collection_sessions (
                        account_id, candidate_id, project_card_id, project_name,
                        source_type, manifest_fingerprint, preserve_originals, status,
                        file_count, selected_file_count, uploaded_file_count,
                        total_size, selected_size, error_summary, created_at, updated_at
                    ) VALUES (?, ?, NULL, ?, ?, ?, ?, 'planned', ?, ?, 0, ?, ?, NULL, ?, ?)
                    """,
                    (
                        account_id,
                        candidate_id,
                        normalized_name,
                        str(source_type)[:64],
                        normalized_fingerprint,
                        False,
                        len(files),
                        len(selected),
                        sum(int(item["file_size"]) for item in files),
                        sum(int(item["file_size"]) for item in selected),
                        created_at,
                        created_at,
                    ),
                )
                collection_id = int(cursor.lastrowid)
                for item in files:
                    conn.execute(
                        """
                        INSERT INTO project_collection_files (
                            collection_id, relative_path, file_kind, media_type,
                            file_size, client_sha256, server_sha256, selection_status,
                            selection_reason, extraction_method, text_length, long_text_id,
                            storage_key, knowledge_asset_id, knowledge_asset_version_id,
                            metadata_json, created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, NULL, ?, ?, NULL, 0, NULL,
                                  NULL, NULL, NULL, ?, ?, ?)
                        """,
                        (
                            collection_id,
                            str(item["relative_path"])[:2048],
                            str(item["file_kind"])[:64],
                            str(item["media_type"])[:128],
                            int(item["file_size"]),
                            str(item["sha256"]) if item.get("sha256") else None,
                            str(item["selection_status"])[:32],
                            str(item["selection_reason"])[:256],
                            json.dumps(dict(item.get("metadata") or {}), ensure_ascii=False),
                            created_at,
                            created_at,
                        ),
                    )
        except Exception as error:
            if is_unique_constraint_violation(error, "uq_project_collection_candidate_manifest"):
                # 两个浏览器请求可能并发创建同一份清单。唯一约束胜出后复用事实记录，
                # 不让网络中断把用户永久锁死在“项目已存在”。
                existing = self.find_project_collection_by_manifest(
                    account_id=account_id,
                    candidate_id=candidate_id,
                    manifest_fingerprint=normalized_fingerprint,
                )
                if existing is not None:
                    return self.resume_project_collection(
                        existing.id,
                        account_id=account_id,
                        project_name=normalized_name,
                    )
                raise DuplicateResourceError("本地项目") from error
            raise
        return self.get_project_collection(collection_id, account_id=account_id)

    def find_project_collection_by_manifest(
        self,
        *,
        account_id: int,
        candidate_id: int,
        manifest_fingerprint: str,
    ) -> ProjectCollectionSessionRecord | None:
        """查找同一候选人的既有目录清单，供中断恢复而不是盲目重复创建。"""

        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM project_collection_sessions
                WHERE account_id = ? AND candidate_id = ? AND manifest_fingerprint = ?
                """,
                (account_id, candidate_id, str(manifest_fingerprint).strip().lower()),
            ).fetchone()
        return self._project_collection_session_from_row(row) if row is not None else None

    def resume_project_collection(
        self,
        collection_id: int,
        *,
        account_id: int,
        project_name: str | None = None,
    ) -> ProjectCollectionSessionRecord:
        """恢复未完成采集；已分析文件保持幂等，失败文件回到待上传状态。"""

        existing = self.get_project_collection(collection_id, account_id=account_id)
        if existing.project_card_id is not None or existing.status == "ready":
            raise DuplicateResourceError("本地项目")
        normalized_name = str(project_name or existing.project_name).strip()[:256]
        if not normalized_name:
            normalized_name = existing.project_name
        updated_at = now_iso()
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE project_collection_files
                SET selection_status = 'selected',
                    selection_reason = 'supported_and_within_policy',
                    updated_at = ?
                WHERE collection_id = ? AND selection_status = 'failed'
                """,
                (updated_at, collection_id),
            )
            conn.execute(
                """
                UPDATE project_collection_sessions
                SET project_name = ?, status = 'planned', error_summary = NULL,
                    uploaded_file_count = (
                        SELECT COUNT(*) FROM project_collection_files
                        WHERE collection_id = ? AND selection_status = 'analyzed'
                    ),
                    updated_at = ?
                WHERE id = ? AND account_id = ?
                """,
                (normalized_name, collection_id, updated_at, collection_id, account_id),
            )
        return self.get_project_collection(collection_id, account_id=account_id)

    def get_project_collection(
        self,
        collection_id: int,
        *,
        account_id: int,
    ) -> ProjectCollectionSessionRecord:
        """Read one account-scoped local project collection session."""

        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM project_collection_sessions WHERE id = ? AND account_id = ?",
                (collection_id, account_id),
            ).fetchone()
        if row is None:
            raise KeyError(f"Project collection not found: {collection_id}")
        return self._project_collection_session_from_row(row)

    def list_project_collection_files(
        self,
        collection_id: int,
        *,
        account_id: int,
    ) -> list[ProjectCollectionFileRecord]:
        """List the complete collection plan without exposing file contents."""

        self.get_project_collection(collection_id, account_id=account_id)
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM project_collection_files
                WHERE collection_id = ?
                ORDER BY id
                """,
                (collection_id,),
            ).fetchall()
        return [self._project_collection_file_from_row(row) for row in rows]

    def get_project_collection_file(
        self,
        file_id: int,
        *,
        collection_id: int,
        account_id: int,
    ) -> ProjectCollectionFileRecord:
        """Read one selected file only when its collection belongs to the account."""

        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT file.*
                FROM project_collection_files AS file
                JOIN project_collection_sessions AS session ON session.id = file.collection_id
                WHERE file.id = ? AND file.collection_id = ? AND session.account_id = ?
                """,
                (file_id, collection_id, account_id),
            ).fetchone()
        if row is None:
            raise KeyError(f"Project collection file not found: {file_id}")
        return self._project_collection_file_from_row(row)

    def complete_project_collection_file(
        self,
        file_id: int,
        *,
        collection_id: int,
        account_id: int,
        server_sha256: str,
        extraction_method: str,
        extracted_text: str,
        metadata: Mapping[str, object] | None = None,
        visual_items: list[Mapping[str, object]] | None = None,
    ) -> ProjectCollectionFileRecord:
        """原子保存文本证据和安全视觉副本，不保留本地源文件。"""

        session = self.get_project_collection(collection_id, account_id=account_id)
        existing = self.get_project_collection_file(
            file_id,
            collection_id=collection_id,
            account_id=account_id,
        )
        if existing.selection_status == "analyzed":
            return existing
        if existing.selection_status != "selected":
            raise ValueError("当前文件不在后端采集计划中。")
        normalized_text = str(extracted_text or "").strip()
        if not normalized_text:
            raise ValueError("项目文件没有可保存的提取文字。")
        digest = str(server_sha256 or "").strip().lower()
        if digest != existing.client_sha256:
            raise ValueError("项目文件内容与预扫描清单不一致，请重新选择目录。")

        updated_at = now_iso()
        with self.connect() as conn:
            long_text_id = self._add_long_text(
                conn,
                "project_collection_file",
                existing.id,
                existing.relative_path[:256],
                normalized_text,
                account_id=account_id,
                candidate_id=session.candidate_id,
            )
            cursor = conn.execute(
                """
                UPDATE project_collection_files
                SET server_sha256 = ?, selection_status = 'analyzed',
                    extraction_method = ?, text_length = ?, long_text_id = ?,
                    metadata_json = ?, updated_at = ?
                WHERE id = ? AND collection_id = ? AND selection_status = 'selected'
                """,
                (
                    digest,
                    str(extraction_method)[:64],
                    len(normalized_text),
                    long_text_id,
                    json.dumps(dict(metadata or {}), ensure_ascii=False),
                    updated_at,
                    file_id,
                    collection_id,
                ),
            )
            if cursor.rowcount == 0:
                raise ValueError("项目文件已被其他请求处理，请刷新采集计划。")
            for visual_item in visual_items or []:
                self._insert_visual_knowledge_item(
                    conn,
                    visual_item,
                    account_id=account_id,
                    candidate_id=session.candidate_id,
                    project_collection_file_id=existing.id,
                    long_text_id=long_text_id,
                    updated_at=updated_at,
                )
            conn.execute(
                """
                UPDATE project_collection_sessions
                SET status = 'uploading',
                    uploaded_file_count = (
                        SELECT COUNT(*) FROM project_collection_files
                        WHERE collection_id = ? AND selection_status = 'analyzed'
                    ),
                    updated_at = ?
                WHERE id = ? AND account_id = ?
                """,
                (collection_id, updated_at, collection_id, account_id),
            )
        return self.get_project_collection_file(
            file_id,
            collection_id=collection_id,
            account_id=account_id,
        )

    def list_visual_knowledge_items(
        self,
        *,
        account_id: int,
        item_ids: list[int] | None = None,
        candidate_id: int | None = None,
        index_status: str | None = None,
        project_archive_file_ids: list[int] | None = None,
        project_collection_file_ids: list[int] | None = None,
    ) -> list[VisualKnowledgeItemRecord]:
        """按账号读取视觉知识项；所有可选过滤都在数据库执行。"""

        conditions = ["account_id = ?"]
        parameters: list[object] = [account_id]
        if item_ids is not None:
            normalized_ids = sorted({int(item_id) for item_id in item_ids if int(item_id) > 0})
            if not normalized_ids:
                return []
            conditions.append(f"id IN ({', '.join('?' for _ in normalized_ids)})")
            parameters.extend(normalized_ids)
        if candidate_id is not None:
            conditions.append("candidate_id = ?")
            parameters.append(candidate_id)
        if index_status is not None:
            if index_status not in {"pending", "indexed", "failed"}:
                raise ValueError("视觉知识索引状态无效。")
            conditions.append("index_status = ?")
            parameters.append(index_status)
        for column_name, raw_ids in (
            ("project_archive_file_id", project_archive_file_ids),
            ("project_collection_file_id", project_collection_file_ids),
        ):
            if raw_ids is None:
                continue
            normalized_source_ids = sorted(
                {int(item_id) for item_id in raw_ids if int(item_id) > 0}
            )
            if not normalized_source_ids:
                return []
            conditions.append(
                f"{column_name} IN ({', '.join('?' for _ in normalized_source_ids)})"
            )
            parameters.extend(normalized_source_ids)
        with self.connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM visual_knowledge_items WHERE {' AND '.join(conditions)} ORDER BY id",
                tuple(parameters),
            ).fetchall()
        return [self._visual_knowledge_item_from_row(row) for row in rows]

    def _insert_visual_knowledge_item(
        self,
        conn: Any,
        item: Mapping[str, object],
        *,
        account_id: int,
        candidate_id: int,
        project_archive_file_id: int | None = None,
        project_collection_file_id: int | None = None,
        long_text_id: int | None,
        updated_at: str,
    ) -> int:
        """校验并插入一个只关联单一来源文件的视觉知识项。"""

        if (project_archive_file_id is None) == (project_collection_file_id is None):
            raise ValueError("视觉知识项必须且只能关联一个项目文件。")
        source_id = str(item.get("source_id") or "").strip()
        source_label = str(item.get("source_label") or "").strip()
        storage_key = str(item.get("storage_key") or "").strip()
        digest = str(item.get("sha256") or "").strip().lower()
        if not source_id or not source_label or not storage_key:
            raise ValueError("视觉知识项缺少来源或对象存储定位。")
        if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
            raise ValueError("视觉知识项摘要无效。")
        page_number_raw = item.get("page_number")
        page_number = int(page_number_raw) if page_number_raw is not None else None
        cursor = conn.execute(
            """
            INSERT INTO visual_knowledge_items (
                account_id, candidate_id, project_archive_file_id,
                project_collection_file_id, long_text_id, source_id, source_label,
                page_number, media_type, storage_key, file_size, sha256, width, height,
                metadata_json, embedding, embedding_model, embedding_dimensions,
                index_status, index_error_type, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, NULL,
                      'pending', NULL, ?, ?)
            """,
            (
                account_id,
                candidate_id,
                project_archive_file_id,
                project_collection_file_id,
                long_text_id,
                source_id[:128],
                source_label[:2048],
                page_number,
                str(item.get("media_type") or "image/png")[:128],
                storage_key[:1024],
                int(item.get("file_size") or 0),
                digest,
                int(item.get("width") or 0),
                int(item.get("height") or 0),
                json.dumps(dict(item.get("metadata") or {}), ensure_ascii=False),
                updated_at,
                updated_at,
            ),
        )
        return int(cursor.lastrowid)

    def fail_project_collection_file(
        self,
        file_id: int,
        *,
        collection_id: int,
        account_id: int,
        reason: str,
    ) -> ProjectCollectionFileRecord:
        """Record a parser failure without losing the rest of the collection plan."""

        self.get_project_collection(collection_id, account_id=account_id)
        updated_at = now_iso()
        with self.connect() as conn:
            cursor = conn.execute(
                """
                UPDATE project_collection_files
                SET selection_status = 'failed', selection_reason = ?, updated_at = ?
                WHERE id = ? AND collection_id = ? AND selection_status = 'selected'
                """,
                (str(reason)[:256], updated_at, file_id, collection_id),
            )
            if cursor.rowcount == 0:
                return self.get_project_collection_file(
                    file_id,
                    collection_id=collection_id,
                    account_id=account_id,
                )
        return self.get_project_collection_file(
            file_id,
            collection_id=collection_id,
            account_id=account_id,
        )

    def complete_project_collection(
        self,
        collection_id: int,
        *,
        account_id: int,
        project_card_id: int,
    ) -> ProjectCollectionSessionRecord:
        """Attach the aggregate project card after every selected file was handled."""

        session = self.get_project_collection(collection_id, account_id=account_id)
        card = self.get_project_card(project_card_id, account_id=account_id)
        if card.candidate_id != session.candidate_id:
            raise ValueError("项目卡片与本地采集会话不属于同一候选人。")
        with self.connect() as conn:
            pending = conn.execute(
                """
                SELECT COUNT(*) AS count
                FROM project_collection_files
                WHERE collection_id = ? AND selection_status = 'selected'
                """,
                (collection_id,),
            ).fetchone()
            if pending is not None and int(pending["count"]) > 0:
                raise ValueError("仍有后端选中的项目文件尚未上传。")
            analyzed = conn.execute(
                """
                SELECT COUNT(*) AS count
                FROM project_collection_files
                WHERE collection_id = ? AND selection_status = 'analyzed'
                """,
                (collection_id,),
            ).fetchone()
            if analyzed is None or int(analyzed["count"]) == 0:
                raise ValueError("没有项目文件成功完成分析。")
            conn.execute(
                """
                UPDATE project_collection_sessions
                SET project_card_id = ?, status = 'ready', error_summary = NULL,
                    uploaded_file_count = ?, updated_at = ?
                WHERE id = ? AND account_id = ?
                """,
                (
                    project_card_id,
                    int(analyzed["count"]),
                    now_iso(),
                    collection_id,
                    account_id,
                ),
            )
        return self.get_project_collection(collection_id, account_id=account_id)

    def delete_incomplete_project_collection(
        self,
        collection_id: int,
        *,
        account_id: int,
    ) -> dict[str, object]:
        """删除尚未生成项目卡片的采集会话及其文本、视觉对象和向量来源。"""

        session = self.get_project_collection(collection_id, account_id=account_id)
        if session.project_card_id is not None:
            raise ValueError("已生成项目卡片的采集会话必须通过项目删除功能清理。")
        with self.connect() as conn:
            task_session_id = f"local-project-collection-{collection_id}"
            running_task = conn.execute(
                """
                SELECT task_key FROM background_tasks
                WHERE account_id = ? AND session_id = ? AND status = 'running'
                LIMIT 1
                """,
                (account_id, task_session_id),
            ).fetchone()
            if running_task is not None:
                raise ValueError("项目索引任务正在完成，请稍后再取消本次采集。")
            cancelled_at = now_iso()
            conn.execute(
                """
                UPDATE background_tasks
                SET status = 'cancelled', finished_at = ?, updated_at = ?
                WHERE account_id = ? AND session_id = ? AND status = 'queued'
                """,
                (cancelled_at, cancelled_at, account_id, task_session_id),
            )
            file_rows = conn.execute(
                """
                SELECT long_text_id, storage_key, knowledge_asset_id
                FROM project_collection_files
                WHERE collection_id = ?
                """,
                (collection_id,),
            ).fetchall()
            visual_rows = conn.execute(
                """
                SELECT visual.storage_key
                FROM visual_knowledge_items AS visual
                JOIN project_collection_files AS file
                  ON file.id = visual.project_collection_file_id
                WHERE file.collection_id = ?
                """,
                (collection_id,),
            ).fetchall()
            long_text_ids = sorted(
                int(row["long_text_id"])
                for row in file_rows
                if row["long_text_id"] is not None
            )
            if long_text_ids:
                conn.execute(
                    f"DELETE FROM long_texts WHERE id IN ({', '.join('?' for _ in long_text_ids)})",
                    tuple(long_text_ids),
                )
            cursor = conn.execute(
                "DELETE FROM project_collection_sessions WHERE id = ? AND account_id = ?",
                (collection_id, account_id),
            )
            if cursor.rowcount != 1:
                raise KeyError(f"Project collection not found: {collection_id}")
            for row in file_rows:
                if row["knowledge_asset_id"] is not None:
                    conn.execute(
                        "DELETE FROM knowledge_assets WHERE id = ?",
                        (int(row["knowledge_asset_id"]),),
                    )
        return {
            "collection_id": collection_id,
            "candidate_id": session.candidate_id,
            "long_text_ids": long_text_ids,
            "storage_keys": [
                *[
                    str(row["storage_key"])
                    for row in file_rows
                    if row["storage_key"] is not None
                ],
                *[str(row["storage_key"]) for row in visual_rows],
            ],
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
        generation_key: str | None = None,
    ) -> ResumeDraftRecord:
        """保存一个职位定制简历草稿版本。

        草稿版本单独保存，不会更新候选人档案，也不会覆盖历史版本。
        """

        if generation_key:
            with self.connect() as conn:
                if account_id is None:
                    existing = conn.execute(
                        "SELECT * FROM resume_drafts WHERE generation_key = ?",
                        (generation_key,),
                    ).fetchone()
                else:
                    existing = conn.execute(
                        """
                        SELECT * FROM resume_drafts
                        WHERE generation_key = ? AND account_id = ?
                        """,
                        (generation_key, account_id),
                    ).fetchone()
            if existing is not None:
                existing_record = self._resume_draft_from_row(existing)
                if (
                    existing_record.candidate_id != candidate_id
                    or existing_record.job_id != job_id
                ):
                    raise ValueError("简历生成任务幂等键与资源归属不一致。")
                return existing_record

        try:
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
                        account_id, candidate_id, job_id, version, status, draft_json, created_at,
                        generation_key
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        account_id,
                        candidate_id,
                        job_id,
                        version,
                        "需候选人确认",
                        json.dumps(asdict(draft), ensure_ascii=False),
                        created_at,
                        generation_key,
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
        except Exception as error:
            if generation_key and is_unique_constraint_violation(
                error,
                "uq_resume_drafts_generation_key",
            ):
                with self.connect() as conn:
                    if account_id is None:
                        existing = conn.execute(
                            "SELECT * FROM resume_drafts WHERE generation_key = ?",
                            (generation_key,),
                        ).fetchone()
                    else:
                        existing = conn.execute(
                            """
                            SELECT * FROM resume_drafts
                            WHERE generation_key = ? AND account_id = ?
                            """,
                            (generation_key, account_id),
                        ).fetchone()
                if existing is not None:
                    return self._resume_draft_from_row(existing)
            raise

    def get_resume_draft_by_generation_key(
        self,
        generation_key: str,
        account_id: int | None = None,
    ) -> ResumeDraftRecord | None:
        """读取 Worker 导出任务已经保存的草稿，供重试恢复使用。"""

        with self.connect() as conn:
            if account_id is None:
                row = conn.execute(
                    "SELECT * FROM resume_drafts WHERE generation_key = ?",
                    (generation_key,),
                ).fetchone()
            else:
                row = conn.execute(
                    """
                    SELECT * FROM resume_drafts
                    WHERE generation_key = ? AND account_id = ?
                    """,
                    (generation_key, account_id),
                ).fetchone()
        return self._resume_draft_from_row(row) if row is not None else None

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

    def register_knowledge_asset(
        self,
        *,
        account_id: int,
        candidate_id: int | None,
        asset_kind: str,
        title: str,
        original_filename: str,
        storage_key: str,
        media_type: str,
        file_size: int,
        sha256: str,
        source_kind: str = "upload",
        source_url: str | None = None,
        processing_status: str = "uploaded",
        scan_status: str = "pending",
        scan_engine: str | None = None,
        scan_reason: str | None = None,
        revision_label: str | None = None,
        metadata: Mapping[str, object] | None = None,
        version_metadata: Mapping[str, object] | None = None,
    ) -> tuple[KnowledgeAssetRecord, KnowledgeAssetVersionRecord]:
        """原子登记一份知识资产及其首个不可变文件版本。"""

        normalized_kind = self._validate_knowledge_asset_text(asset_kind, "知识资产类型", 64)
        normalized_title = self._validate_knowledge_asset_text(title, "知识资产标题", 512)
        self.get_account(account_id)
        if candidate_id is not None:
            self.get_candidate_profile(candidate_id, account_id=account_id)
        created_at = now_iso()
        try:
            with self.connect() as conn:
                cursor = conn.execute(
                    """
                    INSERT INTO knowledge_assets (
                        account_id, candidate_id, asset_kind, title, lifecycle_status,
                        metadata_json, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, 'active', ?, ?, ?)
                    """,
                    (
                        account_id,
                        candidate_id,
                        normalized_kind,
                        normalized_title,
                        json.dumps(dict(metadata or {}), ensure_ascii=False),
                        created_at,
                        created_at,
                    ),
                )
                asset_id = int(cursor.lastrowid)
                version_id = self._insert_knowledge_asset_version(
                    conn,
                    asset_id=asset_id,
                    version_number=1,
                    original_filename=original_filename,
                    storage_key=storage_key,
                    media_type=media_type,
                    file_size=file_size,
                    sha256=sha256,
                    source_kind=source_kind,
                    source_url=source_url,
                    processing_status=processing_status,
                    scan_status=scan_status,
                    scan_engine=scan_engine,
                    scan_reason=scan_reason,
                    revision_label=revision_label,
                    metadata=version_metadata,
                    created_at=created_at,
                )
        except Exception as error:
            if is_unique_constraint_violation(
                error,
                "uq_knowledge_asset_versions_storage_key",
            ):
                raise DuplicateResourceError("知识文件版本") from error
            raise
        return (
            self.get_knowledge_asset(asset_id, account_id=account_id),
            self.get_knowledge_asset_version(version_id, account_id=account_id),
        )

    def add_knowledge_asset_version(
        self,
        asset_id: int,
        *,
        account_id: int,
        original_filename: str,
        storage_key: str,
        media_type: str,
        file_size: int,
        sha256: str,
        source_kind: str = "upload",
        source_url: str | None = None,
        processing_status: str = "uploaded",
        scan_status: str = "pending",
        scan_engine: str | None = None,
        scan_reason: str | None = None,
        revision_label: str | None = None,
        metadata: Mapping[str, object] | None = None,
    ) -> KnowledgeAssetVersionRecord:
        """为已有资产追加版本，并原子切换唯一的当前版本。"""

        self._validate_knowledge_asset_version(
            original_filename=original_filename,
            storage_key=storage_key,
            media_type=media_type,
            file_size=file_size,
            sha256=sha256,
            source_kind=source_kind,
            processing_status=processing_status,
            scan_status=scan_status,
        )
        created_at = now_iso()
        try:
            with self.connect() as conn:
                asset_row = conn.execute(
                    """
                    SELECT * FROM knowledge_assets
                    WHERE id = ? AND account_id = ?
                    FOR UPDATE
                    """,
                    (asset_id, account_id),
                ).fetchone()
                if asset_row is None:
                    raise KeyError(f"Knowledge asset not found: {asset_id}")
                if str(asset_row["lifecycle_status"]) != "active":
                    raise ValueError("已归档的知识资产不能继续添加版本。")
                duplicate = conn.execute(
                    """
                    SELECT id FROM knowledge_asset_versions
                    WHERE asset_id = ? AND sha256 = ?
                    """,
                    (asset_id, sha256.lower()),
                ).fetchone()
                if duplicate is not None:
                    raise DuplicateResourceError("知识文件版本")
                latest = conn.execute(
                    """
                    SELECT COALESCE(MAX(version_number), 0) AS latest_version
                    FROM knowledge_asset_versions
                    WHERE asset_id = ?
                    """,
                    (asset_id,),
                ).fetchone()
                next_version = int(latest["latest_version"]) + 1
                conn.execute(
                    "UPDATE knowledge_asset_versions SET is_current = false WHERE asset_id = ? AND is_current = true",
                    (asset_id,),
                )
                version_id = self._insert_knowledge_asset_version(
                    conn,
                    asset_id=asset_id,
                    version_number=next_version,
                    original_filename=original_filename,
                    storage_key=storage_key,
                    media_type=media_type,
                    file_size=file_size,
                    sha256=sha256,
                    source_kind=source_kind,
                    source_url=source_url,
                    processing_status=processing_status,
                    scan_status=scan_status,
                    scan_engine=scan_engine,
                    scan_reason=scan_reason,
                    revision_label=revision_label,
                    metadata=metadata,
                    created_at=created_at,
                )
                conn.execute(
                    "UPDATE knowledge_assets SET updated_at = ? WHERE id = ?",
                    (created_at, asset_id),
                )
        except Exception as error:
            if is_unique_constraint_violation(
                error,
                "uq_knowledge_asset_versions_content",
            ) or is_unique_constraint_violation(
                error,
                "uq_knowledge_asset_versions_storage_key",
            ):
                raise DuplicateResourceError("知识文件版本") from error
            raise
        return self.get_knowledge_asset_version(version_id, account_id=account_id)

    def get_knowledge_asset(
        self,
        asset_id: int,
        *,
        account_id: int,
    ) -> KnowledgeAssetRecord:
        """读取资产及其当前版本 ID，并强制执行账号隔离。"""

        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT asset.*, current_version.id AS current_version_id
                FROM knowledge_assets AS asset
                LEFT JOIN knowledge_asset_versions AS current_version
                  ON current_version.asset_id = asset.id AND current_version.is_current = true
                WHERE asset.id = ? AND asset.account_id = ?
                """,
                (asset_id, account_id),
            ).fetchone()
        if row is None:
            raise KeyError(f"Knowledge asset not found: {asset_id}")
        return self._knowledge_asset_from_row(row)

    def get_knowledge_asset_version(
        self,
        version_id: int,
        *,
        account_id: int,
    ) -> KnowledgeAssetVersionRecord:
        """按版本 ID 读取原件元数据，并通过资产所有权隔离账号。"""

        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT version.*
                FROM knowledge_asset_versions AS version
                JOIN knowledge_assets AS asset ON asset.id = version.asset_id
                WHERE version.id = ? AND asset.account_id = ?
                """,
                (version_id, account_id),
            ).fetchone()
        if row is None:
            raise KeyError(f"Knowledge asset version not found: {version_id}")
        return self._knowledge_asset_version_from_row(row)

    def list_knowledge_assets(
        self,
        *,
        account_id: int,
        candidate_id: int | None = None,
        include_archived: bool = False,
    ) -> list[KnowledgeAssetRecord]:
        """列出账号的知识资产；默认隐藏已归档资产。"""

        where = ["asset.account_id = ?"]
        parameters: list[object] = [account_id]
        if candidate_id is not None:
            where.append("asset.candidate_id = ?")
            parameters.append(candidate_id)
        if not include_archived:
            where.append("asset.lifecycle_status = 'active'")
        with self.connect() as conn:
            rows = conn.execute(
                f"""
                SELECT asset.*, current_version.id AS current_version_id
                FROM knowledge_assets AS asset
                LEFT JOIN knowledge_asset_versions AS current_version
                  ON current_version.asset_id = asset.id AND current_version.is_current = true
                WHERE {' AND '.join(where)}
                ORDER BY asset.id
                """,
                tuple(parameters),
            ).fetchall()
        return [self._knowledge_asset_from_row(row) for row in rows]

    def list_knowledge_asset_versions(
        self,
        asset_id: int,
        *,
        account_id: int,
    ) -> list[KnowledgeAssetVersionRecord]:
        """按版本号列出一份资产的全部历史原件。"""

        self.get_knowledge_asset(asset_id, account_id=account_id)
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM knowledge_asset_versions
                WHERE asset_id = ?
                ORDER BY version_number
                """,
                (asset_id,),
            ).fetchall()
        return [self._knowledge_asset_version_from_row(row) for row in rows]

    def archive_knowledge_asset(
        self,
        asset_id: int,
        *,
        account_id: int,
    ) -> KnowledgeAssetRecord:
        """归档资产，使其退出默认检索候选集并禁止继续追加版本。"""

        updated_at = now_iso()
        with self.connect() as conn:
            cursor = conn.execute(
                """
                UPDATE knowledge_assets
                SET lifecycle_status = 'archived', updated_at = ?
                WHERE id = ? AND account_id = ?
                """,
                (updated_at, asset_id, account_id),
            )
            if cursor.rowcount == 0:
                raise KeyError(f"Knowledge asset not found: {asset_id}")
        return self.get_knowledge_asset(asset_id, account_id=account_id)

    def _insert_knowledge_asset_version(
        self,
        conn: RepositoryConnection,
        *,
        asset_id: int,
        version_number: int,
        original_filename: str,
        storage_key: str,
        media_type: str,
        file_size: int,
        sha256: str,
        source_kind: str,
        source_url: str | None,
        processing_status: str,
        scan_status: str,
        scan_engine: str | None,
        scan_reason: str | None,
        revision_label: str | None,
        metadata: Mapping[str, object] | None,
        created_at: str,
    ) -> int:
        """在调用方事务中插入版本，供首版登记和追加版本共用。"""

        self._validate_knowledge_asset_version(
            original_filename=original_filename,
            storage_key=storage_key,
            media_type=media_type,
            file_size=file_size,
            sha256=sha256,
            source_kind=source_kind,
            processing_status=processing_status,
            scan_status=scan_status,
        )
        cursor = conn.execute(
            """
            INSERT INTO knowledge_asset_versions (
                asset_id, version_number, is_current, original_filename, storage_key,
                media_type, file_size, sha256, source_kind, source_url, revision_label,
                processing_status, scan_status, scan_engine, scan_reason, metadata_json, created_at
            ) VALUES (?, ?, true, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                asset_id,
                version_number,
                original_filename.strip(),
                storage_key.strip(),
                media_type.strip(),
                file_size,
                sha256.lower(),
                source_kind.strip(),
                source_url.strip() if source_url else None,
                revision_label.strip()[:128] if revision_label and revision_label.strip() else None,
                processing_status,
                scan_status,
                scan_engine.strip()[:64] if scan_engine and scan_engine.strip() else None,
                scan_reason.strip()[:500] if scan_reason and scan_reason.strip() else None,
                json.dumps(dict(metadata or {}), ensure_ascii=False),
                created_at,
            ),
        )
        return int(cursor.lastrowid)

    @staticmethod
    def _validate_knowledge_asset_text(value: str, label: str, max_length: int) -> str:
        normalized = str(value or "").strip()
        if not normalized:
            raise ValueError(f"{label}不能为空。")
        if len(normalized) > max_length:
            raise ValueError(f"{label}长度不能超过 {max_length} 个字符。")
        return normalized

    def _validate_knowledge_asset_version(
        self,
        *,
        original_filename: str,
        storage_key: str,
        media_type: str,
        file_size: int,
        sha256: str,
        source_kind: str,
        processing_status: str,
        scan_status: str,
    ) -> None:
        self._validate_knowledge_asset_text(original_filename, "原始文件名", 512)
        self._validate_knowledge_asset_text(storage_key, "文件存储键", 1024)
        self._validate_knowledge_asset_text(media_type, "文件媒体类型", 128)
        self._validate_knowledge_asset_text(source_kind, "文件来源类型", 32)
        if file_size < 0:
            raise ValueError("文件大小不能小于零。")
        normalized_sha256 = str(sha256 or "").strip().lower()
        if len(normalized_sha256) != 64 or any(character not in "0123456789abcdef" for character in normalized_sha256):
            raise ValueError("文件 SHA-256 必须是 64 位十六进制字符串。")
        if processing_status not in KNOWLEDGE_ASSET_PROCESSING_STATUSES:
            raise ValueError("知识文件处理状态无效。")
        if scan_status not in KNOWLEDGE_ASSET_SCAN_STATUSES:
            raise ValueError("知识文件扫描状态无效。")

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
        generation_key: str | None = None,
        scan_status: str = "clean",
        scan_engine: str | None = None,
        scan_reason: str | None = None,
        knowledge_asset_id: int | None = None,
        knowledge_asset_version_id: int | None = None,
    ) -> ResumeArtifactRecord:
        """保存一份简历文件元数据，可选地同时登记 RAG 长文本来源。

        二进制文件应先由 `ResumeFileStore` 原子写入；调用方在本方法失败时负责
        删除刚写入的文件，避免文件系统和 PostgreSQL 元数据之间留下孤立记录。
        """

        if artifact_type not in {"source", "tailored"}:
            raise ValueError("简历文件类型只能是 source 或 tailored。")
        if status not in RESUME_ARTIFACT_STATUSES:
            raise ValueError("简历文件状态无效。")
        if artifact_type == "tailored" and status != "ready":
            raise ValueError("职位定制简历只能保存为 ready 状态。")
        if register_long_text and status != "ready":
            raise ValueError("只有解析完成的简历才能登记 RAG 长文本。")
        if (knowledge_asset_id is None) != (knowledge_asset_version_id is None):
            raise ValueError("知识资产和知识资产版本必须同时提供。")
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
        if knowledge_asset_id is not None and knowledge_asset_version_id is not None:
            if account_id is None:
                raise ValueError("关联知识资产时必须提供账号 ID。")
            linked_asset = self.get_knowledge_asset(knowledge_asset_id, account_id=account_id)
            linked_version = self.get_knowledge_asset_version(
                knowledge_asset_version_id,
                account_id=account_id,
            )
            if linked_version.asset_id != linked_asset.id:
                raise ValueError("知识资产版本不属于指定资产。")
            if linked_asset.lifecycle_status != "active":
                raise ValueError("已归档的知识资产不能关联新的业务文件。")
            if linked_asset.candidate_id not in {None, candidate_id}:
                raise ValueError("简历和知识资产必须属于同一候选人。")
            if (
                linked_version.storage_key != storage_key
                or linked_version.sha256 != sha256.lower()
                or linked_version.file_size != file_size
            ):
                raise ValueError("简历文件与指定知识资产版本的原件元数据不一致。")
        if artifact_type == "source" and account_id is None:
            raise ValueError("原始简历必须归属于账号。")

        if generation_key:
            existing = self.get_resume_artifact_by_generation_key(
                generation_key,
                account_id=account_id,
            )
            if existing is not None:
                if (
                    existing.candidate_id != candidate_id
                    or existing.draft_id != draft_id
                ):
                    raise ValueError("简历文件生成任务幂等键与资源归属不一致。")
                return existing

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
                linked_asset_id = knowledge_asset_id
                linked_version_id = knowledge_asset_version_id
                created_linked_version = False
                if artifact_type == "source" and linked_asset_id is None:
                    asset_cursor = conn.execute(
                        """
                        INSERT INTO knowledge_assets (
                            account_id, candidate_id, asset_kind, title, lifecycle_status,
                            metadata_json, created_at, updated_at
                        ) VALUES (?, ?, 'resume', ?, 'active', ?, ?, ?)
                        """,
                        (
                            account_id,
                            candidate_id,
                            original_filename,
                            json.dumps({"business_source": "resume_artifacts"}, ensure_ascii=False),
                            created_at,
                            created_at,
                        ),
                    )
                    linked_asset_id = int(asset_cursor.lastrowid)
                    linked_version_id = self._insert_knowledge_asset_version(
                        conn,
                        asset_id=linked_asset_id,
                        version_number=1,
                        original_filename=original_filename,
                        storage_key=storage_key,
                        media_type=media_type,
                        file_size=file_size,
                        sha256=sha256,
                        source_kind="upload",
                        source_url=None,
                        processing_status=status,
                        scan_status=scan_status,
                        scan_engine=scan_engine,
                        scan_reason=scan_reason,
                        revision_label=str(actual_version),
                        metadata=None,
                        created_at=created_at,
                    )
                    created_linked_version = True
                cursor = conn.execute(
                    """
                    INSERT INTO resume_artifacts (
                        account_id, candidate_id, job_id, draft_id, parent_artifact_id,
                        version, artifact_type, original_filename, download_filename,
                        storage_key, media_type, file_size, sha256, extraction_method,
                        extracted_text, text_length, page_count, status, long_text_id, created_at,
                        content_fingerprint, generation_key, scan_status, scan_engine, scan_reason,
                        knowledge_asset_id, knowledge_asset_version_id
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                        generation_key,
                        scan_status,
                        scan_engine,
                        scan_reason,
                        linked_asset_id,
                        linked_version_id,
                    ),
                )
                artifact_id = int(cursor.lastrowid)
                if created_linked_version and linked_version_id is not None:
                    conn.execute(
                        """
                        UPDATE knowledge_asset_versions
                        SET metadata_json = ?
                        WHERE id = ?
                        """,
                        (
                            json.dumps({"resume_artifact_id": artifact_id}, ensure_ascii=False),
                            linked_version_id,
                        ),
                    )
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
            if generation_key and is_unique_constraint_violation(
                error,
                "uq_resume_artifacts_generation_key",
            ):
                existing = self.get_resume_artifact_by_generation_key(
                    generation_key,
                    account_id=account_id,
                )
                if existing is not None:
                    return existing
            raise
        return self.get_resume_artifact(artifact_id, account_id=account_id)

    def complete_resume_artifact_scan(
        self,
        artifact_id: int,
        *,
        next_status: str,
        extraction_method: str,
        page_count: int | None,
        scan_engine: str,
        account_id: int | None = None,
    ) -> ResumeArtifactRecord:
        """把扫描中的原件提升为可解析状态，但不登记任何正文。"""

        if next_status not in {"processing", "ready"}:
            raise ValueError("文件扫描通过后的状态无效。")
        owner_clause = ""
        owner_parameters: tuple[object, ...] = ()
        if account_id is not None:
            owner_clause = " AND account_id = ?"
            owner_parameters = (account_id,)
        with self.connect() as conn:
            cursor = conn.execute(
                f"""
                UPDATE resume_artifacts
                SET extraction_method = ?, page_count = ?, status = ?,
                    scan_status = 'clean', scan_engine = ?, scan_reason = NULL
                WHERE id = ?{owner_clause} AND status = 'scanning'
                """,
                (
                    extraction_method,
                    page_count,
                    next_status,
                    scan_engine,
                    artifact_id,
                    *owner_parameters,
                ),
            )
            if cursor.rowcount == 0:
                raise ValueError("这份简历当前不处于待扫描状态。")
            conn.execute(
                """
                UPDATE knowledge_asset_versions AS version
                SET processing_status = ?, scan_status = 'clean', scan_engine = ?, scan_reason = NULL
                FROM resume_artifacts AS artifact
                WHERE artifact.id = ? AND version.id = artifact.knowledge_asset_version_id
                """,
                (next_status, scan_engine, artifact_id),
            )
        return self.get_resume_artifact(artifact_id, account_id=account_id)

    def quarantine_resume_artifact(
        self,
        artifact_id: int,
        *,
        scan_status: str,
        scan_engine: str,
        scan_reason: str,
        account_id: int | None = None,
    ) -> ResumeArtifactRecord:
        """把未通过扫描或无法完成扫描的原件锁定在隔离状态。"""

        if scan_status not in {"infected", "error"}:
            raise ValueError("隔离文件的扫描状态无效。")
        owner_clause = ""
        owner_parameters: tuple[object, ...] = ()
        if account_id is not None:
            owner_clause = " AND account_id = ?"
            owner_parameters = (account_id,)
        with self.connect() as conn:
            cursor = conn.execute(
                f"""
                UPDATE resume_artifacts
                SET status = 'quarantined', extraction_method = 'scan_blocked',
                    scan_status = ?, scan_engine = ?, scan_reason = ?,
                    extracted_text = '', text_length = 0, long_text_id = NULL,
                    content_fingerprint = NULL
                WHERE id = ?{owner_clause} AND status = 'scanning'
                """,
                (
                    scan_status,
                    scan_engine,
                    scan_reason[:500],
                    artifact_id,
                    *owner_parameters,
                ),
            )
            if cursor.rowcount == 0:
                raise ValueError("这份简历当前不处于待扫描状态。")
            conn.execute(
                """
                UPDATE knowledge_asset_versions AS version
                SET processing_status = 'quarantined', scan_status = ?,
                    scan_engine = ?, scan_reason = ?
                FROM resume_artifacts AS artifact
                WHERE artifact.id = ? AND version.id = artifact.knowledge_asset_version_id
                """,
                (scan_status, scan_engine, scan_reason[:500], artifact_id),
            )
        return self.get_resume_artifact(artifact_id, account_id=account_id)

    def get_resume_artifact_by_generation_key(
        self,
        generation_key: str,
        account_id: int | None = None,
    ) -> ResumeArtifactRecord | None:
        """读取 Worker 导出任务已经登记的文件，供重试恢复使用。"""

        with self.connect() as conn:
            if account_id is None:
                row = conn.execute(
                    "SELECT * FROM resume_artifacts WHERE generation_key = ?",
                    (generation_key,),
                ).fetchone()
            else:
                row = conn.execute(
                    """
                    SELECT * FROM resume_artifacts
                    WHERE generation_key = ? AND account_id = ?
                    """,
                    (generation_key, account_id),
                ).fetchone()
        return self._resume_artifact_from_row(row) if row is not None else None

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
                      AND (scan_status = 'clean' OR scan_status IS NULL)
                      AND (content_fingerprint = ? OR (content_fingerprint IS NULL AND sha256 = ?))
                    """,
                    (candidate_id, fingerprint, fingerprint),
                ).fetchone()
            else:
                row = conn.execute(
                    """
                    SELECT * FROM resume_artifacts
                    WHERE account_id = ? AND candidate_id = ? AND artifact_type = 'source'
                      AND (scan_status = 'clean' OR scan_status IS NULL)
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
            if current_status not in {"scanning", "processing", "ready"}:
                raise ValueError("这份简历不处于可完成 OCR 的状态。")

            conn.execute(
                f"""
                UPDATE resume_artifacts
                SET extraction_method = ?, extracted_text = ?, text_length = ?,
                    page_count = ?, status = 'ready', scan_status = 'clean',
                    scan_reason = NULL
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
            conn.execute(
                """
                UPDATE knowledge_asset_versions AS version
                SET processing_status = 'ready', scan_status = 'clean', scan_reason = NULL
                FROM resume_artifacts AS artifact
                WHERE artifact.id = ? AND version.id = artifact.knowledge_asset_version_id
                """,
                (artifact_id,),
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
            cursor = conn.execute(
                f"""
                UPDATE resume_artifacts
                SET extraction_method = 'ocr_failed', status = 'failed'
                WHERE id = ?{owner_clause} AND status = 'processing'
                """,
                (artifact_id, *owner_parameters),
            )
            if cursor.rowcount:
                conn.execute(
                    """
                    UPDATE knowledge_asset_versions AS version
                    SET processing_status = 'failed'
                    FROM resume_artifacts AS artifact
                    WHERE artifact.id = ? AND version.id = artifact.knowledge_asset_version_id
                    """,
                    (artifact_id,),
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

            deleted_knowledge_asset_id: int | None = None
            if artifact.knowledge_asset_id is not None:
                asset_row = conn.execute(
                    "SELECT metadata_json FROM knowledge_assets WHERE id = ?",
                    (artifact.knowledge_asset_id,),
                ).fetchone()
                asset_metadata = (
                    _json_object(asset_row["metadata_json"])
                    if asset_row is not None
                    else {}
                )
                resume_owned_asset = (
                    asset_metadata.get("business_source") == "resume_artifacts"
                    or asset_metadata.get("migrated_from") == "resume_artifacts"
                )
                remaining_asset_references = conn.execute(
                    """
                    SELECT COUNT(*) AS reference_count
                    FROM resume_artifacts
                    WHERE knowledge_asset_id = ?
                    """,
                    (artifact.knowledge_asset_id,),
                ).fetchone()
                if resume_owned_asset and int(remaining_asset_references["reference_count"]) == 0:
                    asset_owner_clause = ""
                    asset_owner_parameters: tuple[object, ...] = ()
                    if account_id is not None:
                        asset_owner_clause = " AND account_id = ?"
                        asset_owner_parameters = (account_id,)
                    deleted_asset = conn.execute(
                        f"DELETE FROM knowledge_assets WHERE id = ?{asset_owner_clause}",
                        (
                            artifact.knowledge_asset_id,
                            *asset_owner_parameters,
                        ),
                    )
                    if deleted_asset.rowcount:
                        deleted_knowledge_asset_id = artifact.knowledge_asset_id

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
            "knowledge_asset_id": deleted_knowledge_asset_id,
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

    def _project_archive_import_from_row(
        self,
        row: RepositoryRow,
    ) -> ProjectArchiveImportRecord:
        """把项目整包导入行转换成领域记录。"""

        return ProjectArchiveImportRecord(
            id=int(row["id"]),
            account_id=int(row["account_id"]),
            candidate_id=int(row["candidate_id"]),
            knowledge_asset_id=int(row["knowledge_asset_id"]),
            knowledge_asset_version_id=int(row["knowledge_asset_version_id"]),
            project_card_id=(
                int(row["project_card_id"]) if row["project_card_id"] is not None else None
            ),
            source_type=str(row["source_type"]),
            source_url=str(row["source_url"]) if row["source_url"] is not None else None,
            source_ref=str(row["source_ref"]) if row["source_ref"] is not None else None,
            original_filename=str(row["original_filename"]),
            content_fingerprint=str(row["content_fingerprint"]),
            status=str(row["status"]),
            error_summary=(
                str(row["error_summary"]) if row["error_summary"] is not None else None
            ),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
        )

    def _project_archive_file_from_row(
        self,
        row: RepositoryRow,
    ) -> ProjectArchiveFileRecord:
        """把项目文件清单行转换成领域记录。"""

        return ProjectArchiveFileRecord(
            id=int(row["id"]),
            project_archive_id=int(row["project_archive_id"]),
            relative_path=str(row["relative_path"]),
            file_kind=str(row["file_kind"]),
            media_type=str(row["media_type"]),
            file_size=int(row["file_size"]),
            compressed_size=int(row["compressed_size"]),
            sha256=str(row["sha256"]) if row["sha256"] is not None else None,
            analysis_status=str(row["analysis_status"]),
            skip_reason=(
                str(row["skip_reason"]) if row["skip_reason"] is not None else None
            ),
            metadata=_json_object(row["metadata_json"]),
            long_text_id=(
                int(row["long_text_id"])
                if "long_text_id" in row and row["long_text_id"] is not None
                else None
            ),
            extraction_method=(
                str(row["extraction_method"])
                if "extraction_method" in row and row["extraction_method"] is not None
                else None
            ),
            text_length=(
                int(row["text_length"])
                if "text_length" in row and row["text_length"] is not None
                else 0
            ),
        )

    def _project_collection_session_from_row(
        self,
        row: RepositoryRow,
    ) -> ProjectCollectionSessionRecord:
        """Convert a local project collection row into a domain record."""

        return ProjectCollectionSessionRecord(
            id=int(row["id"]),
            account_id=int(row["account_id"]),
            candidate_id=int(row["candidate_id"]),
            project_card_id=(
                int(row["project_card_id"]) if row["project_card_id"] is not None else None
            ),
            project_name=str(row["project_name"]),
            source_type=str(row["source_type"]),
            manifest_fingerprint=str(row["manifest_fingerprint"]),
            status=str(row["status"]),
            file_count=int(row["file_count"]),
            selected_file_count=int(row["selected_file_count"]),
            uploaded_file_count=int(row["uploaded_file_count"]),
            total_size=int(row["total_size"]),
            selected_size=int(row["selected_size"]),
            error_summary=(
                str(row["error_summary"]) if row["error_summary"] is not None else None
            ),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
        )

    def _project_collection_file_from_row(
        self,
        row: RepositoryRow,
    ) -> ProjectCollectionFileRecord:
        """Convert one planned local file row into a domain record."""

        return ProjectCollectionFileRecord(
            id=int(row["id"]),
            collection_id=int(row["collection_id"]),
            relative_path=str(row["relative_path"]),
            file_kind=str(row["file_kind"]),
            media_type=str(row["media_type"]),
            file_size=int(row["file_size"]),
            client_sha256=(
                str(row["client_sha256"]) if row["client_sha256"] is not None else None
            ),
            server_sha256=(
                str(row["server_sha256"]) if row["server_sha256"] is not None else None
            ),
            selection_status=str(row["selection_status"]),
            selection_reason=str(row["selection_reason"]),
            extraction_method=(
                str(row["extraction_method"]) if row["extraction_method"] is not None else None
            ),
            text_length=int(row["text_length"]),
            long_text_id=(
                int(row["long_text_id"]) if row["long_text_id"] is not None else None
            ),
            metadata=_json_object(row["metadata_json"]),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
        )

    def _visual_knowledge_item_from_row(
        self,
        row: RepositoryRow,
    ) -> VisualKnowledgeItemRecord:
        """Convert a visual knowledge row without loading its vector or binary body."""

        return VisualKnowledgeItemRecord(
            id=int(row["id"]),
            account_id=int(row["account_id"]),
            candidate_id=int(row["candidate_id"]),
            project_archive_file_id=(
                int(row["project_archive_file_id"])
                if row["project_archive_file_id"] is not None
                else None
            ),
            project_collection_file_id=(
                int(row["project_collection_file_id"])
                if row["project_collection_file_id"] is not None
                else None
            ),
            long_text_id=(
                int(row["long_text_id"]) if row["long_text_id"] is not None else None
            ),
            source_id=str(row["source_id"]),
            source_label=str(row["source_label"]),
            page_number=(
                int(row["page_number"]) if row["page_number"] is not None else None
            ),
            media_type=str(row["media_type"]),
            storage_key=str(row["storage_key"]),
            file_size=int(row["file_size"]),
            sha256=str(row["sha256"]),
            width=int(row["width"]),
            height=int(row["height"]),
            index_status=str(row["index_status"]),
            embedding_model=(
                str(row["embedding_model"])
                if row["embedding_model"] is not None
                else None
            ),
            embedding_dimensions=(
                int(row["embedding_dimensions"])
                if row["embedding_dimensions"] is not None
                else None
            ),
            index_error_type=(
                str(row["index_error_type"])
                if row["index_error_type"] is not None
                else None
            ),
            metadata=_json_object(row["metadata_json"]),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
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

    def _knowledge_asset_from_row(self, row: RepositoryRow) -> KnowledgeAssetRecord:
        """把统一知识资产行转换成领域记录。"""

        return KnowledgeAssetRecord(
            id=int(row["id"]),
            account_id=int(row["account_id"]),
            candidate_id=int(row["candidate_id"]) if row["candidate_id"] is not None else None,
            asset_kind=str(row["asset_kind"]),
            title=str(row["title"]),
            lifecycle_status=str(row["lifecycle_status"]),
            current_version_id=(
                int(row["current_version_id"])
                if row.get("current_version_id") is not None
                else None
            ),
            metadata=_json_object(row["metadata_json"]),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
        )

    def _knowledge_asset_version_from_row(
        self,
        row: RepositoryRow,
    ) -> KnowledgeAssetVersionRecord:
        """把不可变知识文件版本行转换成领域记录。"""

        return KnowledgeAssetVersionRecord(
            id=int(row["id"]),
            asset_id=int(row["asset_id"]),
            version_number=int(row["version_number"]),
            is_current=bool(row["is_current"]),
            original_filename=str(row["original_filename"]),
            storage_key=str(row["storage_key"]),
            media_type=str(row["media_type"]),
            file_size=int(row["file_size"]),
            sha256=str(row["sha256"]),
            source_kind=str(row["source_kind"]),
            source_url=str(row["source_url"]) if row["source_url"] is not None else None,
            revision_label=(
                str(row["revision_label"])
                if row["revision_label"] is not None
                else None
            ),
            processing_status=str(row["processing_status"]),
            scan_status=str(row["scan_status"]),
            scan_engine=str(row["scan_engine"]) if row["scan_engine"] is not None else None,
            scan_reason=str(row["scan_reason"]) if row["scan_reason"] is not None else None,
            metadata=_json_object(row["metadata_json"]),
            created_at=str(row["created_at"]),
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
            knowledge_asset_id=(
                int(row["knowledge_asset_id"])
                if row.get("knowledge_asset_id") is not None
                else None
            ),
            knowledge_asset_version_id=(
                int(row["knowledge_asset_version_id"])
                if row.get("knowledge_asset_version_id") is not None
                else None
            ),
            scan_status=str(row.get("scan_status") or "clean"),
            scan_engine=(str(row["scan_engine"]) if row.get("scan_engine") else None),
            scan_reason=(str(row["scan_reason"]) if row.get("scan_reason") else None),
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
        email_verified_at=(
            str(row["email_verified_at"])
            if "email_verified_at" in row and row["email_verified_at"] is not None
            else None
        ),
        deleted_at=(
            str(row["deleted_at"])
            if "deleted_at" in row and row["deleted_at"] is not None
            else None
        ),
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
