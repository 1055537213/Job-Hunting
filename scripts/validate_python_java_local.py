"""Run the local Python-to-Java billing acceptance flow in an isolated schema."""

from __future__ import annotations

import argparse
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.parse import quote
from urllib.request import urlopen

import sqlalchemy as sa
from sqlalchemy.engine import make_url


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATABASE_URL = "postgresql+psycopg://job_agent@127.0.0.1:5432/job_agent"
SCHEMA_PATTERN = re.compile(r"^job_agent_java_local_[0-9a-f]{24}$")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=18081, help="本地 Java 服务端口，默认 18081")
    parser.add_argument("--startup-timeout", type=float, default=90.0, help="Java 启动等待秒数，默认 90")
    return parser.parse_args()


def schema_database_url(base_url: str, schema: str) -> str:
    parsed = make_url(base_url)
    if parsed.get_backend_name() != "postgresql":
        raise ValueError("JOB_AGENT_TEST_DATABASE_URL 必须是 PostgreSQL URL。")
    query = dict(parsed.query)
    current_options = str(query.get("options", "")).strip()
    query["options"] = f"{current_options} -csearch_path={schema},public".strip()
    return parsed.set(query=query).render_as_string(hide_password=False)


def jdbc_schema_url(base_url: str, schema: str) -> tuple[str, str, str]:
    parsed = make_url(base_url)
    if parsed.get_backend_name() != "postgresql":
        raise ValueError("JOB_AGENT_TEST_DATABASE_URL 必须是 PostgreSQL URL。")
    if not parsed.host or not parsed.database or not parsed.username:
        raise ValueError("JOB_AGENT_TEST_DATABASE_URL 缺少 host、database 或 username。")
    database = quote(parsed.database, safe="")
    current_schema = quote(f"{schema},public", safe="")
    jdbc = f"jdbc:postgresql://{parsed.host}:{parsed.port or 5432}/{database}?currentSchema={current_schema}"
    return jdbc, parsed.username, parsed.password or ""


def choose_maven() -> str:
    candidates = [
        os.environ.get("JOB_AGENT_MAVEN_CMD", "").strip(),
        r"E:\Java\apache-maven-3.9.9\bin\mvn.cmd",
        shutil.which("mvn.cmd") or "",
        shutil.which("mvn") or "",
    ]
    for candidate in candidates:
        if candidate and (Path(candidate).is_file() or shutil.which(candidate)):
            return candidate
    raise RuntimeError("找不到 Maven，请设置 JOB_AGENT_MAVEN_CMD 或确认 E:\\Java\\apache-maven-3.9.9 已安装。")


def assert_port_available(port: int) -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind(("127.0.0.1", port))
        except OSError as error:
            raise RuntimeError(f"本地端口 {port} 已被占用，请用 --port 指定其他端口。") from error


def wait_for_health(base_url: str, process: subprocess.Popen[str], timeout: float) -> None:
    deadline = time.monotonic() + timeout
    last_error = "尚未收到健康检查响应。"
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"Java 进程提前退出，退出码为 {process.returncode}。")
        try:
            with urlopen(f"{base_url}/actuator/health", timeout=2) as response:
                body = response.read().decode("utf-8", errors="replace")
                if response.status == 200 and '"status":"UP"' in body.replace(" ", ""):
                    return
                last_error = f"健康检查返回 HTTP {response.status}: {body[:300]}"
        except Exception as error:  # noqa: BLE001 - 启动阶段连接失败是正常现象
            last_error = str(error)
        time.sleep(1)
    raise TimeoutError(f"Java 服务在 {timeout:.0f} 秒内未就绪：{last_error}")


def stop_process(process: subprocess.Popen[str] | None) -> None:
    if process is None or process.poll() is not None:
        return
    if os.name == "nt":
        # Maven is a cmd wrapper on Windows; terminate its whole tree or the
        # forked Spring Boot JVM can keep the acceptance port occupied.
        subprocess.run(
            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=10)
        return
    process.terminate()
    try:
        process.wait(timeout=15)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=10)


def tail_log(path: Path, lines: int = 40) -> str:
    if not path.exists():
        return "(Java 日志文件不存在)"
    return "".join(path.read_text(encoding="utf-8", errors="replace").splitlines(True)[-lines:])


