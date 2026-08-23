"""生产发布与恢复基线的静态回归。"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_ci_runs_quality_checks_and_builds_release_image():
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")

    for required in (
        "actions/checkout@v7",
        "actions/setup-python@v7",
        "actions/setup-node@v7",
        "requirements-dev.lock",
        "python -m pip install -r requirements.lock -r requirements-dev.lock",
        "python -m pytest -q",
        "ruff check src tests alembic",
        "python -m compileall -q src tests alembic",
        "tests/frontend_*.mjs",
        "docker compose --env-file .env.example -f compose.yaml config --quiet",
        "prom/prometheus:v3.13.1",
        "check config /etc/prometheus/prometheus.yml",
        "docker build --tag job-hunting-agent:ci .",
    ):
        assert required in workflow


def test_production_compose_does_not_expose_internal_services():
    base_compose = (ROOT / "compose.yaml").read_text(encoding="utf-8")
    compose = (ROOT / "compose.prod.yaml").read_text(encoding="utf-8")

    assert "POSTGRES_HOST_AUTH_METHOD: scram-sha-256" in compose
    assert "ports: !reset []" in compose
    assert "JOB_AGENT_ENVIRONMENT: production" in compose
    assert "JOB_AGENT_COOKIE_SECURE: \"true\"" in compose
    assert 'FORWARDED_ALLOW_IPS: "*"' in compose
    assert 'JOB_AGENT_RATE_LIMIT_BACKEND: "redis"' in base_compose
    assert "JOB_AGENT_RATE_LIMIT_REDIS_URL" in base_compose
    assert 'JOB_AGENT_CONCURRENCY_BACKEND: "redis"' in base_compose
    assert base_compose.count("JOB_AGENT_CONCURRENCY_REDIS_URL") == 2
    assert "JOB_AGENT_OBJECT_STORAGE_AUTO_CREATE_BUCKET: \"false\"" in compose
    assert "reverse-proxy:" in compose
    assert "caddy:2.9.1-alpine" in compose
    assert '"80:80"' in compose
    assert '"443:443"' in compose
    assert "condition: service_healthy" in compose
    assert "postgres_prod_data" in compose
    assert "minio_prod_data" in compose
    assert "redis_prod_data" in compose
    assert "prom/prometheus:v3.13.1" in base_compose
    assert '"127.0.0.1:${JOB_AGENT_PROMETHEUS_PORT:-9090}:9090"' in base_compose
    assert "prometheus_data:/prometheus" in base_compose
    assert "http://127.0.0.1:9090/-/healthy" in base_compose
    assert "volumes: !override" in compose
    assert "prometheus_prod_data" in compose

    caddyfile = (ROOT / "deploy" / "Caddyfile").read_text(encoding="utf-8")
    assert "@internal path /internal/*" in caddyfile
    assert "respond @internal 404" in caddyfile
    assert "dynamic a web 8000" in caddyfile
    assert "lb_policy round_robin" in caddyfile


def test_prometheus_scrape_and_alerting_baseline_is_present():
    config = (ROOT / "deploy" / "prometheus" / "prometheus.yml").read_text(
        encoding="utf-8"
    )
    alerts = (ROOT / "deploy" / "prometheus" / "alerts.yml").read_text(
        encoding="utf-8"
    )

    assert "metrics_path: /internal/metrics" in config
    assert "dns_sd_configs:" in config
    assert "type: A" in config
    assert "port: 8000" in config
    assert "refresh_interval: 5s" in config
    assert "static_configs:" not in config
    assert "/etc/prometheus/alerts.yml" in config
    for alert_name in (
        "JobAgentWebDown",
        "JobAgentHighServerErrorRate",
        "JobAgentSlowAverageResponse",
        "JobAgentSecurityRejectionsSpike",
        "JobAgentHighInFlightRequests",
        "JobAgentConcurrencyProtectionErrors",
        "JobAgentConcurrencyCapacityPressure",
    ):
        assert f"alert: {alert_name}" in alerts
    assert "job_agent_rate_limit_backend_errors_total" in alerts
    assert 'sum(up{job="job-hunting-agent-web"}) == 0' in alerts
    assert 'absent(up{job="job-hunting-agent-web"})' in alerts
    assert "sum(rate(job_agent_http_request_duration_seconds_sum[5m]))" in alerts
    assert "sum(job_agent_http_requests_in_flight) >= 20" in alerts


def test_multi_replica_validation_is_repeatable_and_restores_development():
    overlay = (ROOT / "compose.scale-test.yaml").read_text(encoding="utf-8")
    script = (ROOT / "scripts" / "validate_multi_replica.ps1").read_text(
        encoding="utf-8"
    )

    assert "ports: !reset []" in overlay
    assert 'JOB_AGENT_RATE_LIMIT_AUTH_REQUESTS: "4"' in overlay
    assert '"--scale", "web=$Replicas"' in script
    assert "shared Redis limiter" in script
    assert "shared Redis model concurrency lease" in script
    assert "ConcurrencyLimitExceeded" in script
    assert "Shared Redis model concurrency lease: PASS" in script
    assert 'Invoke-RestMethod "http://127.0.0.1:9090/api/v1/targets"' in script
    assert "$_.labels.instance" in script
    assert "$RestoreFiles" in script
    assert '"--scale", "web=1"' in script


def test_backup_and_restore_scripts_require_safe_operational_guards():
    backup = (ROOT / "scripts" / "backup.ps1").read_text(encoding="utf-8")
    restore = (ROOT / "scripts" / "restore.ps1").read_text(encoding="utf-8")
    guide = (ROOT / "docs" / "learning" / "production-release.md").read_text(encoding="utf-8")

    assert "pg_dump" in backup
    assert "minio-data.tar.gz" in backup
    assert "manifest.json" in backup
    assert "Redis is a rebuildable broker/cache" in backup
    assert "[switch]$ConfirmRestore" in restore
    assert "Restore is destructive" in restore
    assert "pg_restore" in restore
    assert "docker compose -f compose.yaml -f compose.prod.yaml up -d --no-build" in guide
    assert "RPO" in guide
    assert "RTO" in guide
