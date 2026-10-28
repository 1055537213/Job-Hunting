"""Validate real Python/Java containers without touching application data."""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
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
             "/api/admin/tools/traces", f"/api/admin/tools/traces/gateway-tool-{account_id}-1", "/api/admin/tasks",
             "/api/admin/observability/requests"]
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
        observation = admin.get("/api/admin/observability/requests")
        assert observation.status_code == 200, observation.text[:500]
        partial = observation.json()
        assert partial["status"] == "partial" and partial["requests"] is None
        assert partial["services"]["java"]["status"] == "available"
        assert partial["services"]["java"]["api"]["total_requests"] > 0
        assert partial["services"]["python"] == {
            "status": "unavailable", "api": None, "internal": None, "observed_at": None}
        assert partial["account_email"]["status"] == "partial" and partial["account_email"]["summary"] is None
        assert observation.headers["cache-control"] == "no-store"
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
        refund_path = f"/api/admin/recharge/orders/{order_id}/refund"
        refund_body = {
            "reason": "Docker billing acceptance refund",
            "idempotency_key": f"gateway-admin-refund-{os.urandom(12).hex()}",
        }
        refunded = admin.post(refund_path, json=refund_body,
                              headers={**headers, "X-Request-ID": "gateway-admin-refund-trace"})
        assert refunded.status_code == 200, refunded.text[:300]
        assert refunded.json()["recharge_order_id"] == order_id
        assert refunded.json()["replayed"] is False
        assert refunded.json()["refunded_micro_yuan"] == 2_000_000
        replayed_refund = admin.post(refund_path, json=refund_body, headers=headers)
        assert replayed_refund.status_code == 200
        assert replayed_refund.json()["replayed"] is True
        assert replayed_refund.json()["ledger_entry_id"] == refunded.json()["ledger_entry_id"]
        reconciliation = admin.get(f"/api/admin/recharge/reconciliation?account_id={account_id}")
        assert reconciliation.status_code == 200, reconciliation.text[:300]
        assert reconciliation.json()["consistent"] is True
        assert reconciliation.json()["refunded_orders"] == 1
        assert reconciliation.json()["refunded_without_reversal"] == 0
        refunded_order = admin.get(f"/api/admin/recharge/orders?account_id={account_id}&limit=100&offset=0")
        assert refunded_order.status_code == 200
        assert any(row["id"] == order_id and row["status"] == "refunded"
                   for row in refunded_order.json()["orders"])
        assert admin.get("/api/admin/balance/events?limit=101").status_code == 422
        admin_account_id = login.json()["account"]["id"]
        self_path = f"/api/admin/accounts/{admin_account_id}/balance/credit"
        self_credit = admin.post(
            self_path,
            json={**body, "amount_yuan": "0.000001", "idempotency_key": key + "-self"},
            headers=headers,
        )
        assert self_credit.status_code == 200, self_credit.text[:300]
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
        assert balance["balance_micro_yuan"] == 3_000_001
        admin_balance = next(row for row in usage.json()["billing"]["by_account"]
                             if row["account_id"] == admin_account_id)
        assert admin_balance["balance_micro_yuan"] == 1
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
        # Java must report dependency failure, never success, when Python is stopped.
        assert admin.post("/api/admin/tasks/probe", json={}, headers=headers).status_code == 503
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
            assert revoked.get("/api/admin/observability/requests").status_code == 401
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


def dump_container_diagnostics(names: tuple[str, ...], secrets: tuple[str, ...] = ()) -> None:
    """Print bounded, redacted logs before acceptance cleanup removes containers."""

    print("==> Docker acceptance failure diagnostics (last 200 lines per container)")
    for name in names:
        result = subprocess.run(
            ["docker", "logs", "--tail", "200", name],
            capture_output=True,
            text=True,
            check=False,
        )
        output = (result.stdout + result.stderr).strip()
        for secret in secrets:
            if secret:
                output = output.replace(secret, "<redacted>")
        if len(output) > 12000:
            output = output[-12000:]
        print(f"--- {name} ---")
        print(output or "<no container output>")


def run_task_command_flow(gateway_url, web_url, java_url, token, credentials, database_url, worker, broker):
    from job_hunting_agent.config import PlatformAuthSettings
    from job_hunting_agent.platform_auth import PlatformAuthClient
    from job_hunting_agent.sqlalchemy_store import SQLAlchemyStore
    auth = PlatformAuthClient(PlatformAuthSettings(True, java_url, token, 10))
    email = f"task-command-user-{os.urandom(8).hex()}@example.com"
    auth.register_account(email=email, password="task-password-123", display_name=None, email_verified=True, consents=[])
    with httpx.Client(base_url=gateway_url, timeout=15) as admin, httpx.Client(base_url=gateway_url, timeout=15) as user:
        user.post("/api/auth/login", json={"email": email, "password": "task-password-123"}).raise_for_status()
        assert user.post("/api/admin/tasks/probe", headers={"X-CSRF-Token": user.cookies["job_agent_csrf"]}).status_code == 403
        login = admin.post("/api/auth/login", json={"email": credentials[0], "password": credentials[1]})
        login.raise_for_status()
        actor = login.json()["account"]["id"]
        key = "task-command-" + os.urandom(16).hex()
        headers = {"Idempotency-Key": key, "X-CSRF-Token": login.json()["csrf_token"], "X-Request-ID": "gateway-task-command-trace"}
        assert admin.post("/api/admin/tasks/probe").status_code == 403
        assert admin.post("/api/admin/tasks/probe", headers={**headers, "Idempotency-Key": "bad"}).status_code == 422
        assert admin.post("/internal/v1/task-commands", json={}).status_code == 404
        store = SQLAlchemyStore(database_url)
        try:
            def finished(task_key):
                deadline = time.monotonic() + 45
                while time.monotonic() < deadline:
                    result = admin.get(f"/api/tasks/{task_key}")
                    result.raise_for_status()
                    task = result.json()["task"]
                    if task["status"] == "succeeded":
                        assert task["attempt"] == 1 and task["progress"] == 100
                        return task
                    assert task["status"] != "failed", task
                    time.sleep(0.5)
                logs = subprocess.run(["docker", "logs", "--tail", "50", worker], capture_output=True, text=True)
                diagnostic = re.sub(r"redis://[^@\s]+@", "redis://[redacted]@", logs.stdout + logs.stderr)
                for secret in (token, credentials[1]):
                    diagnostic = diagnostic.replace(secret, "[redacted]")
                raise AssertionError(f"Isolated Worker did not finish task: {task_key} status={task['status']} attempt={task['attempt']}\n"
                                     + diagnostic[-4000:])
            probe = admin.post("/api/admin/tasks/probe", headers=headers)
            assert probe.status_code == 200, probe.text[:500]
            task_key = probe.json()["task"]["task_key"]
            finished(task_key)
            replay = admin.post("/api/admin/tasks/probe", headers=headers)
            assert replay.status_code == 200 and replay.json()["task"]["task_key"] == task_key
            assert replay.json()["task"]["status"] == "succeeded"
            failed = store.create_background_task(account_id=actor, task_type="system_probe")
            store.fail_queued_background_task(failed.task_key, "acceptance failure")
            retry_path = f"/api/admin/tasks/{failed.task_key}/retry"
            assert admin.post(retry_path, headers=headers).status_code == 409
            retry_headers = {**headers, "Idempotency-Key": key + "-retry"}
            retried = admin.post(retry_path, headers=retry_headers)
            assert retried.status_code == 200 and len(retried.json()["task"]) == 14, retried.text[:500]
            finished(failed.task_key)
            assert admin.post(retry_path, headers=retry_headers).json()["task"]["status"] == "succeeded"
            compat_headers = {**headers, "Idempotency-Key": key + "-compat"}
            forwarded = httpx.post(web_url + "/api/admin/tasks/probe", headers=compat_headers, cookies=admin.cookies, timeout=15)
            assert forwarded.status_code == 200, forwarded.text[:500]
            compat_key = forwarded.json()["task"]["task_key"]
            assert admin.post("/api/admin/tasks/probe", headers=compat_headers).json()["task"]["task_key"] == compat_key
            finished(compat_key)
            offline_headers = {**headers, "Idempotency-Key": key + "-offline"}
            docker("stop", "--time", "1", broker)
            try:
                unavailable = admin.post("/api/admin/tasks/probe", headers=offline_headers)
                assert unavailable.status_code == 503, unavailable.text[:500]
                with store.connect() as conn:
                    pending = conn.execute("SELECT task_key FROM task_commands WHERE actor_account_id=? AND status='pending'", (actor,)).fetchall()
                assert len(pending) == 1
                pending_key = pending[0]["task_key"]
            finally:
                docker("start", broker)
            # Exercise the same durable recovery dispatcher used by the Beat maintenance task.
            docker("exec", worker, "python", "-c", "from job_hunting_agent.app import JobHuntingApp; from job_hunting_agent.task_commands import TaskCommands; a=JobHuntingApp(); a.initialize(); print(TaskCommands(a.store,a.task_queue).dispatch_pending()); a.store.close()")
            finished(pending_key)
            recovered = admin.post("/api/admin/tasks/probe", headers=offline_headers)
            assert recovered.status_code == 200 and recovered.json()["task"]["task_key"] == pending_key
            audits = [row for row in store.list_admin_audit_events(limit=100) if row.request_id == "gateway-task-command-trace"]
            assert len(audits) == 4
            for audit in audits:
                assert audit.actor_account_id == actor and "command_id" in audit.details
            assert admin.post("/api/auth/logout", headers=headers).status_code == 200
            assert admin.post("/api/admin/tasks/probe", headers=headers).status_code == 401
        finally:
            store.close()
    print("==> Java commands -> Python durable intent/audit -> Redis/real Worker -> replay/conflict/CSRF/queue outage recovery: PASS")


def run_account_profile_gateway_flow(gateway_url: str, web_url: str, java_url: str, internal_token: str,
                                     web_container: str, database_url: str, occupied_email: str) -> None:
    from urllib.parse import parse_qs, urlsplit

    from job_hunting_agent.config import PlatformAuthSettings
    from job_hunting_agent.platform_auth import PlatformAuthClient
    from job_hunting_agent.platform_email import deliver_platform_account_email

    auth = PlatformAuthClient(PlatformAuthSettings(True, java_url, internal_token, 10))
    email = f"profile-{os.urandom(10).hex()}@example.com"
    target = f"changed-{os.urandom(10).hex()}@example.com"
    password = "profile-acceptance-password"
    engine = sa.create_engine(database_url)

    class Sender:
        recipient = ""
        url = ""

        def send_verification(self, recipient, url):
            self.recipient, self.url = recipient, url

        def send_email_change(self, recipient, url):
            self.recipient, self.url = recipient, url

    sender = Sender()

    def email_job(account_id, purpose):
        with engine.connect() as conn:
            return conn.execute(sa.text("SELECT id FROM platform_account_action_emails WHERE account_id=:id AND purpose=:purpose ORDER BY id DESC LIMIT 1"),
                                {"id": account_id, "purpose": purpose}).scalar_one()

    try:
        with httpx.Client(base_url=gateway_url, timeout=10) as browser:
            registered = browser.post("/api/auth/register", json={"email": email, "password": password})
            assert registered.status_code == 200, registered.text[:300]
            account_id = registered.json()["account"]["id"]
            assert deliver_platform_account_email(auth, sender, email_job(account_id, "verify_email"), "verify_email")["accepted"]
            token = parse_qs(urlsplit(sender.url).query)["verify_email_token"][0]
            assert browser.post("/api/auth/verify-email", json={"token": token}).status_code == 200
            login = browser.post("/api/auth/login", json={"email": email, "password": password})
            assert login.status_code == 200, login.text[:300]
            headers = {"X-CSRF-Token": login.json()["csrf_token"], "X-Request-ID": "profile-gateway-trace"}
            forwarded = httpx.patch(web_url + "/api/account/profile", cookies=browser.cookies,
                                   headers=headers, json={"display_name": " Compatibility "}, timeout=10)
            assert forwarded.status_code == 200 and forwarded.json()["account"]["display_name"] == "Compatibility"
            duplicate = httpx.patch(web_url + "/api/account/email", cookies=browser.cookies, headers=headers,
                                   json={"new_email": occupied_email}, timeout=10)
            assert duplicate.status_code == 409, duplicate.text[:300]
            docker("stop", "--time", "1", web_container)
            try:
                assert browser.patch("/api/account/profile", json={"display_name": "Denied"}).status_code == 403
                for display_name in ("  Java Profile  ", "", None):
                    result = browser.patch("/api/account/profile", json={"display_name": display_name, "account_id": 999999}, headers=headers)
                    assert result.status_code == 200, result.text[:300]
                    assert result.json()["account"]["id"] == account_id
                    assert result.json()["account"]["display_name"] == ("Java Profile" if display_name else None)
                    assert result.headers["cache-control"] == "no-store"
                assert browser.patch("/api/account/profile", json={"display_name": "x" * 129}, headers=headers).status_code == 422
                assert browser.patch("/api/account/email", json={"new_email": occupied_email}, headers=headers).status_code == 409
                assert browser.patch("/api/account/email", json={"new_email": email.upper()}, headers=headers).status_code == 400
                assert browser.patch("/api/account/email", json={"new_email": "not-email"}, headers=headers).status_code == 422
                for _ in range(2):
                    requested = browser.patch("/api/account/email", json={"new_email": f" {target.upper()} ", "account_id": 999999}, headers=headers)
                    assert requested.status_code == 200, requested.text[:300]
                assert browser.get("/api/auth/me").json()["account"]["email"] == email
                with engine.connect() as conn:
                    assert conn.execute(sa.text("SELECT count(*) FROM platform_account_action_emails WHERE account_id=:id AND purpose='change_email'"), {"id": account_id}).scalar_one() == 1
                    assert conn.execute(sa.text("SELECT count(*) FROM account_email_outbox WHERE account_id=:id"), {"id": account_id}).scalar_one() == 0
                assert deliver_platform_account_email(auth, sender, email_job(account_id, "change_email"), "change_email")["accepted"]
                assert sender.recipient == target
                token = parse_qs(urlsplit(sender.url).query)["change_email_token"][0]
                confirmed = browser.post("/api/auth/change-email/confirm", json={"token": token}, headers=headers)
                assert confirmed.status_code == 200 and confirmed.json()["account"]["email"] == target
                assert browser.patch("/api/account/profile", json={}, headers=headers).status_code == 401
                assert browser.patch("/api/account/email", json={"new_email": email}, headers=headers).status_code == 401
                assert browser.post("/api/auth/login", json={"email": target, "password": password}).status_code == 200
                assert browser.post("/api/auth/login", json={"email": email, "password": password}).status_code == 401
                assert browser.post("/api/auth/change-email/confirm", json={"token": token},
                                    headers={"X-CSRF-Token": browser.cookies["job_agent_csrf"]}).status_code == 400
            finally:
                docker("start", web_container)
                wait(web_url + "/api/health")
    finally:
        engine.dispose()
    print("==> Java profile/email entry -> Python-stop writes/normalization/CSRF -> outbox/SMTP callback -> revocation/replay: PASS")


