"""生产日志、Trace 与告警通知配置回归。"""

from __future__ import annotations

import json
import logging
import sys
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from job_hunting_agent.config import ObservabilitySettings, load_observability_settings
from job_hunting_agent.observability import (
    JsonLogFormatter,
    install_http_tracing,
    redact_log_text,
)
from job_hunting_agent.observability_config import (
    load_alertmanager_settings,
    render_alertmanager_config,
)

ROOT = Path(__file__).resolve().parents[1]


def test_observability_defaults_are_environment_aware(tmp_path: Path) -> None:
    env_path = tmp_path / ".env"
    env_path.write_text("JOB_AGENT_ENVIRONMENT=production\n", encoding="utf-8")

    production = load_observability_settings(env_path, environ={})
    development = load_observability_settings(tmp_path / "missing.env", environ={})

    assert production.environment == "production"
    assert production.log_format == "json"
    assert production.tracing_enabled is True
    assert production.trace_sample_ratio == 0.1
    assert production.otlp_traces_endpoint == "http://alloy:4318/v1/traces"
    assert development.log_format == "console"
    assert development.tracing_enabled is False


def test_observability_rejects_invalid_values(tmp_path: Path) -> None:
    env_path = tmp_path / ".env"
    env_path.write_text(
        "JOB_AGENT_LOG_FORMAT=xml\nJOB_AGENT_OTEL_TRACE_SAMPLE_RATIO=1.5\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="JOB_AGENT_LOG_FORMAT"):
        load_observability_settings(env_path, environ={})

    env_path.write_text(
        "JOB_AGENT_LOG_FORMAT=json\nJOB_AGENT_OTEL_TRACE_SAMPLE_RATIO=1.5\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="JOB_AGENT_OTEL_TRACE_SAMPLE_RATIO"):
        load_observability_settings(env_path, environ={})


def test_json_log_formatter_is_structured_and_redacts_secrets(monkeypatch) -> None:
    monkeypatch.setattr(
        "job_hunting_agent.observability.current_trace_ids",
        lambda: ("1" * 32, "2" * 16),
    )
    record = logging.LogRecord(
        name="job_hunting_agent.web.access",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg=(
            "request failed password=plain-secret "
            "Authorization: Bearer bearer-secret "
            "postgresql://job:database-secret@postgres/job"
        ),
        args=(),
        exc_info=None,
    )
    record.event = "http_request"
    record.request_id = "request-123"
    record.status_code = 500

    payload = json.loads(
        JsonLogFormatter(
            service_name="job-hunting-web",
            environment="production",
        ).format(record)
    )

    assert payload["service"] == "job-hunting-web"
    assert payload["environment"] == "production"
    assert payload["event"] == "http_request"
    assert payload["trace_id"] == "1" * 32
    assert payload["span_id"] == "2" * 16
    assert payload["request_id"] == "request-123"
    assert payload["status_code"] == 500
    serialized = json.dumps(payload, ensure_ascii=False)
    assert "plain-secret" not in serialized
    assert "bearer-secret" not in serialized
    assert "database-secret" not in serialized
    assert "***" in serialized


def test_json_log_formatter_does_not_store_exception_message() -> None:
    try:
        raise ValueError("user body content must-not-enter-loki")
    except ValueError:
        record = logging.LogRecord(
            name="job_hunting_agent.web",
            level=logging.ERROR,
            pathname=__file__,
            lineno=1,
            msg="Unhandled request",
            args=(),
            exc_info=sys.exc_info(),
        )

    payload = json.loads(
        JsonLogFormatter(
            service_name="job-hunting-web",
            environment="production",
        ).format(record)
    )

    assert payload["exception_type"] == "ValueError"
    assert payload["exception_frames"]
    assert "exception" not in payload
    assert "must-not-enter-loki" not in json.dumps(payload)


def test_log_redaction_preserves_non_secret_operational_text() -> None:
    text = "token usage recorded; path=/api/chat; status=200"
    assert redact_log_text(text) == text


def test_http_trace_records_route_but_not_query_headers_or_body(monkeypatch) -> None:
    class FakeSpan:
        def __init__(self) -> None:
            self.name = ""
            self.attributes: dict[str, object] = {}

        def update_name(self, name: str) -> None:
            self.name = name

        def set_attribute(self, key: str, value: object) -> None:
            self.attributes[key] = value

        def set_status(self, status) -> None:
            self.attributes["status"] = status

    span = FakeSpan()

    @asynccontextmanager
    async def fake_server_span(request, *, service_name):
        yield span

    monkeypatch.setattr(
        "job_hunting_agent.observability._server_span",
        fake_server_span,
    )
    app = FastAPI()

    @app.post("/items/{item_id}")
    def create_item(item_id: int) -> dict[str, int]:
        return {"item_id": item_id}

    install_http_tracing(
        app,
        settings=ObservabilitySettings(tracing_enabled=True),
        service_name="job-hunting-web",
    )
    response = TestClient(app).post(
        "/items/42?access_token=query-secret",
        headers={"Authorization": "Bearer header-secret"},
        json={"password": "body-secret"},
    )

    assert response.status_code == 200
    assert span.name == "POST /items/{item_id}"
    assert span.attributes["http.route"] == "/items/{item_id}"
    serialized = json.dumps(span.attributes)
    assert "query-secret" not in serialized
    assert "header-secret" not in serialized
    assert "body-secret" not in serialized


def test_alertmanager_config_uses_smtp_secret_without_logging_it(tmp_path: Path) -> None:
    env_path = tmp_path / ".env"
    env_path.write_text(
        "\n".join(
            (
                "JOB_AGENT_SMTP_HOST=smtp.example.com",
                "JOB_AGENT_SMTP_PORT=465",
                "JOB_AGENT_SMTP_USERNAME=ops@example.com",
                "JOB_AGENT_SMTP_PASSWORD=smtp-secret",
                "JOB_AGENT_SMTP_FROM_EMAIL=ops@example.com",
                "JOB_AGENT_ALERT_EMAIL_TO=oncall@example.com",
            )
        ),
        encoding="utf-8",
    )

    settings = load_alertmanager_settings(env_path, environ={})
    rendered = render_alertmanager_config(settings)
    config = json.loads(rendered)

    assert config["global"]["smtp_smarthost"] == "smtp.example.com:465"
    assert config["global"]["smtp_auth_password"] == "smtp-secret"
    assert config["receivers"][0]["email_configs"][0]["to"] == "oncall@example.com"
    assert config["receivers"][0]["email_configs"][0]["force_implicit_tls"] is True
    assert config["receivers"][0]["email_configs"][0]["send_resolved"] is True


def test_production_observability_stack_is_private_and_bounded() -> None:
    compose = (ROOT / "compose.prod.yaml").read_text(encoding="utf-8")
    prometheus = (ROOT / "deploy/prometheus/prometheus.yml").read_text(
        encoding="utf-8"
    )
    loki = (ROOT / "deploy/loki/loki.yml").read_text(encoding="utf-8")
    tempo = (ROOT / "deploy/tempo/tempo.yml").read_text(encoding="utf-8")
    alloy = (ROOT / "deploy/alloy/config.alloy").read_text(encoding="utf-8")
    datasources = (
        ROOT / "deploy/grafana/provisioning/datasources/datasources.yml"
    ).read_text(encoding="utf-8")

    for service in ("alertmanager", "loki", "tempo", "alloy", "grafana"):
        assert f"  {service}:" in compose
    assert "grafana/grafana:13.1.4" in compose
    assert '"127.0.0.1:${JOB_AGENT_GRAFANA_PORT:-3000}:3000"' in compose
    assert '"127.0.0.1:${JOB_AGENT_ALERTMANAGER_PORT:-9093}:9093"' in compose
    assert "3100:3100" not in compose
    assert "4317:4317" not in compose
    assert "4318:4318" not in compose
    assert "alertmanager:9093" in prometheus
    assert "retention_enabled: true" in loki
    assert "retention_period: 336h" in loki
    assert "block_retention: 168h" in tempo
    assert "unix:///var/run/docker.sock" in alloy
    assert "otelcol.receiver.otlp" in alloy
    assert "otelcol.exporter.otlp" in alloy
    assert "trace_id" not in "\n".join(
        line for line in alloy.splitlines() if "labels" in line
    )
    for datasource in ("Prometheus", "Loki", "Tempo"):
        assert f"name: {datasource}" in datasources
