"""端到端负载测试使用的确定性支撑组件。

本模块不修改生产路由，也不访问真实模型供应商。手动压测脚本用它启动真实
Uvicorn TCP 服务、维护浏览器式 Cookie/CSRF 会话、解析 SSE，并生成不含凭据的
统计报告。Celery Worker 仍使用正式任务执行器和 PostgreSQL 状态表。
"""

from __future__ import annotations

import json
import math
import socket
import threading
import time
import uuid
from collections import defaultdict
from dataclasses import asdict, dataclass
from http.cookiejar import CookieJar
from typing import Any, Iterable, Iterator, Mapping
from urllib.error import HTTPError
from urllib.parse import urlencode, urlsplit, urlunsplit
from urllib.request import HTTPCookieProcessor, Request, build_opener

import uvicorn

from .model_resilience import ModelCircuitOpenError
from .models import AgentChatResult


@dataclass(frozen=True)
class SSEEvent:
    """一条已解码的 Server-Sent Event。"""

    name: str
    data: object


class SSEDecoder:
    """增量解码可能跨网络分片的 SSE 文本。"""

    def __init__(self) -> None:
        self._buffer = ""

    def feed(self, chunk: bytes | str) -> list[SSEEvent]:
        """追加一个网络分片，并返回其中已经完整结束的事件。"""

        text = chunk.decode("utf-8") if isinstance(chunk, bytes) else chunk
        self._buffer += text.replace("\r\n", "\n").replace("\r", "\n")
        events: list[SSEEvent] = []
        while "\n\n" in self._buffer:
            block, self._buffer = self._buffer.split("\n\n", 1)
            event = self._decode_block(block)
            if event is not None:
                events.append(event)
        return events

    def close(self) -> list[SSEEvent]:
        """在连接结束时解析没有尾随空行的最后一条事件。"""

        block = self._buffer.strip("\n")
        self._buffer = ""
        if not block:
            return []
        event = self._decode_block(block)
        return [event] if event is not None else []

    @staticmethod
    def _decode_block(block: str) -> SSEEvent | None:
        event_name = "message"
        data_lines: list[str] = []
        for line in block.split("\n"):
            if not line or line.startswith(":"):
                continue
            field, separator, value = line.partition(":")
            if separator and value.startswith(" "):
                value = value[1:]
            if field == "event":
                event_name = value or "message"
            elif field == "data":
                data_lines.append(value)
        if not data_lines:
            return None
        raw_data = "\n".join(data_lines)
        try:
            data: object = json.loads(raw_data)
        except json.JSONDecodeError:
            data = raw_data
        return SSEEvent(name=event_name, data=data)


@dataclass(frozen=True)
class LoadSample:
    """一次 HTTP、SSE 或后台任务操作的低敏测量结果。"""

    scenario: str
    concurrency: int
    success: bool
    status_code: int | None
    elapsed_ms: float
    error: str | None = None
    first_event_ms: float | None = None
    event_count: int | None = None


def percentile(values: Iterable[float], percentile_value: float) -> float | None:
    """用最近秩法计算百分位，适合压测报告的离散请求样本。"""

    ordered = sorted(float(value) for value in values)
    if not ordered:
        return None
    if not 0 < percentile_value <= 100:
        raise ValueError("percentile_value 必须位于 (0, 100]。")
    index = max(0, math.ceil(percentile_value / 100 * len(ordered)) - 1)
    return round(ordered[index], 3)


