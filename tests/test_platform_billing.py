from __future__ import annotations

import httpx
import pytest
from fastapi.testclient import TestClient

from job_hunting_agent.config import (
    BillingSettings,
    PlatformBillingSettings,
    load_platform_billing_settings,
)
from job_hunting_agent.models import AdminAuditEventRecord, UsageEventRecord
from job_hunting_agent.platform_billing import (
    PlatformBillingClient,
    PlatformBillingError,
    PlatformBillingUnavailableError,
    PlatformBalanceProjection,
)
from job_hunting_agent.sqlalchemy_store import SQLAlchemyStore
from job_hunting_agent.storage import IdempotencyConflictError, InsufficientBalanceError
from job_hunting_agent.web import create_web_app


@pytest.mark.parametrize("resource", ["balance", "recharge/orders"])
def test_personal_billing_pages_forward_session_not_account_identity(monkeypatch, resource):
    page = {"entries": [], "orders": [], "total": 0, "limit": 3, "offset": 6,
            "summary": {"account_id": 42}, "settings": {}, "page_size": 100, "max_pages": None}
    captured = {}

    def request(method, url, **kwargs):
        captured.update(method=method, url=url, **kwargs)
        return httpx.Response(200, json=page)

    monkeypatch.setattr(httpx, "request", request)
    client = PlatformBillingClient(PlatformBillingSettings(True, "http://platform-service:8081", "secret", 5))
    assert client.personal_page(resource, account_id=42, session_token="session", limit=3, offset=6,
                                trace_id="read-trace") == page
    assert captured["method"] == "POST"
    assert captured["url"].endswith(f"/internal/v1/billing/me/{resource}")
    assert captured["json"] == {"session_token": "session", "limit": 3, "offset": 6}
    assert captured["headers"]["X-Trace-Id"] == "read-trace"
    assert captured["headers"]["X-Internal-Service-Token"] == "secret"


@pytest.mark.parametrize("resource,field,value", [
    ("balance", "summary", {"account_id": 99}),
    ("balance", "entries", [{"account_id": 99}]),
    ("recharge/orders", "orders", [{"account_id": 99}]),
    ("balance", "total", -1),
    ("balance", "total", True),
    ("balance", "limit", 5),
    ("balance", "offset", 1),
    ("balance", "max_pages", 5),
    ("balance", "entries", [{"account_id": 42}, {"account_id": 42}]),
])
def test_personal_billing_pages_reject_invalid_or_cross_account_data(monkeypatch, resource, field, value):
    page = {"entries": [], "orders": [], "total": 0, "limit": 1, "offset": 0,
            "summary": {"account_id": 42}, "settings": {}, "page_size": 100, "max_pages": None}
    page[field] = value
    monkeypatch.setattr(httpx, "request", lambda *args, **kwargs: httpx.Response(200, json=page))
    client = PlatformBillingClient(PlatformBillingSettings(True, "http://platform-service:8081", "secret", 5))
    with pytest.raises(PlatformBillingError, match="无效分页数据"):
        client.personal_page(resource, account_id=42, session_token="session", limit=1, offset=0)


@pytest.mark.parametrize("status,exception", [(502, PlatformBillingUnavailableError), (200, PlatformBillingError)])
def test_platform_billing_handles_non_json_response_without_false_success(monkeypatch, status, exception):
    monkeypatch.setattr(httpx, "request", lambda *args, **kwargs: httpx.Response(status, text="<html>Error</html>"))
    client = PlatformBillingClient(PlatformBillingSettings(True, "http://platform-service:8081", "secret", 5))
    with pytest.raises(exception) as error:
        client.personal_page("balance", account_id=42, session_token="session", limit=1, offset=0)
    assert error.value.status_code is None


