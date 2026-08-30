"""Container publishing and protected production deployment regressions."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess


ROOT = Path(__file__).resolve().parents[1]


def _production_compose_config(*extra_files: str) -> dict:
    environment = os.environ.copy()
    environment.update(
        {
            "COMPOSE_PROFILES": "",
            "JOB_AGENT_ACCOUNT_ACTION_SECRET": "test-account-action-secret-that-is-long-enough",
            "JOB_AGENT_ALERT_EMAIL_TO": "alerts@example.invalid",
            "JOB_AGENT_COEXIST_ALERTMANAGER_PORT": "19093",
            "JOB_AGENT_COEXIST_PROMETHEUS_PORT": "19090",
            "JOB_AGENT_COEXIST_PROMETHEUS_RETENTION": "7d",
            # The coexist Web port is intentionally immutable; this value must be ignored.
            "JOB_AGENT_COEXIST_WEB_PORT": "28081",
            "JOB_AGENT_GRAFANA_ADMIN_PASSWORD": "test-grafana-password",
            "JOB_AGENT_IMAGE": "ghcr.io/example/job-agent:sha-0123456789ab",
            "JOB_AGENT_DOMAIN": "agent.example.invalid",
            "JOB_AGENT_OBJECT_STORAGE_ACCESS_KEY": "test-access-key",
            "JOB_AGENT_OBJECT_STORAGE_SECRET_KEY": "test-secret-key",
            "JOB_AGENT_POSTGRES_PASSWORD": "test-postgres-password",
            "JOB_AGENT_PUBLIC_IP": "203.0.113.10",
            "JOB_AGENT_REDIS_PASSWORD": "test-redis-password",
            "JOB_AGENT_SMTP_FROM_EMAIL": "sender@example.invalid",
            "JOB_AGENT_SMTP_HOST": "smtp.example.invalid",
            "JOB_AGENT_SMTP_PASSWORD": "test-smtp-password",
            "JOB_AGENT_SMTP_USERNAME": "test-smtp-user",
            "JOB_AGENT_TLS_EMAIL": "tls@example.invalid",
        }
    )
    command = [
        "docker",
        "compose",
        "-f",
        "compose.yaml",
        "-f",
        "compose.prod.yaml",
    ]
    for compose_file in extra_files:
        command.extend(("-f", compose_file))
    command.extend(("config", "--format", "json"))
    completed = subprocess.run(
        command,
        cwd=ROOT,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(completed.stdout)


def test_coexist_topology_keeps_only_lightweight_services_and_loopback_web():
    config = _production_compose_config("compose.coexist.yaml")

    assert set(config["services"]) == {
        "alertmanager",
        "alertmanager-config",
        "beat",
        "clamav",
        "coexist-https",
        "migrate",
        "minio",
        "postgres",
        "prometheus",
        "redis",
        "web",
        "worker",
    }
    assert config["services"]["web"]["ports"] == [
        {
            "mode": "ingress",
            "target": 8000,
            "published": "18081",
            "host_ip": "127.0.0.1",
            "protocol": "tcp",
        }
    ]
    assert config["services"]["coexist-https"]["ports"] == [
        {
            "mode": "ingress",
            "target": 8443,
            "published": "8443",
            "host_ip": "0.0.0.0",
            "protocol": "tcp",
        }
    ]
    assert config["services"]["coexist-https"]["environment"] == {
        "JOB_AGENT_PUBLIC_IP": "203.0.113.10",
        "NGINX_ENVSUBST_FILTER": "JOB_AGENT_PUBLIC_IP",
    }
    assert config["services"]["coexist-https"]["depends_on"]["web"] == {
        "condition": "service_healthy",
        "required": True,
    }
    assert {
        volume["target"]: volume
        for volume in config["services"]["coexist-https"]["volumes"]
    }["/etc/letsencrypt"]["read_only"] is True
    for service in ("web", "worker", "beat"):
        assert config["services"][service]["environment"]["JOB_AGENT_OTEL_ENABLED"] == "false"


def test_production_deployment_exposes_an_explicit_coexist_topology():
    workflow = (
        ROOT / ".github" / "workflows" / "deploy-production.yml"
    ).read_text(encoding="utf-8")
    script = (ROOT / "scripts" / "deploy_production.sh").read_text(
        encoding="utf-8"
    )

    for required in (
        "topology:",
        "type: choice",
        "- standalone",
        "- coexist",
        "DEPLOY_TOPOLOGY: ${{ inputs.topology }}",
        "BUNDLE_FILES=(compose.yaml compose.prod.yaml deploy scripts/deploy_production.sh)",
        "BUNDLE_FILES+=(compose.coexist.yaml)",
        "compose.coexist.yaml",
        'TOPOLOGY_ARGUMENT=""',
        "TOPOLOGY_ARGUMENT=\" 'coexist'\"",
        "'${IMAGE_REF}'${TOPOLOGY_ARGUMENT}",
    ):
        assert required in workflow

    for required in (
        "[standalone|coexist]",
        'DEPLOY_TOPOLOGY="${4:-standalone}"',
        "Unsupported deployment topology",
        'ACTIVE_TOPOLOGY="$DEPLOY_TOPOLOGY"',
        'if [[ "$ACTIVE_TOPOLOGY" == "coexist" ]]',
        '"${ACTIVE_RELEASE_DIR}/compose.coexist.yaml"',
        '"${RELEASE_DIR}/deploy/nginx/coexist-ip-https.conf.template"',
    ):
        assert required in script


def test_production_deployment_restores_and_persists_the_release_topology():
    script = (ROOT / "scripts" / "deploy_production.sh").read_text(
        encoding="utf-8"
    )

    for required in (
        'PREVIOUS_TOPOLOGY="standalone"',
        '"${STATE_DIR}/current-topology"',
        'ACTIVE_TOPOLOGY="$PREVIOUS_TOPOLOGY"',
        'topology_files_exist "$PREVIOUS_RELEASE" "$PREVIOUS_TOPOLOGY"',
        '"${STATE_DIR}/current-topology.tmp"',
        'mv "${STATE_DIR}/current-topology.tmp" "${STATE_DIR}/current-topology"',
    ):
        assert required in script


def test_production_deployment_reports_a_failed_rollback_as_failed():
    script = (ROOT / "scripts" / "deploy_production.sh").read_text(
        encoding="utf-8"
    )

    for required in (
        "if ! compose_active config --quiet; then",
        "if ! compose_active up -d --no-build --pull missing --remove-orphans; then",
        "if ! wait_for_healthy_service web 300; then",
        "if ! verify_coexist_web_binding; then",
        "if ! rollback_previous_release; then",
        "Rollback failed; previous release was not restored.",
    ):
        assert required in script
    assert "rollback_previous_release || true" not in script


def test_coexist_deployment_verifies_the_loopback_web_endpoint():
    script = (ROOT / "scripts" / "deploy_production.sh").read_text(
        encoding="utf-8"
    )

    for required in (
        "verify_coexist_web_binding",
        "compose_active port web 8000",
        'if [[ "$binding" != "127.0.0.1:18081" ]]',
        '"http://${binding}/api/health"',
        'if [[ "$ACTIVE_TOPOLOGY" == "coexist" ]]',
    ):
        assert required in script
    assert 'for required_command in docker readlink sha256sum; do' in script
    assert (
        'if [[ "$DEPLOY_TOPOLOGY" == "coexist" || "$PREVIOUS_TOPOLOGY" == "coexist" ]]'
        in script
    )
    assert "Required command is unavailable for coexist topology: curl" in script


def test_coexist_deployment_verifies_the_public_ip_https_endpoint():
    script = (ROOT / "scripts" / "deploy_production.sh").read_text(
        encoding="utf-8"
    )

    for required in (
        "verify_coexist_https_endpoint",
        "compose_active port coexist-https 8443",
        'if [[ "$binding" != "0.0.0.0:8443" ]]',
        "wait_for_healthy_service coexist-https 180",
        'compose_active exec -T coexist-https sh -ec',
        '--resolve "${public_ip}:8443:127.0.0.1"',
        '"https://${public_ip}:8443/api/health"',
    ):
        assert required in script


def test_coexist_certificate_reload_hook_targets_only_the_https_edge():
    hook = ROOT / "scripts" / "reload_coexist_https.sh"

    assert hook.exists()
    script = hook.read_text(encoding="utf-8")
    for required in (
        "set -Eeuo pipefail",
        'APP_ROOT="${1:-/opt/job-hunting-agent}"',
        '"${STATE_DIR}/current-image"',
        "compose.coexist.yaml",
        "ps -q coexist-https",
        "exec -T coexist-https nginx -t",
        "exec -T coexist-https nginx -s reload",
    ):
        assert required in script
    assert "restart" not in script


def test_coexist_deployment_removes_only_inactive_job_agent_containers():
    script = (ROOT / "scripts" / "deploy_production.sh").read_text(
        encoding="utf-8"
    )

    for required in (
        "remove_inactive_coexist_services",
        "reverse-proxy loki tempo alloy grafana",
        'COMPOSE_PROFILES=""',
        "compose_active stop",
        "compose_active rm --force --stop",
    ):
        assert required in script
    assert "down -v" not in script


def test_production_guide_and_template_document_the_coexist_topology():
    guide = (ROOT / "docs" / "learning" / "production-release.md").read_text(
        encoding="utf-8"
    )
    template = (ROOT / "deploy" / "env.production.example").read_text(
        encoding="utf-8"
    )

    for required in (
        "compose.coexist.yaml",
        "127.0.0.1:18081",
        "0.0.0.0:8443",
        "https://121.40.128.252:8443",
        "--preferred-profile shortlived",
        "--ip-address 121.40.128.252",
        "/.well-known/acme-challenge/",
        "Loki、Tempo、Alloy 和 Grafana",
        "先完成回环地址验收",
        "topology` 选择 `coexist",
    ):
        assert required in guide

    for required in (
        "JOB_AGENT_COEXIST_PROMETHEUS_PORT=19090",
        "JOB_AGENT_COEXIST_ALERTMANAGER_PORT=19093",
        "JOB_AGENT_COEXIST_PROMETHEUS_RETENTION=7d",
        "JOB_AGENT_PUBLIC_IP=replace-with-public-ip",
        "JOB_AGENT_LETSENCRYPT_DIR=/etc/letsencrypt",
    ):
        assert required in template
    assert "JOB_AGENT_COEXIST_WEB_PORT" not in template


def test_release_image_is_published_only_after_successful_master_ci():
    workflow = (
        ROOT / ".github" / "workflows" / "publish-image.yml"
    ).read_text(encoding="utf-8")

    for required in (
        "workflow_run:",
        'workflows: ["CI"]',
        "github.event.workflow_run.conclusion == 'success'",
        "github.event.workflow_run.event == 'push'",
        "github.event.workflow_run.head_branch == 'master'",
        "packages: write",
        "github.event.workflow_run.head_sha",
        "git merge-base --is-ancestor",
        'SHORT_SHA="${HEAD_SHA:0:12}"',
        "ghcr.io/${GITHUB_REPOSITORY,,}",
        'org.opencontainers.image.revision=${HEAD_SHA}',
        "docker manifest inspect",
        "Refusing to overwrite",
        "Skipping the master convenience tag",
        "--ignore-unfixed",
        "docker push \"${VERSION_REF}\"",
        "docker push \"${MASTER_REF}\"",
        "release-image.json",
    ):
        assert required in workflow


def test_ci_validates_all_github_actions_workflows():
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(
        encoding="utf-8"
    )

    assert (
        "rhysd/actionlint@sha256:"
        "b1934ee5f1c509618f2508e6eb47ee0d3520686341fec936f3b79331f9315667"
    ) in workflow
    assert 'docker run --rm -v "$PWD:/repo" -w /repo "$ACTIONLINT_IMAGE"' in workflow
    assert "-f compose.prod.yaml -f compose.coexist.yaml config --quiet" in workflow
    assert "JOB_AGENT_PUBLIC_IP: 203.0.113.10" in workflow


def test_production_deployment_requires_manual_confirmation_and_environment():
    workflow = (
        ROOT / ".github" / "workflows" / "deploy-production.yml"
    ).read_text(encoding="utf-8")

    for required in (
        "workflow_dispatch:",
        "commit_sha:",
        "confirmation:",
        "environment: production",
        "packages: read",
        '[[ "${CONFIRMATION}" == "DEPLOY" ]]',
        "git merge-base --is-ancestor",
        "org.opencontainers.image.revision",
        "DEPLOY_KNOWN_HOSTS",
        "StrictHostKeyChecking=yes",
        "docker save \"${IMAGE_REF}\"",
        "BUNDLE_FILES=(compose.yaml compose.prod.yaml deploy scripts/deploy_production.sh)",
        "scripts/deploy_production.sh",
    ):
        assert required in workflow

    assert "ssh-keyscan" not in workflow
    assert "StrictHostKeyChecking=no" not in workflow


def test_remote_deployment_validates_health_and_can_restore_previous_release():
    script = (ROOT / "scripts" / "deploy_production.sh").read_text(
        encoding="utf-8"
    )

    for required in (
        "set -Eeuo pipefail",
        "shared/.env",
        "docker compose",
        "org.opencontainers.image.revision",
        "config --quiet",
        "pg_dump",
        "--no-build --pull missing --remove-orphans",
        "wait_for_active_topology_services",
        "wait_for_healthy_service web",
        "worker beat prometheus alertmanager",
        "reverse-proxy loki tempo alloy grafana",
        'wait_for_running_service "$service" 180',
        "wait_for_completed_service alertmanager-config",
        "deploy/alloy/config.alloy",
        "deploy/grafana/provisioning/datasources/datasources.yml",
        "rollback_previous_release",
        "current-image",
        "ln -sfnT",
    ):
        assert required in script


def test_production_guide_documents_cd_setup_and_secret_boundaries():
    guide = (ROOT / "docs" / "learning" / "production-release.md").read_text(
        encoding="utf-8"
    )

    for required in (
        "publish-image.yml",
        "deploy-production.yml",
        "production Environment",
        "DEPLOY_SSH_PRIVATE_KEY",
        "DEPLOY_KNOWN_HOSTS",
        "DEPLOY_PATH",
        "JOB_AGENT_IMAGE",
        "sha-<commit 前 12 位>",
        "不会上传生产 `.env`",
    ):
        assert required in guide
