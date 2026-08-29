"""端到端负载测试工具的低成本回归测试。"""

from __future__ import annotations

import json
import sys
from types import SimpleNamespace

import pytest

from job_hunting_agent.e2e_load import (
    DeterministicLoadTestAgent,
    LoadSample,
    LoadHttpClient,
    SSEDecoder,
    UvicornTestServer,
    redact_sensitive_data,
    summarize_samples,
)
from job_hunting_agent.web import create_web_app
from job_hunting_agent.e2e_load_runner import (
    evaluate_acceptance,
    isolated_redis_database,
    parse_concurrency_levels,
    replace_service_host,
    schema_database_url,
)


def test_sse_decoder_handles_fragmented_multiline_events() -> None:
    decoder = SSEDecoder()

    assert decoder.feed('event: token\r\ndata: {"content":"hel') == []
    events = decoder.feed('lo"}\r\n\r\nevent: final\ndata: {"ok":')
    events.extend(decoder.feed('true}\n\n'))

    assert [(event.name, event.data) for event in events] == [
        ("token", {"content": "hello"}),
        ("final", {"ok": True}),
    ]


def test_summarize_samples_reports_latency_and_error_rate() -> None:
    samples = [
        LoadSample("health", 5, True, 200, 10.0),
        LoadSample("health", 5, True, 200, 20.0),
        LoadSample("health", 5, False, 500, 30.0, error="HTTP 500"),
        LoadSample("sse", 5, True, 200, 100.0, first_event_ms=25.0),
    ]

    summary = summarize_samples(samples)

    health = summary["health@5"]
    assert health["requests"] == 3
    assert health["success_rate"] == 2 / 3
    assert health["error_rate"] == 1 / 3
    assert health["latency_ms"] == {"p50": 20.0, "p95": 30.0, "p99": 30.0, "max": 30.0}
    assert summary["sse@5"]["first_event_ms"]["p95"] == 25.0


def test_redact_sensitive_data_removes_credentials_and_url_userinfo() -> None:
    payload = {
        "password": "plain-text",
        "cookie": "session=value",
        "database_url": "postgresql+psycopg://user:secret@localhost:5432/app",
        "nested": {"api_key": "sk-test", "queue": "load-test"},
    }

    redacted = redact_sensitive_data(payload)
    rendered = json.dumps(redacted, ensure_ascii=False)

    assert "plain-text" not in rendered
    assert "session=value" not in rendered
    assert "secret" not in rendered
    assert "sk-test" not in rendered
    assert redacted["database_url"] == "postgresql+psycopg://localhost:5432/app"
    assert redacted["nested"]["queue"] == "load-test"


def test_runner_builds_isolated_service_urls() -> None:
    database_url = schema_database_url(
        "postgresql+psycopg://user:p%40ss@127.0.0.1:5432/app?sslmode=disable",
        "job_agent_e2e_test",
    )
    worker_url = replace_service_host(database_url, "postgres", 5432)

    assert "search_path%3Djob_agent_e2e_test%2Cpublic" in database_url
    assert "@postgres:5432/app" in worker_url
    assert "sslmode=disable" in worker_url
    assert parse_concurrency_levels("10, 1,5,5") == (1, 5, 10)


def test_acceptance_uses_separate_latency_budget_for_rag() -> None:
    summaries = {
        "health@50": {
            "scenario": "health",
            "requests": 50,
            "failures": 0,
            "latency_ms": {"p95": 400.0},
        },
        "rag_search@50": {
            "scenario": "rag_search",
            "requests": 50,
            "failures": 0,
            "latency_ms": {"p95": 900.0},
        },
        "sse_chat@50": {
            "scenario": "sse_chat",
            "requests": 50,
            "failures": 0,
            "latency_ms": {"p95": 1000.0},
            "first_event_ms": {"p95": 600.0},
        },
    }

    checks = evaluate_acceptance(
        summaries,
        {},
        SimpleNamespace(skip_faults=True, skip_worker=True),
        None,
    )
    by_name = {check["name"]: check for check in checks}

    assert by_name["normal_api_p95_below_500_ms"]["passed"] is True
    assert by_name["rag_search_p95_below_1500_ms"]["passed"] is True


def test_nonempty_redis_database_is_never_flushed(monkeypatch) -> None:
    class FakeRedisClient:
        flushed = False
        closed = False

        def ping(self) -> bool:
            return True

        def dbsize(self) -> int:
            return 1

        def flushdb(self) -> None:
            self.flushed = True

        def close(self) -> None:
            self.closed = True

    client = FakeRedisClient()
    fake_module = SimpleNamespace(
        Redis=SimpleNamespace(from_url=lambda *args, **kwargs: client)
    )
    monkeypatch.setitem(sys.modules, "redis", fake_module)

    with pytest.raises(RuntimeError, match="已有键"):
        with isolated_redis_database("redis://localhost:6379/15"):
            raise AssertionError("非空 Redis DB 不应进入测试上下文。")

    assert client.flushed is False
    assert client.closed is True


def test_real_network_smoke_covers_cookie_csrf_profiles_and_sse(
    database_url,
    tmp_path,
    monkeypatch,
) -> None:
    env_file = tmp_path / "e2e-smoke.env"
    env_file.write_text("JOB_AGENT_ENVIRONMENT=test\n", encoding="utf-8")
    monkeypatch.setenv("JOB_AGENT_CSRF_ENABLED", "true")
    monkeypatch.setenv("JOB_AGENT_RATE_LIMIT_ENABLED", "false")
    monkeypatch.setenv("JOB_AGENT_EMAIL_VERIFICATION_REQUIRED", "false")
    monkeypatch.setenv("JOB_AGENT_CONSENT_REQUIRED", "false")
    monkeypatch.setenv("JOB_AGENT_EMBEDDING_PROVIDER", "local_hash")
    monkeypatch.setenv("JOB_AGENT_EMBEDDING_API_STYLE", "local_hash")
    monkeypatch.setenv("JOB_AGENT_EMBEDDING_DIMENSIONS", "64")
    web_app = create_web_app(
        env_file=env_file,
        database_url=database_url,
        chat_agent=DeterministicLoadTestAgent(token_delay_seconds=0),
    )

    try:
        with UvicornTestServer(web_app) as server:
            client = LoadHttpClient(server.base_url)
            assert client.request_json("GET", "/api/health").status_code == 200
            registered = client.request_json(
                "POST",
                "/api/auth/register",
                payload={"email": "e2e-network@example.com", "password": "password-123"},
            )
            assert registered.status_code == 200
            assert client.login("e2e-network@example.com", "password-123").status_code == 200

            blocked = client.request_json(
                "POST",
                "/api/profiles",
                payload={"name": "缺少 CSRF"},
            )
            assert blocked.status_code == 403
            created = client.request_json(
                "POST",
                "/api/profiles",
                payload={"name": "端到端测试候选人"},
                csrf=True,
            )
            assert created.status_code == 200
            candidate_id = created.body["candidate_id"]

            streamed = client.stream_sse(
                "/api/chat/stream",
                payload={
                    "candidate_id": candidate_id,
                    "message": "请返回确定性测试回复",
                    "use_env_llm": True,
                    "auto_rag": False,
                },
            )
            event_names = [event.name for event in streamed.events]

            assert streamed.status_code == 200
            assert streamed.first_event_ms is not None
            assert event_names.count("token") == 4
            assert "final" in event_names
            assert client.request_json(
                "GET",
                f"/api/profiles/{candidate_id}",
            ).status_code == 200
    finally:
        web_app.state.backend.store.close()
