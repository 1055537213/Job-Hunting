"""Python 到 Java 平台认证服务的内部 HTTP 客户端。"""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Any
from uuid import uuid4

import httpx

from .config import PlatformAuthSettings
from .account_export import MAX_EXPORT_BYTES, decode_export


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

    def observability(self, *, session_token: str | None, trace_id: str | None = None) -> dict[str, Any]:
        if not session_token:
            raise PlatformAuthError("SESSION_EXPIRED", "登录状态已过期，请重新登录。", 401)
        try:
            response = httpx.post(f"{self.base_url}/internal/v1/observability/requests", headers={
                "X-Internal-Service-Token": self.internal_token, "X-Trace-Id": trace_id or uuid4().hex,
            }, json={"session_token": session_token}, timeout=self.timeout_seconds)
        except httpx.HTTPError as error:
            raise PlatformAuthUnavailableError() from error
        if response.status_code >= 500:
            raise PlatformAuthUnavailableError()
        try:
            result = response.json()
        except ValueError as error:
            raise PlatformAuthError("INVALID_PLATFORM_RESPONSE", "平台观测服务响应无效。", 502) from error
        if response.is_error:
            message = result.get("message") or result.get("detail") if isinstance(result, dict) else None
            raise PlatformAuthError("OBSERVABILITY_ERROR", str(message or "观测查询失败。"), response.status_code)
        try:
            valid = (isinstance(result, dict) and set(result) == {"status", "requests", "services", "account_email"}
                     and result["status"] in {"available", "partial"}
                     and (result["requests"] is None or _valid_request_metrics(result["requests"]))
                     and isinstance(result["services"], dict) and set(result["services"]) == {"java", "python"}
                     and all(_valid_service_snapshot(service) for service in result["services"].values())
                     and _valid_email_snapshot(result["account_email"]))
        except (KeyError, TypeError, ValueError):
            valid = False
        if not valid:
            raise PlatformAuthError("INVALID_PLATFORM_RESPONSE", "平台观测服务响应无效。", 502)
        return result

    def task_command(self, *, session_token: str | None, operation: str, task_key: str | None,
                     idempotency_key: str | None, trace_id: str | None = None) -> dict[str, Any]:
        if not session_token:
            raise PlatformAuthError("SESSION_EXPIRED", "登录状态已过期，请重新登录。", 401)
        try:
            response = httpx.post(f"{self.base_url}/internal/v1/tasks/admin/command", headers={
                "X-Internal-Service-Token": self.internal_token, "X-Trace-Id": trace_id or uuid4().hex,
            }, json={"session_token": session_token, "operation": operation, "task_key": task_key,
                     "idempotency_key": idempotency_key}, timeout=self.timeout_seconds)
        except httpx.HTTPError as error:
            raise PlatformAuthUnavailableError() from error
        if response.status_code >= 500:
            raise PlatformAuthUnavailableError()
        try:
            result = response.json()
        except ValueError as error:
            raise PlatformAuthError("INVALID_PLATFORM_RESPONSE", "平台任务服务响应无效。", 502) from error
        if response.is_error:
            detail = result.get("message") or result.get("detail") if isinstance(result, dict) else None
            raise PlatformAuthError("TASK_COMMAND_ERROR", str(detail or "任务操作失败。"), response.status_code)
        if not isinstance(result, dict) or set(result) != {"task"} or not _valid_task_projection(result["task"], admin=operation == "retry"):
            raise PlatformAuthError("INVALID_PLATFORM_RESPONSE", "平台任务服务响应无效。", 502)
        if operation == "retry" and result["task"]["task_key"] != task_key:
            raise PlatformAuthError("INVALID_PLATFORM_RESPONSE", "平台任务服务响应无效。", 502)
        return result

    def task_query(self, resource: str, *, session_token: str | None, trace_id: str | None = None,
                   **filters: Any) -> dict[str, Any]:
        """Task reads require real sessions; Java failures never query Python's repository."""
        if resource not in {"admin", "me"}:
            raise ValueError("Unknown task read resource")
        if not session_token:
            raise PlatformAuthError("SESSION_EXPIRED", "登录状态已过期，请重新登录。", 401)
        try:
            response = httpx.post(f"{self.base_url}/internal/v1/tasks/{resource}/query", headers={
                "X-Internal-Service-Token": self.internal_token,
                "X-Trace-Id": trace_id or f"platform-task-{uuid4().hex}",
            }, json={"session_token": session_token, **filters}, timeout=self.timeout_seconds)
        except httpx.HTTPError as error:
            raise PlatformAuthUnavailableError() from error
        if response.status_code >= 500:
            raise PlatformAuthUnavailableError()
        try:
            result = response.json()
        except ValueError as error:
            raise PlatformAuthError("INVALID_PLATFORM_RESPONSE", "平台任务服务响应无效。", _platform_response_error_status(response)) from error
        if not isinstance(result, dict):
            raise PlatformAuthError("INVALID_PLATFORM_RESPONSE", "平台任务服务响应无效。", _platform_response_error_status(response))
        if response.is_error:
            raise PlatformAuthError(str(result.get("code") or "PLATFORM_AUTH_ERROR"),
                                    str(result.get("message") or result.get("detail") or "任务查询失败。"), response.status_code)
        try:
            if resource == "me":
                valid = (_valid_task_projection(result["task"], admin=False)
                         and result["task"]["task_key"] == filters["task_key"])
            else:
                valid = (type(result["total"]) is int and result["total"] >= 0
                         and type(result["limit"]) is int and result["limit"] == filters["limit"]
                         and type(result["offset"]) is int and result["offset"] == filters["offset"]
                         and result["page_size"] == 100 and result["max_pages"] == 5 and isinstance(result["tasks"], list)
                         and len(result["tasks"]) <= min(filters["limit"], max(0, result["total"] - filters["offset"]))
                         and all(_valid_task_projection(row, admin=True)
                                 and (filters.get("account_id") is None or row["account_id"] == filters["account_id"])
                                 for row in result["tasks"]))
            if not valid:
                raise ValueError("Invalid task projection")
        except (KeyError, TypeError, ValueError) as error:
            raise PlatformAuthError("INVALID_PLATFORM_RESPONSE", "平台任务服务响应无效。", 502) from error
        return result

    def admin_tool_traces(self, resource: str, *, session_token: str | None, trace_id: str | None = None,
                          **filters: Any) -> dict[str, Any]:
        """Read Java-owned projections without local trace fallback or cache."""
        if resource not in {"list", "detail"}:
            raise ValueError("Unknown tool trace resource")
        if not session_token:
            raise PlatformAuthError("SESSION_EXPIRED", "登录状态已过期，请重新登录。", 401)
        path = "" if resource == "list" else "/detail"
        try:
            response = httpx.post(f"{self.base_url}/internal/v1/admin/tools/traces{path}", headers={
                "X-Internal-Service-Token": self.internal_token,
                "X-Trace-Id": trace_id or f"platform-tools-{uuid4().hex}",
            }, json={"session_token": session_token, **filters}, timeout=self.timeout_seconds)
        except httpx.HTTPError as error:
            raise PlatformAuthUnavailableError() from error
        if response.status_code >= 500:
            raise PlatformAuthUnavailableError()
        try:
            result = response.json()
        except ValueError as error:
            raise PlatformAuthError("INVALID_PLATFORM_RESPONSE", "平台工具记录响应无效。", _platform_response_error_status(response)) from error
        if not isinstance(result, dict):
            raise PlatformAuthError("INVALID_PLATFORM_RESPONSE", "平台工具记录响应无效。", _platform_response_error_status(response))
        if response.is_error:
            raise PlatformAuthError(str(result.get("code") or "PLATFORM_AUTH_ERROR"),
                                    str(result.get("message") or result.get("detail") or "工具记录查询失败。"), response.status_code)
        try:
            if resource == "detail":
                valid = (_valid_tool_trace(result["trace"], detail=True)
                         and result["trace"]["root_request_id"] == filters["root_request_id"])
            else:
                valid = (type(result["total"]) is int and result["total"] >= 0
                         and type(result["limit"]) is int and result["limit"] == filters["limit"]
                         and type(result["offset"]) is int and result["offset"] == filters["offset"]
                         and result["page_size"] == 100 and result["max_pages"] == 5
                         and isinstance(result["traces"], list)
                         and len(result["traces"]) <= min(filters["limit"], max(0, result["total"] - filters["offset"]))
                         and all(_valid_tool_trace(row, detail=False)
                                 and (filters.get("account_id") is None or row["account_id"] == filters["account_id"])
                                 for row in result["traces"]))
            if not valid:
                raise ValueError("Invalid tool trace projection")
        except (KeyError, TypeError, ValueError) as error:
            raise PlatformAuthError("INVALID_PLATFORM_RESPONSE", "平台工具记录响应无效。", 502) from error
        return result

    def admin_usage(self, resource: str, *, session_token: str | None, trace_id: str | None = None,
                    **filters: Any) -> dict[str, Any]:
        """Forward administrator reads; never consult Python's summary cache in Java mode."""
        if resource not in {"summary", "events"}:
            raise ValueError("Unknown usage resource")
        if not session_token:
            raise PlatformAuthError("SESSION_EXPIRED", "登录状态已过期，请重新登录。", 401)
        try:
            response = httpx.post(f"{self.base_url}/internal/v1/admin/usage/{resource}", headers={
                "X-Internal-Service-Token": self.internal_token,
                "X-Trace-Id": trace_id or f"platform-usage-{uuid4().hex}",
            }, json={"session_token": session_token, **filters}, timeout=self.timeout_seconds)
        except httpx.HTTPError as error:
            raise PlatformAuthUnavailableError() from error
        if response.status_code >= 500:
            raise PlatformAuthUnavailableError()
        try:
            result = response.json()
        except ValueError as error:
            raise PlatformAuthError("INVALID_PLATFORM_RESPONSE", "平台用量服务响应无效。", _platform_response_error_status(response)) from error
        if not isinstance(result, dict):
            raise PlatformAuthError("INVALID_PLATFORM_RESPONSE", "平台用量服务响应无效。", _platform_response_error_status(response))
        if response.is_error:
            raise PlatformAuthError(str(result.get("code") or "PLATFORM_AUTH_ERROR"),
                                    str(result.get("message") or result.get("detail") or "平台用量查询失败。"), response.status_code)
        try:
            if result["page_size"] != 100 or result["max_pages"] != 5:
                raise ValueError("Invalid retention window")
            if resource == "summary":
                valid = (isinstance(result["summary"], dict) and isinstance(result["by_account"], list)
                         and isinstance(result["tool_calls_by_account"], list) and isinstance(result["billing"], dict)
                         and isinstance(result["billing"]["summary"], dict) and isinstance(result["billing"]["settings"], dict)
                         and isinstance(result["billing"]["by_account"], list))
            else:
                valid = (isinstance(result["events"], list) and len(result["events"]) <= filters["limit"]
                         and type(result["total"]) is int and result["total"] >= len(result["events"])
                         and result["limit"] == filters["limit"] and result["offset"] == filters["offset"]
                         and all(isinstance(row, dict) and type(row["id"]) is int and row["id"] > 0
                                 and type(row["account_id"]) is int and isinstance(row["raw_usage"], dict)
                                 and isinstance(row["billable"], bool) and isinstance(row["created_at"], str)
                                 for row in result["events"]))
            if not valid:
                raise ValueError("Invalid usage response")
        except (KeyError, TypeError, ValueError) as error:
            raise PlatformAuthError("INVALID_PLATFORM_RESPONSE", "平台用量服务响应无效。", 502) from error
        return result

    def audit_events(self, *, session_token: str | None, limit: int, offset: int,
                    trace_id: str | None = None) -> dict[str, Any]:
        if not session_token:
            raise PlatformAuthError("SESSION_EXPIRED", "登录状态已过期，请重新登录。", 401)
        try:
            response = httpx.post(f"{self.base_url}/internal/v1/auth/admin/audit/events", headers={
                "X-Internal-Service-Token": self.internal_token,
                "X-Trace-Id": trace_id or f"platform-audit-{uuid4().hex}",
            }, json={"session_token": session_token, "limit": limit, "offset": offset}, timeout=self.timeout_seconds)
        except httpx.HTTPError as error:
            raise PlatformAuthUnavailableError() from error
        if response.status_code >= 500:
            raise PlatformAuthUnavailableError()
        try:
            result = response.json()
        except ValueError as error:
            raise PlatformAuthError("INVALID_PLATFORM_RESPONSE", "平台审计服务响应无效。", _platform_response_error_status(response)) from error
        if not isinstance(result, dict):
            raise PlatformAuthError("INVALID_PLATFORM_RESPONSE", "平台审计服务响应无效。", _platform_response_error_status(response))
        if response.is_error:
            raise PlatformAuthError(str(result.get("code") or "PLATFORM_AUTH_ERROR"),
                                    str(result.get("message") or "平台审计查询失败。"), response.status_code)
        try:
            if (not isinstance(result["events"], list) or len(result["events"]) > limit
                    or result["limit"] != limit or result["offset"] != offset
                    or any(not isinstance(row, dict) or type(row["id"]) is not int or row["id"] <= 0
                           or not isinstance(row["details"], dict) or not isinstance(row["created_at"], str)
                           for row in result["events"])):
                raise ValueError("Invalid audit page")
        except (KeyError, TypeError, ValueError) as error:
            raise PlatformAuthError("INVALID_PLATFORM_RESPONSE", "平台审计服务响应无效。", 502) from error
        return result

    def export_account(self, session_token: str) -> dict[str, Any]:
        """Forward the real session; never select an owner or fall back to local SQL."""
        try:
            with httpx.Client(trust_env=False, follow_redirects=False, timeout=35) as client:
                with client.stream("POST", f"{self.base_url}/internal/v1/accounts/export",
                                   headers={"X-Internal-Service-Token": self.internal_token},
                                   json={"session_token": session_token}) as response:
                    if response.status_code != 200:
                        status, code, message = {
                            401: (401, "SESSION_EXPIRED", "登录状态已过期，请重新登录。"),
                            413: (413, "EXPORT_TOO_LARGE", "数据量超过同步导出限制，请联系管理员。"),
                        }.get(response.status_code, (503, "EXPORT_UNAVAILABLE", "账号导出暂时不可用，请稍后重试。"))
                        raise PlatformAuthError(code, message, status)
                    if response.headers.get("Content-Type", "").split(";", 1)[0].strip() != "application/json":
                        raise ValueError("Invalid export content type")
                    body = bytearray()
                    for chunk in response.iter_bytes(chunk_size=65536):
                        if len(body) + len(chunk) > MAX_EXPORT_BYTES:
                            raise ValueError("Oversized export")
                        body.extend(chunk)
                    return decode_export(bytes(body))
        except (httpx.HTTPError, ValueError, KeyError, TypeError, RecursionError):
            raise PlatformAuthError("EXPORT_UNAVAILABLE", "账号导出暂时不可用，请稍后重试。", 503) from None

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
        if response.status_code >= 500:
            raise PlatformAuthUnavailableError()
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
        if response.status_code >= 500:
            raise PlatformAuthUnavailableError()
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