@pytest.mark.parametrize("resource", ["balance", "recharge/orders"])
@pytest.mark.parametrize("available", [True, False])
def test_python_personal_billing_routes_never_fall_back_to_local_queries(tmp_path, monkeypatch, resource, available):
    app = create_web_app(env_file=tmp_path / "missing.env")
    with TestClient(app) as browser:
        registered = browser.post("/api/auth/register", json={"email": "wallet@example.com", "password": "password-123"})
        assert registered.status_code == 200, registered.text
        login = browser.post("/api/auth/login", json={"email": "wallet@example.com", "password": "password-123"})
        assert login.status_code == 200, login.text
        account_id = login.json()["account"]["id"]

        def request(method, url, **kwargs):
            assert kwargs["json"]["session_token"]
            if not available:
                return httpx.Response(503, text="temporarily unavailable")
            return httpx.Response(200, json={"entries": [], "orders": [], "total": 0, "limit": 1, "offset": 0,
                                            "summary": {"account_id": account_id}, "settings": {},
                                            "page_size": 100, "max_pages": None})

        monkeypatch.setattr(httpx, "request", request)
        app.state.backend.platform_billing_client = PlatformBillingClient(
            PlatformBillingSettings(True, "http://platform-service:8081", "secret", 5))
        for name in ("get_account_balance_summary", "list_account_balance_ledger", "count_account_balance_ledger",
                     "list_recharge_orders", "count_recharge_orders"):
            monkeypatch.setattr(app.state.backend.store, name, lambda *args, **kwargs: pytest.fail("Local billing fallback"))
        response = browser.get(f"/api/me/{resource}?limit=1&account_id=99")
        assert response.status_code == (200 if available else 503), response.text
        if available:
            assert response.json()["total"] == 0


def test_platform_billing_settings_are_disabled_and_validated_by_default(tmp_path):
    disabled = load_platform_billing_settings(tmp_path / "missing.env", environ={})

    assert disabled == PlatformBillingSettings()

    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            [
                "JOB_AGENT_JAVA_BILLING_ENABLED=true",
                "JOB_AGENT_JAVA_BILLING_BASE_URL=http://platform-service:8081/",
                "JOB_AGENT_JAVA_BILLING_INTERNAL_TOKEN=platform-secret",
                "JOB_AGENT_JAVA_BILLING_TIMEOUT_SECONDS=7",
            ]
        ),
        encoding="utf-8",
    )
    enabled = load_platform_billing_settings(env_file, environ={})

    assert enabled.enabled is True
    assert enabled.base_url == "http://platform-service:8081/"
    assert enabled.timeout_seconds == 7


def test_personal_recharge_forwards_decimal_session_and_checks_response_owner(monkeypatch):
    captured = {}
    response = {"order": {"id": 9, "account_id": 42, "status": "paid", "payment_provider": "simulated"},
                "entry": {"account_id": 42, "recharge_order_id": 9, "entry_kind": "recharge"},
                "summary": {"account_id": 42}}

    def request(method, url, **kwargs):
        captured.update(method=method, url=url, **kwargs)
        return httpx.Response(200, json=response)

    monkeypatch.setattr(httpx, "request", request)
    client = PlatformBillingClient(PlatformBillingSettings(True, "http://platform-service:8081", "secret", 5))
    args = dict(account_id=42, session_token="session", amount_yuan="1.000001", note=None,
                idempotency_key="personal-client-0001", trace_id="recharge-trace")
    assert client.personal_recharge(**args) == response
    assert captured["url"].endswith("/internal/v1/billing/me/recharge")
    assert captured["json"] == {"session_token": "session", "payload": {
        "amount_yuan": "1.000001", "note": None, "idempotency_key": "personal-client-0001"}}
    assert captured["headers"]["X-Trace-Id"] == "recharge-trace"
    for section in ("order", "entry", "summary"):
        response[section]["account_id"] = 99
        with pytest.raises(PlatformBillingError, match="无效充值数据"):
            client.personal_recharge(**args)
        response[section]["account_id"] = 42


@pytest.mark.parametrize("status", [200, 400, 409, 503])
def test_python_personal_recharge_is_only_an_adapter_in_java_mode(tmp_path, monkeypatch, status):
    env = tmp_path / ".env"
    env.write_text("JOB_AGENT_DEMO_RECHARGE_ENABLED=false\n", encoding="utf-8")
    app = create_web_app(env_file=env)
    with TestClient(app) as browser:
        credentials = {"email": "recharge-adapter@example.com", "password": "password-123"}
        assert browser.post("/api/auth/register", json=credentials).status_code == 200
        account_id = browser.post("/api/auth/login", json=credentials).json()["account"]["id"]
        captured = {}

        def request(method, url, **kwargs):
            captured.update(kwargs["json"])
            if status != 200:
                return httpx.Response(status, json={"code": "INVALID_REQUEST", "message": "Java rejected recharge"})
            return httpx.Response(200, json={
                "order": {"id": 9, "account_id": account_id, "status": "paid", "payment_provider": "simulated"},
                "entry": {"account_id": account_id, "recharge_order_id": 9, "entry_kind": "recharge"},
                "summary": {"account_id": account_id}})

        monkeypatch.setattr(httpx, "request", request)
        app.state.backend.platform_billing_client = PlatformBillingClient(
            PlatformBillingSettings(True, "http://platform-service:8081", "secret", 5))
        for name in ("create_simulated_recharge_order", "get_account_balance_summary"):
            monkeypatch.setattr(app.state.backend.store, name, lambda *args, **kwargs: pytest.fail("Local recharge fallback"))
        result = browser.post("/api/me/balance/recharge", json={
            "amount_yuan": "1.000001", "idempotency_key": "personal-adapter-0001", "account_id": 99})
        assert result.status_code == status, result.text
        assert captured["payload"]["amount_yuan"] == "1.000001"
        assert "account_id" not in captured


