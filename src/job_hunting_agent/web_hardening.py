"""Web 边缘安全、限流和请求观测中间件。"""

from __future__ import annotations

import logging
import secrets
import threading
import time
import uuid
from collections import defaultdict, deque
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

from fastapi import FastAPI, Request, Response
from starlette.responses import JSONResponse

from .config import WebSecuritySettings
from .rate_limiting import (
    RateLimitBackendUnavailable,
    RateLimiter,
    build_rate_limiter,
)

logger = logging.getLogger("job_hunting_agent.web.access")

REQUEST_ID_HEADER = "X-Request-ID"
CSRF_COOKIE_NAME = "job_agent_csrf"
CSRF_HEADER_NAME = "X-CSRF-Token"
CSRF_COOKIE_MAX_AGE_SECONDS = 30 * 24 * 60 * 60
SAFE_METHODS = {"GET", "HEAD", "OPTIONS", "TRACE"}
CSRF_EXEMPT_PATHS = {"/api/auth/login", "/api/auth/register"}
PROMETHEUS_CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"
RATE_LIMIT_FAIL_CLOSED_GROUPS = {"auth", "model", "upload", "admin", "write"}


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
    identity_resolver: Callable[[Request], str] | None = None,
    rate_limiter: RateLimiter | None = None,
) -> None:
    """安装 Web 最外层安全与观测中间件。"""

    limiter = rate_limiter or build_rate_limiter(settings)
    metrics = RequestMetrics()
    web_app.state.request_metrics = metrics
    web_app.state.rate_limiter = limiter

    @web_app.middleware("http")
    async def hardening_middleware(request: Request, call_next: Callable[[Request], Awaitable[Response]]):
        request_id = normalize_request_id(request.headers.get(REQUEST_ID_HEADER))
        request.state.request_id = request_id
        started_at = time.monotonic()
        metrics.mark_started()
        response: Response | None = None
        status_code = 500
        outcome = "handled"
        try:
            group = rate_limit_group(request)
            try:
                retry_after = await limiter.check(
                    client_id=(
                        resolve_client_identity(request, identity_resolver)
                        if settings.rate_limit_enabled
                        else client_identity(request)
                    ),
                    group=group,
                )
            except RateLimitBackendUnavailable:
                outcome = "rate_limit_backend_unavailable"
                logger.exception(
                    "Redis rate limiter unavailable; group=%s request_id=%s",
                    group,
                    request_id,
                )
                if group in RATE_LIMIT_FAIL_CLOSED_GROUPS:
                    response = JSONResponse(
                        {"detail": "请求保护服务暂时不可用，请稍后重试。"},
                        status_code=503,
                        headers={"Retry-After": "1"},
                    )
                    retry_after = None
                else:
                    response = None
                    retry_after = None
            if response is None and retry_after is not None:
                outcome = "rate_limited"
                response = JSONResponse(
                    {"detail": "请求过于频繁，请稍后再试。"},
                    status_code=429,
                    headers={"Retry-After": str(retry_after)},
                )
            elif response is None and (
                csrf_failure := validate_csrf_request(
                    request,
                    settings=settings,
                    session_cookie_name=session_cookie_name,
                )
            ):
                outcome = "csrf_rejected"
                response = JSONResponse({"detail": csrf_failure}, status_code=403)
            elif response is None:
                response = await call_next(request)
            status_code = response.status_code
            return response
        except Exception:  # noqa: BLE001 - 统一生成不泄露异常正文的 500 响应。
            status_code = 500
            outcome = "exception"
            logger.exception(
                "Unhandled web request",
                extra={
                    "event": "http_request_exception",
                    "request_id": request_id,
                    "method": request.method,
                    "path": route_template(request),
                    "status_code": status_code,
                    "outcome": outcome,
                },
            )
            response = JSONResponse(
                {"detail": "服务器内部错误。", "request_id": request_id},
                status_code=500,
            )
            return response
        finally:
            duration_ms = max(0, round((time.monotonic() - started_at) * 1000))
            if response is not None:
                apply_request_headers(response, request_id, settings)
            metrics.record(
                request=request,
                request_id=request_id,
                status_code=status_code,
                duration_ms=duration_ms,
                outcome=outcome,
            )
            log_access(request, status_code, duration_ms, outcome=outcome)