def summarize_samples(samples: Iterable[LoadSample]) -> dict[str, dict[str, object]]:
    """按场景和并发档聚合成功率、状态码与延迟百分位。"""

    grouped: dict[tuple[str, int], list[LoadSample]] = defaultdict(list)
    for sample in samples:
        grouped[(sample.scenario, sample.concurrency)].append(sample)

    summaries: dict[str, dict[str, object]] = {}
    for (scenario, concurrency), group in sorted(grouped.items()):
        successes = sum(1 for sample in group if sample.success)
        status_counts: dict[str, int] = defaultdict(int)
        errors: dict[str, int] = defaultdict(int)
        for sample in group:
            status_counts[str(sample.status_code) if sample.status_code is not None else "network"] += 1
            if sample.error:
                errors[sample.error] += 1
        latency_values = [sample.elapsed_ms for sample in group]
        first_event_values = [
            sample.first_event_ms
            for sample in group
            if sample.first_event_ms is not None
        ]
        summary: dict[str, object] = {
            "scenario": scenario,
            "concurrency": concurrency,
            "requests": len(group),
            "successes": successes,
            "failures": len(group) - successes,
            "success_rate": successes / len(group),
            "error_rate": (len(group) - successes) / len(group),
            "status_counts": dict(sorted(status_counts.items())),
            "latency_ms": _latency_summary(latency_values),
            "errors": dict(sorted(errors.items())),
        }
        if first_event_values:
            summary["first_event_ms"] = _latency_summary(first_event_values)
        summaries[f"{scenario}@{concurrency}"] = summary
    return summaries


def _latency_summary(values: Iterable[float]) -> dict[str, float | None]:
    normalized = [float(value) for value in values]
    return {
        "p50": percentile(normalized, 50),
        "p95": percentile(normalized, 95),
        "p99": percentile(normalized, 99),
        "max": round(max(normalized), 3) if normalized else None,
    }


SENSITIVE_KEYS = {
    "access_key",
    "api_key",
    "authorization",
    "cookie",
    "cookies",
    "csrf_token",
    "password",
    "password_hash",
    "redis_password",
    "secret_key",
    "session_token",
    "token",
}
URL_KEYS = {"base_url", "database_url", "redis_url", "url"}