def run_account_export_gateway_flow(gateway_url: str, web_url: str, java_url: str, internal_token: str,
                                    web_container: str, database_url: str) -> None:
    from job_hunting_agent.account_export import validate_export
    from job_hunting_agent.config import PlatformAuthSettings
    from job_hunting_agent.platform_auth import PlatformAuthClient
    from job_hunting_agent.sqlalchemy_store import SQLAlchemyStore

    auth = PlatformAuthClient(PlatformAuthSettings(True, java_url, internal_token, 10))
    password = "export-acceptance-password"
    emails = [f"export-{os.urandom(10).hex()}@example.com" for _ in range(2)]
    ids = [auth.register_account(email=email, password=password, display_name="Export acceptance",
                                 email_verified=True, consents=[]).account_id for email in emails]
    store = SQLAlchemyStore(database_url)
    try:
        tasks = [store.create_background_task(account_id=owner, task_type="system_probe", payload={"note": f"private-to-{owner}"})
                 for owner in ids]
        assets = [store.register_knowledge_asset(account_id=owner, candidate_id=None, asset_kind="document",
                                                title=f"Export fixture {owner}", original_filename="fixture.txt",
                                                storage_key=f"export-fixture/{owner}/fixture.txt", media_type="text/plain",
                                                file_size=1, sha256="0" * 64) for owner in ids]
        with httpx.Client(base_url=gateway_url, timeout=40) as browser:
            assert browser.get("/api/account/export").status_code == 401
            login = browser.post("/api/auth/login", json={"email": emails[0], "password": password})
            assert login.status_code == 200, login.text[:300]
            headers = {"X-CSRF-Token": login.json()["csrf_token"]}
            session = browser.cookies["job_agent_session"]
            exported = browser.get(f"/api/account/export?account_id={ids[1]}")
            assert exported.status_code == 200, exported.text[:300]
            validate_export(exported.json(), ids[0])
            assert exported.headers["cache-control"] == "no-store"
            assert exported.headers["content-disposition"] == f'attachment; filename="job-agent-account-{ids[0]}-export.json"'
            assert {row["task_key"] for row in exported.json()["background_tasks"]} == {tasks[0].task_key}
            assert {row["id"] for row in exported.json()["knowledge_assets"]} == {assets[0][0].id}
            assert {row["id"] for row in exported.json()["knowledge_asset_versions"]} == {assets[0][1].id}
            assert tasks[1].task_key not in exported.text
            assert "password_hash" not in exported.text and "token_hash" not in exported.text
            forwarded = httpx.get(web_url + "/api/account/export", cookies=browser.cookies, timeout=40)
            assert forwarded.status_code == 200, forwarded.text[:300]
            validate_export(forwarded.json(), ids[0])
            assert forwarded.json()["background_tasks"] == exported.json()["background_tasks"]
            private_url = web_url + "/internal/v1/accounts/export"
            private_headers = {"X-Internal-Service-Token": internal_token}
            assert browser.post("/internal/v1/accounts/export", json={"session_token": session}).status_code == 404
            assert httpx.post(private_url, json={"session_token": session}).status_code == 401
            assert httpx.post(private_url, headers=private_headers, json={"session_token": "revoked"}).status_code == 401
            assert httpx.post(private_url, headers=private_headers, json={"session_token": session, "account_id": ids[1]}).status_code == 422
            docker("stop", "--time", "1", web_container)
            try:
                failed = browser.get("/api/account/export")
                assert failed.status_code == 503, failed.text[:300]
                assert failed.json()["code"] == "EXPORT_UNAVAILABLE"
                assert "Content-Disposition" not in failed.headers and "private-to-" not in failed.text
                assert browser.get("/api/auth/me").status_code == 200
            finally:
                docker("start", web_container)
                wait(web_url + "/api/health")
            assert browser.get("/api/account/export").status_code == 200
            assert browser.post("/api/auth/logout", headers=headers).status_code == 200
            assert httpx.get(gateway_url + "/api/account/export", cookies={"job_agent_session": session}).status_code == 401
            assert httpx.post(private_url, headers=private_headers, json={"session_token": session}).status_code == 401
    finally:
        store.close()
    print("==> Java export -> real-session Python callback -> tenant isolation/no secrets -> outage/recovery/revocation: PASS")


def run_profile_read_gateway_flow(gateway_url: str, web_url: str, java_url: str, internal_token: str, web: str, java: str) -> None:
    """Real account ownership, Python writes/Java reads, and independent dependency failures."""
    from job_hunting_agent.config import PlatformAuthSettings
    from job_hunting_agent.platform_auth import PlatformAuthClient
    auth = PlatformAuthClient(PlatformAuthSettings(True, java_url, internal_token, 10))
    accounts = []
    for _ in range(2):
        email = f"profile-query-{os.urandom(10).hex()}@example.com"
        owner = auth.register_account(email=email, password="profile-query-password", display_name="Profile acceptance",
                                      email_verified=True, consents=[]).account_id
        accounts.append((owner, email))
    with httpx.Client(base_url=gateway_url, timeout=10) as first, httpx.Client(base_url=gateway_url, timeout=10) as second:
        for index, browser in enumerate((first, second)):
            logged = browser.post("/api/auth/login", json={"email": accounts[index][1], "password": "profile-query-password"})
            assert logged.status_code == 200, logged.text[:200]
            browser.headers["X-CSRF-Token"] = logged.json()["csrf_token"]
            accounts[index] = (accounts[index][0], browser.cookies["job_agent_session"])
        first_profiles = first.get("/api/profiles")
        assert first_profiles.status_code == 200, first_profiles.text[:500]
        assert first_profiles.json() == {"profiles": []}, first_profiles.text[:500]
        body = {"name": "Profile first", "preferred_cities": ["杭州"]}
        intent = {"Idempotency-Key": "profile-create-acceptance"}
        assert httpx.post(gateway_url + "/api/profiles", cookies=first.cookies, json=body).status_code == 403
        created = first.post("/api/profiles", json=body, headers=intent)
        assert created.status_code == 200, created.text[:300]
        first_id = created.json()["candidate_id"]
        assert first.post("/api/profiles", json=body, headers=intent).json() == created.json()
        assert first.post("/api/profiles", json={"name": "Other intent"}, headers=intent).status_code == 409
        assert first.post("/api/profiles", json={**body, "account_id": accounts[1][0]}).status_code == 422
        second_body = {"name": "Profile second"}
        second_id = second.post("/api/profiles", json=second_body, headers=intent).json()["candidate_id"]
        path = f"/api/profiles/{first_id}"
        listed = first.get(f"/api/profiles?account_id={accounts[1][0]}", headers={"X-Request-ID": "profile-query-acceptance"})
        assert listed.status_code == 200 and [row["id"] for row in listed.json()["profiles"]] == [first_id]
        assert listed.headers["cache-control"] == "no-store"
        assert listed.headers["x-request-id"] == "profile-query-acceptance"
        assert "account_id" not in listed.json()["profiles"][0]
        assert first.get(f"/api/profiles/{second_id}").status_code == 404
        assert second.get(path).status_code == 404
        assert second.delete(path).status_code == 404
        assert first.get("/api/profiles/0").status_code == 422
        assert first.get("/api/profiles/not-a-number").status_code == 422
        direct = httpx.get(web_url + path, cookies=first.cookies, timeout=10)
        assert direct.status_code == 200 and direct.json() == first.get(path).json()
        assert first.post("/internal/v1/profiles/query", json={"session_token": accounts[0][1]}).status_code == 404
        for url in (web_url, java_url):
            assert httpx.post(url + "/internal/v1/profiles/query", json={"session_token": accounts[0][1]}).status_code == 401
            assert httpx.post(url + "/internal/v1/profiles/create", json={"session_token": accounts[0][1],
                "idempotency_key": "blocked", "profile": body}).status_code == 401
        assert first.post("/internal/v1/profiles/create", json={}).status_code == 404
        compat = httpx.post(web_url + "/api/profiles", cookies=first.cookies,
            headers={**intent, "X-CSRF-Token": first.headers["X-CSRF-Token"]}, json=body, timeout=10)
        assert compat.status_code == 200 and compat.json() == created.json()
        update = f"from job_hunting_agent.app import JobHuntingApp; app=JobHuntingApp(); app.ingest_conversation_message({first_id}, '首选城市改成上海', auto_rebuild_rag=False, account_id={accounts[0][0]}); app.store.close()"
        docker("exec", web, "python", "-c", update)
        assert first.get(path).json()["profile"]["preferred_cities"] == ["上海"]
        assert second.delete(f"/api/profiles/{second_id}").status_code == 200
        assert second.get("/api/profiles").json() == {"profiles": []}
        assert second.get(f"/api/profiles/{second_id}").status_code == 404
        assert second.post("/api/profiles", json=second_body, headers=intent).json()["code"] == "PROFILE_CREATE_CONFLICT"
        compat_conflict = httpx.post(web_url + "/api/profiles", cookies=second.cookies, json=second_body,
            headers={**intent, "X-CSRF-Token": second.headers["X-CSRF-Token"]}, timeout=10)
        assert compat_conflict.status_code == 409 and compat_conflict.json()["code"] == "PROFILE_CREATE_CONFLICT"
        try:
            docker("stop", "--time", "1", web)
            failure = first.get("/api/profiles")
            assert failure.status_code == 503 and failure.json()["code"] == "PROFILE_QUERY_UNAVAILABLE"
            assert "profiles" not in failure.json() and failure.headers["retry-after"] == "3"
            assert first.get("/api/auth/me").json()["authenticated"] is True
            failed_delete = first.delete(path)
            assert failed_delete.status_code == 503 and failed_delete.json()["code"] == "PROFILE_DELETE_UNAVAILABLE"
            failed_create = first.post("/api/profiles", json={"name": "Recovered create"}, headers={"Idempotency-Key": "outage-intent"})
            assert failed_create.status_code == 503 and failed_create.json()["code"] == "PROFILE_CREATE_UNAVAILABLE"
        finally:
            docker("start", web)
            wait(web_url + "/api/health")
        assert first.get(path).json()["profile"]["preferred_cities"] == ["上海"]
        recovered = first.post("/api/profiles", json={"name": "Recovered create"}, headers={"Idempotency-Key": "outage-intent"})
        assert recovered.status_code == 200
        assert first.post("/api/profiles", json={"name": "Recovered create"}, headers={"Idempotency-Key": "outage-intent"}).json() == recovered.json()
        try:
            docker("stop", "--time", "1", java)
            failure = httpx.get(web_url + "/api/profiles", cookies=first.cookies, timeout=10)
            assert failure.status_code == 503 and "profiles" not in failure.json()
            failed_create = httpx.post(web_url + "/api/profiles", cookies=first.cookies, json={"name": "No local fallback"},
                headers={"X-CSRF-Token": first.headers["X-CSRF-Token"], "Idempotency-Key": "no-fallback"}, timeout=10)
            assert failed_create.status_code == 503
            failed_delete = httpx.delete(web_url + path, cookies=first.cookies, timeout=10)
            assert failed_delete.status_code == 503 and failed_delete.json()["code"] == "PROFILE_DELETE_UNAVAILABLE"
        finally:
            docker("start", java)
            wait(java_url + "/actuator/health")
        assert first.get(path).status_code == 200
        auth.session("logout", session_token=accounts[0][1])
        assert first.get(path).status_code == 401
        assert httpx.get(web_url + path, cookies=first.cookies).status_code == 401
        assert first.post("/api/profiles", json=body, headers=intent).status_code == 401
        assert first.delete(path).status_code == 401
    print("==> Profile GET/POST Java entry -> Python owned facts/atomic creation -> replay/conflict/tenant scope -> dependency outages/no fallback -> recovery/revocation: PASS")