def _valid_request_metrics(value: Any) -> bool:
    counts = ("total_requests", "in_flight_requests", "total_duration_ms", "error_requests", "max_duration_ms",
              "rate_limited_requests", "rate_limit_backend_errors", "csrf_rejected_requests", "exception_requests")
    fields = {*counts, "average_duration_ms", "started_at", "status_counts", "method_counts", "endpoint_counts", "outcome_counts", "recent_errors"}
    return (isinstance(value, dict) and set(value) == fields and isinstance(value["started_at"], str)
            and all(type(value.get(key)) in (int, float) and math.isfinite(value[key]) and value[key] >= 0 for key in (*counts, "average_duration_ms"))
            and all(isinstance(value.get(key), dict) and len(value[key]) <= 260
                    and all(isinstance(k, str) and len(k) <= 512 and type(v) is int and v >= 0 for k, v in value[key].items())
                    for key in ("status_counts", "method_counts", "endpoint_counts", "outcome_counts"))
            and isinstance(value.get("recent_errors"), list) and len(value["recent_errors"]) <= 20
            and all(isinstance(error, dict) and set(error) in (
                {"request_id", "method", "endpoint", "status_code", "outcome", "duration_ms", "created_at"},
                {"request_id", "method", "endpoint", "status_code", "outcome", "duration_ms", "created_at", "service"})
                and all(isinstance(error[k], str) and len(error[k]) <= 512 for k in ("request_id", "method", "endpoint", "outcome", "created_at"))
                and type(error["status_code"]) is int and 100 <= error["status_code"] <= 599
                and type(error["duration_ms"]) in (int, float) and math.isfinite(error["duration_ms"]) and error["duration_ms"] >= 0
                and ("service" not in error or error["service"] in {"java", "python"}) for error in value["recent_errors"]))


