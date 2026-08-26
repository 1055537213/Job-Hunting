"""账号事务邮件的持久 Outbox、令牌派生和投递状态机。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from .account_lifecycle import (
    AccountEmailSender,
    account_action_url,
    action_token_hash,
    derive_action_token,
    request_source_hash,
)
from .config import AccountLifecycleSettings
from .models import AccountEmailOutboxRecord, AccountRecord
from .storage import RepositoryStore


class AccountEmailOutboxService:
    """登记和投递账号邮件，PostgreSQL 状态决定是否允许执行。"""

    def __init__(
        self,
        store: RepositoryStore,
        settings: AccountLifecycleSettings,
        sender: AccountEmailSender,
    ) -> None:
        self.store = store
        self.settings = settings
        self.sender = sender

    def enqueue(
        self,
        account: AccountRecord,
        purpose: str,
        requested_ip: str | None,
    ) -> AccountEmailOutboxRecord:
        """原子登记邮件和哈希令牌，不调用 SMTP。"""

        delivery_key = uuid4().hex
        raw_token = derive_action_token(
            self.settings.action_secret,
            delivery_key,
            purpose,
            account.id,
        )
        ttl_minutes = (
            self.settings.verification_token_ttl_minutes
            if purpose == "verify_email"
            else self.settings.password_reset_token_ttl_minutes
        )
        expires_at = (
            datetime.now(UTC) + timedelta(minutes=ttl_minutes)
        ).isoformat(timespec="seconds")
        return self.store.create_account_email_outbox(
            account_id=account.id,
            purpose=purpose,
            recipient_email=account.email,
            delivery_key=delivery_key,
            token_hash=action_token_hash(raw_token),
            expires_at=expires_at,
            request_source_hash=request_source_hash(
                self.settings.action_secret,
                requested_ip,
            ),
            cooldown_seconds=self.settings.email_request_cooldown_seconds,
            account_hourly_limit=self.settings.email_account_hourly_limit,
            source_hourly_limit=self.settings.email_source_hourly_limit,
            max_attempts=self.settings.email_outbox_max_attempts,
        )

    def deliver(self, outbox_id: int) -> AccountEmailOutboxRecord:
        """认领并发送一封邮件；终态和重复消息不会再次发送。"""

        claimed = self.store.claim_account_email_outbox(
            outbox_id,
            self.settings.email_claim_timeout_seconds,
        )
        if claimed is None:
            return self.store.get_account_email_outbox(outbox_id)
        raw_token = derive_action_token(
            self.settings.action_secret,
            claimed.delivery_key,
            claimed.purpose,
            claimed.account_id,
        )
        parameter = (
            "verify_email_token"
            if claimed.purpose == "verify_email"
            else "reset_password_token"
        )
        action_url = account_action_url(
            self.settings.public_base_url,
            parameter,
            raw_token,
        )
        try:
            if claimed.purpose == "verify_email":
                self.sender.send_verification(claimed.recipient_email, action_url)
            else:
                self.sender.send_password_reset(claimed.recipient_email, action_url)
        except Exception as error:  # noqa: BLE001 - SMTP/relay implementations vary.
            delay = min(
                3600,
                self.settings.email_retry_base_seconds
                * (2 ** max(0, claimed.attempt_count - 1)),
            )
            return self.store.fail_account_email_outbox(
                outbox_id,
                error_type=type(error).__name__,
                error_summary=_safe_delivery_error(error),
                next_attempt_at=(
                    datetime.now(UTC) + timedelta(seconds=delay)
                ).isoformat(timespec="seconds"),
            )
        return self.store.complete_account_email_outbox(outbox_id)


def _safe_delivery_error(error: Exception) -> str:
    """把供应商异常归一为不包含邮箱、响应正文或凭据的低敏摘要。"""

    if isinstance(error, TimeoutError):
        return "邮件服务连接超时。"
    if isinstance(error, ConnectionError):
        return "邮件服务连接失败。"
    return "邮件服务暂时不可用。"


def redact_email(email: str) -> str:
    """保留排障所需域名，同时隐藏收件邮箱主体。"""

    local, separator, domain = email.partition("@")
    if not separator:
        return "***"
    visible = local[:1] if local else ""
    return f"{visible}***@{domain}"
