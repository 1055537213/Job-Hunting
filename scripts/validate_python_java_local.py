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
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import quote
from urllib.request import urlopen

import httpx
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


def wait_for_http_ok(url: str, process: subprocess.Popen[str], timeout: float) -> None:
    deadline = time.monotonic() + timeout
    last_error = "尚未收到 HTTP 响应。"
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"Python Web 进程提前退出，退出码为 {process.returncode}。")
        try:
            with urlopen(url, timeout=2) as response:
                if response.status == 200:
                    return
                last_error = f"HTTP {response.status}"
        except Exception as error:  # noqa: BLE001 - 启动阶段连接失败是正常现象
            last_error = str(error)
        time.sleep(1)
    raise TimeoutError(f"Python Web 在 {timeout:.0f} 秒内未就绪：{last_error}")


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


def run_web_billing_flow(
    *,
    web_url: str,
    database_url: str,
    base_url: str,
    internal_token: str,
) -> None:
    """Exercise the real Web routes and the production Python usage path."""

    sys.path.insert(0, str(ROOT / "src"))
    from job_hunting_agent.auth import verify_password  # noqa: PLC0415
    from job_hunting_agent.config import BillingSettings, PlatformBillingSettings  # noqa: PLC0415
    from job_hunting_agent.models import UsageEventRecord  # noqa: PLC0415
    from job_hunting_agent.platform_billing import PlatformBillingClient  # noqa: PLC0415
    from job_hunting_agent.sqlalchemy_store import SQLAlchemyStore  # noqa: PLC0415
    from job_hunting_agent.storage import InsufficientBalanceError  # noqa: PLC0415

    suffix = os.urandom(12).hex()
    email = f"web-java-contract-{suffix}@example.com"
    password = "contract-test-password-123"
    recharge_key = f"web-recharge-{suffix}"
    account_id: int

    with httpx.Client(base_url=web_url, timeout=10, follow_redirects=True) as client:
        health = client.get("/api/health")
        if health.status_code != 200 or health.json().get("status") != "ok":
            raise RuntimeError(f"Python Web health check failed: {health.status_code} {health.text[:300]}")

        registered = client.post(
            "/api/auth/register",
            json={"email": email, "password": password, "display_name": "Python Java Web 联调"},
        )
        if registered.status_code != 200:
            raise RuntimeError(f"Web registration failed: {registered.status_code} {registered.text[:300]}")

        from job_hunting_agent.config import PlatformAuthSettings  # noqa: PLC0415
        from job_hunting_agent.platform_auth import PlatformAuthClient  # noqa: PLC0415
        from job_hunting_agent.platform_email import deliver_platform_verification  # noqa: PLC0415
        from urllib.parse import parse_qs, urlsplit  # noqa: PLC0415

        auth_client = PlatformAuthClient(PlatformAuthSettings(True, base_url, internal_token, 10))
        account_id = registered.json()["account"]["id"]
        denied = client.post("/api/auth/login", json={"email": email, "password": password})
        if denied.status_code != 403:
            raise RuntimeError("Unverified account was allowed to log in")
        jobs = auth_client.email_verification("due")["records"]
        if len(jobs) != 1:
            raise RuntimeError("Registration must create one durable verification job")

        class RecordingSender:
            url = ""

            def send_verification(self, recipient, url):
                if recipient != email:
                    raise RuntimeError("Verification email recipient mismatch")
                self.url = url

        sender = RecordingSender()
        delivered = deliver_platform_verification(auth_client, sender, jobs[0]["id"])
        if not delivered["accepted"]:
            raise RuntimeError("Verification delivery was not accepted")
        token = parse_qs(urlsplit(sender.url).query)["verify_email_token"][0]
        confirmed = client.post("/api/auth/verify-email", json={"token": token})
        if confirmed.status_code != 200 or confirmed.json()["account"]["id"] != account_id:
            raise RuntimeError("Email verification confirmation failed")
        if client.post("/api/auth/verify-email", json={"token": token}).status_code != 400:
            raise RuntimeError("Verification token was reusable")
        if client.post("/api/auth/verify-email", json={"token": "invalid-verification-token-000000000"}).status_code != 400:
            raise RuntimeError("Invalid verification token was accepted")
        print("==> Java verification -> Python SMTP worker -> Web confirm: PASS")
        account_id = int(registered.json()["account"]["id"])

        registration_store = SQLAlchemyStore(database_url)
        try:
            _, stored_password_hash = registration_store.get_account_with_password(account_id)
            if not verify_password(stored_password_hash, password):
                raise AssertionError("Java registration produced a password hash Python cannot verify.")
            if registration_store.get_account_balance_summary(account_id).balance_micro_yuan != 0:
                raise AssertionError("New Java-registered accounts must start with zero balance.")
        finally:
            registration_store.close()

        logged_in = client.post("/api/auth/login", json={"email": email, "password": password})
        if logged_in.status_code != 200:
            raise RuntimeError(f"Web login failed: {logged_in.status_code} {logged_in.text[:300]}")

        recharge_payload = {
            "amount_yuan": 10,
            "note": "Python-Java Web 联调充值",
            "idempotency_key": recharge_key,
        }
        recharged = client.post("/api/me/balance/recharge", json=recharge_payload)
        if recharged.status_code != 200:
            raise RuntimeError(f"Web recharge failed: {recharged.status_code} {recharged.text[:300]}")
        if recharged.json()["summary"]["balance_micro_yuan"] != 10_000_000:
            raise AssertionError(recharged.json())

        replayed_recharge = client.post("/api/me/balance/recharge", json=recharge_payload)
        if replayed_recharge.status_code != 200:
            raise RuntimeError(
                f"Web recharge replay failed: {replayed_recharge.status_code} {replayed_recharge.text[:300]}"
            )
        if replayed_recharge.json()["summary"]["balance_micro_yuan"] != 10_000_000:
            raise AssertionError(replayed_recharge.json())

        store = SQLAlchemyStore(database_url)
        store.configure_billing(BillingSettings(price_per_million_tokens_yuan=25))
        store.configure_platform_billing(
            PlatformBillingClient(
                PlatformBillingSettings(
                    enabled=True,
                    base_url=base_url,
                    internal_token=internal_token,
                    timeout_seconds=5,
                )
            )
        )
        try:
            usage = UsageEventRecord(
                id=0,
                account_id=account_id,
                candidate_id=None,
                session_id=None,
                root_request_id=f"web-root-{suffix}",
                call_id=f"web-call-{suffix}",
                provider="contract-provider",
                model="contract-model",
                operation="web_contract_charge",
                input_tokens=80_000,
                output_tokens=0,
                total_tokens=80_000,
                usage_source="provider",
                status="succeeded",
                attempt=1,
                provider_request_id=None,
                raw_usage={"total_tokens": 80_000},
                created_at=datetime.now(UTC).isoformat(timespec="seconds"),
                billable=True,
                pricing_version=None,
            )
            store.record_usage_event(usage)
            store.record_usage_event(usage)

            balance_after_charge = client.get("/api/me/balance")
            if balance_after_charge.status_code != 200:
                raise RuntimeError(
                    f"Web balance query failed: {balance_after_charge.status_code} {balance_after_charge.text[:300]}"
                )
            if balance_after_charge.json()["summary"]["balance_micro_yuan"] != 8_000_000:
                raise AssertionError(balance_after_charge.json())

            insufficient = UsageEventRecord(
                **{
                    **usage.__dict__,
                    "call_id": f"web-insufficient-{suffix}",
                    "total_tokens": 400_000,
                    "raw_usage": {"total_tokens": 400_000},
                }
            )
            try:
                store.record_usage_event(insufficient)
            except InsufficientBalanceError as error:
                if str(error) != "余额不足，请先充值后重试。":
                    raise AssertionError(str(error)) from error
            else:
                raise AssertionError("Expected the Web billing path to reject an insufficient balance.")

            balance_after_rejection = client.get("/api/me/balance")
            if balance_after_rejection.json()["summary"]["balance_micro_yuan"] != 8_000_000:
                raise AssertionError(balance_after_rejection.json())
        finally:
            store.close()
    print("Local Python Web + Java billing flow: PASS")


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
    web_process: subprocess.Popen[str] | None = None
    acceptance_passed = False
    log_dir = Path(tempfile.mkdtemp(prefix="job-agent-java-local-"))
    log_path = log_dir / "platform-service.log"
    web_log_path = log_dir / "python-web.log"
    log_file = None
    web_log_file = None
    web_env_path = log_dir / "local-web.env"
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
                "PLATFORM_AUTH_ENABLED": "true",
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
        build = subprocess.run(
            [choose_maven(), "-B", "-ntp", "package", "-DskipTests"],
            cwd=ROOT / "platform-service", env=java_environment,
            stdout=log_file, stderr=subprocess.STDOUT, timeout=600,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if build.returncode != 0:
            raise RuntimeError("Java platform packaging failed")
        java_home = java_environment.get("JAVA_HOME")
        java = str(Path(java_home) / "bin" / ("java.exe" if os.name == "nt" else "java")) if java_home else "java"
        process = subprocess.Popen(
            [java, "-jar", "target/platform-service-0.1.0-SNAPSHOT.jar"],
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

        web_port = args.port + 1
        if web_port > 65535:
            raise ValueError("Java 端口过大，无法为 Python Web 分配相邻端口。")
        assert_port_available(web_port)
        web_env_path.write_text(
            "\n".join(
                [
                    "JOB_AGENT_ENVIRONMENT=test",
                    "JOB_AGENT_OBJECT_STORAGE_BACKEND=local",
                    "JOB_AGENT_CSRF_ENABLED=false",
                    "JOB_AGENT_EMAIL_VERIFICATION_REQUIRED=true",
                    "JOB_AGENT_CONSENT_REQUIRED=false",
                    "JOB_AGENT_DEMO_RECHARGE_ENABLED=true",
                    "JOB_AGENT_DEMO_RECHARGE_MAX_AMOUNT_YUAN=20",
                    "JOB_AGENT_DEMO_RECHARGE_MAX_TOTAL_YUAN=50",
                    "JOB_AGENT_TASK_QUEUE_ENABLED=false",
                    "JOB_AGENT_COOKIE_SECURE=false",
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        web_environment = os.environ.copy()
        web_environment.update(
            {
                "PYTHONPATH": str(ROOT / "src"),
                "JOB_AGENT_DATABASE_URL": isolated_url,
                "JOB_AGENT_JAVA_BILLING_ENABLED": "true",
                "JOB_AGENT_JAVA_BILLING_BASE_URL": service_url,
                "JOB_AGENT_JAVA_BILLING_INTERNAL_TOKEN": internal_token,
                "JOB_AGENT_JAVA_AUTH_ENABLED": "true",
                "JOB_AGENT_JAVA_AUTH_BASE_URL": service_url,
                "JOB_AGENT_JAVA_AUTH_INTERNAL_TOKEN": internal_token,
                "JOB_AGENT_ENVIRONMENT": "test",
                "JOB_AGENT_OBJECT_STORAGE_BACKEND": "local",
                "JOB_AGENT_CSRF_ENABLED": "false",
                "JOB_AGENT_EMAIL_VERIFICATION_REQUIRED": "true",
                "JOB_AGENT_CONSENT_REQUIRED": "false",
                "JOB_AGENT_DEMO_RECHARGE_ENABLED": "true",
                "JOB_AGENT_TASK_QUEUE_ENABLED": "false",
                "JOB_AGENT_COOKIE_SECURE": "false",
            }
        )
        web_log_file = web_log_path.open("w", encoding="utf-8")
        web_process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "job_hunting_agent.web",
                "--env-file",
                str(web_env_path),
                "--host",
                "127.0.0.1",
                "--port",
                str(web_port),
            ],
            cwd=ROOT,
            env=web_environment,
            stdout=web_log_file,
            stderr=subprocess.STDOUT,
            text=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        web_url = f"http://127.0.0.1:{web_port}"
        print(f"==> Started Python Web on {web_url}")
        wait_for_http_ok(f"{web_url}/api/health", web_process, args.startup_timeout)
        print("==> Python Web health is OK")
        run_web_billing_flow(
            web_url=web_url,
            database_url=isolated_url,
            base_url=service_url,
            internal_token=internal_token,
        )

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
        stop_process(web_process)
        stop_process(process)
        if log_file is not None:
            log_file.close()
        if web_log_file is not None:
            web_log_file.close()
        if not acceptance_passed:
            print(f"Java service log: {log_path}", file=sys.stderr)
            print(tail_log(log_path), file=sys.stderr)
            print(f"Python Web log: {web_log_path}", file=sys.stderr)
            print(tail_log(web_log_path), file=sys.stderr)
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