def run_chat_session_gateway_flow(gateway_url: str, web_url: str, java_url: str, internal_token: str,
                                  web: str, java: str, database_url: str) -> None:
    from job_hunting_agent.config import PlatformAuthSettings
    from job_hunting_agent.platform_auth import PlatformAuthClient
    from job_hunting_agent.sqlalchemy_store import SQLAlchemyStore
    auth = PlatformAuthClient(PlatformAuthSettings(True, java_url, internal_token, 10))
    store = SQLAlchemyStore(database_url)
    owners = []
    password = "chat-acceptance-password"
    for _ in range(2):
        email = f"chat-gateway-{os.urandom(10).hex()}@example.com"
        owner = auth.register_account(email=email, password=password, display_name="Chat acceptance",
                                      email_verified=True, consents=[]).account_id
        owners.append((owner, email))
    try:
        with httpx.Client(base_url=gateway_url, timeout=20) as first, httpx.Client(base_url=gateway_url, timeout=20) as second:
            for browser, (_, email) in zip((first, second), owners, strict=True):
                login = browser.post("/api/auth/login", json={"email": email, "password": password})
                assert login.status_code == 200, login.text[:200]
                browser.headers["X-CSRF-Token"] = login.json()["csrf_token"]
            candidate = first.post("/api/profiles", json={"name": "Chat scope"}).json()["candidate_id"]
            assert first.get("/api/chat/sessions").json() == {"sessions": []}
            data = {"candidate_id": candidate, "title": "Interview preparation"}
            assert httpx.post(gateway_url + "/api/chat/sessions", cookies=first.cookies, json=data).status_code == 403
            created = first.post("/api/chat/sessions", json=data, headers={"X-Request-ID": "chat-session-acceptance"})
            assert created.status_code == 200, created.text[:500]
            assert created.headers["x-request-id"] == "chat-session-acceptance"
            key = created.json()["session"]["session_id"]
            path = f"/api/chat/sessions/{key}"
            history = f"/api/chat/history?candidate_id={candidate}&session_id={key}&limit=5"
            store.save_chat_message(candidate, key, "user", "Prepare an interview", account_id=owners[0][0])
            store.save_chat_message(candidate, key, "assistant", "Ready.", metadata={"tool_outputs": "private-secret"}, account_id=owners[0][0])
            response = first.get(history)
            assert response.status_code == 200 and len(response.json()["messages"]) == 2, response.text[:500]
            assert "private-secret" not in response.text and response.json()["messages"][1]["metadata"] == {}
            assert response.headers["cache-control"] == "no-store"
            assert httpx.get(web_url + history, cookies=first.cookies, timeout=20).json() == response.json()
            assert second.get(history).status_code == 404
            assert second.post(path + "/archive").status_code == 404
            assert second.delete(path).status_code == 404
            assert second.post("/api/chat/sessions", json=data).status_code == 404
            assert first.get(f"/api/chat/history?candidate_id={candidate}&limit=501").status_code == 422
            assert first.post("/api/chat/sessions", json={**data, "account_id": owners[1][0]}).status_code == 422
            for origin in (java_url, web_url):
                assert httpx.post(origin + "/internal/v1/chat/sessions/list", json={"session_token": first.cookies["job_agent_session"]}).status_code == 401
            assert first.post("/internal/v1/chat/sessions/list", json={}).status_code == 404
            with store.account_work.acquire(owners[0][0]):
                busy = first.delete(path)
                assert busy.status_code == 409 and busy.json()["code"] == "CHAT_BUSY", busy.text[:300]
            assert first.post(path + "/archive").status_code == 200
            assert first.post(path + "/archive").status_code == 200
            assert first.get("/api/chat/sessions").json() == {"sessions": []}
            assert len(first.get("/api/chat/sessions?include_archived=true").json()["sessions"]) == 1
            assert first.get(history).status_code == 409
            assert first.delete(path).json()["deleted"] is True
            assert first.delete(path).status_code == 404
            assert store.list_chat_messages(candidate, key, account_id=owners[0][0]) == []
            for container, health, origin in ((web, web_url + "/api/health", gateway_url),
                                              (java, java_url + "/actuator/health", web_url)):
                try:
                    docker("stop", "--time", "1", container)
                    failed = httpx.get(origin + "/api/chat/sessions", cookies=first.cookies, timeout=20)
                    assert failed.status_code == 503 and "sessions" not in failed.json(), failed.text[:300]
                    failed_create = httpx.post(origin + "/api/chat/sessions", cookies=first.cookies,
                        headers={"X-CSRF-Token": first.headers["X-CSRF-Token"]}, json=data, timeout=20)
                    assert failed_create.status_code == 503
                finally:
                    docker("start", container)
                    wait(health)
                assert first.get("/api/chat/sessions").json() == {"sessions": []}
            assert first.post("/api/chat/sessions", json=data).status_code == 200
            auth.session("logout", session_token=first.cookies["job_agent_session"])
            assert first.get("/api/chat/sessions").status_code == 401
            assert httpx.get(web_url + "/api/chat/sessions", cookies=first.cookies, timeout=20).status_code == 401
    finally:
        store.close()
    print("==> Java chat-session entry -> Python-owned history/create/archive/delete -> scope/CSRF/redaction/in-flight guard -> outages/recovery/revocation: PASS")


def run_chat_message_gateway_flow(gateway_url: str, web_url: str, java_url: str, internal_token: str,
                                   web: str, java: str, model: str, database_url: str) -> None:
    """Real Agent/tools/billing with a private deterministic provider, never a paid model."""
    from job_hunting_agent.config import PlatformAuthSettings
    from job_hunting_agent.platform_auth import PlatformAuthClient
    from job_hunting_agent.sqlalchemy_store import SQLAlchemyStore
    auth = PlatformAuthClient(PlatformAuthSettings(True, java_url, internal_token, 10))
    email = f"chat-message-{os.urandom(10).hex()}@example.com"
    password = "chat-message-acceptance-password"
    owner = auth.register_account(email=email, password=password, display_name="Chat messages",
                                  email_verified=True, consents=[]).account_id
    store = SQLAlchemyStore(database_url)
    def model_stats():
        return json.loads(docker("exec", model, "python", "-c",
            "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8099/stats').read().decode())"))
    try:
        with httpx.Client(base_url=gateway_url, timeout=40) as browser:
            login = browser.post("/api/auth/login", json={"email": email, "password": password})
            assert login.status_code == 200, login.text[:300]
            browser.headers["X-CSRF-Token"] = login.json()["csrf_token"]
            candidate = browser.post("/api/profiles", json={"name": "Chat message scope"}).json()["candidate_id"]
            key = browser.post("/api/chat/sessions", json={"candidate_id": candidate}).json()["session"]["session_id"]
            body = {"candidate_id": candidate, "session_id": key, "message": "acceptance-update", "auto_rag": False}
            before = model_stats()
            assert httpx.post(gateway_url + "/api/chat", cookies=browser.cookies, json=body).status_code == 403
            assert browser.post("/internal/v1/chat/invoke", json={}).status_code == 404
            for origin in (web_url, java_url):
                assert httpx.post(origin + "/internal/v1/chat/invoke", json={**body, "session_token": browser.cookies["job_agent_session"]}).status_code == 401
            failed = browser.post("/api/chat", json=body)
            assert failed.status_code == 402 and failed.json()["detail"] == "余额不足，请先充值后重试。", failed.text[:500]
            assert model_stats() == before
            assert store.list_chat_messages(candidate, key, account_id=owner) == []
            credit = browser.post("/api/me/balance/recharge", json={"amount_yuan": 1, "idempotency_key": os.urandom(16).hex()})
            assert credit.status_code == 200, credit.text[:300]
            response = browser.post("/api/chat", json=body, headers={"X-Request-ID": "chat-message-acceptance"})
            assert response.status_code == 200, response.text[:500]
            result = response.json()
            assert response.headers["x-request-id"] == "chat-message-acceptance" and response.headers["cache-control"] == "no-store"
            assert result["profile"]["preferred_cities"] == ["上海"]
            assert re.fullmatch(r"[a-f0-9]{32}", result["task_trace"]["root_request_id"]) and result["task_trace"]["steps"]
            assert set(result) == {"mode", "reply", "display_reply", "task_trace", "background_tasks", "profile"}
            assert model_stats().get("update", 0) - before.get("update", 0) == 2
            assert len(store.list_chat_messages(candidate, key, account_id=owner)) == 2
            charges = [entry for entry in store.list_account_balance_ledger(owner) if entry.entry_kind == "consumption"]
            assert len(charges) == 1 and charges[0].token_count == 160, str(charges)
            assert store.get_account_balance_summary(owner).total_consumed_micro_yuan == 4000
            repeated_trace = browser.post("/api/chat", json={**body, "message": "acceptance-plain"},
                headers={"X-Request-ID": "chat-message-acceptance"})
            assert repeated_trace.status_code == 200, repeated_trace.text[:300]
            charges = [entry for entry in store.list_account_balance_ledger(owner) if entry.entry_kind == "consumption"]
            assert len(charges) == 2 and len({e.source_reference for e in charges}) == 2, str(charges)
            assert store.get_account_balance_summary(owner).total_consumed_micro_yuan == 6000
            before_failure = model_stats().get("failure", 0)
            failure = browser.post("/api/chat", json={**body, "message": "acceptance-failure"})
            assert failure.status_code == 502 and "private-model-error" not in failure.text, failure.text[:300]
            assert model_stats().get("failure", 0) == before_failure + 1
            assert len(store.list_chat_messages(candidate, key, account_id=owner)) == 4
            assert len([e for e in store.list_account_balance_ledger(owner) if e.entry_kind == "consumption"]) == 2
            for container, health, origin in ((web, web_url + "/api/health", gateway_url),
                                              (java, java_url + "/actuator/health", web_url)):
                try:
                    docker("stop", "--time", "1", container)
                    failure = httpx.post(origin + "/api/chat", cookies=browser.cookies,
                        headers={"X-CSRF-Token": browser.headers["X-CSRF-Token"]}, json=body, timeout=20)
                    assert failure.status_code == 503 and failure.json()["code"] == "CHAT_EXECUTION_UNCONFIRMED", failure.text[:300]
                finally:
                    docker("start", container)
                    wait(health)
            assert len(store.list_chat_messages(candidate, key, account_id=owner)) == 4
            assert browser.post("/api/chat", json={**body, "message": "我的首选城市改为北京", "use_env_llm": False}).json()["profile"]["preferred_cities"] == ["北京"]
            compat = httpx.post(web_url + "/api/chat", cookies=browser.cookies, json={**body,
                "message": "我的首选城市改为上海", "use_env_llm": False}, timeout=30)
            assert compat.status_code == 200 and compat.json()["profile"]["preferred_cities"] == ["上海"], compat.text[:300]
            auth.session("logout", session_token=browser.cookies["job_agent_session"])
            assert browser.post("/api/chat", json=body).status_code == 401
    finally:
        store.close()
    print("==> Java chat entry -> real Python Agent/tool -> profile/history/160 provider tokens -> one Java charge; zero-balance/CSRF/failure/outages/recovery/revocation: PASS")