def _valid_service_snapshot(value: Any) -> bool:
    if not isinstance(value, dict) or set(value) != {"status", "api", "internal", "observed_at"}:
        return False
    if value["status"] == "available":
        return all(_valid_request_metrics(value[key]) for key in ("api", "internal")) and isinstance(value["observed_at"], str)
    return value["status"] in {"unavailable", "not_configured"} and all(value[key] is None for key in ("api", "internal", "observed_at"))


def _valid_email_snapshot(value: Any) -> bool:
    fields = {"id", "account_id", "purpose", "recipient_email", "status", "attempt_count", "max_attempts",
              "last_error_type", "last_error_summary", "created_at", "updated_at", "owner"}
    statuses = {"pending", "sending", "retrying", "sent", "failed", "cancelled"}
    if not isinstance(value, dict) or set(value) != {"status", "summary", "records"} or value["status"] not in {"available", "partial", "unavailable"}:
        return False
    summary = value["summary"]
    if summary is not None and (not isinstance(summary, dict) or not set(summary) <= statuses
                                or not all(type(n) is int and n >= 0 for n in summary.values())):
        return False
    return (isinstance(value["records"], list) and len(value["records"]) <= 20
            and all(isinstance(row, dict) and set(row) == fields and row["owner"] in {"java", "python"}
                    and row["purpose"] in {"verify_email", "reset_password", "change_email"} and row["status"] in statuses
                    and all(type(row[k]) is int and row[k] >= 0 for k in ("id", "account_id", "attempt_count", "max_attempts"))
                    and isinstance(row["recipient_email"], str) and "***" in row["recipient_email"] and len(row["recipient_email"]) <= 254
                    and all(row[k] is None or isinstance(row[k], str) for k in ("last_error_type", "last_error_summary"))
                    and all(isinstance(row[k], str) for k in ("created_at", "updated_at")) for row in value["records"]))


