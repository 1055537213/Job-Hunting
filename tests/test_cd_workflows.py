"""Container publishing and protected production deployment regressions."""

from __future__ import annotations

import json
import importlib
import os
from pathlib import Path
import subprocess
import pytest


ROOT = Path(__file__).resolve().parents[1]


def test_hybrid_browser_acceptance_imports_before_network_or_docker(monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / "scripts"))
    module = importlib.import_module("validate_python_java_docker")

    class DependenciesImported(Exception):
        pass

    def stop_before_network(**kwargs):
        raise DependenciesImported()

    monkeypatch.setattr(module.httpx, "Client", stop_before_network)
    with pytest.raises(DependenciesImported):
        module.run_browser_gateway_flow("http://gateway", "http://web", "http://java", "secret",
                                        "unused-container", ("admin@example.com", "unused"), "unused-database")


def _production_compose_config(*extra_files: str) -> dict:
    environment = os.environ.copy()
    environment.update(
        {
            "COMPOSE_PROFILES": "",
            "JOB_AGENT_ACCOUNT_ACTION_SECRET": "test-account-action-secret-that-is-long-enough",
            "JOB_AGENT_TERMS_VERSION": "test-terms-v1",
            "JOB_AGENT_PRIVACY_VERSION": "test-privacy-v1",
            "JOB_AGENT_ALERT_EMAIL_TO": "alerts@example.invalid",
            "JOB_AGENT_COEXIST_ALERTMANAGER_PORT": "19093",
            "JOB_AGENT_COEXIST_PROMETHEUS_PORT": "19090",
            "JOB_AGENT_COEXIST_PROMETHEUS_RETENTION": "7d",
            # The coexist Web port is intentionally immutable; this value must be ignored.
            "JOB_AGENT_COEXIST_WEB_PORT": "28081",
            "JOB_AGENT_GRAFANA_ADMIN_PASSWORD": "test-grafana-password",
            "JOB_AGENT_IMAGE": "ghcr.io/example/job-agent:sha-0123456789ab",
            "JOB_AGENT_PLATFORM_IMAGE": "ghcr.io/example/job-platform:sha-0123456789ab",
            "JOB_AGENT_PLATFORM_INTERNAL_TOKEN": "test-platform-secret",
            "JOB_AGENT_PLATFORM_METRICS_TOKEN": "test-independent-metrics-secret-at-least-32",
            "JOB_AGENT_DOMAIN": "agent.example.invalid",
            "JOB_AGENT_OBJECT_STORAGE_ACCESS_KEY": "test-access-key",
            "JOB_AGENT_OBJECT_STORAGE_SECRET_KEY": "test-secret-key",
            "JOB_AGENT_POSTGRES_PASSWORD": "test-postgres-password",
            "JOB_AGENT_PUBLIC_BASE_URL": "https://203.0.113.10:8443",
            "JOB_AGENT_PUBLIC_IP": "203.0.113.10",
            "JOB_AGENT_REDIS_PASSWORD": "test-redis-password",
            "JOB_AGENT_RUNTIME_ENV_FILE": ".env.example",
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
        "JOB_AGENT_PUBLIC_BASE_URL": "https://203.0.113.10:8443",
        "JOB_AGENT_PUBLIC_IP": "203.0.113.10",
        "NGINX_ENVSUBST_FILTER": "JOB_AGENT_PUBLIC_IP",
    }
    assert config["services"]["coexist-https"]["depends_on"]["web"] == {
        "condition": "service_healthy",
        "required": True,
    }
    coexist_volumes = {
        volume["target"]: volume
        for volume in config["services"]["coexist-https"]["volumes"]
    }
    for target in (
        "/etc/letsencrypt/live/203.0.113.10",
        "/etc/letsencrypt/archive/203.0.113.10",
    ):
        assert coexist_volumes[target]["read_only"] is True
    assert "/etc/letsencrypt" not in coexist_volumes
    for service in ("web", "worker", "beat"):
        assert config["services"][service]["environment"]["JOB_AGENT_OTEL_ENABLED"] == "false"


def test_production_runtime_env_is_injected_without_mounting_host_secret_file():
    config = _production_compose_config("compose.coexist.yaml")

    for service in ("migrate", "web", "worker", "beat", "alertmanager-config"):
        mounted_targets = {
            volume["target"] for volume in config["services"][service].get("volumes", [])
        }
        assert "/app/.env" not in mounted_targets

    for service in ("web", "worker", "beat", "alertmanager-config"):
        environment = config["services"][service]["environment"]
        assert "JOB_AGENT_LLM_PROVIDER" in environment
        assert "JOB_AGENT_LLM_API_KEY" in environment


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
        "scripts/validate_production_user_flow.py",
        "scripts/validate_production_user_flow.sh",
        "BUNDLE_FILES+=(compose.coexist.yaml scripts/reload_coexist_https.sh)",
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
        '"${RELEASE_DIR}/scripts/reload_coexist_https.sh"',
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
        "coexist_https_available",
        '[[ -f "${ACTIVE_RELEASE_DIR}/deploy/nginx/coexist-ip-https.conf.template" ]]',
        "if coexist_https_available; then",
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
        'expected_public_base_url="https://${public_ip}:8443"',
        'if [[ "$public_base_url" != "$expected_public_base_url" ]]',
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
        'JOB_AGENT_RUNTIME_ENV_FILE="$SHARED_ENV"',
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
        "https://<PUBLIC_IP>:8443",
        "snap install certbot --classic",
        "Certbot 5.4.0",
        "--preferred-profile shortlived",
        '--ip-address "$PUBLIC_IP"',
        "snap.certbot.renew.timer",
        "renew --dry-run --run-deploy-hooks",
        "/.well-known/acme-challenge/",
        "Loki、Tempo、Alloy 和 Grafana",
        "先完成回环地址验收",
        "topology` 选择 `coexist",
    ):
        assert required in guide
    assert "121.40.128.252" not in guide

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
    assert "JOB_AGENT_PUBLIC_BASE_URL: https://203.0.113.10:8443" in workflow
    assert "Validate coexist Nginx HTTPS configuration" in workflow
    assert "coexist-ip-https.conf.template:/etc/nginx/templates/default.conf.template:ro" in workflow
    assert "nginx -t" in workflow


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
        "IMAGE_DIGEST_REF",
        "steps.verified_image.outputs.digest_ref",
        "DEPLOY_KNOWN_HOSTS",
        "StrictHostKeyChecking=yes",
        "ServerAliveInterval=15",
        "ServerAliveCountMax=12",
        "ConnectTimeout=20",
        "for attempt in 1 2 3 4 5 6",
        '[[ "$PULL_SUCCEEDED" == "true" ]]',
        "docker pull '${IMAGE_DIGEST_REF}'",
        "timeout --signal=TERM --kill-after=15s 180s docker pull '${IMAGE_DIGEST_REF}'",
        "docker tag '${IMAGE_DIGEST_REF}' '${IMAGE_REF}'",
        '"https://${DEPLOY_HOST}:8443/api/health"',
        "BUNDLE_FILES=(compose.yaml compose.prod.yaml deploy scripts/deploy_production.sh)",
        "scripts/deploy_production.sh",
        "scripts/validate_production_user_flow.py",
        "scripts/validate_production_user_flow.sh",
    ):
        assert required in workflow

    assert "ssh-keyscan" not in workflow
    assert "StrictHostKeyChecking=no" not in workflow
    assert "docker save" not in workflow


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


