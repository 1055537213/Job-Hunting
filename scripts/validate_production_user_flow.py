"""Run a disposable, real production user-flow smoke test.

This script is designed to run inside the production Web container. It creates a
verified temporary account, exercises the public HTTPS entry point, and deletes
the account and all cascaded records in a ``finally`` block.
"""

from __future__ import annotations

import argparse
import json
import time
import traceback
import uuid
from collections.abc import Mapping, Sequence

from job_hunting_agent.app import JobHuntingApp
from job_hunting_agent.auth import hash_password
from job_hunting_agent.e2e_load import LoadHttpClient


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--env-file", default="/app/.env")
    parser.add_argument(
        "--confirmation",
        required=True,
        help="Must be RUN_PRODUCTION_USER_FLOW.",
    )
    parser.add_argument("--timeout-seconds", type=float, default=180.0)
    return parser.parse_args(argv)


def run(args: argparse.Namespace) -> dict[str, object]:
    if args.confirmation != "RUN_PRODUCTION_USER_FLOW":
        raise ValueError("Refusing to run without --confirmation RUN_PRODUCTION_USER_FLOW.")
    if not args.base_url.startswith("https://"):
        raise ValueError("Production user-flow validation requires an HTTPS base URL.")

    run_id = uuid.uuid4().hex
    marker = f"canaryproof{run_id[:12]}"
    email = f"production-canary-{run_id[:12]}@example.invalid"
    password = f"Canary-{run_id}-Password!"
    backend = JobHuntingApp(env_path=args.env_file)
    account_id: int | None = None
    candidate_id: int | None = None
    job_id: int | None = None
    report: dict[str, object] = {
        "run_id": run_id,
        "base_url": args.base_url.rstrip("/"),
        "checks": [],
        "passed": False,
    }
    started_at = time.perf_counter()

    def check(name: str, condition: bool, detail: str = "") -> None:
        checks = report["checks"]
        assert isinstance(checks, list)
        checks.append({"name": name, "passed": bool(condition), "detail": detail})
        if not condition:
            raise AssertionError(f"{name}: {detail}")

    try:
        account = backend.store.create_account(
            email=email,
            password_hash=hash_password(password),
            display_name="Production acceptance account",
            email_verified=True,
        )
        account_id = account.id
        backend.store.create_simulated_recharge_order(
            account_id,
            2,
            idempotency_key=f"production-canary-credit:{run_id}",
            description="Disposable production user-flow balance",
        )

        client = LoadHttpClient(args.base_url, timeout_seconds=args.timeout_seconds)
        login = client.login(email, password)
        check("login", login.status_code == 200, f"HTTP {login.status_code}")

        me_before = client.request_json("GET", "/api/auth/me")
        before_billing = _mapping_value(me_before.body, "billing")
        check(
            "auth_me",
            me_before.status_code == 200
            and bool(_value(me_before.body, "authenticated", False)),
            f"HTTP {me_before.status_code}",
        )
        check("csrf_issued", bool(client.csrf_token), "missing CSRF token")

        profile = client.request_json(
            "POST",
            "/api/profiles",
            payload={
                "name": "Production canary candidate",
                "status": "Seeking",
                "education": "Bachelor",
                "experience_years": 3,
                "skills": {"Python": "proficient", "FastAPI": "proficient"},
                "preferred_cities": ["Hangzhou"],
                "acceptable_cities": ["Shanghai"],
                "salary_floor_k": 18,
                "expected_salary_k": 25,
                "target_directions": ["Backend development"],
                "unacceptable": ["Long-term travel"],
            },
            csrf=True,
        )
        check(
            "profile_create",
            profile.status_code == 200,
            _http_detail(profile.status_code, profile.body),
        )
        candidate_id = int(_value(profile.body, "candidate_id"))

        profiles = client.request_json("GET", "/api/profiles")
        profile_rows = _sequence_value(profiles.body, "profiles")
        check(
            "profile_list",
            profiles.status_code == 200
            and any(int(item["id"]) == candidate_id for item in profile_rows),
            f"HTTP {profiles.status_code}",
        )
        profile_get = client.request_json("GET", f"/api/profiles/{candidate_id}")
        stored_profile = _mapping_value(profile_get.body, "profile")
        check(
            "profile_get",
            profile_get.status_code == 200
            and stored_profile.get("preferred_cities") == ["Hangzhou"],
            f"HTTP {profile_get.status_code}",
        )

        raw_job = "\n".join(
            (
                "Python 后端工程师",
                f"验收标记：{marker}",
                "公司：验收测试科技有限公司",
                "职位名称：Python 后端工程师",
                "职位描述：负责求职助手服务端接口、数据处理与异步任务开发。",
                "岗位职责：设计 FastAPI 接口，维护 PostgreSQL 数据库和 Celery 任务。",
                "任职要求：熟悉 Python、FastAPI、PostgreSQL、Redis，有三年开发经验。",
                "工作地点：杭州",
                "薪资：20-30K",
            )
        )
        job = client.request_json(
            "POST",
            "/api/jobs",
            payload={
                "raw_text": raw_job,
                "source_url": f"https://example.invalid/jobs/{marker}",
            },
            csrf=True,
        )
        check(
            "job_import",
            job.status_code == 200,
            _http_detail(job.status_code, job.body),
        )
        job_id = int(_value(_mapping_value(job.body, "job"), "id"))
        jobs = client.request_json("GET", "/api/jobs")
        job_rows = _sequence_value(jobs.body, "jobs")
        check(
            "job_list",
            jobs.status_code == 200 and any(int(item["id"]) == job_id for item in job_rows),
            f"HTTP {jobs.status_code}",
        )

        long_text_id = backend.store.add_long_text(
            "project_description",
            candidate_id,
            f"production-canary:{marker}",
            (
                f"{marker} uses FastAPI, PostgreSQL, Redis and Celery for API and "
                "asynchronous task delivery."
            ),
            account_id=account_id,
            candidate_id=candidate_id,
        )
        index_stats = backend.index_rag_long_texts(
            [long_text_id],
            account_id=account_id,
            candidate_id=candidate_id,
            session_id=f"production-canary-index-{run_id[:12]}",
            root_request_id=f"production-canary-index-{run_id}",
        )
        check("rag_index", index_stats.chunk_count >= 1, repr(index_stats))

        rag = client.request_json(
            "GET",
            "/api/rag/search",
            query={"query": marker, "top_n": 5},
        )
        rag_results = _sequence_value(rag.body, "results")
        check(
            "rag_search",
            rag.status_code == 200
            and any(marker in str(item.get("content", "")) for item in rag_results),
            f"HTTP {rag.status_code}; results={len(rag_results)}",
        )

        session_id = f"production-canary-chat-{run_id[:12]}"
        chat = client.stream_sse(
            "/api/chat/stream",
            payload={
                "candidate_id": candidate_id,
                "message": (
                    f"Search my knowledge and tell me which backend technologies {marker} "
                    "uses. Answer in one sentence and do not modify my profile."
                ),
                "use_env_llm": True,
                "auto_rag": True,
                "session_id": session_id,
            },
        )
        event_names = [event.name for event in chat.events]
        final_events = [
            event.data
            for event in chat.events
            if event.name == "final" and isinstance(event.data, Mapping)
        ]
        error_events = [
            event.data for event in chat.events if event.name in {"error", "task_failed"}
        ]
        check("chat_http", chat.status_code == 200, f"HTTP {chat.status_code}")
        check(
            "chat_stream_final",
            bool(final_events),
            f"events={event_names}; errors={error_events}",
        )
        check("chat_no_failure", not error_events, repr(error_events))
        final_reply = str(
            final_events[-1].get("display_reply")
            or final_events[-1].get("reply")
            or ""
        )
        check("chat_reply", bool(final_reply.strip()), "empty assistant reply")

        history = client.request_json(
            "GET",
            "/api/chat/history",
            query={"candidate_id": candidate_id, "session_id": session_id, "limit": 20},
        )
        messages = _sequence_value(history.body, "messages")
        roles = [item.get("role") for item in messages]
        check(
            "chat_history",
            history.status_code == 200 and "user" in roles and "assistant" in roles,
            f"HTTP {history.status_code}; roles={roles}",
        )

        me_after = client.request_json("GET", "/api/auth/me")
        after_billing = _mapping_value(me_after.body, "billing")
        consumed_before = int(before_billing.get("total_consumed_micro_yuan", 0) or 0)
        consumed_after = int(after_billing.get("total_consumed_micro_yuan", 0) or 0)
        check(
            "billing_consumed",
            consumed_after > consumed_before,
            f"before={consumed_before}; after={consumed_after}",
        )

        deleted_job = client.request_json("DELETE", f"/api/jobs/{job_id}", csrf=True)
        check(
            "job_delete",
            deleted_job.status_code == 200
            and bool(_value(deleted_job.body, "deleted", False)),
            f"HTTP {deleted_job.status_code}",
        )
        job_id = None
        deleted_profile = client.request_json(
            "DELETE", f"/api/profiles/{candidate_id}", csrf=True
        )
        check(
            "profile_delete",
            deleted_profile.status_code == 200
            and bool(_value(deleted_profile.body, "deleted", False)),
            f"HTTP {deleted_profile.status_code}",
        )
        candidate_id = None
        logout = client.request_json(
            "POST", "/api/auth/logout", payload={}, csrf=True
        )
        check(
            "logout",
            logout.status_code == 200
            and bool(_value(logout.body, "ok", False)),
            f"HTTP {logout.status_code}",
        )

        report["timings_ms"] = {
            "rag_search": round(rag.elapsed_ms, 1),
            "chat_total": round(chat.elapsed_ms, 1),
            "chat_first_event": round(chat.first_event_ms or 0, 1),
        }
        report["chat_event_names"] = event_names
        report["billing_consumed_micro_yuan"] = consumed_after - consumed_before
        report["passed"] = True
    except Exception as error:  # noqa: BLE001 - report failure, then always clean up.
        report["error"] = f"{type(error).__name__}: {error}"
        report["traceback"] = traceback.format_exc(limit=8)
    finally:
        if account_id is not None:
            try:
                with backend.store.connect() as connection:
                    connection.execute("DELETE FROM accounts WHERE id = ?", (account_id,))
                with backend.store.connect() as connection:
                    remaining = int(
                        connection.execute(
                            "SELECT COUNT(*) AS count FROM accounts WHERE id = ?",
                            (account_id,),
                        ).fetchone()["count"]
                    )
                report["cleanup"] = {"account_removed": remaining == 0}
                if remaining != 0:
                    report["passed"] = False
            except Exception as cleanup_error:  # noqa: BLE001 - preserve cleanup evidence.
                report["cleanup"] = {
                    "account_removed": False,
                    "error": f"{type(cleanup_error).__name__}: {cleanup_error}",
                }
                report["passed"] = False
        backend.store.close()
        report["duration_seconds"] = round(time.perf_counter() - started_at, 3)
    return report


def _mapping_value(
    value: object,
    key: str,
) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        return {}
    item = value.get(key)
    return item if isinstance(item, Mapping) else {}


def _value(value: object, key: str, default: object | None = None) -> object:
    if not isinstance(value, Mapping):
        return default
    return value.get(key, default)


def _http_detail(status_code: int, body: object) -> str:
    if status_code < 400:
        return f"HTTP {status_code}"
    return f"HTTP {status_code}: {body}"


def _sequence_value(value: object, key: str) -> list[Mapping[str, object]]:
    if not isinstance(value, Mapping):
        return []
    items = value.get(key)
    if not isinstance(items, list):
        return []
    return [item for item in items if isinstance(item, Mapping)]


def main(argv: Sequence[str] | None = None) -> int:
    report = run(parse_args(argv))
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if bool(report["passed"]) else 1


if __name__ == "__main__":
    raise SystemExit(main())
