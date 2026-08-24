"""模型供应商调用的进程内熔断边界。

模型调用已经由 ``ChatOpenAI`` 自带有限重试和请求超时保护；这里再增加一层
熔断，避免供应商持续故障时每个请求都重复等待超时。熔断状态只保存低敏计数和
时间，不保存 prompt、回复或账号信息。生产环境的 Agent 对话上下文仍由
PostgreSQL 共享，本模块只负责当前 Web 进程的快速故障隔离。
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.messages import BaseMessage

from .concurrency_control import ConcurrencyControlError


class ModelCircuitOpenError(RuntimeError):
    """模型供应商熔断中，当前请求应稍后重试。"""

    def __init__(self, retry_after_seconds: int) -> None:
        self.retry_after_seconds = max(1, retry_after_seconds)
        super().__init__("模型服务暂时不可用，请稍后重试。")


@dataclass(frozen=True)
class CircuitSnapshot:
    """熔断器的低敏运行快照。"""

    state: str
    consecutive_failures: int
    retry_after_seconds: int


class CircuitBreaker:
    """线程安全的 closed/open/half-open 熔断器。"""

    def __init__(
        self,
        *,
        failure_threshold: int = 5,
        recovery_seconds: float = 30.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if failure_threshold < 1:
            raise ValueError("熔断失败阈值必须至少为 1")
        if recovery_seconds <= 0:
            raise ValueError("熔断恢复时间必须大于 0")
        self.failure_threshold = failure_threshold
        self.recovery_seconds = recovery_seconds
        self._clock = clock
        self._lock = threading.Lock()
        self._consecutive_failures = 0
        self._opened_at: float | None = None
        self._probe_in_flight = False

    def before_call(self) -> None:
        """在真实供应商调用前检查当前熔断状态。"""

        with self._lock:
            if self._opened_at is None:
                return
            elapsed = self._clock() - self._opened_at
            if elapsed < self.recovery_seconds:
                raise ModelCircuitOpenError(self._retry_after(elapsed))
            if self._probe_in_flight:
                raise ModelCircuitOpenError(1)
            self._probe_in_flight = True

    def record_success(self) -> None:
        """成功请求关闭熔断并清除连续失败计数。"""

        with self._lock:
            self._consecutive_failures = 0
            self._opened_at = None
            self._probe_in_flight = False

    def record_failure(self, error: BaseException) -> None:
        """记录一次供应商失败；并发保护和已熔断不计入供应商失败。"""

        if isinstance(error, (ModelCircuitOpenError, ConcurrencyControlError)):
            return
        with self._lock:
            self._probe_in_flight = False
            self._consecutive_failures += 1
            if self._consecutive_failures >= self.failure_threshold:
                self._opened_at = self._clock()

    def snapshot(self) -> CircuitSnapshot:
        """返回适合健康检查和管理员观测的低敏状态。"""

        with self._lock:
            if self._opened_at is None:
                state = "closed"
                retry_after = 0
            else:
                elapsed = self._clock() - self._opened_at
                if elapsed >= self.recovery_seconds and not self._probe_in_flight:
                    state = "half_open"
                    retry_after = 0
                else:
                    state = "open"
                    retry_after = self._retry_after(elapsed)
            return CircuitSnapshot(
                state=state,
                consecutive_failures=self._consecutive_failures,
                retry_after_seconds=retry_after,
            )

    def _retry_after(self, elapsed: float) -> int:
        return max(1, int(self.recovery_seconds - max(0.0, elapsed) + 0.999))


class ModelCircuitCallbackHandler(BaseCallbackHandler):
    """把熔断器接入 LangChain ChatModel 生命周期。"""

    raise_error = True

    def __init__(self, breaker: CircuitBreaker) -> None:
        self.breaker = breaker
        self._active_runs: set[object] = set()
        self._lock = threading.Lock()

    def on_chat_model_start(
        self,
        serialized: dict[str, object],
        messages: list[list[BaseMessage]],
        *,
        run_id: object,
        **kwargs: object,
    ) -> None:
        self._start(run_id)

    def on_llm_start(
        self,
        serialized: dict[str, object],
        prompts: list[str],
        *,
        run_id: object,
        **kwargs: object,
    ) -> None:
        self._start(run_id)

    def on_llm_end(self, response: object, *, run_id: object, **kwargs: object) -> None:
        self._finish(run_id, succeeded=True)

    def on_llm_error(
        self,
        error: BaseException,
        *,
        run_id: object,
        **kwargs: object,
    ) -> None:
        self._finish(run_id, succeeded=False, error=error)

    def _start(self, run_id: object) -> None:
        with self._lock:
            if run_id in self._active_runs:
                return
        self.breaker.before_call()
        with self._lock:
            self._active_runs.add(run_id)

    def _finish(
        self,
        run_id: object,
        *,
        succeeded: bool,
        error: BaseException | None = None,
    ) -> None:
        with self._lock:
            if run_id not in self._active_runs:
                return
            self._active_runs.remove(run_id)
        if succeeded:
            self.breaker.record_success()
        elif error is not None:
            if is_transient_model_error(error):
                self.breaker.record_failure(error)
            else:
                # 参数、鉴权和模型名称错误不属于短暂供应商故障；如果它发生在
                # half-open 探测阶段，也要释放探测占位，避免无意义地继续熔断。
                self.breaker.record_success()


def is_transient_model_error(error: BaseException) -> bool:
    """判断是否应计入供应商熔断。

    不直接依赖某个供应商 SDK 的异常类，而读取通用状态码和异常类名，兼容
    OpenAI-compatible 中转站。未知的业务/参数异常默认不触发熔断。
    """

    current: BaseException | None = error
    for _ in range(4):
        if current is None:
            break
        if isinstance(current, (TimeoutError, ConnectionError)):
            return True
        status_code = getattr(current, "status_code", None)
        if status_code is None:
            response = getattr(current, "response", None)
            status_code = getattr(response, "status_code", None)
        if isinstance(status_code, int) and status_code in {408, 409, 425, 429}:
            return True
        if isinstance(status_code, int) and status_code >= 500:
            return True
        name = type(current).__name__.lower()
        if any(
            marker in name
            for marker in (
                "timeout",
                "connection",
                "rate_limit",
                "ratelimit",
                "serviceunavailable",
                "internalserver",
                "badgateway",
                "gatewaytimeout",
            )
        ):
            return True
        cause = current.__cause__ or current.__context__
        current = cause if isinstance(cause, BaseException) else None
    return False