def test_java_platform_release_has_healthcheck_and_security_gates():
    dockerfile = (ROOT / "platform-service" / "Dockerfile").read_text(encoding="utf-8")
    assert "HEALTHCHECK" in dockerfile

    ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert "publish-platform:" in ci
    assert "refs/heads/java-platform-migration" in ci
    assert "-platform:sha-" in ci
    for required in (
        "OSV_SCANNER_IMAGE: ghcr.io/google/osv-scanner@sha256:",
        "name: Audit Java dependencies",
        "id: java_audit",
        "java-dependencies.json",
        "steps.java_audit.outcome",
    ):
        assert required in ci


def test_migration_push_runs_fast_checks_and_only_full_ci_publishes():
    ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert "branches: [master, java-platform-migration]" in ci
    assert "workflow_dispatch:" in ci
    assert "fast-checks:\n    if: github.event_name == 'push' && github.ref == 'refs/heads/java-platform-migration' && !contains(github.event.head_commit.message, '[full ci]')" in ci
    assert "checks:\n    if: github.event_name != 'push' || github.ref != 'refs/heads/java-platform-migration' || contains(github.event.head_commit.message, '[full ci]')" in ci
    assert "publish-platform:\n    if: github.ref == 'refs/heads/java-platform-migration' && (github.event_name == 'workflow_dispatch' || (github.event_name == 'push' && contains(github.event.head_commit.message, '[full ci]')))" in ci
    for required in (
        "Run focused Python tests",
        "tests/test_platform_auth.py",
        "tests/test_platform_billing.py",
        "Run frontend regression tests",
        "Run Java platform service tests",
        "Validate GitHub Actions workflows",
    ):
        assert required in ci

    deploy = (ROOT / ".github" / "workflows" / "deploy-hybrid.yml").read_text(encoding="utf-8")
    assert "head_sha=$SHA&per_page=100" in deploy
    assert '.head_sha == \\"$SHA\\"' in deploy
    assert '.name == "checks" or .name == "publish-platform"' in deploy
    assert '[[ "$completed_jobs" == "checks,publish-platform" ]]' in deploy
    assert 'if [[ "$verified" != true ]]; then' in deploy