def test_platform_billing_requires_endpoint_and_token(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("JOB_AGENT_JAVA_BILLING_ENABLED=true\n", encoding="utf-8")

    with pytest.raises(ValueError, match="BASE_URL"):
        load_platform_billing_settings(env_file, environ={})

    env_file.write_text(
        "JOB_AGENT_JAVA_BILLING_ENABLED=true\n"
        "JOB_AGENT_JAVA_BILLING_BASE_URL=http://platform-service:8081\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="INTERNAL_TOKEN"):
        load_platform_billing_settings(env_file, environ={})


def test_platform_client_sends_idempotent_charge_headers(monkeypatch):
    calls: list[dict[str, object]] = []

    def fake_request(method, url, *, headers, json=None, timeout):
        calls.append({"method": method, "url": url, "headers": headers, "json": json, "timeout": timeout})
        return httpx.Response(
            200,
            json={
                "account_id": 42,
                "balance_micro_yuan": 8_000_000,
                "ledger_entry_id": 9,
                "replayed": False,
                "charged_micro_yuan": 2_000_000,
            },
        )

    monkeypatch.setattr(httpx, "request", fake_request)
    client = PlatformBillingClient(
        PlatformBillingSettings(True, "http://platform-service:8081/", "secret", 7)
    )

    result = client.consume(
        account_id=42,
        amount_micro_yuan=2_000_000,
        token_count=80_000,
        source_reference="call-20260927-0001",
        description="agent_chat 扣费",
        trace_id="trace-42",
    )

    assert result.ledger_entry_id == 9
    assert calls[0]["url"] == "http://platform-service:8081/internal/v1/billing/consume"
    assert calls[0]["headers"] == {
        "X-Internal-Service-Token": "secret",
        "X-Trace-Id": "trace-42",
        "Idempotency-Key": "call-20260927-0001",
    }
    assert calls[0]["timeout"] == 7
    assert calls[0]["json"]["source_reference"] == "call-20260927-0001"


def test_platform_client_sends_recharge_limits_and_returns_projection(monkeypatch):
    calls: list[dict[str, object]] = []

    def fake_request(method, url, *, headers, json=None, timeout):
        calls.append({"method": method, "url": url, "headers": headers, "json": json, "timeout": timeout})
        return httpx.Response(
            200,
            json={
                "account_id": 42,
                "balance_micro_yuan": 10_000_000,
                "total_recharge_micro_yuan": 10_000_000,
                "total_consumed_micro_yuan": 0,
                "ledger_entry_count": 1,
            },
        )

    monkeypatch.setattr(httpx, "request", fake_request)
    client = PlatformBillingClient(
        PlatformBillingSettings(True, "http://platform-service:8081", "secret", 5)
    )

    result = client.recharge(
        account_id=42,
        amount_micro_yuan=10_000_000,
        source_reference="recharge-20260927-0001",
        actor_account_id=42,
        description="个人中心模拟充值",
        max_amount_micro_yuan=20_000_000,
        max_total_micro_yuan=100_000_000,
    )

    assert result.balance_micro_yuan == 10_000_000
    assert calls[0]["url"] == "http://platform-service:8081/internal/v1/billing/recharge"
    assert calls[0]["headers"]["Idempotency-Key"] == "recharge-20260927-0001"
    assert calls[0]["json"]["max_amount_micro_yuan"] == 20_000_000


def test_platform_client_preserves_business_error_and_maps_network_failure(monkeypatch):
    monkeypatch.setattr(
        httpx,
        "request",
        lambda *args, **kwargs: httpx.Response(
            409,
            json={"code": "INSUFFICIENT_BALANCE", "message": "余额不足，请先充值后重试"},
        ),
    )
    client = PlatformBillingClient(
        PlatformBillingSettings(True, "http://platform-service:8081", "secret", 5)
    )
    with pytest.raises(PlatformBillingError) as error:
        client.consume(
            account_id=42,
            amount_micro_yuan=1,
            token_count=1,
            source_reference="call-20260927-0002",
            description="test",
        )
    assert error.value.code == "INSUFFICIENT_BALANCE"
    assert str(error.value) == "余额不足，请先充值后重试"

    def unavailable(*args, **kwargs):
        raise httpx.ConnectTimeout("test timeout")

    monkeypatch.setattr(httpx, "request", unavailable)
    with pytest.raises(PlatformBillingUnavailableError):
        client.get_balance(42)

    monkeypatch.setattr(
        httpx,
        "request",
        lambda *args, **kwargs: httpx.Response(503, json={"code": "TEMPORARY_FAILURE"}),
    )
    with pytest.raises(PlatformBillingUnavailableError):
        client.get_balance(42)


def test_platform_credit_forwards_session_idempotency_and_audit_trace(monkeypatch):
    captured = {}

    def credit_request(method, url, **kwargs):
        captured.update(method=method, url=url, **kwargs)
        return httpx.Response(200, json={
            "account_id": 42, "balance_micro_yuan": 9_000_000, "ledger_entry_id": 12, "replayed": False,
        })

    monkeypatch.setattr(httpx, "request", credit_request)
    client = PlatformBillingClient(PlatformBillingSettings(True, "http://platform-service:8081", "secret", 5))
    result = client.credit(account_id=42, actor_account_id=1, amount_micro_yuan=1_000_000,
                           idempotency_key="manual-credit-test-0001", reason="payment correction",
                           session_token="administrator-session", trace_id="credit-trace")
    assert result.ledger_entry_id == 12
    assert captured["url"].endswith("/internal/v1/billing/credit")
    assert captured["headers"]["Idempotency-Key"] == captured["json"]["idempotency_key"]
    assert captured["headers"]["X-Trace-Id"] == "credit-trace"
    assert captured["json"]["session_token"] == "administrator-session"


@pytest.mark.parametrize("field,value", [("account_id", 43), ("replayed", "false"), ("ledger_entry_id", 0)])
def test_platform_credit_rejects_invalid_response_identity(monkeypatch, field, value):
    response = {"account_id": 42, "balance_micro_yuan": 9_000_000, "ledger_entry_id": 12, "replayed": False}
    response[field] = value
    monkeypatch.setattr(httpx, "request", lambda *args, **kwargs: httpx.Response(200, json=response))
    client = PlatformBillingClient(PlatformBillingSettings(True, "http://platform-service:8081", "secret", 5))
    with pytest.raises(PlatformBillingError, match="无效补款数据"):
        client.credit(account_id=42, actor_account_id=1, amount_micro_yuan=1_000_000,
                      idempotency_key="manual-credit-test-0001", reason="reason", session_token="session")


@pytest.mark.parametrize("status,code,exception", [
    (503, "UNAVAILABLE", PlatformBillingUnavailableError),
    (403, "ADMIN_REQUIRED", PlatformBillingError),
    (409, "IDEMPOTENCY_CONFLICT", IdempotencyConflictError),
])
def test_java_credit_failure_never_falls_back_to_local_balance_write(database_url, monkeypatch, status, code, exception):
    store = SQLAlchemyStore(database_url)
    actor = store.create_account("remote-credit-admin@example.com", "unused-hash", role="admin")
    target = store.create_account("remote-credit-target@example.com", "unused-hash")
    before = store.get_account_balance_summary(target.id)
    event = AdminAuditEventRecord(id=0, actor_account_id=actor.id, target_account_id=target.id,
                                 action="balance.manual_credit", target_type="account_balance", target_id=str(target.id),
                                 outcome="succeeded", summary="manual credit", request_id="credit-trace")
    monkeypatch.setattr(httpx, "request", lambda *args, **kwargs: httpx.Response(
        status, json={"code": code, "message": "credit rejected"},
    ))
    store.configure_platform_billing(PlatformBillingClient(
        PlatformBillingSettings(True, "http://platform-service:8081", "secret", 5),
    ))
    try:
        with pytest.raises(exception):
            store.credit_account_balance_with_audit(target.id, 5, actor_account_id=actor.id, reason="reason",
                                                    idempotency_key="remote-credit-failure-0001", audit_event=event,
                                                    session_token="session")
        store.configure_platform_billing(None)
        assert store.get_account_balance_summary(target.id).balance_micro_yuan == before.balance_micro_yuan
        assert store.list_account_balance_ledger(target.id) == []
        assert store.list_admin_audit_events() == []
    finally:
        store.close()

class FakePlatformBilling:
    def __init__(self, *, insufficient: bool = False):
        self.insufficient = insufficient
        self.charges: list[dict[str, object]] = []

    def get_balance(self, account_id: int) -> PlatformBalanceProjection:
        consumed = sum(int(item["amount_micro_yuan"]) for item in self.charges)
        return PlatformBalanceProjection(
            account_id,
            max(0, 10_000_000 - consumed),
            10_000_000,
            consumed,
            len(self.charges),
        )

    def consume(self, **kwargs):
        self.charges.append(kwargs)
        if self.insufficient:
            raise PlatformBillingError("INSUFFICIENT_BALANCE", "余额不足，请先充值后重试", 409)
        return None


def test_repository_uses_java_as_the_only_balance_writer_when_enabled(database_url):
    store = SQLAlchemyStore(database_url)
    store.initialize()
    fake = FakePlatformBilling()
    store.configure_billing(BillingSettings(price_per_million_tokens_yuan=25))
    store.configure_platform_billing(fake)
    account = store.create_account("java-billing@example.com", "not-used")

    store.record_usage_event(
        UsageEventRecord(
            id=0,
            account_id=account.id,
            candidate_id=None,
            session_id=None,
            root_request_id="root-java-billing-1",
            call_id="call-java-billing-1",
            provider="relay",
            model="chat-model",
            operation="agent_chat",
            input_tokens=80_000,
            output_tokens=0,
            total_tokens=80_000,
            usage_source="provider",
            status="succeeded",
            attempt=1,
            provider_request_id=None,
            raw_usage={"total_tokens": 80_000},
            created_at="2026-09-27T00:00:00+00:00",
            billable=True,
            pricing_version=None,
        )
    )

    assert fake.charges[0]["amount_micro_yuan"] == 2_000_000
    assert fake.charges[0]["source_reference"] == "call-java-billing-1"
    assert store.get_account_balance_summary(account.id).balance_micro_yuan == 8_000_000
    store.close()


def test_balance_reads_use_java_projection_when_platform_billing_is_enabled(database_url):
    """Java 账务开启后，余额查询不能返回旧的 Python 本地投影。"""

    store = SQLAlchemyStore(database_url)
    store.initialize()
    store.configure_billing(BillingSettings(price_per_million_tokens_yuan=25))
    store.configure_platform_billing(FakePlatformBilling())
    account = store.create_account("java-billing-read@example.com", "not-used")

    summary = store.get_account_balance_summary(account.id)

    assert summary.account_id == account.id
    assert summary.balance_micro_yuan == 10_000_000
    assert summary.total_recharge_micro_yuan == 10_000_000
    assert summary.total_consumed_micro_yuan == 0
    assert summary.ledger_entry_count == 0
    assert summary.state == "low_balance"
    assert summary.state_label == "低余额"
    store.close()


def test_remote_insufficient_balance_is_exposed_as_existing_domain_error(database_url):
    store = SQLAlchemyStore(database_url)
    store.initialize()
    store.configure_billing(BillingSettings(price_per_million_tokens_yuan=25))
    store.configure_platform_billing(FakePlatformBilling(insufficient=True))
    account = store.create_account("java-billing-insufficient@example.com", "not-used")

    with pytest.raises(InsufficientBalanceError, match="余额不足，请先充值后重试"):
        store.record_usage_event(
            UsageEventRecord(
                id=0,
                account_id=account.id,
                candidate_id=None,
                session_id=None,
                root_request_id="root-java-billing-2",
                call_id="call-java-billing-2",
                provider="relay",
                model="chat-model",
                operation="agent_chat",
                input_tokens=1,
                output_tokens=0,
                total_tokens=1,
                usage_source="provider",
                status="succeeded",
                attempt=1,
                provider_request_id=None,
                raw_usage={"total_tokens": 1},
                created_at="2026-09-27T00:00:00+00:00",
                billable=True,
                pricing_version=None,
            )
        )
    assert len(store.list_usage_events(account_id=account.id)) == 1
    store.close()
