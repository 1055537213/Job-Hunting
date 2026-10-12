"""Validate real Python/Java containers without touching application data."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import time
import tempfile
from pathlib import Path

import httpx
import sqlalchemy as sa
from sqlalchemy.engine import make_url

from validate_python_java_local import (
    DEFAULT_DATABASE_URL,
    assert_port_available,
    jdbc_schema_url,
    run_web_billing_flow,
    run_java_security_flow,
    run_java_ingress_flow,
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
    parser.add_argument("--java-jar", type=Path, help="仅供本地离线验收：在已有 Java 21 运行镜像中挂载新构建的 jar")
    parser.add_argument("--port", type=int, default=18085)
    args = parser.parse_args()
    if not 1024 <= args.port <= 65534:
        raise ValueError("Invalid acceptance port")
    assert_port_available(args.port)
    assert_port_available(args.port + 1)
    for image in (args.java_image, args.ai_image):
        docker("image", "inspect", image)
    jar_mount = []
    if args.java_jar is not None:
        jar = args.java_jar.resolve(strict=True)
        if not jar.is_file() or jar.suffix != ".jar":
            raise ValueError("--java-jar must point to a packaged jar")
        jar_mount = ["--mount", f"type=bind,source={jar},target=/app/platform-service.jar,readonly"]
    suffix = os.urandom(12).hex()
    schema = f"job_agent_java_local_{suffix}"
    prefix = f"job-agent-hybrid-test-{suffix}"
    java, web = f"{prefix}-java", f"{prefix}-web"
    prometheus = f"{prefix}-prometheus"
    redis_name = f"{prefix}-redis"
    redis_password = os.urandom(24).hex()
    base_url = os.environ.get("JOB_AGENT_TEST_DATABASE_URL", DEFAULT_DATABASE_URL)
    isolated_url = schema_database_url(base_url, schema)
    container_url = (
        make_url(isolated_url)
        .set(host="host.docker.internal")
        .render_as_string(hide_password=False)
    )
    jdbc, username, password = jdbc_schema_url(container_url, schema)
    token = f"acceptance-internal-{os.urandom(16).hex()}"
    metrics_token = f"acceptance-metrics-{os.urandom(24).hex()}"
    engine = sa.create_engine(base_url, pool_pre_ping=True)
    schema_created = False
    network_created = False
    try:
        with engine.begin() as conn:
            conn.execute(sa.text(f'CREATE SCHEMA "{schema}"'))
        schema_created = True
        docker("network", "create", prefix)
        network_created = True
        network_info = json.loads(docker("network", "inspect", prefix))
        trusted_subnet = network_info[0]["IPAM"]["Config"][0]["Subnet"]
        common = [
            "--network",
            prefix,
            "--add-host",
            "host.docker.internal:host-gateway",
        ]
        docker("run", "-d", "--name", redis_name, "--network", prefix,
               "--network-alias", "security-redis", "redis:7.4-alpine",
               "redis-server", "--requirepass", redis_password)
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
            "PLATFORM_WEB_SECURITY_ENABLED=true",
            "-e", f"PLATFORM_TRUSTED_PROXY_CIDRS={trusted_subnet}",
            "-e", "PLATFORM_METRICS_ENABLED=true",
            "-e", f"PLATFORM_METRICS_TOKEN={metrics_token}",
            "-e",
            "PLATFORM_REDIS_HOST=security-redis",
            "-e",
            f"PLATFORM_REDIS_PASSWORD={redis_password}",
            "-e",
            "JOB_AGENT_CSRF_ENABLED=true",
            "-e",
            "JOB_AGENT_COOKIE_SECURE=false",
            "-e",
            "JOB_AGENT_EMAIL_VERIFICATION_REQUIRED=true",
            "-e",
            "JOB_AGENT_RATE_LIMIT_AUTH_REQUESTS=20",
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
            *jar_mount,
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
        admin_credentials = run_web_billing_flow(
            web_url=f"http://127.0.0.1:{args.port + 1}",
            database_url=isolated_url,
            base_url=f"http://127.0.0.1:{args.port}",
            internal_token=token,
        )
        run_java_security_flow(f"http://127.0.0.1:{args.port}", token, admin_credentials, redis_name)
        run_java_ingress_flow(f"http://127.0.0.1:{args.port}", metrics_token, token)
        with tempfile.TemporaryDirectory(prefix="job-agent-prometheus-") as scratch:
            credential = Path(scratch) / "platform_metrics_token"
            credential.write_text(metrics_token, encoding="utf-8")
            credential.chmod(0o644)
            root = Path(__file__).resolve().parents[1]
            docker("run", "-d", "--name", prometheus, "--network", prefix,
                   "-p", "127.0.0.1::9090",
                   "--mount", f"type=bind,source={root / 'deploy/prometheus/prometheus.hybrid.yml'},target=/etc/prometheus/prometheus.yml,readonly",
                   "--mount", f"type=bind,source={root / 'deploy/prometheus/alerts.yml'},target=/etc/prometheus/alerts.yml,readonly",
                   "--mount", f"type=bind,source={credential},target=/run/secrets/platform_metrics_token,readonly",
                   "prom/prometheus:v3.13.1", "--config.file=/etc/prometheus/prometheus.yml")
            try:
                port = json.loads(docker("inspect", prometheus))[0]["NetworkSettings"]["Ports"]["9090/tcp"][0]["HostPort"]
                prometheus_url = f"http://127.0.0.1:{port}"
                wait(f"{prometheus_url}/-/healthy")
                deadline = time.monotonic() + 45
                while time.monotonic() < deadline:
                    result = httpx.get(f"{prometheus_url}/api/v1/query", params={"query": 'up{job="job-hunting-platform"}'}, timeout=5)
                    values = result.json().get("data", {}).get("result", [])
                    if values and all(value["value"][1] == "1" for value in values):
                        break
                    time.sleep(1)
                assert values and all(value["value"][1] == "1" for value in values), "Prometheus Java target was not UP"
                print("==> Real Prometheus -> protected Java metrics target UP: PASS")
            finally:
                docker("rm", "-f", prometheus)
        print("Docker Python-Java account and billing acceptance: PASS")
    finally:
        for name in (prometheus, web, java, redis_name):
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