def run_chat_stream_gateway_flow(gateway_url: str, web_url: str, java_url: str, internal_token: str,
                                  web: str, java: str, model: str, database_url: str) -> None:
    """Exercise the real proxy/SSE/disconnect path, not an in-memory TestClient stream."""
    from job_hunting_agent.account_work import AccountWorkBusy
    from job_hunting_agent.config import PlatformAuthSettings
    from job_hunting_agent.platform_auth import PlatformAuthClient
    from job_hunting_agent.sqlalchemy_store import SQLAlchemyStore
    auth = PlatformAuthClient(PlatformAuthSettings(True, java_url, internal_token, 10))
    store = SQLAlchemyStore(database_url)
    owners = []
    password = "stream-acceptance-password"
    for _ in range(2):
        email = f"chat-stream-{os.urandom(10).hex()}@example.com"
        owner = auth.register_account(email=email, password=password, display_name="Stream acceptance", email_verified=True, consents=[]).account_id
        owners.append((owner, email))
    def events(response):
        name, data = None, []
        for line in response.iter_lines():
            if line.startswith("event: "):
                name = line[7:]
            elif line.startswith("data: "):
                data.append(line[6:])
            elif not line and name is not None:
                yield name, json.loads("\n".join(data))
                name, data = None, []
    try:
        with httpx.Client(base_url=gateway_url, timeout=40) as browser, httpx.Client(base_url=gateway_url, timeout=40) as other:
            for client, (_, email) in zip((browser, other), owners, strict=True):
                login=client.post("/api/auth/login",json={"email":email,"password":password})
                assert login.status_code == 200, login.text[:300]
                client.headers["X-CSRF-Token"] = login.json()["csrf_token"]
            owner=owners[0][0]
            candidate=browser.post("/api/profiles",json={"name":"Stream scope"}).json()["candidate_id"]
            key=browser.post("/api/chat/sessions",json={"candidate_id":candidate}).json()["session"]["session_id"]
            body={"candidate_id":candidate,"session_id":key,"message":"acceptance-stream-update","auto_rag":False}
            assert httpx.post(gateway_url+"/api/chat/stream",cookies=browser.cookies,json=body).status_code==403
            assert other.post("/api/chat/stream",json=body).status_code==404
            assert browser.post("/internal/v1/chat/stream",json={}).status_code==404
            for origin in (java_url,web_url):
                assert httpx.post(origin+"/internal/v1/chat/stream",json={**body,"session_token":browser.cookies["job_agent_session"]}).status_code==401
            denied=browser.post("/api/chat/stream",json=body)
            assert denied.status_code==402 and denied.json()["detail"]=="余额不足，请先充值后重试。",denied.text[:300]
            assert browser.post("/api/me/balance/recharge",json={"amount_yuan":1,"idempotency_key":os.urandom(16).hex()}).status_code==200
            collected=[]
            first_token=None
            with browser.stream("POST","/api/chat/stream",json=body,headers={"X-Request-ID":"stream-acceptance"}) as response:
                assert response.status_code==200 and response.headers["content-type"].startswith("text/event-stream")
                assert response.headers["x-request-id"]=="stream-acceptance" and response.headers["cache-control"]=="no-store"
                for name,value in events(response):
                    if name=="token" and first_token is None:
                        first_token=time.monotonic()
                    collected.append((name,value))
            assert first_token is not None and time.monotonic()-first_token>=0.5, "Proxy buffered until final instead of streaming tokens"
            assert collected[-1][0]=="final" and sum(name=="final" for name,_ in collected)==1,str(collected)[-500:]
            assert collected[-1][1]["profile"]["preferred_cities"]==["上海"]
            assert "scope" not in [name for name,_ in collected]
            assert not any(secret in json.dumps(collected) for secret in ("session_token","account_id","tool_outputs"))
            assert len(store.list_chat_messages(candidate,key,account_id=owner))==2
            charges=[entry for entry in store.list_account_balance_ledger(owner) if entry.entry_kind=="consumption"]
            assert len(charges)==1 and charges[0].token_count==160,str(charges)
            # Disconnect during the second model call. The first call/tool is already complete.
            with browser.stream("POST","/api/chat/stream",json={**body,"message":"acceptance-cancel"},
                headers={"X-Request-ID":"stream-acceptance"}) as response:
                assert response.status_code==200
                for name,value in events(response):
                    if name=="token" and value["content"]=="Partial":
                        break
                else:
                    raise AssertionError("Missing partial token before cancellation")
            with_error=False
            try:
                store.account_work.acquire(owner,deletion=True).release()
            except AccountWorkBusy:
                with_error=True
            assert with_error,"Cancellation released account guard while model work was still running"
            deadline=time.monotonic()+12
            while True:
                try:
                    store.account_work.acquire(owner,deletion=True).release()
                    break
                except AccountWorkBusy:
                    assert time.monotonic()<deadline,"Disconnected worker failed to drain"
                    time.sleep(0.1)
            assert len(store.list_chat_messages(candidate,key,account_id=owner))==2,"Cancelled request persisted a false successful turn"
            charges=[entry for entry in store.list_account_balance_ledger(owner) if entry.entry_kind=="consumption"]
            assert sorted(entry.token_count for entry in charges)==[80,160],str(charges)
            assert store.get_account_balance_summary(owner).total_consumed_micro_yuan==6000
            assert store.get_candidate_profile(candidate,account_id=owner).preferred_cities==["北京"],"Completed tool side effects must not be presented as rolled back"
            assert sum(t.status=="cancelled" for t in store.list_tool_call_traces(account_id=owner))==1
            failure=browser.post("/api/chat/stream",json={**body,"message":"acceptance-failure"})
            failure_events=list(events(failure))
            assert failure_events[-1][0]=="error" and not any(n=="final" for n,_ in failure_events)
            assert "private-model-error" not in failure.text
            compat=httpx.post(web_url+"/api/chat/stream",cookies=browser.cookies,
                json={**body,"message":"我的首选城市改为上海","use_env_llm":False},timeout=30)
            assert compat.status_code==200 and list(events(compat))[-1][0]=="final",compat.text[:300]
            for container,health,origin in ((web,web_url+"/api/health",gateway_url),(java,java_url+"/actuator/health",web_url)):
                try:
                    docker("stop","--time","1",container)
                    failure=httpx.post(origin+"/api/chat/stream",cookies=browser.cookies,
                        headers={"X-CSRF-Token":browser.headers["X-CSRF-Token"]},json=body,timeout=25)
                    assert failure.status_code==503 and failure.json()["code"]=="CHAT_EXECUTION_UNCONFIRMED",failure.text[:300]
                finally:
                    docker("start",container)
                    wait(health)
            assert browser.post("/api/chat/stream",json={**body,"message":"我的首选城市改为北京","use_env_llm":False}).status_code==200
            auth.session("logout",session_token=browser.cookies["job_agent_session"])
            assert browser.post("/api/chat/stream",json=body).status_code==401
    finally:
        store.close()
    print("==> Java SSE -> incremental Unicode-safe events -> real Agent/tool/history/160-token settlement -> disconnect drains guard/no false history/80 known tokens -> scope/CSRF/balance/outages/recovery: PASS")


def run_local_collection_management_flow(gateway_url: str, web_url: str, java_url: str, internal_token: str,
                                         web: str, java: str, worker: str, broker: str, database_url: str) -> None:
    import hashlib
    from job_hunting_agent.config import PlatformAuthSettings
    from job_hunting_agent.platform_auth import PlatformAuthClient
    from job_hunting_agent.sqlalchemy_store import SQLAlchemyStore
    auth = PlatformAuthClient(PlatformAuthSettings(True, java_url, internal_token, 10))
    store = SQLAlchemyStore(database_url)
    emails = [f"local-{os.urandom(10).hex()}@example.com" for _ in range(2)]
    password = "local-collection-password"
    owners = [auth.register_account(email=email, password=password, display_name="Local collection", email_verified=True, consents=[]).account_id for email in emails]
    try:
        with httpx.Client(base_url=gateway_url, timeout=20) as browser, httpx.Client(base_url=gateway_url, timeout=20) as other:
            for client, email in zip((browser, other), emails, strict=True):
                response = client.post("/api/auth/login", json={"email": email, "password": password})
                assert response.status_code == 200
                client.headers["X-CSRF-Token"] = response.json()["csrf_token"]
            owner = owners[0]
            candidate = browser.post("/api/profiles", json={"name": "Local collection acceptance"}).json()["candidate_id"]
            assert browser.post("/api/me/balance/recharge", json={"amount_yuan": 1, "idempotency_key": os.urandom(16).hex()}).status_code == 200
            manifest_path = "/api/projects/local/manifest"
            data = {"candidate_id": candidate, "project_name": "Local design", "files": [
                {"relative_path": "README.md", "file_size": 9, "sha256": hashlib.sha256(b"prototype").hexdigest()},
                {"relative_path": ".env", "file_size": 1, "sha256": "a"*64}]}
            assert httpx.post(gateway_url + manifest_path, json=data).status_code == 401
            assert httpx.post(gateway_url + manifest_path, cookies=browser.cookies, json=data).status_code == 403
            assert other.post(manifest_path, json=data).status_code == 404
            assert browser.post(manifest_path, json={**data, "files": [{**data["files"][0], "content": "SECRET"}]}).status_code == 422
            plan = browser.post(manifest_path, json=data)
            assert plan.status_code == 200, plan.text[:300]
            collection = plan.json()["collection"]["id"]
            path = f"/api/projects/local/{collection}"
            file_id = plan.json()["selected_files"][0]["id"]
            assert len(plan.json()["selected_files"]) == 1 and "account_id" not in plan.json()["collection"]
            assert browser.post(manifest_path, json=data).json()["collection"]["id"] == collection
            assert browser.post("/internal/v1/projects/local-get", json={}).status_code == 404
            for origin in (web_url, java_url):
                assert httpx.post(origin + "/internal/v1/projects/local-get", json={"session_token": browser.cookies["job_agent_session"], "collection_id": collection}).status_code == 401
            assert other.get(path).status_code == 404 and other.delete(path).status_code == 404
            assert httpx.delete(gateway_url + path, cookies=browser.cookies).status_code == 403
            assert httpx.get(web_url + path, cookies=browser.cookies).json() == browser.get(path).json()
            completion_path = path + "/complete"
            assert httpx.post(gateway_url + completion_path).status_code == 401
            assert httpx.post(gateway_url + completion_path, cookies=browser.cookies).status_code == 403
            assert other.post(completion_path).status_code == 404
            assert browser.post(completion_path, json={"account_id": owners[1]}).status_code == 422
            assert browser.post(completion_path).json()["code"] == "PROJECT_COLLECTION_INCOMPLETE"
            assert store.list_project_cards(candidate, account_id=owner) == []
            for origin in (web_url, java_url):
                assert httpx.post(origin + "/internal/v1/projects/local-complete", json={"session_token": browser.cookies["job_agent_session"], "collection_id": collection}).status_code == 401

            def upload_and_wait(target, target_file):
                uploaded = browser.post(target + "/files", data={"file_ids": json.dumps([target_file])}, files=[("files", ("README.md", b"prototype", "text/markdown"))])
                assert uploaded.status_code == 200, uploaded.text[:300]
                task = uploaded.json()["rag_task"]
                assert task is not None
                deadline = time.monotonic() + 30
                while time.monotonic() < deadline:
                    snapshot = browser.get(f"/api/tasks/{task['task_key']}").json()["task"]
                    state = snapshot["status"]
                    if state in {"succeeded", "failed"}:
                        break
                    time.sleep(0.2)
                assert state == "succeeded", {"status": state, "error_summary": snapshot.get("error_summary")}

            with store.connect() as conn:
                baseline_texts = {row["id"] for row in conn.execute("SELECT id FROM long_texts WHERE account_id=?", (owner,)).fetchall()}
                baseline_chunks = {row["id"] for row in conn.execute("SELECT id FROM rag_chunks WHERE account_id=?", (owner,)).fetchall()}
            upload_and_wait(path, file_id)
            resumed = browser.post(manifest_path, json=data)
            assert resumed.status_code == 200 and resumed.json()["resumed"] and resumed.json()["selected_files"] == []
            fixture = f"""
import json
from job_hunting_agent.app import JobHuntingApp
a=JobHuntingApp();a.initialize()
objects=[a.resume_files.save(account_id={owner},candidate_id={candidate},filename='design.txt',content=b'original'+bytes([i])) for i in (1,2)]
asset,version=a.store.register_knowledge_asset(account_id={owner},candidate_id={candidate},asset_kind='document',title='Design',original_filename='design.txt',storage_key=objects[0].storage_key,media_type='text/plain',file_size=objects[0].file_size,sha256=objects[0].sha256)
a.store.add_knowledge_asset_version(asset.id,account_id={owner},original_filename='design.txt',storage_key=objects[1].storage_key,media_type='text/plain',file_size=objects[1].file_size,sha256=objects[1].sha256)
with a.store.connect() as c:c.execute('UPDATE project_collection_files SET knowledge_asset_id=?,knowledge_asset_version_id=?,storage_key=?,metadata_json=? WHERE id=?',(asset.id,version.id,objects[0].storage_key,json.dumps({{'last_modified':12,'extracted_text':'SECRET_CONTENT','storage_key':'SECRET_OBJECT'}}),{file_id}))
print(json.dumps([o.storage_key for o in objects]));a.store.close()
"""
            keys = json.loads(docker("exec", worker, "python", "-c", fixture).splitlines()[-1])
            queried = browser.get(path)
            assert queried.status_code == 200 and "SECRET" not in queried.text and not any(k in queried.text for k in keys)
            assert queried.json()["collection"]["uploaded_file_count"] == 1
            with store.connect() as conn:
                assert conn.execute("SELECT count(*) AS n FROM rag_chunks WHERE account_id=?", (owner,)).fetchone()["n"] > 0
            with store.account_work.acquire(owner):
                assert browser.delete(path).json()["code"] == "PROJECT_BUSY"
            docker("stop", "--time", "1", worker)
            docker("stop", "--time", "1", broker)
            cancelled = browser.delete(path)
            assert cancelled.status_code == 200 and cancelled.json()["cleanup_status"] == "pending", cancelled.text[:300]
            assert not any(k in cancelled.text for k in keys)
            assert browser.delete(path).json() == cancelled.json() and browser.get(path).status_code == 404
            assert browser.post(path + "/files", data={"file_ids": json.dumps([file_id])}, files=[("files", ("README.md", b"prototype", "text/markdown"))]).status_code == 404
            with store.connect() as conn:
                assert {row["id"] for row in conn.execute("SELECT id FROM rag_chunks WHERE account_id=?", (owner,)).fetchall()} == baseline_chunks
                assert {row["id"] for row in conn.execute("SELECT id FROM long_texts WHERE account_id=?", (owner,)).fetchall()} == baseline_texts
            docker("start", broker)
            docker("start", worker)
            fail_cleanup = f"""
from job_hunting_agent.app import JobHuntingApp
from job_hunting_agent.project_deletion import cleanup
a=JobHuntingApp();a.initialize()
def fail(key):raise RuntimeError('SECRET_STORAGE')
a.resume_files.delete=fail
assert cleanup(a,{owner},{collection},collection=True,force=True)['cleanup_status']=='pending'
a.store.close()
"""
            docker("exec", worker, "python", "-c", fail_cleanup)
            docker("restart", "--time", "1", worker)
            with store.connect() as conn:
                conn.execute("UPDATE project_collection_deletions SET next_attempt_at=now() WHERE account_id=?", (owner,))
            maintenance = "from job_hunting_agent.worker import create_worker_app; from job_hunting_agent.task_queue import STALE_BACKGROUND_TASK_RECOVERY_TASK_NAME; print(create_worker_app().tasks[STALE_BACKGROUND_TASK_RECOVERY_TASK_NAME].apply().get())"
            docker("exec", worker, "python", "-c", maintenance)
            assert browser.delete(path).json()["cleanup_status"] == "completed"
            docker("exec", worker, "python", "-c", f"from job_hunting_agent.app import JobHuntingApp; a=JobHuntingApp(); assert all(not a.resume_files.path_for(k).exists() for k in {keys!r}); a.store.close()")
            fresh = browser.post(manifest_path, json=data).json()
            fresh_path = f"/api/projects/local/{fresh['collection']['id']}"
            assert fresh["collection"]["id"] != collection
            for container, health, origin in ((web, web_url+"/api/health",gateway_url),(java,java_url+"/actuator/health",web_url)):
                try:
                    docker("stop", "--time", "1", container)
                    unavailable = httpx.get(origin + fresh_path, cookies=browser.cookies, timeout=20)
                    assert unavailable.status_code == 503 and "files" not in unavailable.json()
                    completion_unavailable = httpx.post(origin + fresh_path + "/complete", cookies=browser.cookies,
                        headers={"X-CSRF-Token": browser.headers["X-CSRF-Token"]}, timeout=20)
                    assert completion_unavailable.status_code == 503 and "project_card" not in completion_unavailable.json()
                finally:
                    docker("start", container)
                    wait(health)
            upload_and_wait(fresh_path, fresh["selected_files"][0]["id"])
            complete = browser.post(fresh_path + "/complete")
            assert complete.status_code == 200, complete.text[:300]
            assert set(complete.json()) == {"project_card", "processing_async"} and complete.json()["processing_async"] is False
            assert browser.post(fresh_path + "/complete").json() == complete.json()
            assert httpx.post(web_url + fresh_path + "/complete", cookies=browser.cookies, timeout=20).json() == complete.json()
            assert len(store.list_project_cards(candidate, account_id=owner)) == 1
            assert browser.get(fresh_path).json()["collection"]["status"] == "ready"
            assert browser.delete(fresh_path).json()["code"] == "PROJECT_COLLECTION_CONFLICT"
            assert browser.post(manifest_path, json=data).json()["code"] == "PROJECT_COLLECTION_CONFLICT"
            auth.session("logout", session_token=browser.cookies["job_agent_session"])
            assert browser.get(fresh_path).status_code == 401
            assert browser.post(fresh_path + "/complete").status_code == 401
    finally:
        for name in (broker,worker,web,java):
            docker("start", name)
        store.close()
    print("==> Java local manifest/query/cancel/complete -> Python-owned upload/real Worker RAG -> fingerprint resume/scope/CSRF/redaction -> atomic cancellation + all-version cleanup/failure/restart -> idempotent atomic completion/late upload 404/ready protection/outages/revocation: PASS")