class RequestMetrics:
    """进程内请求指标聚合器，不保存请求正文或查询参数。"""

    def __init__(self) -> None:
        self.started_at = datetime.now(UTC).isoformat(timespec="seconds")
        self._lock = threading.Lock()
        self._in_flight = 0
        self._total_requests = 0
        self._total_duration_ms = 0
        self._max_duration_ms = 0
        self._status_counts: dict[str, int] = defaultdict(int)
        self._method_counts: dict[str, int] = defaultdict(int)
        self._endpoint_counts: dict[str, int] = defaultdict(int)
        self._outcome_counts: dict[str, int] = defaultdict(int)
        self._recent_errors: deque[dict[str, object]] = deque(maxlen=20)

    def mark_started(self) -> None:
        """记录当前进程内正在处理的请求数量。"""

        with self._lock:
            self._in_flight += 1

    def record(
        self,
        *,
        request: Request,
        request_id: str,
        status_code: int,
        duration_ms: int,
        outcome: str,
    ) -> None:
        """写入一次请求的低敏指标。"""

        status_family = f"{max(1, min(status_code // 100, 5))}xx"
        method = request.method.upper()
        endpoint = route_template(request)
        with self._lock:
            self._in_flight = max(0, self._in_flight - 1)
            self._total_requests += 1
            self._total_duration_ms += duration_ms
            self._max_duration_ms = max(self._max_duration_ms, duration_ms)
            self._status_counts[status_family] += 1
            self._method_counts[method] += 1
            self._endpoint_counts[endpoint] += 1
            self._outcome_counts[outcome] += 1
            if status_code >= 400 or outcome in {"rate_limited", "csrf_rejected", "exception"}:
                self._recent_errors.appendleft(
                    {
                        "request_id": request_id,
                        "method": method,
                        "endpoint": endpoint,
                        "status_code": status_code,
                        "outcome": outcome,
                        "duration_ms": duration_ms,
                        "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
                    }
                )

    def snapshot(self) -> dict[str, object]:
        """返回管理员可查看的请求指标快照。"""

        with self._lock:
            total_requests = self._total_requests
            average_duration_ms = (
                round(self._total_duration_ms / total_requests, 2)
                if total_requests
                else 0
            )
            error_requests = sum(
                count
                for family, count in self._status_counts.items()
                if family in {"4xx", "5xx"}
            )
            return {
                "started_at": self.started_at,
                "in_flight_requests": self._in_flight,
                "total_requests": total_requests,
                "total_duration_ms": self._total_duration_ms,
                "error_requests": error_requests,
                "average_duration_ms": average_duration_ms,
                "max_duration_ms": self._max_duration_ms,
                "rate_limited_requests": self._outcome_counts.get("rate_limited", 0),
                "rate_limit_backend_errors": self._outcome_counts.get(
                    "rate_limit_backend_unavailable",
                    0,
                ),
                "csrf_rejected_requests": self._outcome_counts.get("csrf_rejected", 0),
                "exception_requests": self._outcome_counts.get("exception", 0),
                "status_counts": dict(sorted(self._status_counts.items())),
                "method_counts": dict(sorted(self._method_counts.items())),
                "endpoint_counts": dict(
                    sorted(
                        self._endpoint_counts.items(),
                        key=lambda item: (-item[1], item[0]),
                    )[:20]
                ),
                "outcome_counts": dict(sorted(self._outcome_counts.items())),
                "recent_errors": list(self._recent_errors),
            }


def format_prometheus_request_metrics(
    snapshot: dict[str, object],
    concurrency_snapshot: dict[str, object] | None = None,
    intent_routing_snapshot: dict[str, object] | None = None,
) -> str:
    """把低敏请求、共享租约和意图路由快照导出为 Prometheus 文本。"""

    total_requests = int(snapshot.get("total_requests", 0))
    total_duration_seconds = float(snapshot.get("total_duration_ms", 0)) / 1000
    lines = [
        "# HELP job_agent_http_requests_total Total HTTP requests handled by this Web process.",
        "# TYPE job_agent_http_requests_total counter",
        f"job_agent_http_requests_total {total_requests}",
        "# HELP job_agent_http_requests_in_flight HTTP requests currently being handled.",
        "# TYPE job_agent_http_requests_in_flight gauge",
        f'job_agent_http_requests_in_flight {int(snapshot.get("in_flight_requests", 0))}',
        "# HELP job_agent_http_request_duration_seconds Request duration for this Web process.",
        "# TYPE job_agent_http_request_duration_seconds summary",
        f"job_agent_http_request_duration_seconds_sum {_prometheus_number(total_duration_seconds)}",
        f"job_agent_http_request_duration_seconds_count {total_requests}",
        "# HELP job_agent_http_request_duration_max_seconds Maximum observed request duration.",
        "# TYPE job_agent_http_request_duration_max_seconds gauge",
        f'job_agent_http_request_duration_max_seconds {_prometheus_number(float(snapshot.get("max_duration_ms", 0)) / 1000)}',
    ]
    _append_labeled_counter(
        lines,
        name="job_agent_http_responses_total",
        help_text="HTTP responses grouped by status family.",
        label_name="status_family",
        values=snapshot.get("status_counts"),
    )
    _append_labeled_counter(
        lines,
        name="job_agent_http_requests_by_method_total",
        help_text="HTTP requests grouped by method.",
        label_name="method",
        values=snapshot.get("method_counts"),
    )
    _append_labeled_counter(
        lines,
        name="job_agent_http_endpoint_requests_total",
        help_text="HTTP requests grouped by low-cardinality route template.",
        label_name="endpoint",
        values=snapshot.get("endpoint_counts"),
    )
    _append_labeled_counter(
        lines,
        name="job_agent_http_outcomes_total",
        help_text="HTTP requests grouped by middleware outcome.",
        label_name="outcome",
        values=snapshot.get("outcome_counts"),
    )
    lines.extend(
        [
            "# HELP job_agent_security_rejections_total Requests rejected by an application security control.",
            "# TYPE job_agent_security_rejections_total counter",
            f'job_agent_security_rejections_total{{reason="rate_limit"}} {int(snapshot.get("rate_limited_requests", 0))}',
            f'job_agent_security_rejections_total{{reason="csrf"}} {int(snapshot.get("csrf_rejected_requests", 0))}',
            "# HELP job_agent_rate_limit_backend_errors_total Requests affected by an unavailable rate-limit backend.",
            "# TYPE job_agent_rate_limit_backend_errors_total counter",
            f'job_agent_rate_limit_backend_errors_total {int(snapshot.get("rate_limit_backend_errors", 0))}',
        ]
    )
    _append_concurrency_metrics(lines, concurrency_snapshot)
    _append_intent_routing_metrics(lines, intent_routing_snapshot)
    return "\n".join(lines) + "\n"