def test_hybrid_release_pair_and_production_compose_are_connected(monkeypatch):
    monkeypatch.setenv("PLATFORM_RATE_LIMIT_KEY_PREFIX", "job_agent:ci_platform_limit")
    monkeypatch.setenv("JOB_AGENT_DEMO_RECHARGE_ENABLED", "")
    monkeypatch.setenv("JOB_AGENT_BILLING_PRICE_PER_MILLION_TOKENS_YUAN", "30")
    monkeypatch.setenv("JOB_AGENT_BILLING_LOW_BALANCE_THRESHOLD_YUAN", "12.5")
    for extra in (("compose.hybrid.prod.yaml",), ("compose.coexist.yaml", "compose.hybrid.prod.yaml")):
        config = _production_compose_config(*extra)
        java = config["services"]["platform-service"]
        assert not java.get("ports")
        assert "build" not in java
        assert java["environment"]["PLATFORM_AUTH_ENABLED"] == "true"
        assert java["environment"]["JOB_AGENT_DEMO_RECHARGE_ENABLED"] == "false"
        assert java["environment"]["JOB_AGENT_BILLING_PRICE_PER_MILLION_TOKENS_YUAN"] == "30"
        assert java["environment"]["JOB_AGENT_BILLING_LOW_BALANCE_THRESHOLD_YUAN"] == "12.5"
        assert java["environment"]["PLATFORM_WEB_SECURITY_ENABLED"] == "true"
        assert java["environment"]["PLATFORM_REDIS_HOST"] == "redis"
        assert java["environment"]["PLATFORM_REDIS_PASSWORD"]
        assert java["environment"]["PLATFORM_RATE_LIMIT_KEY_PREFIX"] == "job_agent:ci_platform_limit"
        assert java["environment"]["PLATFORM_METRICS_ENABLED"] == "true"
        assert java["environment"]["PLATFORM_METRICS_TOKEN_FILE"] == "/run/secrets/platform_metrics_token"
        assert "PLATFORM_METRICS_TOKEN" not in java["environment"]
        assert java["secrets"][0]["source"] == "platform_metrics_token"
        prometheus = config["services"]["prometheus"]
        assert prometheus["secrets"][0]["source"] == "platform_metrics_token"
        assert any(volume["source"].endswith("prometheus.hybrid.yml") for volume in prometheus["volumes"])
        assert java["depends_on"]["redis"]["condition"] == "service_healthy"
        assert java["depends_on"]["migrate"]["condition"] == "service_completed_successfully"
        for name in ("web", "worker", "beat"):
            service = config["services"][name]
            assert service["depends_on"]["platform-service"]["condition"] == "service_healthy"
            assert service["environment"]["JOB_AGENT_JAVA_AUTH_ENABLED"] == "true"
            assert service["environment"]["JOB_AGENT_JAVA_AUTH_INTERNAL_TOKEN"] == java["environment"]["PLATFORM_INTERNAL_TOKEN"]
        if "reverse-proxy" in config["services"]:
            assert config["services"]["reverse-proxy"]["depends_on"]["platform-service"]["condition"] == "service_healthy"
    workflow = (ROOT / ".github/workflows/deploy-hybrid.yml").read_text(encoding="utf-8")
    for required in ("environment: production", "DEPLOY", "origin/java-platform-migration", ".conclusion == \"success\"",
                     "-ai:$release", "-platform:$release", "org.opencontainers.image.revision", "chmod +x"):
        assert required in workflow
    script = (ROOT / "scripts/deploy_production.sh").read_text(encoding="utf-8")
    assert '"${RELEASE_DIR}/deploy/prometheus/prometheus.hybrid.yml"' in script
    metrics_config = (ROOT / "deploy/prometheus/prometheus.hybrid.yml").read_text(encoding="utf-8")
    assert "credentials_file: /run/secrets/platform_metrics_token" in metrics_config
    assert "credentials:" not in metrics_config
    nginx = (ROOT / "deploy/nginx/coexist-ip-https.conf.template").read_text(encoding="utf-8")
    assert "location ^~ /internal/ {\n        return 404;\n    }" in nginx
    for required in ("compose.hybrid.prod.yaml", "ACTIVE_PLATFORM_IMAGE", "PREVIOUS_PLATFORM_IMAGE",
                     '"$PLATFORM_REVISION" == "$IMAGE_REVISION"', "current-platform-image"):
        assert required in script


