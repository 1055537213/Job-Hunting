"""Validate real Python/Java containers without touching application data."""

from __future__ import annotations

import argparse
import os
import subprocess
import time

import httpx
import sqlalchemy as sa
from sqlalchemy.engine import make_url

from validate_python_java_local import (
    DEFAULT_DATABASE_URL,
    assert_port_available,
    jdbc_schema_url,
    run_web_billing_flow,
    schema_database_url,
)


def docker(*args: str) -> str:
    result = subprocess.run(
        ["docker", *args], capture_output=True, text=True, check=False
    )
    if result.returncode:
        raise RuntimeError(f"Docker operation failed: {result.stderr[-1500:]}")
    return result.stdout.strip()


def wait(url: str) -> None:
    deadline = time.monotonic() + 180
    while time.monotonic() < deadline:
        try:
            if httpx.get(url, timeout=3).status_code == 200:
                return
        except httpx.HTTPError:
            pass
        time.sleep(1)
    raise RuntimeError(f"Container health timeout: {url}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--java-image", default="job-hunting-platform:hybrid-validation"
    )
    parser.add_argument("--ai-image", default="job-hunting-agent:hybrid-validation")
    parser.add_argument("--port", type=int, default=18085)
    args = parser.parse_args()
    if not 1024 <= args.port <= 65534:
        raise ValueError("Invalid acceptance port")
    assert_port_available(args.port)
    assert_port_available(args.port + 1)
    for image in (args.java_image, args.ai_image):
        docker("image", "inspect", image)
    suffix = os.urandom(12).hex()
    schema = f"job_agent_java_local_{suffix}"
    prefix = f"job-agent-hybrid-test-{suffix}"
    java, web = f"{prefix}-java", f"{prefix}-web"
    base_url = os.environ.get("JOB_AGENT_TEST_DATABASE_URL", DEFAULT_DATABASE_URL)
    isolated_url = schema_database_url(base_url, schema)
    container_url = (
        make_url(isolated_url)
        .set(host="host.docker.internal")
        .render_as_string(hide_password=False)
    )
    jdbc, username, password = jdbc_schema_url(container_url, schema)
    token = f"acceptance-internal-{os.urandom(16).hex()}"
    engine = sa.create_engine(base_url, pool_pre_ping=True)
    schema_created = False
    network_created = False
    try:
        with engine.begin() as conn:
            conn.execute(sa.text(f'CREATE SCHEMA "{schema}"'))
        schema_created = True
        docker("network", "create", prefix)
        network_created = True
        common = [
            "--network",
            prefix,
            "--add-host",
            "host.docker.internal:host-gateway",
        ]
        docker(
            "run",
            "--rm",
            *common,
            "-e",
            f"JOB_AGENT_DATABASE_URL={container_url}",
            "--entrypoint",
            "alembic",
            args.ai_image,
            "upgrade",
            "head",
        )
        docker(
            "run",
            "-d",
            "--name",
            java,
            *common,
            "--network-alias",
            "platform-service",
            "-p",
            f"127.0.0.1:{args.port}:8081",
            "-e",
            "PLATFORM_AUTH_ENABLED=true",
            "-e",
            "PLATFORM_BILLING_ENABLED=true",
            "-e",
            f"PLATFORM_INTERNAL_TOKEN={token}",
            "-e",
            f"SPRING_DATASOURCE_URL={jdbc}",
            "-e",
            f"SPRING_DATASOURCE_USERNAME={username}",
            "-e",
            f"SPRING_DATASOURCE_PASSWORD={password}",
            args.java_image,
        )
        wait(f"http://127.0.0.1:{args.port}/actuator/health")
        settings = {
            "JOB_AGENT_DATABASE_URL": container_url,
            "JOB_AGENT_ENVIRONMENT": "test",
            "JOB_AGENT_OBJECT_STORAGE_BACKEND": "local",
            "JOB_AGENT_CSRF_ENABLED": "false",
            "JOB_AGENT_COOKIE_SECURE": "false",
            "JOB_AGENT_CONSENT_REQUIRED": "false",
            "JOB_AGENT_EMAIL_VERIFICATION_REQUIRED": "true",
            "JOB_AGENT_DEMO_RECHARGE_ENABLED": "true",
            "JOB_AGENT_TASK_QUEUE_ENABLED": "false",
            "JOB_AGENT_RATE_LIMIT_AUTH_REQUESTS": "100",
            "JOB_AGENT_JAVA_AUTH_ENABLED": "true",
            "JOB_AGENT_JAVA_BILLING_ENABLED": "true",
            "JOB_AGENT_JAVA_AUTH_BASE_URL": "http://platform-service:8081",
            "JOB_AGENT_JAVA_BILLING_BASE_URL": "http://platform-service:8081",
            "JOB_AGENT_JAVA_AUTH_INTERNAL_TOKEN": token,
            "JOB_AGENT_JAVA_BILLING_INTERNAL_TOKEN": token,
        }
        env_args = [
            arg for key, value in settings.items() for arg in ("-e", f"{key}={value}")
        ]
        docker(
            "run",
            "-d",
            "--name",
            web,
            *common,
            "-p",
            f"127.0.0.1:{args.port + 1}:8000",
            *env_args,
            args.ai_image,
        )
        wait(f"http://127.0.0.1:{args.port + 1}/api/health")
        run_web_billing_flow(
            web_url=f"http://127.0.0.1:{args.port + 1}",
            database_url=isolated_url,
            base_url=f"http://127.0.0.1:{args.port}",
            internal_token=token,
        )
        print("Docker Python-Java account and billing acceptance: PASS")
    finally:
        for name in (web, java):
            subprocess.run(
                ["docker", "rm", "-f", name], capture_output=True, check=False
            )
        if network_created:
            subprocess.run(
                ["docker", "network", "rm", prefix], capture_output=True, check=False
            )
        if schema_created:
            with engine.begin() as conn:
                conn.execute(sa.text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        engine.dispose()


if __name__ == "__main__":
    main()
