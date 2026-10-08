import httpx
import pytest
from fastapi.testclient import TestClient

from job_hunting_agent.config import PlatformAuthSettings
from job_hunting_agent.platform_auth import (
    PlatformAuthClient,
    PlatformAuthError,
    PlatformAuthUnavailableError,
)
from job_hunting_agent.platform_email import deliver_platform_verification
from job_hunting_agent.web import create_web_app


def test_verification_client_fails_closed(monkeypatch):
    client = PlatformAuthClient(PlatformAuthSettings(True, "http://java", "secret", 5))
    monkeypatch.setattr(httpx, "post", lambda *a, **kw: httpx.Response(200, json={}))
    with pytest.raises(PlatformAuthError, match="响应无效"):
        client.email_verification("confirm", token="opaque")

    def unavailable(*a, **kw):
        raise httpx.ConnectError("no connection")

    monkeypatch.setattr(httpx, "post", unavailable)
    with pytest.raises(PlatformAuthUnavailableError):
        client.email_verification("request", email="test@example.com")


@pytest.mark.parametrize("send_fails", [False, True])
def test_worker_reports_claim_key_and_no_sensitive_failure_text(send_fails):
    calls = []

    class Client:
        def email_verification(self, operation, **data):
            calls.append((operation, data))
            if operation == "claim":
                return {
                    "claim": {
                        "claim_key": "lease",
                        "recipient_email": "a@example.com",
                        "action_url": "https://app/login?secret",
                    }
                }
            return {"ok": True}

    class Sender:
        def send_verification(self, email, url):
            if send_fails:
                raise RuntimeError("smtp password and token must not be persisted")

    result = deliver_platform_verification(Client(), Sender(), 8)
    assert result["accepted"] is True
    assert calls[1] == (
        "finish",
        {
            "id": 8,
            "claim_key": "lease",
            "sent": not send_fails,
            "error_type": "SmtpDeliveryFailed" if send_fails else None,
        },
    )


def test_duplicate_worker_task_does_not_send():
    class Client:
        def email_verification(self, operation, **data):
            assert operation == "claim"
            return {"claim": None}

    assert deliver_platform_verification(Client(), None, 3)["status"] == "not_claimed"


def test_web_verification_never_falls_back_on_platform_outage(tmp_path):
    app = create_web_app(env_file=tmp_path / "missing.env")

    class Unavailable:
        def email_verification(self, operation, **data):
            raise PlatformAuthUnavailableError()

    app.state.backend.platform_auth_client = Unavailable()
    client = TestClient(app)
    assert (
        client.post("/api/auth/verify-email", json={"token": "x" * 43}).status_code
        == 503
    )
    assert (
        client.post(
            "/api/auth/verification/request", json={"email": "a@example.com"}
        ).status_code
        == 503
    )
    assert app.state.backend.store.list_account_email_outbox(limit=20) == []


def test_legacy_verification_does_not_starve_password_reset(tmp_path):
    app = create_web_app(env_file=tmp_path / "missing.env")
    store = app.state.backend.store
    account = store.create_account(
        email="legacy@example.com", password_hash="test-hash"
    )
    verification = app.state.account_email_outbox.enqueue(account, "verify_email", None)
    reset = app.state.account_email_outbox.enqueue(account, "reset_password", None)
    assert store.list_due_account_email_outbox(60, limit=1)[0].id == verification.id
    assert (
        store.list_due_account_email_outbox(
            60, limit=1, exclude_purpose="verify_email"
        )[0].id
        == reset.id
    )
