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
        "grafana/alloy:v1.18.0",
        "grafana/loki:3.7.4",
        "grafana/tempo:2.10.5",
        "prom/alertmanager:v0.32.1",
        "check-config /etc/alertmanager/alertmanager.yml",
        "docker build --pull --no-cache --tag job-hunting-agent:ci .",
        'PIP_AUDIT_VERSION: "2.10.1"',
        "python -m pip_audit",
        "--pkg-types os",
        "--ignore-unfixed",
        "--format cyclonedx",
        "container-vulnerabilities.json",
        "image-sbom.cdx.json",
        "actions/upload-artifact@ea165f8d65b6e75b540449e92b4886f43607fa02",
    ):
        assert required in workflow


def test_supply_chain_security_gate_is_pinned_and_reproducible():
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    compose = (ROOT / "compose.yaml").read_text(encoding="utf-8")
    script = (ROOT / "scripts" / "security_scan.ps1").read_text(encoding="utf-8")
    guide = (ROOT / "docs" / "learning" / "security-scanning.md").read_text(
        encoding="utf-8"
    )
    python_image = (
        "python:3.12.13-slim@sha256:"
        "229a2c5bfa27522db7815ea81f9bed70af17ccb9de9fc7ad142b1877b5830d36"
    )
    trivy_image = (
        "aquasec/trivy@sha256:"
        "62b1e65e8869bc4b4c6aa4fa2b21595256c7c2f6018a9d9ad61caf87187c1969"
    )

    assert f"ARG BASE_IMAGE={python_image}" in dockerfile
    assert "apt-get upgrade -y" in dockerfile
    assert compose.count(python_image) == 4
    assert '$pipAuditVersion = "2.10.1"' in script
    assert f'$trivyImage = "{trivy_image}"' in script
    assert "/workspace/requirements.lock:ro" in script
    assert "/workspace/requirements-dev.lock:ro" in script
    assert '"build",' in script
    assert '"--pull",' in script
    assert '"--no-cache",' in script
    assert '"--pkg-types", "os"' in script
    assert '"--ignore-unfixed"' in script
    assert '"image-sbom.cdx.json"' in script
    assert '"security-summary.json"' in script
    assert not (ROOT / ".trivyignore").exists()
    assert "没有 `package.json`" in guide
    assert "明确到期时间" in guide


def test_clamav_acceptance_is_isolated_and_fails_closed():
    production = (ROOT / "compose.prod.yaml").read_text(encoding="utf-8")
    overlay = (ROOT / "compose.file-scan-test.yaml").read_text(encoding="utf-8")
    script = (ROOT / "scripts" / "validate_file_scanning.ps1").read_text(
        encoding="utf-8"
    )
    guide = (
        ROOT / "docs" / "learning" / "file-scanning-acceptance.md"
    ).read_text(encoding="utf-8")
    clamav_image = (
        "clamav/clamav:1.4.6@sha256:"
        "761f6c99b8d9134b39431f8c200189cda749b17310091561bfa8b732f32bfada"
    )

    assert clamav_image in production
    assert clamav_image in overlay
    assert "clamdscan --ping 1 >/dev/null" in production
    assert "--host 127.0.0.1" not in production
    assert 'memory: 4G' in production
    assert overlay.count("ports: !reset []") == 5
    assert "JOB_AGENT_ENVIRONMENT: production" in overlay
    assert "JOB_AGENT_FILE_SCAN_BACKEND: clamav" in overlay
    assert "sigtool --info" in script
    assert "Build time:" in script
    assert "EICAR-STANDARD-ANTIVIRUS-TEST-FILE" in script
    assert '@("stop", "clamav")' in script
    assert '@("start", "clamav")' in script
    assert '"--reload"' in script
    assert "remaining_objects" in script
    assert "database_and_object_cleanup_passed" in script
    assert "down -v" in guide


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
    assert 'targets: ["alertmanager:9093"]' in config
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


def test_worker_recovery_acceptance_is_present_and_isolated():
    overlay = (ROOT / "compose.acceptance.yaml").read_text(encoding="utf-8")
    script = (ROOT / "scripts" / "validate_worker_recovery.ps1").read_text(
        encoding="utf-8"
    )

    assert 'JOB_AGENT_TASK_TIME_LIMIT_SECONDS: "30"' in overlay
    assert 'JOB_AGENT_TASK_SOFT_TIME_LIMIT_SECONDS: "25"' in overlay
    assert 'JOB_AGENT_TASK_STALE_AFTER_SECONDS: "60"' in overlay
    assert '"run", "--rm", "migrate"' in script
    assert "worker_recovery_acceptance" in script
    assert '"--queue", $MaintenanceQueue' in script
    assert '"kill", "--signal", "KILL"' in script
    assert "Beat stale-task recovery: PASS" in script
    assert "JOB_AGENT_ACCEPTANCE_IDEMPOTENCY_KEY" in script
    assert "account_balance_ledger" in script
    assert "resume_artifacts" in script
    assert '"--force-recreate", "--scale", "web=1"' in script
    assert "Automatic topology restore failed" in script


