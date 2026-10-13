from __future__ import annotations

import httpx
import pytest

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
