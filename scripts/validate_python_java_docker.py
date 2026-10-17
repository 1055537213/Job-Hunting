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


def run_browser_gateway_flow(gateway_url: str, web_url: str, java_url: str, internal_token: str,
                             web_container: str, admin_credentials: tuple[str, str], database_url: str) -> None:
    """Verify that the same browser session crosses the Java/Python proxy boundary."""
    from urllib.parse import parse_qs, urlsplit

    from job_hunting_agent.config import PlatformAuthSettings
    from job_hunting_agent.platform_auth import PlatformAuthClient
    from job_hunting_agent.platform_email import deliver_platform_verification
    from job_hunting_agent.models import UsageEventRecord
    from job_hunting_agent.sqlalchemy_store import SQLAlchemyStore
    from job_hunting_agent.tool_audit import build_tool_trace_record
    from job_hunting_agent.web import new_task_trace, start_task_step, complete_task_step

    auth = PlatformAuthClient(PlatformAuthSettings(True, java_url, internal_token, 10))
    email = f"gateway-{os.urandom(12).hex()}@example.com"
    password = "gateway-password-123"

    class RecordingSender:
        url = ""

        def send_verification(self, recipient, url):
            assert recipient == email
            self.url = url

    with httpx.Client(base_url=gateway_url, timeout=10) as browser:
        assert browser.get("/internal/metrics").status_code == 404
        assert browser.get("/api/health").json()["status"] == "ok"
        config_response = browser.get("/api/auth/config")
        assert config_response.status_code == 200, config_response.text[:500]
        config = config_response.json()
        assert config["email_verification_required"] is True
        assert browser.get("/api/auth/me").json()["authenticated"] is False
        assert browser.get("/api/me/balance").status_code == 401
        assert browser.get("/api/me/recharge/orders").status_code == 401
        registered = browser.post("/api/auth/register", json={
            "email": email, "password": password, "display_name": "Gateway acceptance",
        })
        assert registered.status_code == 200, registered.text[:300]
        assert registered.json()["verification_required"] is True
        store = SQLAlchemyStore(database_url)
        try:
            from datetime import UTC, datetime
            progress_task = store.create_background_task(
                account_id=registered.json()["account"]["id"], task_type="system_probe",
                payload={"private": "must-not-leak"}, session_id="private-session",
                idempotency_key=f"private-task-{email}")
            task_key = progress_task.task_key
            store.create_background_task(account_id=registered.json()["account"]["id"], task_type="system_probe")
            for index in range(2):
                store.record_usage_event(UsageEventRecord(
                    id=0, account_id=registered.json()["account"]["id"], candidate_id=None, session_id="gateway-usage",
                    root_request_id=None, call_id=f"gateway-usage-{email}-{index}", provider="contract", model="contract",
                    operation="chat", input_tokens=8, output_tokens=2, total_tokens=10, usage_source="estimated",
                    status="succeeded", attempt=1, provider_request_id=None, raw_usage={"total_tokens": 10},
                    created_at=datetime.now(UTC).isoformat(timespec="seconds"), billable=False, pricing_version=None))
                task = new_task_trace(f"gateway-tool-{registered.json()['account']['id']}-{index}")
                start_task_step(task, "search_candidate_evidence")
                complete_task_step(task, "search_candidate_evidence", {"results": [{"id": 1}, {"id": 2}]})
                if index == 1:
                    start_task_step(task, "analyze_project")
                    complete_task_step(task, "analyze_project", {"error": "provider unavailable"})
                    task["status"] = "failed"
                    task["finished_at"] = task["updated_at"]
                else:
                    task["status"] = "waiting_confirmation"
                    task["approval"] = {"status": "pending"}
                store.record_tool_call_trace(build_tool_trace_record(task, account_id=registered.json()["account"]["id"],
                    candidate_id=None, session_id="gateway-usage"))
        finally:
            store.close()
        assert browser.post("/api/auth/login", json={"email": email, "password": password}).status_code == 403
        pending = auth.email_verification("due")["records"]
        assert len(pending) == 1
        sender = RecordingSender()
        assert deliver_platform_verification(auth, sender, pending[0]["id"])["accepted"]
        token = parse_qs(urlsplit(sender.url).query)["verify_email_token"][0]
        confirmed = browser.post("/api/auth/verify-email", json={"token": token})
        assert confirmed.status_code == 200, confirmed.text[:300]
        login = browser.post("/api/auth/login", json={"email": email, "password": password})
        assert login.status_code == 200, login.text[:300]
        assert "job_agent_session" in browser.cookies
        java_me = browser.get("/api/auth/me")
        assert java_me.status_code == 200 and java_me.json()["authenticated"]
        assert java_me.json()["billing"]["balance_micro_yuan"] == 0
        balance = browser.get("/api/me/balance")
        assert balance.status_code == 200, balance.text[:300]
        assert balance.json()["summary"] == java_me.json()["billing"]
        assert balance.json()["entries"] == [] and balance.json()["max_pages"] is None
        assert balance.headers["cache-control"] == "no-store"
        assert browser.post("/api/me/balance/recharge", json={"amount_yuan": 1}).status_code == 403
        for amount in (1, 2):
            recharged = browser.post("/api/me/balance/recharge", json={
                "amount_yuan": amount, "idempotency_key": f"gateway-recharge-{amount}-{os.urandom(12).hex()}",
            }, headers={"X-CSRF-Token": login.json()["csrf_token"]})
            assert recharged.status_code == 200, recharged.text[:300]
        order_page = browser.get("/api/me/recharge/orders?limit=1&offset=0&account_id=999999").json()
        older_page = browser.get("/api/me/recharge/orders?limit=1&offset=1").json()
        assert order_page["total"] == 2 and order_page["orders"][0]["amount_micro_yuan"] == 2_000_000
        assert older_page["orders"][0]["amount_micro_yuan"] == 1_000_000
        assert order_page["orders"][0]["account_id"] == registered.json()["account"]["id"]
        assert browser.get("/api/me/balance?limit=101").status_code == 422
        task_path = f"/api/tasks/{task_key}"
        assert browser.get(task_path + "?account_id=999999").json()["task"]["status"] == "queued"
        store = SQLAlchemyStore(database_url)
        try:
            assert store.claim_background_task(task_key) is not None
            store.update_background_task_progress(task_key, 60)
            running = browser.get(task_path).json()["task"]
            assert running["status"] == "running" and running["progress"] == 60 and running["attempt"] == 1
            store.complete_background_task(task_key, result={"artifact_id": 7})
        finally:
            store.close()
        completed = browser.get(task_path)
        assert completed.status_code == 200 and completed.headers["cache-control"] == "no-store"
        assert completed.json()["task"]["status"] == "succeeded"
        assert completed.json()["task"]["result"] == {"artifact_id": 7}
        assert len(completed.json()["task"]) == 12
        for field in ("account_id", "candidate_id", "payload_json", "idempotency_key", "session_id"):
            assert field not in completed.json()["task"]
        forwarded = httpx.get(web_url + task_path, cookies=browser.cookies, timeout=10)
        assert forwarded.status_code == 200 and forwarded.json() == completed.json()
        with httpx.Client(base_url=web_url, timeout=10) as adapter:
            authenticated = adapter.post("/api/auth/login", json={"email": admin_credentials[0], "password": admin_credentials[1]})
            assert authenticated.status_code == 200, authenticated.text[:300]
            account_id = registered.json()["account"]["id"]
            for endpoint in (f"/api/admin/tools/traces?account_id={account_id}&limit=1&offset=0",
                             f"/api/admin/tools/traces/gateway-tool-{account_id}-1",
                             f"/api/admin/tasks?account_id={account_id}&task_type=system_probe&status=succeeded&limit=1&offset=0"):
                forwarded = adapter.get(endpoint)
                direct = httpx.get(gateway_url + endpoint, cookies=adapter.cookies, timeout=10)
                assert forwarded.status_code == direct.status_code == 200, forwarded.text[:300]
                assert forwarded.json() == direct.json()
            assert adapter.get(task_path).status_code == 404
            assert adapter.post("/api/auth/logout").status_code == 200
        print("==> Python compatibility -> Java tool list/detail matches direct gateway response: PASS")
        docker("stop", "--time", "1", web_container)
        try:
            independent_task = browser.get(task_path)
            assert independent_task.status_code == 200 and independent_task.json() == completed.json()
            assert browser.get("/api/tasks/missing-task").status_code == 404
            assert browser.get("/api/tasks/" + "a" * 65).status_code == 422
            key = f"gateway-java-only-{os.urandom(12).hex()}"
            payload = {"amount_yuan": "0.000001", "idempotency_key": key,
                       "account_id": 999999, "max_total_micro_yuan": 999999999}
            credited = browser.post("/api/me/balance/recharge", json=payload,
                                    headers={"X-CSRF-Token": login.json()["csrf_token"]})
            assert credited.status_code == 200, credited.text[:300]
            replay = browser.post("/api/me/balance/recharge", json=payload,
                                  headers={"X-CSRF-Token": login.json()["csrf_token"]})
            assert replay.status_code == 200 and replay.json()["order"]["id"] == credited.json()["order"]["id"]
            conflict = browser.post("/api/me/balance/recharge", json={**payload, "amount_yuan": "1"},
                                    headers={"X-CSRF-Token": login.json()["csrf_token"]})
            assert conflict.status_code == 409
            excessive = browser.post("/api/me/balance/recharge", json={"amount_yuan": 21},
                                     headers={"X-CSRF-Token": login.json()["csrf_token"]})
            assert excessive.status_code == 400
            independent = browser.get("/api/me/balance?limit=1&offset=0&account_id=999999")
            assert independent.status_code == 200, "Personal balance still depends on Python Web"
            assert independent.json()["summary"]["balance_micro_yuan"] == 3_000_001
            assert independent.json()["total"] == 3 and len(independent.json()["entries"]) == 1
            assert independent.json()["entries"][0]["account_id"] == registered.json()["account"]["id"]
            assert browser.get("/api/me/recharge/orders").json()["total"] == 3
            run_admin_billing_gateway_flow(gateway_url, browser, admin_credentials,
                                           registered.json()["account"]["id"], order_page["orders"][0]["id"], (email, password), task_key)
        finally:
            docker("start", web_container)
            wait(f"{web_url}/api/health")
        print("==> Java wallet read/write -> owner-scoped pagination -> recharge/replay/conflict with Python stopped: PASS")
        print("==> Python task queued/running/succeeded -> Java fresh progress/owner scope -> compatibility and Python-stop reads: PASS")
        login = browser.get("/api/auth/me")
        assert login.status_code == 200 and login.json()["authenticated"]
        old_cookies = dict(browser.cookies)
        changed = browser.post("/api/account/password", json={
            "current_password": password, "new_password": "gateway-new-password-456",
        }, headers={"X-CSRF-Token": login.json()["csrf_token"]})
        assert changed.status_code == 200, changed.text[:300]
        with httpx.Client(base_url=web_url, cookies=old_cookies) as old_session:
            assert old_session.get("/api/auth/me").json()["authenticated"] is False
        assert browser.get("/api/auth/me").json()["authenticated"] is False
        assert browser.get("/api/me/balance").status_code == 401
        assert browser.get("/api/me/recharge/orders").status_code == 401
        assert browser.post("/api/me/balance/recharge", json={"amount_yuan": 1}).status_code == 401
        print("==> Java browser auth gateway -> Python account route -> password revocation: PASS")