def run_resume_upload_gateway_flow(gateway_url: str, web_url: str, java_url: str, internal_token: str,
                                  web: str, database_url: str) -> None:
    """Exercise the Java multipart boundary and Python-owned resume pipeline."""
    from io import BytesIO
    from docx import Document
    from job_hunting_agent.config import PlatformAuthSettings
    from job_hunting_agent.platform_auth import PlatformAuthClient

    auth = PlatformAuthClient(PlatformAuthSettings(True, java_url, internal_token, 10))
    email = f"resume-{os.urandom(10).hex()}@example.com"
    password = "resume-upload-password"
    auth.register_account(email=email, password=password, display_name="Resume upload",
                          email_verified=True, consents=[])
    document = Document()
    document.add_paragraph("Python and Java backend project experience.")
    content = BytesIO()
    document.save(content)
    resume = content.getvalue()

    with httpx.Client(base_url=gateway_url, timeout=30) as browser:
        login = browser.post("/api/auth/login", json={"email": email, "password": password})
        assert login.status_code == 200, login.text[:300]
        browser.headers["X-CSRF-Token"] = login.json()["csrf_token"]
        candidate = browser.post("/api/profiles", json={"name": "Resume upload acceptance"}).json()["candidate_id"]
        path = "/api/resumes/upload"
        multipart = {"file": ("resume.docx", resume,
                               "application/vnd.openxmlformats-officedocument.wordprocessingml.document")}
        assert httpx.post(gateway_url + path, files=multipart, data={"candidate_id": candidate}).status_code == 401
        assert httpx.post(gateway_url + path, cookies=browser.cookies, files=multipart,
                          data={"candidate_id": candidate}).status_code == 403
        uploaded = browser.post(path, data={"candidate_id": str(candidate)}, files=multipart)
        assert uploaded.status_code == 200, uploaded.text[:500]
        artifact = uploaded.json()["artifact"]
        assert artifact["candidate_id"] == candidate
        assert artifact["extraction_method"] == "docx"
        assert "storage_key" not in uploaded.text and "account_id" not in uploaded.text
        duplicate = browser.post(path, data={"candidate_id": str(candidate)}, files=multipart)
        assert duplicate.status_code == 409, duplicate.text[:300]
        direct = httpx.post(web_url + path, cookies=browser.cookies, files=multipart,
                            data={"candidate_id": candidate})
        assert direct.status_code == 503 and "artifact" not in direct.json()
        assert httpx.post(gateway_url + "/internal/v1/resumes/upload",
                          headers={"X-Internal-Service-Token": internal_token}, files=multipart,
                          data={"session_token": browser.cookies["job_agent_session"], "candidate_id": candidate}).status_code == 404
        docker("stop", "--time", "1", web)
        try:
            unavailable = browser.post(path, data={"candidate_id": str(candidate)}, files=multipart)
            assert unavailable.status_code == 503 and "artifact" not in unavailable.json()
        finally:
            docker("start", web)
            wait(web_url + "/api/health")
    print("==> Java resume multipart -> Python ClamAV/parser/RAG boundary -> duplicate/scope/outage/hidden-internal checks: PASS")


def run_job_screenshot_gateway_flow(gateway_url: str, web_url: str, java_url: str, internal_token: str,
                                   web: str) -> None:
    """Exercise the Java multipart boundary and Python-owned vision import pipeline."""
    from job_hunting_agent.config import PlatformAuthSettings
    from job_hunting_agent.platform_auth import PlatformAuthClient

    auth = PlatformAuthClient(PlatformAuthSettings(True, java_url, internal_token, 10))
    email = f"job-screenshot-{os.urandom(10).hex()}@example.com"
    password = "job-screenshot-password"
    auth.register_account(email=email, password=password, display_name="Job screenshot", email_verified=True, consents=[])
    # A valid 1x1 PNG is enough for the deterministic model fixture; the Python
    # image validator still executes the real Pillow format and pixel checks.
    screenshot = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
    )
    path = "/api/jobs/screenshots"

    def files():
        return {"screenshots": ("job.png", screenshot, "image/png")}

    with httpx.Client(base_url=gateway_url, timeout=30) as browser:
        login = browser.post("/api/auth/login", json={"email": email, "password": password})
        assert login.status_code == 200, login.text[:300]
        browser.headers["X-CSRF-Token"] = login.json()["csrf_token"]
        recharge = browser.post("/api/me/balance/recharge", json={
            "amount_yuan": 1,
            "idempotency_key": f"screenshot:{os.urandom(16).hex()}",
        })
        assert recharge.status_code == 200, recharge.text[:300]
        assert httpx.post(gateway_url + path, files=files()).status_code == 401
        assert httpx.post(gateway_url + path, cookies=browser.cookies, files=files()).status_code == 403
        uploaded = browser.post(path, data={"source_url": "https://www.zhipin.com/job_detail/acceptance"}, files=files())
        assert uploaded.status_code == 200, uploaded.text[:500]
        payload = uploaded.json()
        assert payload["job"]["import_method"] == "screenshot"
        assert payload["job"]["title"] == "Python 后端开发工程师"
        assert payload["extraction"] == {"source": "screenshot", "screenshot_count": 1}
        assert "account_id" not in uploaded.text and "storage_key" not in uploaded.text
        duplicate = browser.post(path, data={"source_url": "https://www.zhipin.com/job_detail/acceptance"}, files=files())
        assert duplicate.status_code == 409, duplicate.text[:300]
        direct = httpx.post(web_url + path, cookies=browser.cookies, files=files(), timeout=20)
        assert direct.status_code == 503 and "统一入口" in direct.text
        assert httpx.post(gateway_url + "/internal/v1/jobs/screenshots",
                          headers={"X-Internal-Service-Token": internal_token}, files=files(),
                          data={"session_token": browser.cookies["job_agent_session"]}).status_code == 404
        docker("stop", "--time", "1", web)
        try:
            unavailable = browser.post(path, files=files())
            assert unavailable.status_code == 503 and "job" not in unavailable.json()
        finally:
            docker("start", web)
            wait(web_url + "/api/health")
    print("==> Java job screenshot multipart -> Python Pillow/vision/import boundary -> duplicate/scope/outage/hidden-internal checks: PASS")