def test_hybrid_browser_auth_routes_and_release_assets_are_connected():
    standalone = _production_compose_config("compose.hybrid.prod.yaml")
    assert any(volume["source"].endswith("Caddyfile.hybrid")
               for volume in standalone["services"]["reverse-proxy"]["volumes"])
    coexist = _production_compose_config("compose.coexist.yaml", "compose.hybrid.prod.yaml",
                                         "compose.hybrid.coexist.yaml")
    https = coexist["services"]["coexist-https"]
    assert any(volume["source"].endswith("coexist-hybrid-ip-https.conf.template")
               for volume in https["volumes"])
    assert https["depends_on"]["platform-service"]["condition"] == "service_healthy"
    assert https["ports"] == [{"mode": "ingress", "host_ip": "0.0.0.0", "target": 8443,
                               "published": "8443", "protocol": "tcp"}]
    for name in ("Caddyfile.hybrid", "Caddyfile.hybrid.local"):
        edge = (ROOT / "deploy" / name).read_text(encoding="utf-8")
        assert "@browser_auth path /api/auth/* /api/account/password" in edge
        assert "reverse_proxy @browser_auth platform-service:8081" in edge
        assert "method GET" in edge
        assert "path /api/me/balance /api/me/recharge/orders" in edge
        assert "reverse_proxy @billing_reads platform-service:8081" in edge
        assert "method POST" in edge
        assert "path /api/me/balance/recharge" in edge
        assert "reverse_proxy @personal_recharge platform-service:8081" in edge
        assert "reverse_proxy @admin_billing_reads platform-service:8081" in edge
        assert "reverse_proxy @admin_credit platform-service:8081" in edge
        assert "reverse_proxy @admin_accounts_read platform-service:8081" in edge
        assert "path /api/admin/accounts /api/admin/audit/events" in edge
        assert "method PATCH" in edge
        assert "reverse_proxy @admin_account_status platform-service:8081" in edge
        assert "path /api/admin/usage/summary /api/admin/usage/events" in edge
        assert "reverse_proxy @admin_usage platform-service:8081" in edge
        assert "path_regexp adminToolTraces ^/api/admin/tools/traces(/[^/]+)?$" in edge
        assert "reverse_proxy @admin_tool_traces platform-service:8081" in edge
        assert "path_regexp taskReads ^/api/(admin/tasks|tasks/[^/]+)$" in edge
        assert "reverse_proxy @task_reads platform-service:8081" in edge
        assert "path_regexp adminAccountStatus ^/api/admin/accounts/[0-9]+/status$" in edge
        assert "path_regexp adminBillingReads ^/api/admin/(balance/events|recharge/orders|recharge/orders/[0-9]+/events)$" in edge
        assert "path_regexp adminCredit ^/api/admin/accounts/[0-9]+/balance/credit$" in edge
        assert "respond @internal 404" in edge
    nginx = (ROOT / "deploy/nginx/coexist-hybrid-ip-https.conf.template").read_text(encoding="utf-8")
    assert "proxy_pass http://platform-service:8081" in nginx
    assert "location ^~ /api/auth/" in nginx
    assert "location = /api/account/password" in nginx
    assert "location = /api/me/balance" in nginx
    assert "location = /api/me/recharge/orders" in nginx
    assert "location = /api/me/balance/recharge" in nginx
    assert "location ~ ^/api/admin/(balance/events|recharge/orders|recharge/orders/[0-9]+/events|accounts/[0-9]+/balance/credit)$" in nginx
    assert "location ~ ^/api/admin/(accounts|audit/events|accounts/[0-9]+/status)$" in nginx
    assert "location ~ ^/api/admin/usage/(summary|events)$" in nginx
    assert "location ~ ^/api/admin/tools/traces(/[^/]+)?$" in nginx
    assert "location ~ ^/api/(admin/tasks|tasks/[^/]+)$" in nginx
    assert 'if ($request_method != GET) { return 418; }' in nginx
    assert "location ^~ /internal/" in nginx
    workflow = (ROOT / ".github/workflows/deploy-hybrid.yml").read_text(encoding="utf-8")
    assert "compose.hybrid.coexist.yaml deploy scripts" in workflow
    deploy = (ROOT / "scripts/deploy_production.sh").read_text(encoding="utf-8")
    assert 'compose_arguments+=(-f "${ACTIVE_RELEASE_DIR}/compose.hybrid.coexist.yaml")' in deploy


def test_production_operations_preserve_hybrid_configuration():
    for name in ("run_production_backup.sh", "validate_production_recovery.sh", "reload_coexist_https.sh"):
        script = (ROOT / "scripts" / name).read_text(encoding="utf-8")
        for required in ("current-platform-image", "compose.hybrid.prod.yaml", 'JOB_AGENT_PLATFORM_IMAGE="$PLATFORM_IMAGE"'):
            assert required in script
        if name != "reload_coexist_https.sh":
            assert "compose_production stop platform-service" in script
