"""Web 请求指标格式化回归。"""

from __future__ import annotations

from job_hunting_agent.web_hardening import format_prometheus_request_metrics


def test_prometheus_metrics_export_is_low_cardinality_and_escapes_labels() -> None:
    snapshot: dict[str, object] = {
        "in_flight_requests": 2,
        "total_requests": 7,
        "total_duration_ms": 3250,
        "max_duration_ms": 1250,
        "rate_limited_requests": 1,
        "csrf_rejected_requests": 2,
        "status_counts": {"2xx": 4, "5xx": 3},
        "method_counts": {"GET": 6, "POST": 1},
        "endpoint_counts": {'/api/items/{item_id}\n"quoted"': 3},
        "outcome_counts": {"handled": 4, "exception": 3},
        "recent_errors": [
            {
                "request_id": "private-request-id",
                "endpoint": "/api/items/{item_id}",
            }
        ],
    }

    text = format_prometheus_request_metrics(snapshot)

    assert text.endswith("\n")
    assert "job_agent_http_requests_total 7" in text
    assert "job_agent_http_request_duration_seconds_sum 3.25" in text
    assert "job_agent_http_request_duration_seconds_count 7" in text
    assert 'job_agent_security_rejections_total{reason="rate_limit"} 1' in text
    assert 'endpoint="/api/items/{item_id}\\n\\"quoted\\""' in text
    assert "private-request-id" not in text
    assert "recent_errors" not in text


def test_prometheus_metrics_export_shared_concurrency_events() -> None:
    """模型和截图租约只暴露固定资源标签，不暴露账号或 token。"""

    text = format_prometheus_request_metrics(
        {"total_requests": 0},
        {
            "resources": {
                "model": {
                    "acquired": 3,
                    "rejected": 1,
                    "backend_errors": 2,
                    "release_errors": 1,
                    "in_flight": 1,
                }
            }
        },
    )

    assert 'job_agent_concurrency_leases_acquired_total{resource="model"} 3' in text
    assert 'job_agent_concurrency_leases_rejected_total{resource="model"} 1' in text
    assert 'job_agent_concurrency_backend_errors_total{resource="model"} 2' in text
    assert 'job_agent_concurrency_release_errors_total{resource="model"} 1' in text
    assert 'job_agent_concurrency_leases_in_flight{resource="model"} 1' in text
    assert "account_id" not in text
    assert "token" not in text


def test_prometheus_metrics_export_intent_routing_counters_and_histogram() -> None:
    """路由指标使用固定原因标签和可聚合的累计直方图。"""

    text = format_prometheus_request_metrics(
        {"total_requests": 0},
        intent_routing_snapshot={
            "direct_total": 7,
            "fallback_total": 3,
            "timeout_total": 1,
            "fallback_reason_counts": {
                "router_timeout": 1,
                "multi_step": 2,
            },
            "latency_bucket_counts_ms": {
                50: 0,
                100: 1,
                250: 2,
                500: 3,
                1000: 4,
                2000: 5,
                3000: 6,
            },
            "latency_count": 6,
            "latency_sum_ms": 4321,
        },
    )

    assert "job_agent_intent_router_direct_total 7" in text
    assert "job_agent_intent_router_fallback_total 3" in text
    assert "job_agent_intent_router_timeouts_total 1" in text
    assert (
        'job_agent_intent_router_fallback_reasons_total{reason="router_timeout"} 1'
        in text
    )
    assert (
        'job_agent_intent_router_model_duration_seconds_bucket{le="1"} 4'
        in text
    )
    assert (
        'job_agent_intent_router_model_duration_seconds_bucket{le="+Inf"} 6'
        in text
    )
    assert "job_agent_intent_router_model_duration_seconds_sum 4.321" in text
    assert "job_agent_intent_router_model_duration_seconds_count 6" in text
