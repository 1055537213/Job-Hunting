"""Web 边缘安全、限流和请求观测中间件。"""

from __future__ import annotations

import json
import logging
import secrets
import threading
import time
import uuid
from collections import defaultdict, deque
from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import FastAPI, Request, Response
from starlette.responses import JSONResponse

from .config import WebSecuritySettings

logger = logging.getLogger("job_hunting_agent.web.access")

REQUEST_ID_HEADER = "X-Request-ID"
CSRF_COOKIE_NAME = "job_agent_csrf"
CSRF_HEADER_NAME = "X-CSRF-Token"
CSRF_COOKIE_MAX_AGE_SECONDS = 30 * 24 * 60 * 60
SAFE_METHODS = {"GET", "HEAD", "OPTIONS", "TRACE"}
CSRF_EXEMPT_PATHS = {"/api/auth/login", "/api/auth/register"}


def new_csrf_token() -> str:
    """生成浏览器可读、仅用于 CSRF 双提交校验的随机 token。"""

    return secrets.token_urlsafe(32)


def set_csrf_cookie(response: Response, token: str, *, secure: bool) -> None:
    """写入非 HttpOnly CSRF cookie，前端会把同值放入请求头。"""

    response.set_cookie(
        CSRF_COOKIE_NAME,
        token,
        max_age=CSRF_COOKIE_MAX_AGE_SECONDS,
        httponly=False,
        secure=secure,
        samesite="lax",
        path="/",
    )


def delete_csrf_cookie(response: Response) -> None:
    """删除 CSRF cookie。"""

    response.delete_cookie(CSRF_COOKIE_NAME, path="/")


def install_web_hardening(
    web_app: FastAPI,
    *,
    settings: WebSecuritySettings,
    session_cookie_name: str,
) -> None:
    """安装 Web 最外层安全与观测中间件。"""

    limiter = InMemoryRateLimiter(settings)

    @web_app.middleware("http")
    async def hardening_middleware(request: Request, call_next: Callable[[Request], Awaitable[Response]]):
        request_id = normalize_request_id(request.headers.get(REQUEST_ID_HEADER))
        request.state.request_id = request_id
        started_at = time.monotonic()
        response: Response | None = None
        status_code = 500
        try:
            retry_after = limiter.check(
                client_id=client_identity(request),
                group=rate_limit_group(request),
            )
            if retry_after is not None:
                response = JSONResponse(
                    {"detail": "请求过于频繁，请稍后再试。"},
                    status_code=429,
                    headers={"Retry-After": str(retry_after)},
                )
            elif csrf_failure := validate_csrf_request(
                request,
                settings=settings,
                session_cookie_name=session_cookie_name,
            ):
                response = JSONResponse({"detail": csrf_failure}, status_code=403)
            else:
                response = await call_next(request)
            status_code = response.status_code
            return response
        except Exception:
            status_code = 500
            raise
        finally:
            if response is not None:
                apply_request_headers(response, request_id, settings)
            log_access(request, status_code, started_at)


class InMemoryRateLimiter:
    """进程内滑动窗口限流器，适合单机早期部署和测试。"""

    def __init__(
        self,
        settings: WebSecuritySettings,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.settings = settings
        self.clock = clock
        self._lock = threading.Lock()
        self._buckets: dict[tuple[str, str], deque[float]] = defaultdict(deque)

    def check(self, *, client_id: str, group: str) -> int | None:
        """返回 None 表示放行；返回秒数表示应拒绝并告知 Retry-After。"""

        if not self.settings.rate_limit_enabled:
            return None
        limit = self._limit_for_group(group)
        window = float(self.settings.rate_limit_window_seconds)
        now = self.clock()
        key = (client_id, group)
        with self._lock:
            bucket = self._buckets[key]
            cutoff = now - window
            while bucket and bucket[0] <= cutoff:
                bucket.popleft()
            if len(bucket) >= limit:
                retry_after = max(1, int(round(window - (now - bucket[0]))))
                return retry_after
            bucket.append(now)
        return None

    def _limit_for_group(self, group: str) -> int:
        if group == "auth":
            return self.settings.rate_limit_auth_requests
        return self.settings.rate_limit_default_requests


def validate_csrf_request(
    request: Request,
    *,
    settings: WebSecuritySettings,
    session_cookie_name: str,
) -> str | None:
    """校验已登录浏览器的状态变更请求是否携带 CSRF token。"""

    if not settings.csrf_enabled:
        return None
    if request.method.upper() in SAFE_METHODS:
        return None
    if request.url.path in CSRF_EXEMPT_PATHS:
        return None
    if not request.cookies.get(session_cookie_name):
        return None
    cookie_token = request.cookies.get(CSRF_COOKIE_NAME, "")
    header_token = request.headers.get(CSRF_HEADER_NAME, "")
    if not cookie_token or not header_token:
        return "缺少 CSRF 校验信息，请刷新页面后重试。"
    if not secrets.compare_digest(cookie_token, header_token):
        return "CSRF 校验失败，请刷新页面后重试。"
    return None


def apply_request_headers(
    response: Response,
    request_id: str,
    settings: WebSecuritySettings,
) -> None:
    """给所有响应附加请求追踪和安全响应头。"""

    response.headers[REQUEST_ID_HEADER] = request_id
    if not settings.security_headers_enabled:
        return
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "same-origin")
    response.headers.setdefault(
        "Permissions-Policy",
        "camera=(), microphone=(), geolocation=()",
    )
    response.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; "
        "script-src 'self'; "
        "style-src 'self'; "
        "img-src 'self' data: blob:; "
        "connect-src 'self'; "
        "object-src 'none'; "
        "base-uri 'self'; "
        "frame-ancestors 'none'; "
        "form-action 'self'",
    )


def normalize_request_id(raw_value: str | None) -> str:
    """接受上游 request id 或生成新的本地 ID。"""

    value = (raw_value or "").strip()
    if 8 <= len(value) <= 128 and all(char.isalnum() or char in "._-" for char in value):
        return value
    return uuid.uuid4().hex


def rate_limit_group(request: Request) -> str:
    """将请求归入限流组。"""

    path = request.url.path
    if path in {"/api/auth/login", "/api/auth/register"}:
        return "auth"
    return "default"


def client_identity(request: Request) -> str:
    """返回不依赖可伪造代理头的本地客户端标识。"""

    return request.client.host if request.client and request.client.host else "unknown"


def log_access(request: Request, status_code: int, started_at: float) -> None:
    """输出不含请求正文的结构化访问日志。"""

    duration_ms = max(0, round((time.monotonic() - started_at) * 1000))
    account = getattr(request.state, "account", None)
    payload: dict[str, Any] = {
        "event": "http_request",
        "request_id": getattr(request.state, "request_id", ""),
        "method": request.method,
        "path": request.url.path,
        "status_code": status_code,
        "duration_ms": duration_ms,
    }
    if account is not None and getattr(account, "id", None) is not None:
        payload["account_id"] = int(account.id)
    logger.info(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
