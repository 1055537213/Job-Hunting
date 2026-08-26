"""Account verification and recovery email boundary.

Raw action tokens only exist in memory and outbound URLs. PostgreSQL stores SHA-256
digests so a database disclosure cannot directly verify an email or reset a password.
"""

from __future__ import annotations

import hashlib
import secrets
import smtplib
from email.message import EmailMessage
from typing import Protocol
from urllib.parse import urlencode

from .config import AccountLifecycleSettings


class AccountEmailSender(Protocol):
    """Outbound account-email boundary."""

    def send_verification(self, email: str, action_url: str) -> None: ...

    def send_password_reset(self, email: str, action_url: str) -> None: ...


class ConsoleAccountEmailSender:
    """Development no-op sender; tests should inject a recording implementation."""

    def send_verification(self, email: str, action_url: str) -> None:
        return None

    def send_password_reset(self, email: str, action_url: str) -> None:
        return None


class SmtpAccountEmailSender:
    """Send lifecycle messages through a configured SMTP relay."""

    def __init__(self, settings: AccountLifecycleSettings) -> None:
        self.settings = settings

    def _send(self, recipient: str, subject: str, body: str) -> None:
        message = EmailMessage()
        message["Subject"] = subject
        message["From"] = self.settings.smtp_from_email
        message["To"] = recipient
        message.set_content(body)
        with smtplib.SMTP(self.settings.smtp_host, self.settings.smtp_port, timeout=15) as smtp:
            if self.settings.smtp_use_starttls:
                smtp.starttls()
            if self.settings.smtp_username:
                smtp.login(self.settings.smtp_username, self.settings.smtp_password or "")
            smtp.send_message(message)

    def send_verification(self, email: str, action_url: str) -> None:
        self._send(email, "验证求职助手账号邮箱", f"请打开以下链接完成邮箱验证：\n\n{action_url}")

    def send_password_reset(self, email: str, action_url: str) -> None:
        self._send(email, "重置求职助手账号密码", f"请打开以下链接重置密码：\n\n{action_url}")


def build_account_email_sender(settings: AccountLifecycleSettings) -> AccountEmailSender:
    """Build the configured sender without exposing SMTP secrets."""

    if settings.email_backend == "smtp":
        return SmtpAccountEmailSender(settings)
    return ConsoleAccountEmailSender()


def new_action_token() -> str:
    """Return a high-entropy URL-safe one-time token."""

    return secrets.token_urlsafe(48)


def action_token_hash(token: str) -> str:
    """Return the persistent digest of an action token."""

    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def account_action_url(base_url: str, parameter: str, token: str) -> str:
    """Build a frontend URL that carries one account action token."""

    return f"{base_url.rstrip('/')}/login?{urlencode({parameter: token})}"