def run_project_submission_gateway_flow(gateway_url: str, web_url: str, java_url: str, internal_token: str,
                                       web: str, worker: str, broker: str, database_url: str) -> None:
    from job_hunting_agent.config import PlatformAuthSettings
    from job_hunting_agent.platform_auth import PlatformAuthClient
    from job_hunting_agent.sqlalchemy_store import SQLAlchemyStore
    auth = PlatformAuthClient(PlatformAuthSettings(True, java_url, internal_token, 10))
    email = f"submit-{os.urandom(10).hex()}@example.com"
    password = "project-submit-password"
    owner = auth.register_account(email=email, password=password, display_name="Submission", email_verified=True, consents=[]).account_id
    store = SQLAlchemyStore(database_url)
    try:
        with httpx.Client(base_url=gateway_url, timeout=20) as browser:
            login = browser.post("/api/auth/login", json={"email": email, "password": password})
            assert login.status_code == 200
            browser.headers["X-CSRF-Token"] = login.json()["csrf_token"]
            candidate = browser.post("/api/profiles", json={"name": "GitHub submission"}).json()["candidate_id"]
            assert browser.post("/api/me/balance/recharge", json={"amount_yuan": 1, "idempotency_key": os.urandom(16).hex()}).status_code == 200
            data = {"candidate_id": candidate, "repository_url": "https://github.com/example/submission-fixture",
                    "idempotency_key": f"github:{os.urandom(16).hex()}"}
            path = "/api/projects/github"
            assert httpx.post(gateway_url + path, cookies=browser.cookies, json=data).status_code == 403
            assert httpx.post(gateway_url + path, json=data).status_code == 401
            assert browser.post("/internal/v1/projects/github", json=data).status_code == 404
            for origin in (web_url, java_url):
                assert httpx.post(origin + "/internal/v1/projects/github", json={"session_token": browser.cookies["job_agent_session"], **data}).status_code == 401
            assert browser.post(path, json={**data, "repository_url": "https://github.com@evil.test/a/b"}).status_code == 400
            # Keep network input deterministic: no production worker, credentials or public GitHub traffic.
            docker("stop", "--time", "1", worker)
            docker("stop", "--time", "1", broker)
            pending = browser.post(path, json=data)
            assert pending.status_code == 503 and "task" not in pending.json(), pending.text[:300]
            with store.connect() as conn:
                receipt = conn.execute("SELECT * FROM project_submissions WHERE account_id=?", (owner,)).fetchone()
                assert receipt["status"] == "pending" and receipt["attempts"] == 1
                assert conn.execute("SELECT status FROM background_tasks WHERE task_key=?", (receipt["task_key"],)).fetchone()["status"] == "queued"
                conn.execute("UPDATE project_submissions SET next_dispatch_at=now() WHERE account_id=?", (owner,))
            docker("start", broker)
            maintenance = "from job_hunting_agent.worker import create_worker_app; from job_hunting_agent.task_queue import STALE_BACKGROUND_TASK_RECOVERY_TASK_NAME; print(create_worker_app().tasks[STALE_BACKGROUND_TASK_RECOVERY_TASK_NAME].apply().get())"
            docker("exec", web, "python", "-c", maintenance)
            response = browser.post(path, json=data)
            assert response.status_code == 200 and response.json()["task"]["task_key"] == receipt["task_key"], response.text[:300]
            assert not {"account_id", "submission_candidate_id"} & response.json().keys()
            assert httpx.post(web_url + path, cookies=browser.cookies, json=data, timeout=20).json() == response.json()
            assert browser.post(path, json={**data, "repository_url": "https://github.com/example/other"}).status_code == 409
            # Exercise the registered Worker entry and full archive pipeline; only GitHub byte fetching is replaced.
            execute = f"""
from io import BytesIO
from zipfile import ZipFile
from job_hunting_agent import app as module
from job_hunting_agent.github_project import FetchedGitHubRepository,normalize_public_github_repository_url
from job_hunting_agent.worker import create_worker_app
from job_hunting_agent.task_queue import BACKGROUND_TASK_NAME
b=BytesIO()
with ZipFile(b,'w') as z:
    z.writestr('submission-main/README.md','Public design project using Python and FastAPI. Project evidence and acceptance criteria.')
    z.writestr('submission-main/main.py','def run(): return 1')
module.fetch_public_github_repository_archive=lambda url: FetchedGitHubRepository(normalize_public_github_repository_url(url),'main','a'*40,b.getvalue())
task=create_worker_app().tasks[BACKGROUND_TASK_NAME]
first=task.apply(args=[{receipt['task_key']!r}]).get(propagate=True)
assert first['status']=='succeeded',first
second=task.apply(args=[{receipt['task_key']!r}]).get(propagate=True)
assert second['status']=='succeeded'
print('Controlled snapshot Worker execution: PASS')
"""
            docker("exec", web, "python", "-c", execute)
            replay = browser.post(path, json=data)
            assert replay.status_code == 200 and replay.json()["task"]["status"] == "succeeded"
            assert replay.json()["task"]["result"] is None
            with store.connect() as conn:
                assert conn.execute("SELECT count(*) AS n FROM background_tasks WHERE account_id=? AND task_type='github_project_analysis'", (owner,)).fetchone()["n"] == 1
                assert conn.execute("SELECT count(*) AS n FROM project_experience_cards WHERE account_id=?", (owner,)).fetchone()["n"] == 1
                assert conn.execute("SELECT attempt FROM background_tasks WHERE task_key=?", (receipt["task_key"],)).fetchone()["attempt"] == 1
                card_id = conn.execute("SELECT id FROM project_experience_cards WHERE account_id=?", (owner,)).fetchone()["id"]
            cancelled_data = {**data, "idempotency_key": f"github:{os.urandom(16).hex()}"}
            docker("stop", "--time", "1", broker)
            assert browser.post(path, json=cancelled_data).status_code == 503
            deleted = browser.delete(f"/api/projects/{card_id}")
            assert deleted.status_code == 200, deleted.text[:300]
            docker("start", broker)
            with store.connect() as conn:
                conn.execute("UPDATE project_submissions SET next_dispatch_at=now() WHERE account_id=?", (owner,))
            docker("exec", web, "python", "-c", maintenance)
            assert browser.post(path, json=cancelled_data).json()["code"] == "PROJECT_SUBMISSION_CONFLICT"
            with store.connect() as conn:
                assert conn.execute("SELECT count(*) AS n FROM background_tasks WHERE account_id=? AND status='queued'", (owner,)).fetchone()["n"] == 0
            # Broker's original duplicate delivery cannot run the already-completed analysis again.
            docker("start", worker)
            assert browser.get(f"/api/tasks/{receipt['task_key']}").json()["task"]["status"] == "succeeded"
    finally:
        docker("start", broker)
        docker("start", worker)
        store.close()
    print("==> Java GitHub submit -> durable SQL intent -> Redis outage/maintenance recovery -> real Worker entry + controlled archive pipeline -> replay/conflict/CSRF/private scope: PASS")


def run_project_gateway_flow(gateway_url: str, web_url: str, java_url: str, internal_token: str,
                             web: str, java: str, worker: str, database_url: str) -> None:
    from job_hunting_agent.config import PlatformAuthSettings
    from job_hunting_agent.platform_auth import PlatformAuthClient
    from job_hunting_agent.models import ProjectExperienceCard
    from job_hunting_agent.sqlalchemy_store import SQLAlchemyStore
    auth = PlatformAuthClient(PlatformAuthSettings(True, java_url, internal_token, 10))
    store = SQLAlchemyStore(database_url)
    owners = []
    password = "project-acceptance-password"
    for _ in range(2):
        email = f"projects-{os.urandom(10).hex()}@example.com"
        owners.append((auth.register_account(email=email, password=password, display_name="Projects", email_verified=True, consents=[]).account_id, email))
    try:
        with httpx.Client(base_url=gateway_url, timeout=20) as browser, httpx.Client(base_url=gateway_url, timeout=20) as other:
            for client, (_, email) in zip((browser, other), owners, strict=True):
                login = client.post("/api/auth/login", json={"email": email, "password": password})
                assert login.status_code == 200, login.text[:200]
                client.headers["X-CSRF-Token"] = login.json()["csrf_token"]
            owner = owners[0][0]
            candidate = browser.post("/api/profiles", json={"name": "Project lifecycle"}).json()["candidate_id"]
            assert browser.post("/api/me/balance/recharge", json={"amount_yuan": 1, "idempotency_key": os.urandom(16).hex()}).status_code == 200
            card = ProjectExperienceCard("project", "Cross-industry design", ["design.pdf"], {}, [], ["Tolerance review"], [], [], [], [])
            record = store.save_project_card(candidate, card, account_id=owner)
            path = f"/api/projects/{record.id}"
            listing = f"/api/projects?candidate_id={candidate}"
            assert other.get(listing).status_code == 404
            assert other.delete(path).status_code == 404
            assert other.post(path + "/confirm", json={"confirmed_summary": "Mine"}).status_code == 404
            assert httpx.delete(gateway_url + path, cookies=browser.cookies).status_code == 403
            assert browser.post("/internal/v1/projects/delete", json={}).status_code == 404
            for origin in (java_url, web_url):
                assert httpx.post(origin + "/internal/v1/projects/list", json={"session_token": browser.cookies["job_agent_session"], "candidate_id": candidate}).status_code == 401
            response = browser.get(listing)
            assert response.status_code == 200 and response.json()["project_cards"][0]["id"] == record.id, response.text[:300]
            assert httpx.get(web_url + listing, cookies=browser.cookies).json() == response.json()
            fixture = f"""
import json
from job_hunting_agent.app import JobHuntingApp
a=JobHuntingApp(); a.initialize()
files=[a.resume_files.save(account_id={owner},candidate_id={candidate},filename='design.zip',content=b'archive'+bytes([i])) for i in (1,2)]
item=a.store.register_project_archive_import(account_id={owner},candidate_id={candidate},original_filename='design.zip',
    storage_key=files[0].storage_key,file_size=files[0].file_size,sha256=files[0].sha256,scan_engine='acceptance')
a.store.add_knowledge_asset_version(item.knowledge_asset_id,account_id={owner},original_filename='design.zip',storage_key=files[1].storage_key,
    media_type='application/zip',file_size=files[1].file_size,sha256=files[1].sha256)
with a.store.connect() as c:c.execute('UPDATE project_archive_imports SET project_card_id=? WHERE id=?',({record.id},item.id))
print(json.dumps([f.storage_key for f in files]));a.store.close()
"""
            keys = json.loads(docker("exec", worker, "python", "-c", fixture).splitlines()[-1])
            summary = {"confirmed_summary": "I reviewed design tolerances and acceptance criteria."}
            confirmed = browser.post(path + "/confirm", json=summary)
            assert confirmed.status_code == 200, confirmed.text[:400]
            key = confirmed.json()["task"]["task_key"]
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                task = browser.get(f"/api/tasks/{key}").json()["task"]
                if task["status"] in {"succeeded", "failed"}:
                    break
                time.sleep(0.2)
            assert task["status"] == "succeeded", task
            assert browser.post(path + "/confirm", json=summary).json()["task"]["task_key"] == key
            assert browser.post(path + "/confirm", json={"confirmed_summary": "Different"}).status_code == 409
            assert httpx.post(web_url + path + "/confirm", cookies=browser.cookies, json=summary).json()["task"]["task_key"] == key
            assert browser.get(f"/api/profiles/{candidate}").json()["profile"]["skills"] == {}
            with store.connect() as conn:
                assert conn.execute("SELECT count(*) AS n FROM long_texts WHERE entity_type='project_experience_card' AND entity_id=?", (record.id,)).fetchone()["n"] == 1
                assert conn.execute("SELECT count(*) AS n FROM rag_chunks WHERE candidate_id=?", (candidate,)).fetchone()["n"] > 0
                assert conn.execute("SELECT count(*) AS n FROM background_tasks WHERE account_id=? AND task_type='rag_index'", (owner,)).fetchone()["n"] == 1
            with store.account_work.acquire(owner):
                assert browser.delete(path).json()["code"] == "PROJECT_BUSY"
            accepted = browser.delete(path)
            assert accepted.status_code == 200 and accepted.json()["cleanup_status"] == "pending", accepted.text[:300]
            assert not any(k in accepted.text for k in keys)
            assert browser.delete(path).json() == accepted.json()
            assert browser.post(path + "/confirm", json=summary).status_code == 404
            assert browser.get(listing).json() == {"project_cards": []}
            with store.connect() as conn:
                assert conn.execute("SELECT count(*) AS n FROM rag_chunks WHERE candidate_id=?", (candidate,)).fetchone()["n"] == 0
                assert conn.execute("SELECT count(*) AS n FROM knowledge_assets WHERE account_id=?", (owner,)).fetchone()["n"] == 0
            failed_cleanup = f"""
from job_hunting_agent.app import JobHuntingApp
from job_hunting_agent.project_deletion import cleanup
a=JobHuntingApp();a.initialize()
def fail(key):raise RuntimeError('private storage credentials')
a.resume_files.delete=fail
assert cleanup(a,{owner},{record.id},force=True)['cleanup_status']=='pending'
a.store.close()
"""
            docker("exec", worker, "python", "-c", failed_cleanup)
            docker("restart", "--time", "1", worker)
            with store.connect() as conn:
                conn.execute("UPDATE project_deletions SET next_attempt_at=now() WHERE account_id=?", (owner,))
            maintenance = "from job_hunting_agent.worker import create_worker_app; from job_hunting_agent.task_queue import STALE_BACKGROUND_TASK_RECOVERY_TASK_NAME; print(create_worker_app().tasks[STALE_BACKGROUND_TASK_RECOVERY_TASK_NAME].apply().get())"
            docker("exec", worker, "python", "-c", maintenance)
            assert browser.delete(path).json()["cleanup_status"] == "completed"
            docker("exec", worker, "python", "-c", f"from job_hunting_agent.app import JobHuntingApp; a=JobHuntingApp(); assert all(not a.resume_files.path_for(k).exists() for k in {keys!r}); a.store.close()")
            for container, health, origin in ((web, web_url + "/api/health", gateway_url), (java, java_url + "/actuator/health", web_url)):
                try:
                    docker("stop", "--time", "1", container)
                    unavailable = httpx.get(origin + listing, cookies=browser.cookies, timeout=20)
                    assert unavailable.status_code == 503 and "project_cards" not in unavailable.json()
                    assert httpx.delete(origin + path, cookies=browser.cookies, headers={"X-CSRF-Token": browser.headers["X-CSRF-Token"]}, timeout=20).status_code == 503
                finally:
                    docker("start", container)
                    wait(health)
                assert browser.get(listing).json() == {"project_cards": []}
            auth.session("logout", session_token=browser.cookies["job_agent_session"])
            assert browser.delete(path).status_code == 401
    finally:
        store.close()
    print("==> Java project entry -> confirmation/real Worker RAG/idempotency -> atomic SQL/vector deletion + all-version manifest -> storage failure/restart cleanup -> scope/CSRF/outages/revocation: PASS")


