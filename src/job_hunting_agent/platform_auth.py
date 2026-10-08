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
class PlatformCredentialResult:
    account_id: int


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

    def verify_credentials(
        self,
        *,
        email: str,
        password: str,
        email_verification_required: bool,
        trace_id: str | None = None,
    ) -> PlatformCredentialResult:
        headers = {
            "X-Internal-Service-Token": self.internal_token,
            "X-Trace-Id": trace_id or f"platform-auth-{uuid4().hex}",
        }
        try:
            response = httpx.post(
                f"{self.base_url}/internal/v1/auth/verify-credentials",
                headers=headers,
                json={
                    "email": email,
                    "password": password,
                    "email_verification_required": email_verification_required,
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
            return PlatformCredentialResult(account_id=int(payload["account_id"]))
        except (KeyError, TypeError, ValueError) as error:
            raise PlatformAuthError(
                "INVALID_PLATFORM_RESPONSE",
                "平台认证服务返回了无效账号数据。",
                _platform_response_error_status(response),
            ) from error

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
        """Internal verification and delivery state API; never falls back to local writes."""

        if operation not in {
            "request",
            "confirm",
            "due",
            "claim",
            "finish",
            "observations",
        }:
            raise ValueError("Unknown verification operation")
        try:
            response = httpx.post(
                f"{self.base_url}/internal/v1/auth/email-verification/{operation}",
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
