"""低敏结构化日志与 OpenTelemetry Trace 运行时。"""

from __future__ import annotations

import atexit
import json
import logging
import re
import sys
import threading
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any, AsyncIterator

from fastapi import FastAPI, Request, Response

from .config import ObservabilitySettings

_TRACE_LOCK = threading.Lock()
_TRACE_PROVIDER: Any | None = None
_INSTRUMENTATION_INSTALLED = False

_EXTRA_FIELDS = (
    "event",
    "request_id",
    "method",
    "path",
    "route",
    "status_code",
    "duration_ms",
    "outcome",
    "account_id",
    "task_id",
    "task_name",
)
_URL_CREDENTIALS = re.compile(r"(://[^:/\s]+:)[^@/\s]+(@)")
_BEARER_TOKEN = re.compile(r"(?i)(\bBearer\s+)[A-Za-z0-9._~+/=-]+")
_ASSIGNED_SECRET = re.compile(
    r"(?i)(\b(?:api[_-]?key|authorization|password|secret|access[_-]?token|"
    r"refresh[_-]?token)\s*[:=]\s*)(?!\*\*\*)[^\s,;]+"
)


def redact_log_text(value: str) -> str:
    """遮盖常见凭据形式，不改写普通的 token 用量等运维文本。"""

    redacted = _URL_CREDENTIALS.sub(r"\1***\2", value)
    redacted = _BEARER_TOKEN.sub(r"\1***", redacted)
    return _ASSIGNED_SECRET.sub(r"\1***", redacted)


def current_trace_ids() -> tuple[str | None, str | None]:
    """返回当前有效 span 的十六进制标识；未启用 OTel 时返回空值。"""

    try:
        from opentelemetry import trace

        context = trace.get_current_span().get_span_context()
    except (ImportError, AttributeError):
        return None, None
    if not context.is_valid:
        return None, None
    return f"{context.trace_id:032x}", f"{context.span_id:016x}"


class JsonLogFormatter(logging.Formatter):
    """把应用和依赖日志统一为一行 JSON，并关联当前 Trace。"""

    def __init__(self, *, service_name: str, environment: str) -> None:
        super().__init__()
        self.service_name = service_name
        self.environment = environment

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "timestamp": datetime.now(UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "service": self.service_name,
            "environment": self.environment,
            "message": redact_log_text(record.getMessage()),
        }
        for field in _EXTRA_FIELDS:
            if hasattr(record, field):
                value = getattr(record, field)
                if value is not None and value != "":
                    payload[field] = value
        trace_id, span_id = current_trace_ids()
        if trace_id:
            payload["trace_id"] = trace_id
        if span_id:
            payload["span_id"] = span_id
        if record.exc_info:
            payload["exception_type"] = record.exc_info[0].__name__
            payload["exception_frames"] = [
                {
                    "file": frame.filename.replace("\\", "/").rsplit("/", 1)[-1],
                    "line": frame.lineno,
                    "function": frame.name,
                }
                for frame in _exception_frames(record.exc_info[2])
            ]
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def configure_logging(settings: ObservabilitySettings, *, service_name: str) -> None:
    """配置标准输出日志；允许多次调用以支持 Uvicorn reload 子进程。"""

    handler = logging.StreamHandler(sys.stdout)
    if settings.log_format == "json":
        handler.setFormatter(
            JsonLogFormatter(
                service_name=service_name,
                environment=settings.environment,
            )
        )
    else:
        handler.setFormatter(
            logging.Formatter(
                "%(asctime)s %(levelname)s %(name)s: %(message)s",
                datefmt="%Y-%m-%d %H:%M:%S",
            )
        )
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(settings.log_level)
    for logger_name in ("uvicorn", "uvicorn.error", "uvicorn.access", "celery"):
        child = logging.getLogger(logger_name)
        child.handlers.clear()
        child.propagate = True