def run_profile_deletion_gateway_flow(gateway_url: str, web_url: str, java_url: str, internal_token: str,
                                      worker: str, broker: str, database_url: str) -> None:
    from job_hunting_agent.config import PlatformAuthSettings
    from job_hunting_agent.platform_auth import PlatformAuthClient
    from job_hunting_agent.sqlalchemy_store import SQLAlchemyStore
    auth = PlatformAuthClient(PlatformAuthSettings(True, java_url, internal_token, 10))
    email = f"delete-profile-{os.urandom(10).hex()}@example.com"
    owner = auth.register_account(email=email, password="profile-deletion-password", display_name="Delete profile",
                                  email_verified=True, consents=[]).account_id
    store = SQLAlchemyStore(database_url)
    try:
        with httpx.Client(base_url=gateway_url, timeout=10) as browser:
            login = browser.post("/api/auth/login", json={"email": email, "password": "profile-deletion-password"})
            assert login.status_code == 200
            headers = {"X-CSRF-Token": login.json()["csrf_token"]}
            candidate = browser.post("/api/profiles", json={"name": "Delete fixture"}, headers=headers).json()["candidate_id"]
            path = f"/api/profiles/{candidate}"
            assert browser.delete(path).status_code == 403
            assert browser.post("/internal/v1/profiles/delete", json={}).status_code == 404
            for url in (web_url, java_url):
                assert httpx.post(url + "/internal/v1/profiles/delete", json={"session_token": browser.cookies["job_agent_session"],
                    "candidate_id": candidate}).status_code == 401
            with store.account_work.acquire(owner):
                busy = browser.delete(path, headers=headers)
                assert busy.status_code == 409 and busy.json()["code"] == "PROFILE_DELETE_BUSY"
            fixture = f"""
import json
from job_hunting_agent.app import JobHuntingApp
a=JobHuntingApp(); a.initialize()
objects=[a.resume_files.save(account_id={owner},candidate_id={candidate},filename='fixture.txt',content=b'private-'+bytes([v])) for v in (1,2)]
asset,version=a.store.register_knowledge_asset(account_id={owner},candidate_id={candidate},asset_kind='document',title='Private',
    original_filename='fixture.txt',storage_key=objects[0].storage_key,media_type='text/plain',file_size=objects[0].file_size,sha256=objects[0].sha256)
a.store.add_knowledge_asset_version(asset.id,account_id={owner},original_filename='fixture.txt',storage_key=objects[1].storage_key,
    media_type='text/plain',file_size=objects[1].file_size,sha256=objects[1].sha256)
text=a.store.add_long_text('candidate_profile',{candidate},'delete-fixture','private evidence',account_id={owner},candidate_id={candidate})
with a.store.connect() as c:
    c.execute('''INSERT INTO rag_chunks(id,account_id,candidate_id,long_text_id,entity_type,entity_id,source_label,chunk_index,
        content,content_sha256,embedding,embedding_model,embedding_dimensions,created_at,updated_at)
        VALUES (?,?,?,?,'candidate_profile',?,'delete-fixture',0,'private evidence',?,'[0.1,0.2,0.3]'::vector,'fixture',3,now(),now()) RETURNING 1 AS fixture''',
        ('profile-delete-{candidate}',{owner},{candidate},text,{candidate},'0'*64))
print(json.dumps([o.storage_key for o in objects])); a.store.close()
"""
            keys = json.loads(docker("exec", worker, "python", "-c", fixture).splitlines()[-1])
            docker("stop", "--time", "1", worker)
            docker("stop", "--time", "1", broker)
            accepted = browser.delete(path, headers=headers)
            assert accepted.status_code == 202 and accepted.json()["cleanup_status"] == "pending", accepted.text[:200]
            assert not any(key in accepted.text for key in keys)
            assert browser.get(path).status_code == 404
            assert browser.get("/api/profiles").json() == {"profiles": []}
            assert browser.delete(path, headers=headers).json() == accepted.json()
            compat = httpx.delete(web_url + path, cookies=browser.cookies, timeout=10)
            assert compat.status_code == 202 and compat.json() == accepted.json()
            with store.connect() as conn:
                assert conn.execute("SELECT COUNT(*) AS n FROM rag_chunks WHERE candidate_id=?", (candidate,)).fetchone()["n"] == 0
                row = conn.execute("SELECT storage_keys_json FROM profile_deletions WHERE account_id=? AND candidate_id=?", (owner, candidate)).fetchone()
                assert sorted(json.loads(row["storage_keys_json"])) == sorted(keys)
            docker("start", broker)
            docker("start", worker)
            failure = f"""
from job_hunting_agent.app import JobHuntingApp
from job_hunting_agent.profile_deletion import cleanup
a=JobHuntingApp(); a.initialize()
def fail(key): raise RuntimeError('private storage failure')
a.resume_files.delete=fail
assert cleanup(a,{owner},{candidate},force=True)['cleanup_status']=='pending'
a.store.close()
"""
            docker("exec", worker, "python", "-c", failure)
            docker("restart", "--time", "1", worker)
            with store.connect() as conn:
                conn.execute("UPDATE profile_deletions SET next_attempt_at=now() WHERE account_id=? AND candidate_id=?", (owner, candidate))
            maintenance = "from job_hunting_agent.worker import create_worker_app; from job_hunting_agent.task_queue import STALE_BACKGROUND_TASK_RECOVERY_TASK_NAME; print(create_worker_app().tasks[STALE_BACKGROUND_TASK_RECOVERY_TASK_NAME].apply().get())"
            docker("exec", worker, "python", "-c", maintenance)
            done = browser.delete(path, headers=headers)
            assert done.status_code == 200 and done.json()["cleanup_status"] == "completed", done.text[:200]
            physical = f"from job_hunting_agent.app import JobHuntingApp; a=JobHuntingApp(); assert all(not a.resume_files.path_for(k).exists() for k in {keys!r}); a.store.close()"
            docker("exec", worker, "python", "-c", physical)
    finally:
        docker("start", broker)
        docker("start", worker)
        store.close()
    print("==> Profile DELETE Java -> atomic SQL/RAG deletion + durable all-version manifest -> queue/storage outage -> Worker restart/maintenance recovery -> physical objects gone: PASS")


def run_account_deletion_gateway_flow(gateway_url: str, web_url: str, java_url: str, internal_token: str,
                                      web: str, worker: str, broker: str, database_url: str, credentials: tuple[str, str]) -> None:
    from job_hunting_agent.config import PlatformAuthSettings
    from job_hunting_agent.platform_auth import PlatformAuthClient
    from job_hunting_agent.sqlalchemy_store import SQLAlchemyStore

    auth = PlatformAuthClient(PlatformAuthSettings(True, java_url, internal_token, 10))
    password = "deletion-acceptance-password"
    emails = [f"delete-{os.urandom(10).hex()}@example.com" for _ in range(2)]
    ids = [auth.register_account(email=email, password=password, display_name="Delete acceptance",
                                 email_verified=True, consents=[]).account_id for email in emails]
    store = SQLAlchemyStore(database_url)
    in_flight = store.account_work.acquire(ids[0])
    try:
        fixture_code = f"""
import json
from job_hunting_agent.app import JobHuntingApp
from job_hunting_agent.models import CandidateProfileInput
app=JobHuntingApp()
app.initialize()
keys=[]
for owner in {ids!r}:
    profile=CandidateProfileInput(name='Deletion fixture',status='student',education='bachelor',experience_years=0,
        skills={{}},preferred_cities=[],salary_floor_k=None,expected_salary_k=None,target_directions=[])
    candidate=app.store.save_candidate_profile(profile,account_id=owner)
    text=app.store.add_long_text('profile',candidate,'deletion-fixture','private fixture',account_id=owner,candidate_id=candidate)
    with app.store.connect() as conn:
        conn.execute('''INSERT INTO rag_chunks(id,account_id,candidate_id,long_text_id,entity_type,entity_id,
            source_label,chunk_index,content,content_sha256,embedding,embedding_model,embedding_dimensions,created_at,updated_at)
            VALUES (?,?,?,?,'profile',?,'deletion-fixture',0,'private fixture',?,'[0.1,0.2,0.3]'::vector,'fixture',3,now(),now()) RETURNING 1 AS fixture''',
            ('deletion-fixture-'+str(owner),owner,candidate,text,candidate,'0'*64))
    obj=app.resume_files.save(account_id=owner,candidate_id=candidate,filename='deletion.txt',content=b'private deletion fixture')
    app.store.register_knowledge_asset(account_id=owner,candidate_id=candidate,asset_kind='document',title='Deletion fixture',
        original_filename='deletion.txt',storage_key=obj.storage_key,media_type='text/plain',file_size=obj.file_size,sha256=obj.sha256)
    keys.append(obj.storage_key)
print(json.dumps(keys))
app.store.close()
"""
        keys = json.loads(docker("exec", worker, "python", "-c", fixture_code).splitlines()[-1])
        with httpx.Client(base_url=gateway_url, timeout=15) as browser, httpx.Client(base_url=gateway_url, timeout=15) as admin:
            logged = admin.post("/api/auth/login", json={"email": credentials[0], "password": credentials[1]})
            assert logged.status_code == 200
            admin_headers = {"X-CSRF-Token": logged.json()["csrf_token"]}
            for owner in ids:
                assert admin.post(f"/api/admin/accounts/{owner}/balance/credit", json={"amount_yuan": 1,
                                  "reason": "deletion fixture", "idempotency_key": f"deletion-credit-{owner}"}, headers=admin_headers).status_code == 200
            assert admin.post("/api/account/delete", json={"current_password": credentials[1], "confirmation": "注销账号"},
                              headers=admin_headers).status_code == 403
            login = browser.post("/api/auth/login", json={"email": emails[0], "password": password})
            assert login.status_code == 200
            session = browser.cookies["job_agent_session"]
            headers = {"X-CSRF-Token": login.json()["csrf_token"]}
            body = {"current_password": password, "confirmation": "注销账号", "account_id": ids[1]}
            assert browser.post("/api/account/delete", json=body).status_code == 403
            assert browser.post("/api/account/delete", json={**body, "current_password": "wrong"}, headers=headers).status_code == 400
            docker("stop", "--time", "1", worker)
            docker("stop", "--time", "1", web)
            receipt = browser.post("/api/account/delete", json=body, headers=headers)
            assert receipt.status_code == 202 and receipt.json()["status"] == "accepted", receipt.text[:300]
            assert "job_agent_session=" in receipt.headers["set-cookie"] and "Max-Age=0" in receipt.headers["set-cookie"]
            first_key = receipt.json()["task_key"]
            assert store.get_account(ids[1]).status == "active"
            assert store.get_account(ids[0]).status == "disabled" and store.get_account(ids[0]).deleted_at is None
            assert admin.patch(f"/api/admin/accounts/{ids[0]}/status", json={"status": "active"}, headers=admin_headers).status_code == 409
            assert httpx.get(java_url + "/api/account/export", cookies={"job_agent_session": session}).status_code == 401
            assert store.list_background_tasks(account_id=ids[0]) == []
            docker("start", web)
            wait(web_url + "/api/health")
            login = browser.post("/api/auth/login", json={"email": emails[1], "password": password})
            assert login.status_code == 200
            second = httpx.post(web_url + "/api/account/delete", json=body, cookies=browser.cookies,
                                headers={"X-CSRF-Token": login.json()["csrf_token"]}, timeout=15)
            assert second.status_code == 202, second.text[:300]
            second_key = second.json()["task_key"]
            assert httpx.post(java_url + "/internal/v1/accounts/deletions/due", json={"limit": 20}).status_code == 401
            docker("stop", "--time", "1", broker)
            docker("start", worker)
            dispatch_code = "from job_hunting_agent.app import JobHuntingApp; from job_hunting_agent.account_deletion import dispatch_account_deletions; app=JobHuntingApp(); app.store.initialize(); print(dispatch_account_deletions(app)); app.store.close()"
            docker("exec", worker, "python", "-c", dispatch_code)
            assert store.get_background_task(first_key).status == "queued"
            assert store.get_background_task(second_key).status == "queued"
            docker("stop", "--time", "1", worker)
            docker("start", broker)
            docker("start", worker)
            with store.connect() as conn:
                conn.execute("UPDATE platform_account_deletion_requests SET next_dispatch_at=CURRENT_TIMESTAMP-INTERVAL '1 second'")
            maintenance = "from job_hunting_agent.worker import create_worker_app; from job_hunting_agent.task_queue import STALE_BACKGROUND_TASK_RECOVERY_TASK_NAME; print(create_worker_app().tasks[STALE_BACKGROUND_TASK_RECOVERY_TASK_NAME].apply().get())"
            docker("exec", worker, "python", "-c", maintenance)
            deadline = time.monotonic() + 45
            while time.monotonic() < deadline:
                waiting = store.get_background_task(first_key)
                if waiting.status == "queued" and waiting.error_summary == "等待账号在途请求结束。":
                    break
                time.sleep(0.5)
            assert waiting.status == "queued" and waiting.attempt == 0, waiting
            assert store.get_account(ids[0]).deleted_at is None
            # Model an already-admitted upload committing after Java disables the account.
            late_file = f"""
from job_hunting_agent.app import JobHuntingApp
app=JobHuntingApp()
obj=app.resume_files.save(account_id={ids[0]},candidate_id=1,filename='in-flight.txt',content=b'in-flight data')
app.store.register_knowledge_asset(account_id={ids[0]},candidate_id=None,asset_kind='document',title='In flight',
    original_filename='in-flight.txt',storage_key=obj.storage_key,media_type='text/plain',file_size=obj.file_size,sha256=obj.sha256)
print(obj.storage_key)
app.store.close()
"""
            keys.append(docker("exec", worker, "python", "-c", late_file).splitlines()[-1])
            in_flight.release()
            with store.connect() as conn:
                conn.execute("UPDATE platform_account_deletion_requests SET next_dispatch_at=CURRENT_TIMESTAMP-INTERVAL '1 second' WHERE account_id=?", (ids[0],))
            docker("exec", worker, "python", "-c", maintenance)
            deadline = time.monotonic() + 90
            while time.monotonic() < deadline:
                if all(store.get_background_task(key).status == "succeeded" for key in (first_key, second_key)):
                    break
                time.sleep(1)
            for owner, key in zip(ids, (first_key, second_key), strict=True):
                assert store.get_background_task(key).status == "succeeded"
                assert store.get_account(owner).deleted_at is not None
                assert store.list_knowledge_assets(account_id=owner) == []
                with store.connect() as conn:
                    assert conn.execute("SELECT completed_at IS NOT NULL AS done FROM platform_account_deletion_requests WHERE account_id=?", (owner,)).fetchone()["done"]
                    assert conn.execute("SELECT count(*) AS n FROM account_balance_ledger WHERE account_id=?", (owner,)).fetchone()["n"] == 1
                    assert conn.execute("SELECT count(*) AS n FROM auth_sessions WHERE account_id=?", (owner,)).fetchone()["n"] == 0
                    assert conn.execute("SELECT count(*) AS n FROM rag_chunks WHERE account_id=?", (owner,)).fetchone()["n"] == 0
                    assert conn.execute("SELECT count(*) AS n FROM long_texts WHERE account_id=?", (owner,)).fetchone()["n"] == 0
            check_files = f"from job_hunting_agent.app import JobHuntingApp; app=JobHuntingApp(); assert all(not app.resume_files.path_for(key).exists() for key in {keys!r}); app.store.close()"
            docker("exec", worker, "python", "-c", check_files)
            assert browser.post("/api/auth/login", json={"email": emails[1], "password": password}).status_code == 401
    finally:
        in_flight.release()
        for name in (broker, worker, web):
            docker("start", name)
        wait(web_url + "/api/health")
        store.close()
    print("==> Java deletion intent -> Python/broker recovery -> in-flight request drain -> late files/RAG cleanup and financial retention: PASS")