def _valid_task_projection(value: Any, *, admin: bool) -> bool:
    fields = {"task_key", "task_type", "status", "progress", "attempt", "max_attempts", "result", "error_summary",
              "created_at", "started_at", "finished_at", "updated_at"}
    if admin:
        fields.update({"account_id", "candidate_id"})
    if not isinstance(value, dict) or set(value) != fields:
        return False
    return (all(isinstance(value[key], str) for key in ("task_key", "task_type", "created_at", "updated_at"))
            and value["status"] in {"queued", "running", "succeeded", "failed", "cancelled"}
            and type(value["progress"]) is int and 0 <= value["progress"] <= 100
            and type(value["attempt"]) is int and value["attempt"] >= 0
            and type(value["max_attempts"]) is int and value["max_attempts"] > 0
            and isinstance(value["result"], dict)
            and all(value[key] is None or isinstance(value[key], str) for key in ("error_summary", "started_at", "finished_at"))
            and (not admin or (type(value["account_id"]) is int and value["account_id"] > 0
                               and (value["candidate_id"] is None or type(value["candidate_id"]) is int))))


def _valid_tool_trace(value: Any, *, detail: bool) -> bool:
    if not isinstance(value, dict):
        return False
    fields = {"id", "account_id", "candidate_id", "session_id", "root_request_id", "title", "status", "source",
              "step_count", "attempt_count", "last_step_name", "last_error_summary", "created_at", "started_at", "finished_at", "updated_at"}
    if detail:
        fields.add("trace")
    if set(value) != fields:
        return False
    return (all(type(value[key]) is int and value[key] > 0 for key in ("id", "account_id"))
            and (value["candidate_id"] is None or type(value["candidate_id"]) is int)
            and all(isinstance(value[key], str) for key in ("root_request_id", "title", "source", "created_at", "updated_at"))
            and value["status"] in {"running", "waiting_confirmation", "completed", "failed", "cancelled"}
            and all(type(value[key]) is int and value[key] >= 0 for key in ("step_count", "attempt_count"))
            and all(value[key] is None or isinstance(value[key], str)
                    for key in ("session_id", "last_step_name", "last_error_summary", "started_at", "finished_at"))
            and (not detail or isinstance(value["trace"], dict)))


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
