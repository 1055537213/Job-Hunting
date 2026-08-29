"""Container publishing and protected production deployment regressions."""

from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


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
        "wait_for_healthy_service web",
        "wait_for_running_service worker",
        "wait_for_running_service beat",
        "wait_for_running_service reverse-proxy",
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