def run_observability_gateway_flow(gateway_url: str, web_url: str, internal_token: str,
                                    credentials: tuple[str, str]) -> None:
    """Check real aggregation after Python recovery, without mocks or application data."""
    with httpx.Client(base_url=gateway_url, timeout=10) as admin:
        login = admin.post("/api/auth/login", json={"email": credentials[0], "password": credentials[1]})
        assert login.status_code == 200, login.text[:300]
        assert admin.get("/internal/v1/observability/requests").status_code == 404
        assert httpx.get(web_url + "/internal/v1/observability/requests").status_code == 401
        assert admin.get("/api/health").status_code == 200
        assert admin.get("/api/auth/me").status_code == 200
        response = admin.get("/api/admin/observability/requests?private=must-not-leak")
        assert response.status_code == 200, response.text[:500]
        data = response.json()
        assert data["status"] == "available", data
        assert response.headers["cache-control"] == "no-store"
        snapshots = [data["services"][name]["api"] for name in ("java", "python")]
        for name in ("java", "python"):
            assert data["services"][name]["status"] == "available"
            assert data["services"][name]["api"]["total_requests"] > 0
            assert data["services"][name]["internal"]["total_requests"] > 0
        assert data["requests"]["total_requests"] == sum(item["total_requests"] for item in snapshots)
        assert data["requests"]["total_duration_ms"] == sum(item["total_duration_ms"] for item in snapshots)
        expected_average = data["requests"]["total_duration_ms"] / data["requests"]["total_requests"]
        assert abs(data["requests"]["average_duration_ms"] - expected_average) <= 0.01
        assert all(key.startswith(("java:", "python:")) for key in data["requests"]["endpoint_counts"])
        assert "must-not-leak" not in str(data)
        for row in data["account_email"]["records"]:
            assert "***" in row["recipient_email"]
            assert row["owner"] in {"java", "python"}
            assert "delivery_key" not in row and "action_url" not in row
        forwarded = httpx.get(web_url + "/api/admin/observability/requests", cookies=admin.cookies, timeout=10)
        assert forwarded.status_code == 200, forwarded.text[:500]
        assert forwarded.json()["status"] == "available"
        direct = httpx.get(web_url + "/internal/v1/observability/requests", headers={
            "X-Internal-Service-Token": internal_token}, timeout=10)
        assert direct.status_code == 200 and direct.json()["status"] == "available"
        assert direct.headers["cache-control"] == "no-store"
    print("==> Java/Python observability -> scoped/weighted aggregation -> private callback -> Python outage/recovery: PASS")


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
    parser.add_argument("--only-local-collections", action="store_true", help="Only run isolated local collection management acceptance")
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
    broker = f"{prefix}-broker"
    worker = f"{prefix}-worker"
    model = f"{prefix}-chat-model"
    redis_password = os.urandom(24).hex()

    def reset_scenario_limits():
        # Only this run's randomly named protection Redis, never the broker or application Redis.
        # Independent scenarios must not consume one another's IP authentication budget.
        docker("exec", redis_name, "redis-cli", "--no-auth-warning", "-a", redis_password, "-n", "1", "FLUSHDB")

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
    acceptance_passed = False
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
        docker("run", "-d", "--name", broker, "--network", prefix,
               "--network-alias", "task-broker", "redis:7.4-alpine",
               "redis-server", "--requirepass", redis_password)
        root = Path(__file__).resolve().parents[1]
        docker("run", "-d", "--name", model, "--network", prefix, "--network-alias", "chat-model",
               "--mount", f"type=bind,source={root / 'tests/fixtures/chat_completion_server.py'},target=/tmp/chat_completion_server.py,readonly",
               args.ai_image, "python", "/tmp/chat_completion_server.py")
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
            "JOB_AGENT_TASK_QUEUE_ENABLED": "true",
            "JOB_AGENT_REDIS_URL": f"redis://:{redis_password}@task-broker:6379/0",
            "JOB_AGENT_RATE_LIMIT_ENABLED": "false",
            "JOB_AGENT_RATE_LIMIT_AUTH_REQUESTS": "100",
            "JOB_AGENT_JAVA_AUTH_ENABLED": "true",
            "JOB_AGENT_JAVA_BILLING_ENABLED": "true",
            "JOB_AGENT_JAVA_AUTH_BASE_URL": "http://platform-service:8081",
            "JOB_AGENT_JAVA_BILLING_BASE_URL": "http://platform-service:8081",
            "JOB_AGENT_JAVA_AUTH_INTERNAL_TOKEN": token,
            "JOB_AGENT_JAVA_BILLING_INTERNAL_TOKEN": token,
            "JOB_AGENT_LLM_PROVIDER": "acceptance",
            "JOB_AGENT_LLM_MODEL": "acceptance-chat",
            "JOB_AGENT_LLM_API_KEY": "local-fixture-only",
            "JOB_AGENT_LLM_BASE_URL": "http://chat-model:8099/v1",
            "JOB_AGENT_LLM_TIMEOUT_SECONDS": "10",
            "JOB_AGENT_MODEL_GATEWAY_CHAT_MAX_RETRIES": "0",
            "JOB_AGENT_INTENT_ROUTER_ENABLED": "false",
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
        admin_credentials = None if args.only_local_collections else run_web_billing_flow(
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
        if args.only_local_collections:
            docker("run", "-d", "--name", worker, *common, *env_args, args.ai_image,
                   "job-agent-worker", "--concurrency", "1", "--log-level", "WARNING")
            run_local_collection_management_flow(f"http://127.0.0.1:{args.port + 2}", f"http://127.0.0.1:{args.port + 1}",
                                                f"http://127.0.0.1:{args.port}", token, web, java, worker, broker, isolated_url)
            print("Docker local collection management acceptance: PASS")
            acceptance_passed = True
            return
        run_browser_gateway_flow(f"http://127.0.0.1:{args.port + 2}",
                                 f"http://127.0.0.1:{args.port + 1}",
                                 f"http://127.0.0.1:{args.port}", token, web, admin_credentials, isolated_url)
        reset_scenario_limits()
        run_profile_read_gateway_flow(f"http://127.0.0.1:{args.port + 2}", f"http://127.0.0.1:{args.port + 1}",
                                      f"http://127.0.0.1:{args.port}", token, web, java)
        reset_scenario_limits()
        run_chat_session_gateway_flow(f"http://127.0.0.1:{args.port + 2}", f"http://127.0.0.1:{args.port + 1}",
                                      f"http://127.0.0.1:{args.port}", token, web, java, isolated_url)
        reset_scenario_limits()
        run_chat_message_gateway_flow(f"http://127.0.0.1:{args.port + 2}", f"http://127.0.0.1:{args.port + 1}",
                                      f"http://127.0.0.1:{args.port}", token, web, java, model, isolated_url)
        reset_scenario_limits()
        run_chat_stream_gateway_flow(f"http://127.0.0.1:{args.port + 2}", f"http://127.0.0.1:{args.port + 1}",
                                      f"http://127.0.0.1:{args.port}", token, web, java, model, isolated_url)
        reset_scenario_limits()
        docker("run", "-d", "--name", worker, *common, *env_args, args.ai_image,
               "job-agent-worker", "--concurrency", "1", "--log-level", "WARNING")
        run_task_command_flow(f"http://127.0.0.1:{args.port + 2}", f"http://127.0.0.1:{args.port + 1}",
                              f"http://127.0.0.1:{args.port}", token, admin_credentials, isolated_url, worker, broker)
        reset_scenario_limits()
        run_project_submission_gateway_flow(f"http://127.0.0.1:{args.port + 2}", f"http://127.0.0.1:{args.port + 1}",
                                           f"http://127.0.0.1:{args.port}", token, web, worker, broker, isolated_url)
        reset_scenario_limits()
        run_local_collection_management_flow(f"http://127.0.0.1:{args.port + 2}", f"http://127.0.0.1:{args.port + 1}",
                                            f"http://127.0.0.1:{args.port}", token, web, java, worker, broker, isolated_url)
        reset_scenario_limits()
        run_resume_upload_gateway_flow(f"http://127.0.0.1:{args.port + 2}", f"http://127.0.0.1:{args.port + 1}",
                                       f"http://127.0.0.1:{args.port}", token, web, isolated_url)
        reset_scenario_limits()
        run_job_screenshot_gateway_flow(f"http://127.0.0.1:{args.port + 2}", f"http://127.0.0.1:{args.port + 1}",
                                        f"http://127.0.0.1:{args.port}", token, web)
        reset_scenario_limits()
        run_project_gateway_flow(f"http://127.0.0.1:{args.port + 2}", f"http://127.0.0.1:{args.port + 1}",
                                 f"http://127.0.0.1:{args.port}", token, web, java, worker, isolated_url)
        reset_scenario_limits()
        run_profile_deletion_gateway_flow(f"http://127.0.0.1:{args.port + 2}", f"http://127.0.0.1:{args.port + 1}",
                                          f"http://127.0.0.1:{args.port}", token, worker, broker, isolated_url)
        reset_scenario_limits()
        run_account_profile_gateway_flow(f"http://127.0.0.1:{args.port + 2}", f"http://127.0.0.1:{args.port + 1}",
                                         f"http://127.0.0.1:{args.port}", token, web, isolated_url, admin_credentials[0])
        reset_scenario_limits()
        run_account_export_gateway_flow(f"http://127.0.0.1:{args.port + 2}", f"http://127.0.0.1:{args.port + 1}",
                                        f"http://127.0.0.1:{args.port}", token, web, isolated_url)
        reset_scenario_limits()
        run_account_deletion_gateway_flow(f"http://127.0.0.1:{args.port + 2}", f"http://127.0.0.1:{args.port + 1}",
                                          f"http://127.0.0.1:{args.port}", token, web, worker, broker, isolated_url, admin_credentials)
        reset_scenario_limits()
        run_observability_gateway_flow(f"http://127.0.0.1:{args.port + 2}",
                                      f"http://127.0.0.1:{args.port + 1}", token, admin_credentials)
        reset_scenario_limits()
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
        acceptance_passed = True
    finally:
        if not acceptance_passed:
            dump_container_diagnostics(
                (prometheus, gateway, worker, web, java, redis_name, broker, model),
                (token, metrics_token, redis_password, container_url, jdbc, password),
            )
        for name in (prometheus, gateway, worker, web, java, redis_name, broker, model):
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
