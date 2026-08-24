from __future__ import annotations

import pytest

from job_hunting_agent.model_resilience import (
    CircuitBreaker,
    ModelCircuitCallbackHandler,
    ModelCircuitOpenError,
    is_transient_model_error,
)


def test_circuit_breaker_opens_and_allows_one_recovery_probe():
    """连续供应商失败会快速拒绝，冷却后只放行一次探测请求。"""

    now = [0.0]
    breaker = CircuitBreaker(
        failure_threshold=2,
        recovery_seconds=10,
        clock=lambda: now[0],
    )

    breaker.before_call()
    breaker.record_failure(RuntimeError("upstream timeout"))
    breaker.before_call()
    breaker.record_failure(RuntimeError("upstream timeout"))

    assert breaker.snapshot().state == "open"
    with pytest.raises(ModelCircuitOpenError) as blocked:
        breaker.before_call()
    assert blocked.value.retry_after_seconds == 10

    now[0] = 10
    breaker.before_call()
    assert breaker.snapshot().state == "open"
    with pytest.raises(ModelCircuitOpenError):
        breaker.before_call()

    breaker.record_success()
    assert breaker.snapshot().state == "closed"
    assert breaker.snapshot().consecutive_failures == 0


def test_circuit_breaker_ignores_concurrency_failures():
    """模型并发额度不足不能把供应商熔断器误判为上游故障。"""

    breaker = CircuitBreaker(failure_threshold=1, recovery_seconds=10)
    from job_hunting_agent.concurrency_control import ConcurrencyLimitExceeded

    breaker.record_failure(ConcurrencyLimitExceeded("busy"))

    assert breaker.snapshot().state == "closed"


def test_model_circuit_callback_records_timeout_and_releases_non_transient_probe():
    """模型回调生命周期会熔断超时，并释放非瞬时探测错误。"""

    now = [0.0]
    breaker = CircuitBreaker(
        failure_threshold=1,
        recovery_seconds=10,
        clock=lambda: now[0],
    )
    callback = ModelCircuitCallbackHandler(breaker)

    callback.on_chat_model_start({}, [[]], run_id="timeout-run")
    callback.on_llm_error(TimeoutError("upstream read timeout"), run_id="timeout-run")

    assert breaker.snapshot().state == "open"
    with pytest.raises(ModelCircuitOpenError):
        callback.on_chat_model_start({}, [[]], run_id="blocked-run")

    now[0] = 10
    callback.on_chat_model_start({}, [[]], run_id="probe-run")
    callback.on_llm_error(ValueError("invalid request parameter"), run_id="probe-run")

    assert breaker.snapshot().state == "closed"
    assert breaker.snapshot().consecutive_failures == 0


def test_model_error_classifier_only_marks_recoverable_failures():
    """鉴权/参数错误不能触发全局模型熔断。"""

    assert is_transient_model_error(TimeoutError("read timeout")) is True
    assert is_transient_model_error(RuntimeError("invalid api key")) is False

    server_error = RuntimeError("upstream 503")
    server_error.status_code = 503
    assert is_transient_model_error(server_error) is True