def run_admin_billing_gateway_flow(gateway_url: str, user: httpx.Client, credentials: tuple[str, str],
                                   account_id: int, order_id: int, user_credentials: tuple[str, str], task_key: str) -> None:
    """Runs while the isolated Python Web is stopped; every tested route must reach Java."""
    paths = ["/api/admin/balance/events", "/api/admin/recharge/orders", f"/api/admin/recharge/orders/{order_id}/events",
             "/api/admin/accounts", "/api/admin/audit/events", "/api/admin/usage/summary", "/api/admin/usage/events",
             "/api/admin/tools/traces", f"/api/admin/tools/traces/gateway-tool-{account_id}-1", "/api/admin/tasks"]
    for path in paths:
        assert user.get(path).status_code == 403
    path = f"/api/admin/accounts/{account_id}/balance/credit"
    key = f"gateway-admin-credit-{os.urandom(12).hex()}"
    body = {"amount_yuan": 2, "reason": "gateway correction", "idempotency_key": key, "actor_account_id": 999999}
    assert user.post(path, json=body, headers={"X-CSRF-Token": user.cookies["job_agent_csrf"]}).status_code == 403
    with httpx.Client(base_url=gateway_url, timeout=10) as admin:
        login = admin.post("/api/auth/login", json={"email": credentials[0], "password": credentials[1]})
        assert login.status_code == 200, login.text[:300]
        headers = {"X-CSRF-Token": login.json()["csrf_token"], "X-Request-ID": "gateway-admin-credit-trace"}
        assert admin.post(path, json=body).status_code == 403
        credited = admin.post(path, json=body, headers=headers)
        assert credited.status_code == 200, credited.text[:300]
        assert credited.json()["entry"]["operator_account_id"] == login.json()["account"]["id"]
        assert credited.json()["summary"]["balance_micro_yuan"] == 5_000_001
        assert credited.json()["summary"]["total_recharge_micro_yuan"] == 3_000_001
        replay = admin.post(path, json=body, headers=headers)
        assert replay.status_code == 200 and replay.json()["entry"]["id"] == credited.json()["entry"]["id"]
        assert replay.json()["summary"]["balance_micro_yuan"] == 5_000_001
        assert admin.post(path, json={**body, "amount_yuan": 3}, headers=headers).status_code == 409
        page = admin.get(f"/api/admin/balance/events?account_id={account_id}&limit=1&offset=0")
        assert page.status_code == 200 and page.json()["total"] == 4
        assert page.json()["entries"][0]["id"] == credited.json()["entry"]["id"]
        assert page.json()["max_pages"] is None and page.headers["cache-control"] == "no-store"
        assert len(admin.get(f"/api/admin/balance/events?account_id={account_id}&limit=1&offset=1").json()["entries"]) == 1
        orders = admin.get(f"/api/admin/recharge/orders?account_id={account_id}&limit=1&offset=1")
        assert orders.status_code == 200 and orders.json()["total"] == 3
        assert all(row["account_id"] == account_id for row in orders.json()["orders"])
        events = admin.get(f"/api/admin/recharge/orders/{order_id}/events")
        assert events.status_code == 200 and len(events.json()["events"]) == 1
        assert events.json()["events"][0]["recharge_order_id"] == order_id
        assert admin.get("/api/admin/recharge/orders/999999999/events").status_code == 404
        assert admin.get("/api/admin/balance/events?limit=101").status_code == 422
        self_path = f"/api/admin/accounts/{login.json()['account']['id']}/balance/credit"
        assert admin.post(self_path, json={**body, "amount_yuan": "0.000001", "idempotency_key": key + "-self"}, headers=headers).status_code == 200
        listed = admin.get("/api/admin/accounts")
        assert listed.status_code == 200, listed.text[:300]
        safe_fields = {"id", "email", "display_name", "role", "status", "must_change_password",
                       "email_verified_at", "deleted_at", "created_at", "updated_at"}
        assert all(set(row) == safe_fields for row in listed.json()["accounts"])
        assert account_id in {row["id"] for row in listed.json()["accounts"]}
        usage = admin.get("/api/admin/usage/summary")
        assert usage.status_code == 200 and usage.headers["cache-control"] == "no-store", usage.text[:300]
        assert usage.json()["page_size"] == 100 and usage.json()["max_pages"] == 5
        balance = next(row for row in usage.json()["billing"]["by_account"] if row["account_id"] == account_id)
        assert balance["balance_micro_yuan"] == credited.json()["summary"]["balance_micro_yuan"]
        token_page = admin.get(f"/api/admin/usage/events?account_id={account_id}&limit=1&offset=0")
        assert token_page.status_code == 200 and token_page.json()["max_pages"] == 5
        assert token_page.json()["total"] == 2 and len(token_page.json()["events"]) == 1
        assert token_page.json()["events"][0]["raw_usage"] == {"total_tokens": 10}
        next_token = admin.get(f"/api/admin/usage/events?account_id={account_id}&session_id=gateway-usage&limit=1&offset=1").json()
        assert next_token["total"] == 2 and next_token["events"][0]["id"] != token_page.json()["events"][0]["id"]
        usage_account = next(row for row in usage.json()["by_account"] if row["account_id"] == account_id)
        assert usage_account["total_tokens"] == 20 and usage_account["billable_tokens"] == 0
        tool_counts = next(row for row in usage.json()["tool_calls_by_account"] if row["account_id"] == account_id)
        assert tool_counts["trace_count"] == 2 and tool_counts["failed_trace_count"] == 1
        assert all(row["account_id"] == account_id for row in token_page.json()["events"])
        assert admin.get("/api/admin/usage/events?limit=101").status_code == 422
        assert admin.get("/api/admin/usage/events?offset=-1").status_code == 422
        traces = admin.get(f"/api/admin/tools/traces?account_id={account_id}&limit=1&offset=0")
        assert traces.status_code == 200 and traces.headers["cache-control"] == "no-store", traces.text[:300]
        assert traces.json()["total"] == 2 and traces.json()["page_size"] == 100 and traces.json()["max_pages"] == 5
        assert len(traces.json()["traces"]) == 1 and "trace" not in traces.json()["traces"][0]
        assert "trace_json" not in traces.json()["traces"][0]
        older = admin.get(f"/api/admin/tools/traces?account_id={account_id}&limit=1&offset=1").json()
        assert older["total"] == 2 and older["traces"][0]["id"] != traces.json()["traces"][0]["id"]
        detail = admin.get(f"/api/admin/tools/traces/gateway-tool-{account_id}-1")
        assert detail.status_code == 200 and detail.headers["cache-control"] == "no-store", detail.text[:300]
        task = detail.json()["trace"]
        assert task["account_id"] == account_id and task["status"] == "failed" and task["last_error_summary"] == "provider unavailable"
        assert [step["name"] for step in task["trace"]["steps"]] == ["search_candidate_evidence", "analyze_project"]
        assert task["trace"]["steps"][0]["result"] == {"ok": True, "result_count": 2}
        assert task["trace"]["steps"][1]["attempts"][0]["result"] == {"ok": False, "error": "provider unavailable"}
        pending = admin.get(f"/api/admin/tools/traces/gateway-tool-{account_id}-0").json()["trace"]
        assert pending["status"] == "waiting_confirmation" and pending["trace"]["approval"]["status"] == "pending"
        assert admin.get("/api/admin/tools/traces/missing-task").status_code == 404
        assert admin.get("/api/admin/tools/traces?limit=101").status_code == 422
        assert admin.get("/api/admin/tools/traces?offset=-1").status_code == 422
        assert admin.get("/api/admin/tools/traces/" + "a" * 129).status_code == 422
        task_page = admin.get(f"/api/admin/tasks?account_id={account_id}&task_type=system_probe&limit=1&offset=0")
        assert task_page.status_code == 200 and task_page.json()["total"] == 2, task_page.text[:300]
        older_task = admin.get(f"/api/admin/tasks?account_id={account_id}&limit=1&offset=1").json()
        assert older_task["tasks"][0]["task_key"] != task_page.json()["tasks"][0]["task_key"]
        done = admin.get(f"/api/admin/tasks?account_id={account_id}&status=succeeded").json()
        assert done["total"] == 1 and done["tasks"][0]["task_key"] == task_key
        assert len(done["tasks"][0]) == 14 and done["tasks"][0]["account_id"] == account_id
        assert admin.get(f"/api/tasks/{task_key}").status_code == 404
        assert admin.get("/api/admin/tasks?status=unknown").status_code == 400
        assert admin.get("/api/admin/tasks?limit=101").status_code == 422
        # Write requests must still reach the stopped Python service, not Java.
        assert admin.post("/api/admin/tasks/probe", json={}, headers=headers).status_code == 502
        assert admin.post("/api/admin/tasks", json={}, headers=headers).status_code == 502
        status_path = f"/api/admin/accounts/{account_id}/status"
        assert user.patch(status_path, json={"status": "disabled"},
                          headers={"X-CSRF-Token": user.cookies["job_agent_csrf"]}).status_code == 403
        assert admin.patch(status_path, json={"status": "disabled"}).status_code == 403
        assert admin.patch(f"/api/admin/accounts/{login.json()['account']['id']}/status",
                           json={"status": "disabled"}, headers=headers).status_code == 400
        disabled = admin.patch(status_path, json={"status": "disabled", "actor_account_id": 999999},
                               headers={**headers, "X-Request-ID": "gateway-account-disable-trace"})
        assert disabled.status_code == 200 and disabled.json()["account"]["status"] == "disabled"
        suspended = admin.get("/api/admin/usage/summary").json()["billing"]["by_account"]
        assert next(row for row in suspended if row["account_id"] == account_id)["state"] == "suspended"
        old_user_cookies = dict(user.cookies)
        assert user.get("/api/me/balance").status_code == 401
        restored = admin.patch(status_path, json={"status": "active"}, headers=headers)
        assert restored.status_code == 200 and restored.json()["account"]["status"] == "active"
        with httpx.Client(base_url=gateway_url, cookies=old_user_cookies, timeout=10) as stale_user:
            assert stale_user.get("/api/me/balance").status_code == 401
        relogin = user.post("/api/auth/login", json={"email": user_credentials[0], "password": user_credentials[1]})
        assert relogin.status_code == 200
        audit = admin.get("/api/admin/audit/events")
        assert audit.status_code == 200 and audit.json()["limit"] == 50
        status_event = [row for row in audit.json()["events"] if row["request_id"] == "gateway-account-disable-trace"]
        assert len(status_event) == 1
        assert status_event[0]["actor_account_id"] == login.json()["account"]["id"]
        assert status_event[0]["target_account_id"] == account_id and status_event[0]["details"]["next_status"] == "disabled"
        first = admin.get("/api/admin/audit/events?limit=1&offset=0").json()
        second = admin.get("/api/admin/audit/events?limit=1&offset=1").json()
        assert first["events"][0]["id"] != second["events"][0]["id"]
        assert admin.get("/api/admin/audit/events?limit=201").status_code == 422
        old_cookies = dict(admin.cookies)
        assert admin.post("/api/auth/logout", headers=headers).status_code == 200
        with httpx.Client(base_url=gateway_url, cookies=old_cookies, timeout=10) as revoked:
            assert revoked.get(paths[0]).status_code == 401
            assert revoked.get("/api/admin/accounts").status_code == 401
            assert revoked.get("/api/admin/audit/events").status_code == 401
            assert revoked.get("/api/admin/usage/summary").status_code == 401
            assert revoked.get("/api/admin/usage/events").status_code == 401
            assert revoked.get("/api/admin/tools/traces").status_code == 401
            assert revoked.get(f"/api/admin/tools/traces/gateway-tool-{account_id}-1").status_code == 401
            assert revoked.get("/api/admin/tasks").status_code == 401
            assert revoked.post(path, json=body, headers=headers).status_code == 401
    print("==> Java admin billing -> RBAC/CSRF/pagination/events/self-credit/replay/revocation with Python stopped: PASS")
    print("==> Java admin account list/status/audit -> safe fields/CSRF/session revocation/pagination with Python stopped: PASS")
    print("==> Java usage summary/events -> RBAC/pagination/current balance/status without Python or summary cache: PASS")
    print("==> Java tool traces -> Python persisted results/order/errors/approval -> list/detail/RBAC/revocation with Python stopped: PASS")
    print("==> Java task list -> filters/pagination/RBAC/revocation; task writes remain on Python: PASS")


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
    assert_port_available(args.port + 2)
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
    gateway = f"{prefix}-gateway"
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
            "-e", "JOB_AGENT_RATE_LIMIT_ADMIN_REQUESTS=200",
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
            "--network-alias", "web",
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
        root = Path(__file__).resolve().parents[1]
        docker("run", "-d", "--name", gateway, "--network", prefix,
               "-p", f"127.0.0.1:{args.port + 2}:8080",
               "--mount", f"type=bind,source={root / 'deploy/Caddyfile.hybrid.local'},target=/etc/caddy/Caddyfile,readonly",
               "caddy:2.9.1-alpine")
        wait(f"http://127.0.0.1:{args.port + 2}/api/health")
        run_browser_gateway_flow(f"http://127.0.0.1:{args.port + 2}",
                                 f"http://127.0.0.1:{args.port + 1}",
                                 f"http://127.0.0.1:{args.port}", token, web, admin_credentials, isolated_url)
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
        for name in (prometheus, gateway, web, java, redis_name):
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
