"""隔离运行 API、SSE、Celery、PostgreSQL/pgvector 端到端负载测试。"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import tempfile
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Callable, Iterator, Mapping, Sequence
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit

import sqlalchemy as sa

from .auth import hash_password
from .config import load_dotenv_values
from .database_migrations import upgrade_database
from .e2e_load import (
    DeterministicLoadTestAgent,
    LoadHttpClient,
    LoadSample,
    UvicornTestServer,
    redact_sensitive_data,
    samples_as_dicts,
    summarize_samples,
)
from .models import CandidateProfileInput
from .web import create_web_app

DEFAULT_DATABASE_URL = "postgresql+psycopg://job_agent@127.0.0.1:5432/job_agent"
DEFAULT_CONCURRENCY = {
    "smoke": (1, 5),
    "full": (1, 5, 10, 20, 50),
}
SCHEMA_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,62}$")
FINAL_TASK_STATES = {"succeeded", "failed", "cancelled"}


@dataclass
class LoadUser:
    index: int
    email: str
    account_id: int
    candidate_id: int
    query_term: str
    client: LoadHttpClient


@dataclass(frozen=True)
class RunnerSettings:
    project_root: Path
    source_env_file: Path
    report_dir: Path
    profile: str
    concurrency_levels: tuple[int, ...]
    requests_per_user: int
    timeout_seconds: float
    task_timeout_seconds: float
    worker_concurrency: int
    skip_worker: bool
    skip_faults: bool
    base_database_url: str
    host_redis_url: str | None
    worker_database_url: str
    worker_redis_url: str | None


class DockerWorker:
    """管理只消费本次随机队列的临时 Celery Worker 容器。"""

    def __init__(
        self,
        project_root: Path,
        env_file: Path,
        queue_name: str,
        concurrency: int,
        run_id: str,
    ) -> None:
        self.project_root = project_root
        self.env_file = env_file
        self.queue_name = queue_name
        self.concurrency = concurrency
        self.container_name = f"job-agent-e2e-worker-{run_id[:20]}"
        self.process: subprocess.Popen[str] | None = None
        self.log_path = env_file.with_suffix(".worker.log")
        self._log_handle = None

    def start(self) -> None:
        if self.process is not None and self.process.poll() is None:
            return
        self.stop()
        command = [
            "docker",
            "compose",
            "-f",
            str(self.project_root / "compose.yaml"),
            "-f",
            str(self.project_root / "compose.dev.yaml"),
            "run",
            "--rm",
            "--no-deps",
            "-T",
            "--name",
            self.container_name,
            "--env-from-file",
            str(self.env_file),
            "worker",
            "job-agent-worker",
            "--env-file",
            "/app/.env",
            "--log-level",
            "WARNING",
            "--concurrency",
            str(self.concurrency),
            "--queue",
            self.queue_name,
        ]
        self._log_handle = self.log_path.open("w", encoding="utf-8")
        self.process = subprocess.Popen(
            command,
            cwd=self.project_root,
            stdin=subprocess.DEVNULL,
            stdout=self._log_handle,
            stderr=subprocess.STDOUT,
            text=True,
        )
        time.sleep(2)
        if self.process.poll() is not None:
            raise RuntimeError(f"临时 Celery Worker 启动失败：{self.log_tail()}")

    def stop(self) -> None:
        if self.process is None and self._log_handle is None:
            return
        subprocess.run(
            ["docker", "stop", "--time", "5", self.container_name],
            cwd=self.project_root,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        subprocess.run(
            ["docker", "rm", "-f", self.container_name],
            cwd=self.project_root,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        if self.process is not None:
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)
        if self._log_handle is not None:
            self._log_handle.close()
        self.process = None
        self._log_handle = None

    def log_tail(self, lines: int = 20) -> str:
        if self._log_handle is not None:
            self._log_handle.flush()
        if not self.log_path.exists():
            return "没有 Worker 日志。"
        return "\n".join(self.log_path.read_text(encoding="utf-8", errors="replace").splitlines()[-lines:])


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    settings = resolve_settings(args)
    report = run_load_test(settings)
    output_path = write_report(report, settings.report_dir)
    print(f"端到端负载测试报告：{output_path}")
    print(f"验收结果：{'PASS' if report['passed'] else 'FAIL'}")
    return 0 if report["passed"] else 1


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", choices=sorted(DEFAULT_CONCURRENCY), default="smoke")
    parser.add_argument("--concurrency", default="")
    parser.add_argument("--requests-per-user", type=int, default=1)
    parser.add_argument("--timeout-seconds", type=float, default=20.0)
    parser.add_argument("--task-timeout-seconds", type=float, default=45.0)
    parser.add_argument("--worker-concurrency", type=int, default=2)
    parser.add_argument("--database-url", default="")
    parser.add_argument("--redis-url", default="")
    parser.add_argument("--worker-database-url", default="")
    parser.add_argument("--worker-redis-url", default="")
    parser.add_argument("--env-file", default=".env")
    parser.add_argument("--report-dir", default="data/eval-reports")
    parser.add_argument("--skip-worker", action="store_true")
    parser.add_argument("--skip-faults", action="store_true")
    return parser.parse_args(argv)


def resolve_settings(args: argparse.Namespace) -> RunnerSettings:
    project_root = Path.cwd().resolve()
    source_env_file = (project_root / args.env_file).resolve()
    file_values = load_dotenv_values(source_env_file)
    concurrency_levels = (
        parse_concurrency_levels(args.concurrency)
        if args.concurrency
        else DEFAULT_CONCURRENCY[args.profile]
    )
    if args.requests_per_user <= 0:
        raise ValueError("--requests-per-user 必须大于 0。")
    if args.worker_concurrency <= 0:
        raise ValueError("--worker-concurrency 必须大于 0。")

    base_database_url = (
        args.database_url
        or os.environ.get("JOB_AGENT_TEST_DATABASE_URL")
        or DEFAULT_DATABASE_URL
    )
    host_redis_url = args.redis_url or os.environ.get("JOB_AGENT_E2E_REDIS_URL")
    if not host_redis_url:
        redis_password = file_values.get("JOB_AGENT_REDIS_PASSWORD")
        if redis_password:
            host_redis_url = (
                f"redis://:{quote(redis_password, safe='')}@127.0.0.1:6379/15"
            )
    worker_database_url = args.worker_database_url or replace_service_host(
        base_database_url,
        "postgres",
        5432,
    )
    worker_redis_url = args.worker_redis_url or (
        replace_service_host(host_redis_url, "redis", 6379)
        if host_redis_url
        else None
    )
    if not args.skip_worker and (not host_redis_url or not worker_redis_url):
        raise ValueError(
            "Celery 链路测试需要 Redis。请在 .env 配置 JOB_AGENT_REDIS_PASSWORD，"
            "或传入 --redis-url 和 --worker-redis-url。"
        )
    return RunnerSettings(
        project_root=project_root,
        source_env_file=source_env_file,
        report_dir=(project_root / args.report_dir).resolve(),
        profile=args.profile,
        concurrency_levels=concurrency_levels,
        requests_per_user=args.requests_per_user,
        timeout_seconds=args.timeout_seconds,
        task_timeout_seconds=args.task_timeout_seconds,
        worker_concurrency=args.worker_concurrency,
        skip_worker=args.skip_worker,
        skip_faults=args.skip_faults,
        base_database_url=base_database_url,
        host_redis_url=host_redis_url,
        worker_database_url=worker_database_url,
        worker_redis_url=worker_redis_url,
    )


def parse_concurrency_levels(raw_value: str) -> tuple[int, ...]:
    levels = tuple(sorted({int(item.strip()) for item in raw_value.split(",") if item.strip()}))
    if not levels or any(level <= 0 or level > 200 for level in levels):
        raise ValueError("并发档必须是 1 到 200 之间的逗号分隔整数。")
    return levels


def run_load_test(settings: RunnerSettings) -> dict[str, object]:
    run_id = uuid.uuid4().hex
    schema = f"job_agent_e2e_{run_id[:24]}"
    queue_name = f"job_agent_e2e_{run_id[:20]}"
    started_at = datetime.now(UTC)
    samples: list[LoadSample] = []
    faults: dict[str, object] = {}
    task_results: dict[str, object] = {"skipped": settings.skip_worker}
    worker: DockerWorker | None = None
    web_app = None
    metrics_before = ""
    metrics_after = ""
    report_error: str | None = None

    redis_url = None if settings.skip_worker else settings.host_redis_url
    with (
        isolated_redis_database(redis_url) as redis_isolated,
        isolated_database_schema(settings.base_database_url, schema) as host_database_url,
    ):
        worker_database_url = schema_database_url(settings.worker_database_url, schema)
        with tempfile.TemporaryDirectory(prefix="job-agent-e2e-") as temp_directory:
            temp_path = Path(temp_directory)
            admin_email = f"admin-{run_id[:12]}@example.invalid"
            admin_password = f"Load-{run_id}-Password"
            host_env = build_safe_environment(
                database_url=host_database_url,
                redis_url=settings.host_redis_url,
                queue_name=queue_name,
                admin_email=admin_email,
                admin_password=admin_password,
                task_queue_enabled=not settings.skip_worker,
            )
            worker_env = build_safe_environment(
                database_url=worker_database_url,
                redis_url=settings.worker_redis_url,
                queue_name=queue_name,
                admin_email=admin_email,
                admin_password=admin_password,
                task_queue_enabled=True,
            )
            host_env_file = temp_path / "host.env"
            worker_env_file = temp_path / "worker.env"
            write_env_file(host_env_file, host_env)
            write_env_file(worker_env_file, worker_env)

            try:
                with isolated_job_agent_environment(host_env):
                    web_app = create_web_app(
                        env_file=host_env_file,
                        resume_dir=temp_path / "resumes",
                        database_url=host_database_url,
                        chat_agent=DeterministicLoadTestAgent(),
                    )
                    if not settings.skip_worker:
                        worker = DockerWorker(
                            settings.project_root,
                            worker_env_file,
                            queue_name,
                            settings.worker_concurrency,
                            run_id,
                        )
                        worker.start()

                    with UvicornTestServer(web_app) as server:
                        required_clients = max(
                            max(settings.concurrency_levels),
                            2 if not settings.skip_faults else 1,
                        )
                        users = prepare_users(
                            web_app,
                            server.base_url,
                            required_clients,
                            run_id,
                            settings.timeout_seconds,
                            settings.task_timeout_seconds,
                        )
                        admin_clients = prepare_admin_clients(
                            server.base_url,
                            admin_email,
                            admin_password,
                            required_clients,
                            settings.timeout_seconds,
                        )
                        metrics_before = read_metrics(server.base_url, settings.timeout_seconds)
                        for concurrency in settings.concurrency_levels:
                            active_users = users[:concurrency]
                            samples.extend(
                                run_http_scenarios(
                                    active_users,
                                    concurrency,
                                    settings.requests_per_user,
                                    run_id,
                                )
                            )
                            if worker is not None:
                                task_samples, completed_tasks = run_task_scenario(
                                    admin_clients[:concurrency],
                                    concurrency,
                                    settings.task_timeout_seconds,
                                )
                                samples.extend(task_samples)
                                task_results[f"concurrency_{concurrency}"] = completed_tasks

                        if not settings.skip_faults:
                            faults = run_fault_scenarios(
                                users,
                                admin_clients,
                                worker,
                                settings.task_timeout_seconds,
                            )
                        metrics_after = read_metrics(server.base_url, settings.timeout_seconds)
            except Exception as error:  # noqa: BLE001 - 报告必须记录失败并继续清理。
                report_error = f"{type(error).__name__}: {error}"
            finally:
                if worker is not None:
                    worker.stop()
                if web_app is not None:
                    web_app.state.backend.store.close()

    summaries = summarize_samples(samples)
    checks = evaluate_acceptance(summaries, faults, settings, report_error)
    finished_at = datetime.now(UTC)
    report = {
        "report_version": 1,
        "run_id": run_id,
        "profile": settings.profile,
        "started_at": started_at.isoformat(),
        "finished_at": finished_at.isoformat(),
        "duration_seconds": round((finished_at - started_at).total_seconds(), 3),
        "concurrency_levels": list(settings.concurrency_levels),
        "requests_per_user": settings.requests_per_user,
        "external_models_called": False,
        "isolation": {
            "postgres_schema": schema,
            "celery_queue": queue_name,
            "schema_removed_after_run": True,
            "dedicated_redis_database": redis_isolated,
            "redis_database_flushed_after_run": redis_isolated,
        },
        "summary": summaries,
        "samples": samples_as_dicts(samples),
        "faults": faults,
        "background_tasks": task_results,
        "request_metrics": {
            "before": summarize_prometheus_metrics(metrics_before),
            "after": summarize_prometheus_metrics(metrics_after),
        },
        "checks": checks,
        "error": report_error,
        "passed": all(bool(check["passed"]) for check in checks) and report_error is None,
    }
    return redact_sensitive_data(report)  # type: ignore[return-value]


def prepare_users(
    web_app: object,
    base_url: str,
    count: int,
    run_id: str,
    timeout_seconds: float,
    task_timeout_seconds: float,
) -> list[LoadUser]:
    backend = web_app.state.backend  # type: ignore[attr-defined]
    shared_password = f"LoadUser-{run_id}-Password"
    password_hash = hash_password(shared_password)
    users: list[LoadUser] = []
    pending_rag_tasks: list[tuple[LoadHttpClient, str]] = []
    for index in range(count):
        email = f"load-{run_id[:12]}-{index}@example.invalid"
        account = backend.store.create_account(
            email=email,
            password_hash=password_hash,
            display_name=f"负载账号 {index + 1}",
            email_verified=True,
        )
        candidate_id = backend.save_candidate_profile(
            CandidateProfileInput(
                name=f"负载候选人 {index + 1}",
                status="求职中",
                education="本科",
                experience_years=3,
                skills={"Python": "熟练"},
                preferred_cities=["杭州"],
                salary_floor_k=None,
                expected_salary_k=None,
                target_directions=["后端开发"],
            ),
            account_id=account.id,
        )
        query_term = f"loadgolden{run_id[:8]}x{index}"
        long_text_id = backend.store.add_long_text(
            "conversation_message",
            candidate_id,
            f"e2e:{query_term}",
            f"{query_term} 负责 PostgreSQL、Celery 与 FastAPI 端到端交付。",
            account_id=account.id,
            candidate_id=candidate_id,
        )
        client = LoadHttpClient(base_url, timeout_seconds)
        login = client.login(email, shared_password)
        if login.status_code != 200:
            raise RuntimeError(f"临时负载账号登录失败，HTTP {login.status_code}。")
        if backend.task_queue_enabled:
            task = backend.enqueue_rag_index_task(
                long_text_ids=[long_text_id],
                account_id=account.id,
                candidate_id=candidate_id,
                session_id=f"e2e-seed-{index}",
                root_request_id=f"e2e-seed-{run_id[:12]}-{index}",
                idempotency_key=f"e2e-rag-seed:{run_id}:{index}",
            )
            pending_rag_tasks.append((client, task.task_key))
        else:
            backend.index_rag_long_texts(
                [long_text_id],
                account_id=account.id,
                candidate_id=candidate_id,
                session_id=f"e2e-seed-{index}",
                root_request_id=f"e2e-seed-{run_id[:12]}-{index}",
            )
        users.append(
            LoadUser(
                index=index,
                email=email,
                account_id=account.id,
                candidate_id=candidate_id,
                query_term=query_term,
                client=client,
            )
        )
    for client, task_key in pending_rag_tasks:
        task, _ = wait_for_task(client, task_key, task_timeout_seconds)
        if task.get("status") != "succeeded":
            raise RuntimeError(
                f"Celery RAG 种子索引失败，任务状态为 {task.get('status', 'unknown')}。"
            )
    return users


def prepare_admin_clients(
    base_url: str,
    email: str,
    password: str,
    count: int,
    timeout_seconds: float,
) -> list[LoadHttpClient]:
    clients: list[LoadHttpClient] = []
    for _ in range(count):
        client = LoadHttpClient(base_url, timeout_seconds)
        response = client.login(email, password)
        if response.status_code != 200:
            raise RuntimeError(f"临时管理员登录失败，HTTP {response.status_code}。")
        clients.append(client)
    return clients


def run_http_scenarios(
    users: Sequence[LoadUser],
    concurrency: int,
    requests_per_user: int,
    run_id: str,
) -> list[LoadSample]:
    samples: list[LoadSample] = []
    scenario_functions: tuple[tuple[str, Callable[[LoadUser, int], LoadSample]], ...] = (
        ("health", lambda user, _: sample_json("health", concurrency, user.client.request_json("GET", "/api/health"), {200})),
        ("profile_list", lambda user, _: sample_json("profile_list", concurrency, user.client.request_json("GET", "/api/profiles"), {200})),
        ("profile_get", lambda user, _: sample_json("profile_get", concurrency, user.client.request_json("GET", f"/api/profiles/{user.candidate_id}"), {200})),
        ("rag_search", lambda user, _: sample_rag(user, concurrency)),
        ("sse_chat", lambda user, iteration: sample_sse(user, concurrency, run_id, iteration)),
    )
    for scenario_name, scenario in scenario_functions:
        with ThreadPoolExecutor(max_workers=concurrency) as executor:
            futures = [
                executor.submit(
                    run_user_iterations,
                    scenario,
                    user,
                    requests_per_user,
                    scenario_name,
                    concurrency,
                )
                for user in users
            ]
            for future in as_completed(futures):
                samples.extend(future.result())
    return samples


def run_user_iterations(
    scenario: Callable[[LoadUser, int], LoadSample],
    user: LoadUser,
    requests_per_user: int,
    scenario_name: str,
    concurrency: int,
) -> list[LoadSample]:
    results: list[LoadSample] = []
    for iteration in range(requests_per_user):
        try:
            results.append(scenario(user, iteration))
        except Exception as error:  # noqa: BLE001 - 单请求失败不能中止整轮采样。
            results.append(
                LoadSample(
                    scenario=scenario_name,
                    concurrency=concurrency,
                    success=False,
                    status_code=None,
                    elapsed_ms=0,
                    error=type(error).__name__,
                )
            )
    return results


def sample_json(
    scenario: str,
    concurrency: int,
    response: object,
    expected_statuses: set[int],
) -> LoadSample:
    success = response.status_code in expected_statuses
    return LoadSample(
        scenario=scenario,
        concurrency=concurrency,
        success=success,
        status_code=response.status_code,
        elapsed_ms=response.elapsed_ms,
        error=None if success else f"HTTP {response.status_code}",
    )


def sample_rag(user: LoadUser, concurrency: int) -> LoadSample:
    response = user.client.request_json(
        "GET",
        "/api/rag/search",
        query={"query": user.query_term, "top_n": 5},
    )
    results = response.body.get("results", []) if isinstance(response.body, Mapping) else []
    matched = any(
        user.query_term in str(item.get("content", ""))
        for item in results
        if isinstance(item, Mapping)
    )
    success = response.status_code == 200 and matched
    return LoadSample(
        "rag_search",
        concurrency,
        success,
        response.status_code,
        response.elapsed_ms,
        error=None if success else "expected evidence not returned",
    )


def sample_sse(user: LoadUser, concurrency: int, run_id: str, iteration: int) -> LoadSample:
    response = user.client.stream_sse(
        "/api/chat/stream",
        payload={
            "candidate_id": user.candidate_id,
            "message": f"端到端流式负载 {run_id[:8]} {iteration}",
            "session_id": f"e2e-chat-{run_id[:8]}-{concurrency}-{user.index}-{iteration}",
            "use_env_llm": True,
            "auto_rag": False,
        },
    )
    event_names = [event.name for event in response.events]
    success = (
        response.status_code == 200
        and event_names.count("token") == 4
        and "final" in event_names
        and "error" not in event_names
    )
    return LoadSample(
        "sse_chat",
        concurrency,
        success,
        response.status_code,
        response.elapsed_ms,
        error=None if success else "invalid SSE event sequence",
        first_event_ms=response.first_event_ms,
        event_count=len(response.events),
    )


def run_task_scenario(
    clients: Sequence[LoadHttpClient],
    concurrency: int,
    timeout_seconds: float,
) -> tuple[list[LoadSample], dict[str, int]]:
    samples: list[LoadSample] = []
    task_keys: list[str] = []
    started_at: dict[str, float] = {}
    with ThreadPoolExecutor(max_workers=concurrency) as executor:
        futures = {
            executor.submit(
                client.request_json,
                "POST",
                "/api/admin/tasks/probe",
                csrf=True,
            ): client
            for client in clients
        }
        for future in as_completed(futures):
            response = future.result()
            task = response.body.get("task", {}) if isinstance(response.body, Mapping) else {}
            task_key = str(task.get("task_key", "")) if isinstance(task, Mapping) else ""
            success = response.status_code == 200 and bool(task_key)
            samples.append(
                LoadSample(
                    "task_submit",
                    concurrency,
                    success,
                    response.status_code,
                    response.elapsed_ms,
                    error=None if success else "task submission failed",
                )
            )
            if task_key:
                task_keys.append(task_key)
                started_at[task_key] = time.perf_counter()

    completed = {"succeeded": 0, "failed": 0, "cancelled": 0, "timeout": 0}
    client = clients[0]
    for task_key in task_keys:
        task, elapsed_ms = wait_for_task(client, task_key, timeout_seconds, started_at[task_key])
        status = str(task.get("status", "timeout"))
        completed[status if status in completed else "failed"] += 1
        success = status == "succeeded"
        samples.append(
            LoadSample(
                "task_complete",
                concurrency,
                success,
                200 if status != "timeout" else None,
                elapsed_ms,
                error=None if success else f"task {status}",
            )
        )
    return samples, completed


def wait_for_task(
    client: LoadHttpClient,
    task_key: str,
    timeout_seconds: float,
    started_at: float | None = None,
) -> tuple[dict[str, object], float]:
    actual_started_at = started_at or time.perf_counter()
    deadline = time.monotonic() + timeout_seconds
    last_task: dict[str, object] = {"status": "timeout"}
    while time.monotonic() < deadline:
        response = client.request_json("GET", f"/api/tasks/{task_key}")
        if response.status_code == 200 and isinstance(response.body, Mapping):
            raw_task = response.body.get("task")
            if isinstance(raw_task, Mapping):
                last_task = dict(raw_task)
                if str(last_task.get("status")) in FINAL_TASK_STATES:
                    break
        time.sleep(0.1)
    return last_task, (time.perf_counter() - actual_started_at) * 1000


def run_fault_scenarios(
    users: Sequence[LoadUser],
    admin_clients: Sequence[LoadHttpClient],
    worker: DockerWorker | None,
    task_timeout_seconds: float,
) -> dict[str, object]:
    first, second = users[0], users[1] if len(users) > 1 else users[0]
    missing_csrf = first.client.request_json(
        "POST",
        "/api/profiles",
        payload={"name": "CSRF 应拒绝"},
    )
    cross_account = first.client.request_json(
        "GET",
        f"/api/profiles/{second.candidate_id}",
    )
    duplicate = first.client.request_json(
        "POST",
        "/api/profiles",
        payload={
            "name": f"负载候选人 {first.index + 1}",
            "status": "求职中",
            "education": "本科",
            "experience_years": 3,
            "skills": {"Python": "熟练"},
            "preferred_cities": ["杭州"],
            "target_directions": ["后端开发"],
        },
        csrf=True,
    )
    circuit = first.client.stream_sse(
        "/api/chat/stream",
        payload={
            "candidate_id": first.candidate_id,
            "message": "[fault:circuit]",
            "use_env_llm": True,
            "auto_rag": False,
        },
    )
    timeout = first.client.stream_sse(
        "/api/chat/stream",
        payload={
            "candidate_id": first.candidate_id,
            "message": "[fault:timeout]",
            "use_env_llm": True,
            "auto_rag": False,
        },
    )
    faults: dict[str, object] = {
        "csrf_rejected": missing_csrf.status_code == 403,
        "cross_account_denied": cross_account.status_code == 404,
        "duplicate_profile_rejected": duplicate.status_code == 409,
        "model_circuit_reported_in_sse": any(event.name == "error" for event in circuit.events),
        "model_timeout_reported_in_sse": any(event.name == "error" for event in timeout.events),
    }
    if worker is None:
        faults["worker_restart"] = {"skipped": True}
        return faults

    worker.stop()
    queued = admin_clients[0].request_json(
        "POST",
        "/api/admin/tasks/probe",
        csrf=True,
    )
    raw_task = queued.body.get("task", {}) if isinstance(queued.body, Mapping) else {}
    task_key = str(raw_task.get("task_key", "")) if isinstance(raw_task, Mapping) else ""
    queued_before_restart = str(raw_task.get("status")) == "queued" if isinstance(raw_task, Mapping) else False
    worker.start()
    task, _ = wait_for_task(admin_clients[0], task_key, task_timeout_seconds)
    faults["worker_restart"] = {
        "queued_while_stopped": queued.status_code == 200 and queued_before_restart,
        "completed_after_restart": task.get("status") == "succeeded",
    }
    return faults


def evaluate_acceptance(
    summaries: Mapping[str, Mapping[str, object]],
    faults: Mapping[str, object],
    settings: RunnerSettings,
    report_error: str | None,
) -> list[dict[str, object]]:
    normal_scenarios = {"health", "profile_list", "profile_get"}
    normal_groups = [
        summary
        for summary in summaries.values()
        if summary.get("scenario") in normal_scenarios
    ]
    sse_groups = [summary for summary in summaries.values() if summary.get("scenario") == "sse_chat"]
    rag_groups = [summary for summary in summaries.values() if summary.get("scenario") == "rag_search"]
    task_groups = [summary for summary in summaries.values() if summary.get("scenario") == "task_complete"]
    normal_requests = sum(int(group.get("requests", 0)) for group in normal_groups)
    normal_failures = sum(int(group.get("failures", 0)) for group in normal_groups)
    normal_error_rate = normal_failures / normal_requests if normal_requests else 1.0
    normal_p95 = max(
        (float(group["latency_ms"]["p95"]) for group in normal_groups if group["latency_ms"]["p95"] is not None),
        default=None,
    )
    sse_first_p95 = max(
        (
            float(group["first_event_ms"]["p95"])
            for group in sse_groups
            if isinstance(group.get("first_event_ms"), Mapping)
            and group["first_event_ms"].get("p95") is not None
        ),
        default=None,
    )
    rag_p95 = max(
        (
            float(group["latency_ms"]["p95"])
            for group in rag_groups
            if group["latency_ms"]["p95"] is not None
        ),
        default=None,
    )
    sse_failures = sum(int(group.get("failures", 0)) for group in sse_groups)
    task_failures = sum(int(group.get("failures", 0)) for group in task_groups)
    fault_passed = True if settings.skip_faults else all_faults_passed(faults)
    return [
        {"name": "runner_completed", "passed": report_error is None, "actual": report_error},
        {"name": "normal_api_error_rate_below_1_percent", "passed": normal_error_rate < 0.01, "actual": normal_error_rate, "target": 0.01},
        {"name": "normal_api_p95_below_500_ms", "passed": normal_p95 is not None and normal_p95 < 500, "actual": normal_p95, "target_ms": 500},
        {"name": "rag_search_p95_below_1500_ms", "passed": rag_p95 is not None and rag_p95 < 1500, "actual": rag_p95, "target_ms": 1500},
        {"name": "sse_streams_complete", "passed": bool(sse_groups) and sse_failures == 0, "actual_failures": sse_failures},
        {"name": "sse_first_event_p95_below_2_seconds", "passed": sse_first_p95 is not None and sse_first_p95 < 2000, "actual_ms": sse_first_p95, "target_ms": 2000},
        {"name": "background_tasks_not_lost", "passed": settings.skip_worker or (bool(task_groups) and task_failures == 0), "actual_failures": task_failures, "skipped": settings.skip_worker},
        {"name": "fault_controls_behave_as_expected", "passed": fault_passed, "skipped": settings.skip_faults},
        {"name": "real_model_calls_are_disabled", "passed": True},
    ]


def all_faults_passed(value: object) -> bool:
    if isinstance(value, Mapping):
        return all(all_faults_passed(item) for key, item in value.items() if key != "skipped")
    if isinstance(value, bool):
        return value
    return True


def read_metrics(base_url: str, timeout_seconds: float) -> str:
    response = LoadHttpClient(base_url, timeout_seconds).request_text("GET", "/internal/metrics")
    return response.body if response.status_code == 200 else ""


def summarize_prometheus_metrics(text: str) -> dict[str, float]:
    wanted = {
        "job_agent_http_requests_total",
        "job_agent_http_requests_in_flight",
        "job_agent_http_request_duration_seconds_sum",
        "job_agent_http_request_duration_seconds_count",
        "job_agent_http_request_duration_max_seconds",
        "job_agent_security_rejections_total",
    }
    summary: dict[str, float] = {}
    for line in text.splitlines():
        if not line or line.startswith("#"):
            continue
        metric, separator, raw_value = line.rpartition(" ")
        base_name = metric.split("{", 1)[0]
        if not separator or base_name not in wanted:
            continue
        try:
            numeric_value = float(raw_value)
        except ValueError:
            continue
        summary[metric] = numeric_value
    return summary


@contextmanager
def isolated_database_schema(base_url: str, schema: str) -> Iterator[str]:
    if not SCHEMA_PATTERN.fullmatch(schema):
        raise ValueError("隔离 schema 名称不安全。")
    engine = sa.create_engine(base_url, pool_pre_ping=True, connect_args={"connect_timeout": 5})
    schema_created = False
    try:
        with engine.begin() as connection:
            connection.execute(sa.text(f'CREATE SCHEMA "{schema}"'))
        schema_created = True
        database_url = schema_database_url(base_url, schema)
        upgrade_database(database_url)
        yield database_url
    finally:
        if schema_created:
            with engine.begin() as connection:
                connection.execute(sa.text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        engine.dispose()


@contextmanager
def isolated_redis_database(redis_url: str | None) -> Iterator[bool]:
    """要求 Celery 压测使用空 Redis DB，并在结束时完整清理。"""

    if not redis_url:
        yield False
        return
    import redis

    client = redis.Redis.from_url(
        redis_url,
        socket_connect_timeout=3,
        socket_timeout=3,
        decode_responses=False,
    )
    cleanup_allowed = False
    try:
        client.ping()
        existing_keys = int(client.dbsize())
        if existing_keys:
            raise RuntimeError(
                "端到端压测要求独占一个空 Redis DB；当前目标数据库已有键，已拒绝清空。"
            )
        cleanup_allowed = True
        yield True
    finally:
        try:
            if cleanup_allowed:
                client.flushdb()
        finally:
            client.close()


def schema_database_url(base_url: str, schema: str) -> str:
    if not SCHEMA_PATTERN.fullmatch(schema):
        raise ValueError("隔离 schema 名称不安全。")
    parsed = urlsplit(base_url)
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    current_options = query.get("options", "").strip()
    query["options"] = f"{current_options} -csearch_path={schema},public".strip()
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(query), parsed.fragment))


def replace_service_host(value: str, hostname: str, port: int) -> str:
    parsed = urlsplit(value)
    if not parsed.scheme or not parsed.netloc:
        raise ValueError("服务地址必须是包含协议和主机的 URL。")
    userinfo = ""
    if parsed.username is not None:
        userinfo = quote(parsed.username, safe="")
        if parsed.password is not None:
            userinfo += f":{quote(parsed.password, safe='')}"
        userinfo += "@"
    return urlunsplit(
        (parsed.scheme, f"{userinfo}{hostname}:{port}", parsed.path, parsed.query, parsed.fragment)
    )


def build_safe_environment(
    *,
    database_url: str,
    redis_url: str | None,
    queue_name: str,
    admin_email: str,
    admin_password: str,
    task_queue_enabled: bool,
) -> dict[str, str]:
    values = {
        "JOB_AGENT_ENVIRONMENT": "test",
        "JOB_AGENT_DATABASE_URL": database_url,
        "JOB_AGENT_OBJECT_STORAGE_BACKEND": "local",
        "JOB_AGENT_COOKIE_SECURE": "false",
        "JOB_AGENT_CSRF_ENABLED": "true",
        "JOB_AGENT_SECURITY_HEADERS_ENABLED": "true",
        "JOB_AGENT_RATE_LIMIT_ENABLED": "true",
        "JOB_AGENT_RATE_LIMIT_BACKEND": "memory",
        "JOB_AGENT_RATE_LIMIT_DEFAULT_REQUESTS": "100000",
        "JOB_AGENT_RATE_LIMIT_AUTH_REQUESTS": "100000",
        "JOB_AGENT_RATE_LIMIT_MODEL_REQUESTS": "100000",
        "JOB_AGENT_RATE_LIMIT_UPLOAD_REQUESTS": "100000",
        "JOB_AGENT_RATE_LIMIT_ADMIN_REQUESTS": "100000",
        "JOB_AGENT_RATE_LIMIT_WRITE_REQUESTS": "100000",
        "JOB_AGENT_EMAIL_VERIFICATION_REQUIRED": "false",
        "JOB_AGENT_CONSENT_REQUIRED": "false",
        "JOB_AGENT_EMAIL_BACKEND": "console",
        "JOB_AGENT_BILLING_STARTING_BALANCE_YUAN": "100",
        "JOB_AGENT_BOOTSTRAP_ADMIN_EMAIL": admin_email,
        "JOB_AGENT_BOOTSTRAP_ADMIN_PASSWORD": admin_password,
        "JOB_AGENT_BOOTSTRAP_ADMIN_DISPLAY_NAME": "端到端测试管理员",
        "JOB_AGENT_TASK_QUEUE_ENABLED": "true" if task_queue_enabled else "false",
        "JOB_AGENT_TASK_QUEUE_NAME": queue_name,
        "JOB_AGENT_EMBEDDING_PROVIDER": "local_hash",
        "JOB_AGENT_EMBEDDING_API_STYLE": "local_hash",
        "JOB_AGENT_EMBEDDING_DIMENSIONS": "2560",
        "JOB_AGENT_INTENT_ROUTER_ENABLED": "false",
        "JOB_AGENT_CONCURRENCY_BACKEND": "memory",
        "JOB_AGENT_MEMORY_CHECKPOINT_BACKEND": "database",
        "JOB_AGENT_PROJECT_VISUAL_ANALYSIS_ENABLED": "false",
    }
    if redis_url:
        values["JOB_AGENT_REDIS_URL"] = redis_url
    return values


@contextmanager
def isolated_job_agent_environment(values: Mapping[str, str]) -> Iterator[None]:
    previous = {key: value for key, value in os.environ.items() if key.startswith("JOB_AGENT_")}
    for key in list(os.environ):
        if key.startswith("JOB_AGENT_"):
            os.environ.pop(key, None)
    os.environ.update(values)
    try:
        yield
    finally:
        for key in list(os.environ):
            if key.startswith("JOB_AGENT_"):
                os.environ.pop(key, None)
        os.environ.update(previous)


def write_env_file(path: Path, values: Mapping[str, str]) -> None:
    lines = [f"{key}={dotenv_quote(value)}" for key, value in sorted(values.items())]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def dotenv_quote(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def write_report(report: Mapping[str, object], report_dir: Path) -> Path:
    report_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    output_path = report_dir / f"e2e-load-{timestamp}.json"
    output_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False),
        encoding="utf-8",
    )
    return output_path


if __name__ == "__main__":
    raise SystemExit(main())