def _append_concurrency_metrics(
    lines: list[str],
    snapshot: dict[str, object] | None,
) -> None:
    """追加模型/截图租约的固定资源标签指标。"""

    lines.extend(
        [
            "# HELP job_agent_concurrency_leases_acquired_total Shared lease acquisitions.",
            "# TYPE job_agent_concurrency_leases_acquired_total counter",
            "# HELP job_agent_concurrency_leases_rejected_total Shared lease requests rejected by capacity.",
            "# TYPE job_agent_concurrency_leases_rejected_total counter",
            "# HELP job_agent_concurrency_backend_errors_total Shared lease backend failures.",
            "# TYPE job_agent_concurrency_backend_errors_total counter",
            "# HELP job_agent_concurrency_release_errors_total Shared lease release failures.",
            "# TYPE job_agent_concurrency_release_errors_total counter",
            "# HELP job_agent_concurrency_leases_in_flight Shared leases currently held by this Web process.",
            "# TYPE job_agent_concurrency_leases_in_flight gauge",
        ]
    )
    resources: object = snapshot.get("resources") if isinstance(snapshot, dict) else None
    if not isinstance(resources, dict):
        return
    for resource, raw_values in sorted(resources.items()):
        if not isinstance(raw_values, dict):
            continue
        label = _escape_prometheus_label(str(resource))
        lines.extend(
            [
                f'job_agent_concurrency_leases_acquired_total{{resource="{label}"}} {int(raw_values.get("acquired", 0))}',
                f'job_agent_concurrency_leases_rejected_total{{resource="{label}"}} {int(raw_values.get("rejected", 0))}',
                f'job_agent_concurrency_backend_errors_total{{resource="{label}"}} {int(raw_values.get("backend_errors", 0))}',
                f'job_agent_concurrency_release_errors_total{{resource="{label}"}} {int(raw_values.get("release_errors", 0))}',
                f'job_agent_concurrency_leases_in_flight{{resource="{label}"}} {int(raw_values.get("in_flight", 0))}',
            ]
        )