def test_backup_and_restore_scripts_require_safe_operational_guards():
    backup = (ROOT / "scripts" / "backup.ps1").read_text(encoding="utf-8")
    restore = (ROOT / "scripts" / "restore.ps1").read_text(encoding="utf-8")
    drill = (ROOT / "scripts" / "validate_backup_restore.ps1").read_text(
        encoding="utf-8"
    )
    overlay = (ROOT / "compose.recovery-test.yaml").read_text(encoding="utf-8")
    guide = (ROOT / "docs" / "learning" / "production-release.md").read_text(encoding="utf-8")

    assert "pg_dump" in backup
    assert "minio-data.tar.gz" in backup
    assert "manifest.json" in backup
    assert "Redis is a rebuildable broker/cache" in backup
    assert '[string]$ProjectName = "job-hunting-agent-production"' in backup
    assert '[string[]]$ComposeFiles = @()' in backup
    assert "Get-MinioVolumeName" in backup
    assert "[switch]$ConfirmRestore" in restore
    assert "Restore is destructive" in restore
    assert "pg_restore" in restore
    assert "Get-FileHash -Algorithm SHA256" in restore
    assert "Backup manifest and SHA-256 verification: PASS" in restore
    assert restore.index("Get-FileHash -Algorithm SHA256") < restore.index(
        "Stop-AvailableServices -Services"
    )
    assert "ports: !reset []" in overlay
    assert '"down", "-v", "--remove-orphans"' in drill
    assert "manifest_tamper_rejected" in drill
    assert "missing_manifest_rejected" in drill
    assert "postgres_dump_tamper_rejected" in drill
    assert "queued_task_state_restored" in drill
    assert "post_backup_database_change_removed" in drill
    assert "post_backup_object_removed" in drill
    assert "recovery_time_objective_observed_seconds" in drill
    assert "docker compose -f compose.yaml -f compose.prod.yaml up -d --no-build" in guide
    assert "RPO" in guide
    assert "RTO" in guide


def test_linux_production_recovery_drill_is_bundled_and_isolated():
    script = (ROOT / "scripts" / "validate_production_recovery.sh").read_text(
        encoding="utf-8"
    )
    workflow = (
        ROOT / ".github" / "workflows" / "deploy-production.yml"
    ).read_text(encoding="utf-8")

    for required in (
        '"BACKUP_AND_VALIDATE"',
        'LOCK_DIR="${STATE_DIR}/production-recovery.lock"',
        'RECOVERY_PREFIX="job-agent-recovery-${SUFFIX}"',
        '"${MINIO_VOLUME}:/data:ro"',
        'docker network create "$RECOVERY_NETWORK"',
        'docker volume create "$RECOVERY_POSTGRES_VOLUME"',
        'docker volume create "$RECOVERY_MINIO_VOLUME"',
        '"production_data_modified": False',
        '"isolated_restore_used_unique_volumes": True',
        'PRODUCTION_QUIESCED=1',
        'compose_production up -d --no-build',
    ):
        assert required in script
    assert "docker volume rm" in script
    assert "validate_production_recovery.sh" in workflow
    assert "chmod 700" in workflow


def test_local_release_acceptance_pack_covers_recovery_uploads_and_alert_delivery():
    orchestrator = (ROOT / "scripts" / "validate_local_release.ps1").read_text(
        encoding="utf-8"
    )
    alert_script = (ROOT / "scripts" / "validate_alert_delivery.ps1").read_text(
        encoding="utf-8"
    )
    alert_overlay = (ROOT / "compose.observability-test.yaml").read_text(
        encoding="utf-8"
    )

    assert "validate_backup_restore.ps1" in orchestrator
    assert "validate_file_scanning.ps1" in orchestrator
    assert "validate_alert_delivery.ps1" in orchestrator
    assert "tests/test_upload_security.py" in orchestrator
    assert "local-release-report.json" in orchestrator
    assert "JobAgentLocalAlertDeliveryAcceptance" in alert_script
    assert "api/v1/messages" in alert_script
    assert "smtp_require_tls=False" in alert_script
    assert 'tests_passed = $TestsPassed' in alert_script
    assert 'tests_passed = $TestsPassed' in orchestrator
    assert "axllent/mailpit:v1.30.6" in alert_overlay
    assert "ports:" not in alert_overlay
