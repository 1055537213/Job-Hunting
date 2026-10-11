"""Python 到 Java 平台认证服务的内部 HTTP 客户端。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import uuid4

import httpx

from .config import PlatformAuthSettings


def _platform_response_error_status(response: httpx.Response) -> int:
    """Never expose a malformed successful upstream response as HTTP 200."""

    return response.status_code if response.is_error else 502


class PlatformAuthError(RuntimeError):
    """Java 认证服务返回的业务错误。"""

    def __init__(self, code: str, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code


class PlatformAuthUnavailableError(PlatformAuthError):
    """Java 认证服务暂时无法访问。"""

    def __init__(self) -> None:
        super().__init__(
            "PLATFORM_AUTH_UNAVAILABLE", "平台认证服务暂时不可用，请稍后重试。"
        )


@dataclass(frozen=True)
class PlatformRegistrationResult:
    account_id: int


class PlatformAuthClient:
    """调用 Java 认证接口；密码只存在于当前请求内，不写日志。"""

    def __init__(self, settings: PlatformAuthSettings) -> None:
        if not settings.enabled:
            raise ValueError("Java 平台认证客户端只能在 enabled 配置下创建。")
        self.base_url = settings.base_url.rstrip("/")
        self.internal_token = settings.internal_token
        self.timeout_seconds = settings.timeout_seconds

    def register_account(
        self,
        *,
        email: str,
        password: str,
        display_name: str | None,
        email_verified: bool,
        consents: list[dict[str, str | None]],
        trace_id: str | None = None,
    ) -> PlatformRegistrationResult:
        """Create an account through Java without logging the password."""

        headers = {
            "X-Internal-Service-Token": self.internal_token,
            "X-Trace-Id": trace_id or f"platform-auth-{uuid4().hex}",
        }
        try:
            response = httpx.post(
                f"{self.base_url}/internal/v1/auth/register",
                headers=headers,
                json={
                    "email": email,
                    "password": password,
                    "display_name": display_name,
                    "email_verified": email_verified,
                    "consents": consents,
                },
                timeout=self.timeout_seconds,
            )
        except httpx.HTTPError as error:
            raise PlatformAuthUnavailableError() from error
        try:
            payload: Any = response.json()
        except ValueError as error:
            raise PlatformAuthError(
                "INVALID_PLATFORM_RESPONSE",
                "平台认证服务返回了无法解析的响应。",
                _platform_response_error_status(response),
            ) from error

        if not isinstance(payload, dict):
            raise PlatformAuthError(
                "INVALID_PLATFORM_RESPONSE",
                "平台认证服务返回了无效响应。",
                _platform_response_error_status(response),
            )
        if response.is_error:
            raise PlatformAuthError(
                str(payload.get("code") or "PLATFORM_AUTH_ERROR"),
                str(payload.get("message") or "平台认证服务请求失败。"),
                response.status_code,
            )
        try:
            return PlatformRegistrationResult(account_id=int(payload["account_id"]))
        except (KeyError, TypeError, ValueError) as error:
            raise PlatformAuthError(
                "INVALID_PLATFORM_RESPONSE",
                "平台认证服务返回了无效账号数据。",
                _platform_response_error_status(response),
            ) from error

    def email_verification(self, operation: str, **data: Any) -> dict[str, Any]:
        return self._account_email("email-verification", operation, data)

    def email_change(self, operation: str, **data: Any) -> dict[str, Any]:
        return self._account_email("email-change", operation, data)

    def password_reset(self, operation: str, **data: Any) -> dict[str, Any]:
        return self._account_email("password-reset", operation, data)

    def session(self, operation: str, *, trace_id: str | None = None, **data: Any) -> dict[str, Any]:
        """Java is the sole session writer in hybrid mode; outages never authorize locally."""

        if operation not in {"login", "resolve", "logout", "logout-all", "change-password"}:
            raise ValueError("Unknown session operation")
        try:
            response = httpx.post(
                f"{self.base_url}/internal/v1/auth/sessions/{operation}",
                headers={
                    "X-Internal-Service-Token": self.internal_token,
                    "X-Trace-Id": trace_id or f"platform-session-{uuid4().hex}",
                },
                json=data,
                timeout=self.timeout_seconds,
            )
        except httpx.HTTPError as error:
            raise PlatformAuthUnavailableError() from error
        try:
            payload = response.json()
        except ValueError as error:
            raise PlatformAuthError("INVALID_PLATFORM_RESPONSE", "平台认证服务响应无效。", 502) from error
        if not isinstance(payload, dict):
            raise PlatformAuthError("INVALID_PLATFORM_RESPONSE", "平台认证服务响应无效。", 502)
        if response.is_error:
            raise PlatformAuthError(
                str(payload.get("code") or "PLATFORM_AUTH_ERROR"),
                str(payload.get("message") or "平台认证服务请求失败。"), response.status_code,
            )
        value = payload.get("account_id")
        valid_id = isinstance(value, int) and not isinstance(value, bool) and value > 0
        if operation == "login":
            raw = payload.get("session_token")
            valid = valid_id and isinstance(raw, str) and 32 <= len(raw) <= 128 and raw.isascii() and all(
                c.isalnum() or c in "_-" for c in raw
            )
        elif operation == "resolve":
            valid = "account_id" in payload and (value is None or valid_id)
        else:
            valid = payload.get("ok") is True
            if operation == "logout-all":
                count = payload.get("revoked_sessions")
                valid = valid and isinstance(count, int) and not isinstance(count, bool) and count >= 0
        if not valid:
            raise PlatformAuthError("INVALID_PLATFORM_RESPONSE", "平台认证服务响应无效。", 502)
        return payload

    def account(self, operation: str, *, trace_id: str | None = None, **data: Any) -> dict[str, Any]:
        """Call Java-owned current-account and administrator operations.

        Account listing/status changes authenticate with the real session token
        inside Java. Python must not turn a locally read account id into an
        authorization decision.
        """

        if operation not in {
            "me", "profile", "list", "status", "bootstrap", "delete-admission", "delete-complete"
        }:
            raise ValueError("Unknown account operation")
        try:
            response = httpx.post(
                f"{self.base_url}/internal/v1/auth/accounts/{operation}",
                headers={
                    "X-Internal-Service-Token": self.internal_token,
                    "X-Trace-Id": trace_id or f"platform-account-{uuid4().hex}",
                },
                json=data,
                timeout=self.timeout_seconds,
            )
        except httpx.HTTPError as error:
            raise PlatformAuthUnavailableError() from error
        try:
            payload = response.json()
        except ValueError as error:
            raise PlatformAuthError("INVALID_PLATFORM_RESPONSE", "平台认证服务响应无效。", 502) from error
        if not isinstance(payload, dict):
            raise PlatformAuthError("INVALID_PLATFORM_RESPONSE", "平台认证服务响应无效。", 502)
        if response.is_error:
            raise PlatformAuthError(
                str(payload.get("code") or "PLATFORM_AUTH_ERROR"),
                str(payload.get("message") or "平台认证服务请求失败。"),
                response.status_code,
            )

        if operation in {"me", "profile"}:
            valid = _is_safe_account_view(payload.get("account"))
        elif operation == "list":
            accounts = payload.get("accounts")
            valid = isinstance(accounts, list) and all(_is_safe_account_view(account) for account in accounts)
        elif operation in {"status", "delete-admission"}:
            valid = _is_safe_account_view(payload.get("account"))
        elif operation == "delete-complete":
            account_id = payload.get("account_id")
            valid = (
                isinstance(account_id, int)
                and not isinstance(account_id, bool)
                and account_id > 0
                and payload.get("deleted") is True
            )
        else:
            account_id = payload.get("account_id")
            valid = isinstance(payload.get("created"), bool) and (
                account_id is None
                or (isinstance(account_id, int) and not isinstance(account_id, bool) and account_id > 0)
            )
        if not valid:
            raise PlatformAuthError("INVALID_PLATFORM_RESPONSE", "平台认证服务响应无效。", 502)
        return payload

    def _account_email(self, action: str, operation: str, data: dict[str, Any]) -> dict[str, Any]:
        """Java owns tokens and delivery state; never fall back to local writes."""

        if operation not in {
            "request",
            "confirm",
            "due",
            "claim",
            "finish",
            "observations",
        }:
            raise ValueError("Unknown account email operation")
        try:
            response = httpx.post(
                f"{self.base_url}/internal/v1/auth/{action}/{operation}",
                headers={
                    "X-Internal-Service-Token": self.internal_token,
                    "X-Trace-Id": f"platform-email-{uuid4().hex}",
                },
                json=data,
                timeout=self.timeout_seconds,
            )
        except httpx.HTTPError as error:
            raise PlatformAuthUnavailableError() from error
        try:
            payload = response.json()
        except ValueError as error:
            raise PlatformAuthError(
                "INVALID_PLATFORM_RESPONSE", "平台认证服务响应无效。", 502
            ) from error
        if not isinstance(payload, dict):
            raise PlatformAuthError(
                "INVALID_PLATFORM_RESPONSE", "平台认证服务响应无效。", 502
            )
        if response.is_error:
            raise PlatformAuthError(
                str(payload.get("code") or "PLATFORM_AUTH_ERROR"),
                str(payload.get("message") or "平台认证服务请求失败。"),
                response.status_code,
            )
        field = {
            "request": "ok",
            "confirm": "account_id",
            "due": "records",
            "claim": "claim",
            "finish": "ok",
            "observations": "summary",
        }[operation]
        value = payload.get(field)
        valid = field in payload
        if field == "ok":
            valid = isinstance(value, bool)
        elif field == "account_id":
            valid = isinstance(value, int) and not isinstance(value, bool) and value > 0
        elif field == "records":
            valid = isinstance(value, list) and all(
                isinstance(job, dict)
                and isinstance(job.get("id"), int)
                and job["id"] > 0
                and isinstance(job.get("attempt_count"), int)
                and job["attempt_count"] >= 0
                for job in value
            )
        elif field == "claim":
            valid = valid and (
                value is None
                or (
                    isinstance(value, dict)
                    and all(
                        isinstance(value.get(key), str) and value[key]
                        for key in ("claim_key", "recipient_email", "action_url")
                    )
                )
            )
        elif field == "summary":
            valid = isinstance(value, dict) and isinstance(payload.get("records"), list)
        if not valid:
            raise PlatformAuthError(
                "INVALID_PLATFORM_RESPONSE", "平台认证服务响应无效。", 502
            )
        return payload


def _is_safe_account_view(value: Any) -> bool:
    """Validate the Java projection before handing it to the frontend."""

    if not isinstance(value, dict):
        return False
    required = {
        "account_id", "email", "display_name", "role", "status",
        "must_change_password", "email_verified_at", "deleted_at",
        "created_at", "updated_at",
    }
    if set(value) != required:
        return False
    return (
        isinstance(value["account_id"], int)
        and not isinstance(value["account_id"], bool)
        and value["account_id"] > 0
        and isinstance(value["email"], str)
        and (value["display_name"] is None or isinstance(value["display_name"], str))
        and value["role"] in ("user", "admin")
        and value["status"] in ("active", "disabled")
        and isinstance(value["must_change_password"], bool)
        and all(isinstance(value[field], str) and value[field] for field in ("created_at", "updated_at"))
        and all(value[field] is None or isinstance(value[field], str) for field in ("email_verified_at", "deleted_at"))
    )