def _append_intent_routing_metrics(
    lines: list[str],
    snapshot: dict[str, object] | None,
) -> None:
    """追加低基数路由计数和小模型判断耗时直方图。"""

    values = snapshot if isinstance(snapshot, dict) else {}
    lines.extend(
        [
            "# HELP job_agent_intent_router_direct_total Agent turns completed through a direct read-only route.",
            "# TYPE job_agent_intent_router_direct_total counter",
            f'job_agent_intent_router_direct_total {int(values.get("direct_total", 0))}',
            "# HELP job_agent_intent_router_fallback_total Agent turns that continued through the main Agent path.",
            "# TYPE job_agent_intent_router_fallback_total counter",
            f'job_agent_intent_router_fallback_total {int(values.get("fallback_total", 0))}',
            "# HELP job_agent_intent_router_timeouts_total Router model decisions stopped by the total deadline.",
            "# TYPE job_agent_intent_router_timeouts_total counter",
            f'job_agent_intent_router_timeouts_total {int(values.get("timeout_total", 0))}',
        ]
    )
    _append_labeled_counter(
        lines,
        name="job_agent_intent_router_fallback_reasons_total",
        help_text="Main Agent fallbacks grouped by a fixed low-cardinality reason.",
        label_name="reason",
        values=values.get("fallback_reason_counts"),
    )
    histogram_name = "job_agent_intent_router_model_duration_seconds"
    lines.extend(
        [
            f"# HELP {histogram_name} End-to-end lightweight router model decision duration.",
            f"# TYPE {histogram_name} histogram",
        ]
    )
    raw_buckets = values.get("latency_bucket_counts_ms")
    if isinstance(raw_buckets, dict):
        numeric_buckets: list[tuple[int, int]] = []
        for raw_boundary, raw_count in raw_buckets.items():
            try:
                numeric_buckets.append((int(raw_boundary), int(raw_count)))
            except (TypeError, ValueError):
                continue
        for boundary_ms, count in sorted(numeric_buckets):
            boundary_seconds = _prometheus_number(boundary_ms / 1000)
            lines.append(f'{histogram_name}_bucket{{le="{boundary_seconds}"}} {count}')
    latency_count = int(values.get("latency_count", 0))
    latency_sum_seconds = float(values.get("latency_sum_ms", 0)) / 1000
    lines.extend(
        [
            f'{histogram_name}_bucket{{le="+Inf"}} {latency_count}',
            f"{histogram_name}_sum {_prometheus_number(latency_sum_seconds)}",
            f"{histogram_name}_count {latency_count}",
        ]
    )


def _append_labeled_counter(
    lines: list[str],
    *,
    name: str,
    help_text: str,
    label_name: str,
    values: object,
) -> None:
    lines.extend((f"# HELP {name} {help_text}", f"# TYPE {name} counter"))
    if not isinstance(values, dict):
        return
    for label_value, count in values.items():
        escaped = _escape_prometheus_label(str(label_value))
        lines.append(f'{name}{{{label_name}="{escaped}"}} {int(count)}')


def _escape_prometheus_label(value: str) -> str:
    return value.replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def _prometheus_number(value: float) -> str:
    return format(value, ".12g")


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
        # Vue global build compiles the in-page template at mount time. Until
        # the frontend has a build step with precompiled templates, CSP must
        # allow that runtime compiler.
        "script-src 'self' 'unsafe-eval'; "
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
    if path.startswith("/api/auth/"):
        return "auth"
    if path.startswith("/api/admin/"):
        return "admin"
    if path in {
        "/api/jobs/screenshots",
        "/api/projects/local",
        "/api/projects/github",
        "/api/resumes/upload",
    }:
        return "upload"
    if (
        path in {"/api/chat", "/api/chat/stream", "/api/rag/search"}
        or path.startswith("/api/matches/")
        or (path.startswith("/api/resumes/") and path.endswith("/tailor"))
    ):
        return "model"
    if request.method.upper() not in SAFE_METHODS:
        return "write"
    return "default"


def route_template(request: Request) -> str:
    """返回低基数 endpoint 标签，避免把用户输入或 ID 写进指标。"""

    route = request.scope.get("route")
    path = getattr(route, "path", None)
    if isinstance(path, str) and path:
        return path
    raw_path = request.url.path
    if raw_path.startswith("/static/"):
        return "/static/*"
    if raw_path.startswith("/api/admin/"):
        return "/api/admin/*"
    if raw_path.startswith("/api/"):
        parts = [part for part in raw_path.split("/") if part]
        return "/" + "/".join(parts[:2]) + ("/*" if len(parts) > 2 else "")
    return raw_path or "/"


def client_identity(request: Request) -> str:
    """返回不依赖可伪造代理头的本地客户端标识。"""

    host = request.client.host if request.client and request.client.host else "unknown"
    return f"ip:{host}"


def resolve_client_identity(
    request: Request,
    identity_resolver: Callable[[Request], str] | None,
) -> str:
    """优先使用已认证账号标识，无法解析时退回可信网络来源。"""

    if identity_resolver is None:
        return client_identity(request)
    resolved = identity_resolver(request).strip()
    return resolved or client_identity(request)


def log_access(
    request: Request,
    status_code: int,
    duration_ms: int,
    *,
    outcome: str,
) -> None:
    """输出不含请求正文的结构化访问日志。"""

    account = getattr(request.state, "account", None)
    payload: dict[str, Any] = {
        "event": "http_request",
        "request_id": getattr(request.state, "request_id", ""),
        "method": request.method,
        "path": route_template(request),
        "status_code": status_code,
        "duration_ms": duration_ms,
        "outcome": outcome,
    }
    if account is not None and getattr(account, "id", None) is not None:
        payload["account_id"] = int(account.id)
    logger.info("HTTP request completed", extra=payload)
