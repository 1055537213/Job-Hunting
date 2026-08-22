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
        "docker build --tag job-hunting-agent:ci .",
    ):
        assert required in workflow


def test_production_compose_does_not_expose_internal_services():
    compose = (ROOT / "compose.prod.yaml").read_text(encoding="utf-8")

    assert "POSTGRES_HOST_AUTH_METHOD: scram-sha-256" in compose
    assert "ports: !reset []" in compose
    assert "JOB_AGENT_ENVIRONMENT: production" in compose
    assert "JOB_AGENT_COOKIE_SECURE: \"true\"" in compose
    assert "JOB_AGENT_OBJECT_STORAGE_AUTO_CREATE_BUCKET: \"false\"" in compose
    assert "reverse-proxy:" in compose
    assert "caddy:2.9.1-alpine" in compose
    assert '"80:80"' in compose
    assert '"443:443"' in compose
    assert "condition: service_healthy" in compose
    assert "postgres_prod_data" in compose
    assert "minio_prod_data" in compose
    assert "redis_prod_data" in compose


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