def configure_tracing(
    settings: ObservabilitySettings,
    *,
    service_name: str,
) -> bool:
    """初始化 OTLP Trace 导出和 Celery/SQLAlchemy 插件。

    BatchSpanProcessor 在后台发送数据；导出失败只记录 OTel 自身错误，不进入
    业务异常路径。HTTP span 由本模块单独创建，确保不采集 URL 查询参数和正文。
    """

    global _INSTRUMENTATION_INSTALLED, _TRACE_PROVIDER
    if not settings.tracing_enabled:
        return False
    with _TRACE_LOCK:
        if _TRACE_PROVIDER is None:
            from opentelemetry import trace
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
                OTLPSpanExporter,
            )
            from opentelemetry.sdk.resources import Resource
            from opentelemetry.sdk.trace import TracerProvider
            from opentelemetry.sdk.trace.export import BatchSpanProcessor
            from opentelemetry.sdk.trace.sampling import ParentBased, TraceIdRatioBased

            provider = TracerProvider(
                resource=Resource.create(
                    {
                        "service.name": service_name,
                        "service.version": "0.1.0",
                        "deployment.environment.name": settings.environment,
                    }
                ),
                sampler=ParentBased(TraceIdRatioBased(settings.trace_sample_ratio)),
            )
            provider.add_span_processor(
                BatchSpanProcessor(
                    OTLPSpanExporter(
                        endpoint=settings.otlp_traces_endpoint,
                        timeout=settings.export_timeout_seconds,
                    )
                )
            )
            trace.set_tracer_provider(provider)
            _TRACE_PROVIDER = provider
            atexit.register(provider.shutdown)
        if not _INSTRUMENTATION_INSTALLED:
            from opentelemetry.instrumentation.celery import CeleryInstrumentor
            from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor

            CeleryInstrumentor().instrument()
            SQLAlchemyInstrumentor().instrument(enable_commenter=False)
            _INSTRUMENTATION_INSTALLED = True
    return True


def install_http_tracing(
    web_app: FastAPI,
    *,
    settings: ObservabilitySettings,
    service_name: str,
) -> None:
    """安装不记录查询参数、请求头和正文的 HTTP server span。"""

    if not settings.tracing_enabled:
        return

    @web_app.middleware("http")
    async def low_sensitive_trace_middleware(request: Request, call_next) -> Response:
        async with _server_span(request, service_name=service_name) as span:
            response: Response | None = None
            try:
                response = await call_next(request)
                return response
            finally:
                if span is not None:
                    route = request.scope.get("route")
                    route_path = getattr(route, "path", None) or _bounded_path(
                        request.url.path
                    )
                    status_code = response.status_code if response is not None else 500
                    span.update_name(f"{request.method} {route_path}")
                    span.set_attribute("http.request.method", request.method)
                    span.set_attribute("http.route", route_path)
                    span.set_attribute("http.response.status_code", status_code)
                    request_id = getattr(request.state, "request_id", "")
                    if request_id:
                        span.set_attribute("job_agent.request_id", request_id)
                    if status_code >= 500:
                        from opentelemetry.trace import Status, StatusCode

                        span.set_status(Status(StatusCode.ERROR))


@asynccontextmanager
async def _server_span(
    request: Request,
    *,
    service_name: str,
) -> AsyncIterator[Any | None]:
    try:
        from opentelemetry import propagate, trace
        from opentelemetry.trace import SpanKind
    except ImportError:
        yield None
        return
    parent_context = propagate.extract(dict(request.headers))
    tracer = trace.get_tracer(service_name)
    with tracer.start_as_current_span(
        "HTTP request",
        context=parent_context,
        kind=SpanKind.SERVER,
    ) as span:
        yield span


def _bounded_path(path: str) -> str:
    """路由尚未匹配时只保留低基数路径前缀。"""

    if path.startswith("/api/"):
        parts = [part for part in path.split("/") if part]
        return "/" + "/".join(parts[:2]) + ("/*" if len(parts) > 2 else "")
    return path if path in {"/", "/login", "/workspace", "/admin"} else "other"


def _exception_frames(traceback_value: Any) -> list[Any]:
    """提取有限栈帧，不保存可能包含用户输入的异常消息或源码文本。"""

    import traceback

    return list(traceback.extract_tb(traceback_value, limit=20))