def main() -> int:
    args = parse_args()
    if not 1024 <= args.port <= 65535:
        raise ValueError("--port 必须在 1024 到 65535 之间。")
    sys.path.insert(0, str(ROOT / "src"))
    from job_hunting_agent.database_migrations import upgrade_database  # noqa: PLC0415

    base_url = os.environ.get("JOB_AGENT_TEST_DATABASE_URL", DEFAULT_DATABASE_URL).strip()
    schema = f"job_agent_java_local_{os.urandom(12).hex()}"
    if not SCHEMA_PATTERN.fullmatch(schema):
        raise RuntimeError("内部生成的临时 schema 名称不符合安全规则。")
    isolated_url = schema_database_url(base_url, schema)
    jdbc_url, jdbc_username, jdbc_password = jdbc_schema_url(base_url, schema)
    service_url = f"http://127.0.0.1:{args.port}"
    internal_token = f"local-java-contract-{os.urandom(16).hex()}"
    schema_engine = sa.create_engine(base_url, pool_pre_ping=True, connect_args={"connect_timeout": 5})
    schema_created = False
    process: subprocess.Popen[str] | None = None
    acceptance_passed = False
    log_dir = Path(tempfile.mkdtemp(prefix="job-agent-java-local-"))
    log_path = log_dir / "platform-service.log"
    log_file = None
    try:
        assert_port_available(args.port)
        with schema_engine.begin() as connection:
            connection.execute(sa.text(f'CREATE SCHEMA "{schema}"'))
        schema_created = True
        print(f"==> Created isolated PostgreSQL schema: {schema}")
        upgrade_database(isolated_url)
        print("==> Alembic migrations passed")

        java_environment = os.environ.copy()
        java_environment.update(
            {
                "SERVER_PORT": str(args.port),
                "PLATFORM_BILLING_ENABLED": "true",
                "PLATFORM_INTERNAL_TOKEN": internal_token,
                "SPRING_DATASOURCE_URL": jdbc_url,
                "SPRING_DATASOURCE_USERNAME": jdbc_username,
                "SPRING_DATASOURCE_PASSWORD": jdbc_password,
                "APP_VERSION": "local-java-acceptance",
                # Windows Java 25 can fail to create Tomcat's WEPoll loopback pipe;
                # NIO2 avoids that OS-specific selector path for local acceptance.
                "SERVER_TOMCAT_PROTOCOL": "org.apache.coyote.http11.Http11Nio2Protocol",
            }
        )
        log_file = log_path.open("w", encoding="utf-8")
        process = subprocess.Popen(
            [choose_maven(), "-B", "-ntp", "-DskipTests", "spring-boot:run"],
            cwd=ROOT / "platform-service",
            env=java_environment,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            text=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        print(f"==> Started Java platform service on {service_url}")
        wait_for_health(service_url, process, args.startup_timeout)
        print("==> Java actuator health is UP")

        contract_environment = os.environ.copy()
        contract_environment.update(
            {
                "JOB_AGENT_DATABASE_URL": isolated_url,
                "JOB_AGENT_PLATFORM_CONTRACT_URL": service_url,
                "JOB_AGENT_JAVA_BILLING_INTERNAL_TOKEN": internal_token,
                "PYTHONPATH": str(ROOT / "src"),
            }
        )
        contract = subprocess.run(
            [sys.executable, str(ROOT / "tests" / "python_java_billing_contract.py")],
            cwd=ROOT,
            env=contract_environment,
            check=False,
            text=True,
        )
        if contract.returncode != 0:
            raise RuntimeError(f"Python-Java billing contract failed with exit code {contract.returncode}。")
        acceptance_passed = True
        print("Local Python-Java billing acceptance: PASS")
        return 0
    finally:
        stop_process(process)
        if log_file is not None:
            log_file.close()
        if not acceptance_passed:
            print(f"Java service log: {log_path}", file=sys.stderr)
            print(tail_log(log_path), file=sys.stderr)
        if schema_created:
            with schema_engine.begin() as connection:
                connection.execute(sa.text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
            print(f"==> Removed isolated PostgreSQL schema: {schema}")
        schema_engine.dispose()
        if acceptance_passed:
            shutil.rmtree(log_dir, ignore_errors=True)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:  # noqa: BLE001 - CLI prints a concise actionable failure
        print(f"Local Python-Java acceptance: FAILED: {error}", file=sys.stderr)
        raise