def redact_sensitive_data(value: object, key: str | None = None) -> object:
    """递归移除报告中的密码、Cookie、密钥和 URL userinfo。"""

    normalized_key = (key or "").strip().lower()
    if normalized_key in SENSITIVE_KEYS:
        return "[REDACTED]"
    if isinstance(value, Mapping):
        return {
            str(item_key): redact_sensitive_data(item_value, str(item_key))
            for item_key, item_value in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [redact_sensitive_data(item) for item in value]
    if isinstance(value, str) and (normalized_key in URL_KEYS or "://" in value):
        return redact_url(value)
    return value


def redact_url(value: str) -> str:
    """保留协议、主机和路径，同时删除 URL 中的用户名、密码和查询参数。"""

    try:
        parsed = urlsplit(value)
    except ValueError:
        return "[REDACTED_URL]"
    if not parsed.scheme or not parsed.netloc:
        return value
    hostname = parsed.hostname or ""
    if ":" in hostname and not hostname.startswith("["):
        hostname = f"[{hostname}]"
    netloc = hostname
    if parsed.port:
        netloc = f"{netloc}:{parsed.port}"
    return urlunsplit((parsed.scheme, netloc, parsed.path, "", ""))


class _RoutingMetrics:
    def snapshot(self) -> dict[str, object]:
        return {"enabled": False, "mode": "deterministic_load_test"}


class DeterministicLoadTestAgent:
    """只供隔离压测使用、不会访问模型供应商的流式 Agent 替身。"""

    def __init__(self, token_delay_seconds: float = 0.002) -> None:
        self.token_delay_seconds = max(0.0, float(token_delay_seconds))
        self.routing_metrics = _RoutingMetrics()

    def chat(
        self,
        message: str,
        candidate_id: int | None,
        session_id: str | None = None,
        **kwargs: object,
    ) -> AgentChatResult:
        return self._result(message, candidate_id, session_id, kwargs.get("root_request_id"))

    def stream_chat(
        self,
        message: str,
        candidate_id: int | None,
        session_id: str | None = None,
        **kwargs: object,
    ) -> Iterator[dict[str, object]]:
        if "[fault:circuit]" in message:
            raise ModelCircuitOpenError(1)
        if "[fault:timeout]" in message:
            time.sleep(max(0.05, self.token_delay_seconds * 10))
            raise TimeoutError("deterministic load-test timeout")
        for token in ("确定性", "流式", "负载", "回复"):
            if self.token_delay_seconds:
                time.sleep(self.token_delay_seconds)
            yield {"type": "token", "content": token}
        yield {
            "type": "final",
            "result": self._result(
                message,
                candidate_id,
                session_id,
                kwargs.get("root_request_id"),
            ),
        }

    @staticmethod
    def _result(
        message: str,
        candidate_id: int | None,
        session_id: str | None,
        root_request_id: object,
    ) -> AgentChatResult:
        del message
        return AgentChatResult(
            reply="确定性流式负载回复",
            candidate_id=candidate_id,
            session_id=session_id or "load-test-session",
            mode="deterministic_load_test",
            used_tools=[],
            tool_outputs=[],
            usage={},
            root_request_id=str(root_request_id or uuid.uuid4().hex),
            routing={"mode": "deterministic_load_test"},
        )


@dataclass(frozen=True)
class JSONResponseResult:
    status_code: int
    body: object
    elapsed_ms: float


@dataclass(frozen=True)
class TextResponseResult:
    status_code: int
    body: str
    elapsed_ms: float


@dataclass(frozen=True)
class SSEResponseResult:
    status_code: int
    events: tuple[SSEEvent, ...]
    elapsed_ms: float
    first_event_ms: float | None


class LoadHttpClient:
    """使用标准库发起真实网络请求，并维持独立浏览器 Cookie 会话。"""

    def __init__(self, base_url: str, timeout_seconds: float = 15.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = float(timeout_seconds)
        self.cookie_jar = CookieJar()
        self.opener = build_opener(HTTPCookieProcessor(self.cookie_jar))
        self.csrf_token: str | None = None

    def request_json(
        self,
        method: str,
        path: str,
        *,
        payload: object | None = None,
        query: Mapping[str, object] | None = None,
        csrf: bool = False,
        headers: Mapping[str, str] | None = None,
    ) -> JSONResponseResult:
        url = self._url(path, query)
        body = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request_headers = {
            "Accept": "application/json",
            "User-Agent": "job-agent-e2e-load-test",
        }
        if body is not None:
            request_headers["Content-Type"] = "application/json"
        if csrf and self.csrf_token:
            request_headers["X-CSRF-Token"] = self.csrf_token
        request_headers.update(headers or {})
        request = Request(url, data=body, headers=request_headers, method=method.upper())
        started_at = time.perf_counter()
        try:
            with self.opener.open(request, timeout=self.timeout_seconds) as response:
                status_code = response.status
                raw_body = response.read()
        except HTTPError as error:
            status_code = error.code
            raw_body = error.read()
        elapsed_ms = (time.perf_counter() - started_at) * 1000
        parsed_body = _parse_json_body(raw_body)
        return JSONResponseResult(status_code, parsed_body, elapsed_ms)

    def request_text(self, method: str, path: str) -> TextResponseResult:
        request = Request(
            self._url(path),
            headers={"Accept": "text/plain", "User-Agent": "job-agent-e2e-load-test"},
            method=method.upper(),
        )
        started_at = time.perf_counter()
        try:
            with self.opener.open(request, timeout=self.timeout_seconds) as response:
                status_code = response.status
                raw_body = response.read()
        except HTTPError as error:
            status_code = error.code
            raw_body = error.read()
        elapsed_ms = (time.perf_counter() - started_at) * 1000
        return TextResponseResult(
            status_code,
            raw_body.decode("utf-8", errors="replace"),
            elapsed_ms,
        )

    def stream_sse(
        self,
        path: str,
        *,
        payload: object,
        csrf: bool = True,
    ) -> SSEResponseResult:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = {
            "Accept": "text/event-stream",
            "Content-Type": "application/json",
            "User-Agent": "job-agent-e2e-load-test",
        }
        if csrf and self.csrf_token:
            headers["X-CSRF-Token"] = self.csrf_token
        request = Request(self._url(path), data=body, headers=headers, method="POST")
        started_at = time.perf_counter()
        decoder = SSEDecoder()
        events: list[SSEEvent] = []
        first_event_ms: float | None = None
        try:
            with self.opener.open(request, timeout=self.timeout_seconds) as response:
                status_code = response.status
                while chunk := response.read(256):
                    decoded = decoder.feed(chunk)
                    if decoded and first_event_ms is None:
                        first_event_ms = (time.perf_counter() - started_at) * 1000
                    events.extend(decoded)
                trailing = decoder.close()
                if trailing and first_event_ms is None:
                    first_event_ms = (time.perf_counter() - started_at) * 1000
                events.extend(trailing)
        except HTTPError as error:
            status_code = error.code
            decoder.feed(error.read())
            events.extend(decoder.close())
        elapsed_ms = (time.perf_counter() - started_at) * 1000
        return SSEResponseResult(status_code, tuple(events), elapsed_ms, first_event_ms)

    def login(self, email: str, password: str) -> JSONResponseResult:
        result = self.request_json(
            "POST",
            "/api/auth/login",
            payload={"email": email, "password": password},
        )
        if result.status_code == 200 and isinstance(result.body, Mapping):
            token = result.body.get("csrf_token")
            self.csrf_token = str(token) if token else None
        return result

    def _url(self, path: str, query: Mapping[str, object] | None = None) -> str:
        normalized_path = path if path.startswith("/") else f"/{path}"
        url = f"{self.base_url}{normalized_path}"
        if query:
            url = f"{url}?{urlencode(query)}"
        return url


def _parse_json_body(raw_body: bytes) -> object:
    if not raw_body:
        return None
    text = raw_body.decode("utf-8", errors="replace")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {"non_json_response": text[:500]}


def find_free_tcp_port(host: str = "127.0.0.1") -> int:
    """让操作系统分配一个当前可绑定的本机 TCP 端口。"""

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind((host, 0))
        return int(probe.getsockname()[1])


class UvicornTestServer:
    """在线程中启动真实 Uvicorn 监听，并提供确定的关闭行为。"""

    def __init__(self, app: Any, host: str = "127.0.0.1", port: int | None = None) -> None:
        self.host = host
        self.port = port or find_free_tcp_port(host)
        self.server = uvicorn.Server(
            uvicorn.Config(
                app,
                host=host,
                port=self.port,
                log_level="warning",
                access_log=False,
                log_config=None,
            )
        )
        self.thread = threading.Thread(target=self.server.run, name="e2e-load-uvicorn", daemon=True)

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"

    def start(self, timeout_seconds: float = 10.0) -> "UvicornTestServer":
        self.thread.start()
        deadline = time.monotonic() + timeout_seconds
        while not self.server.started:
            if not self.thread.is_alive():
                raise RuntimeError("Uvicorn 压测服务启动失败。")
            if time.monotonic() >= deadline:
                raise TimeoutError("等待 Uvicorn 压测服务启动超时。")
            time.sleep(0.02)
        return self

    def stop(self, timeout_seconds: float = 10.0) -> None:
        self.server.should_exit = True
        self.thread.join(timeout_seconds)
        if self.thread.is_alive():
            raise TimeoutError("Uvicorn 压测服务未在期限内退出。")

    def __enter__(self) -> "UvicornTestServer":
        return self.start()

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.stop()


def samples_as_dicts(samples: Iterable[LoadSample]) -> list[dict[str, object]]:
    """把样本转换为可序列化、已脱敏的字典。"""

    return [redact_sensitive_data(asdict(sample)) for sample in samples]  # type: ignore[list-item]
