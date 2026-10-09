"""Java-owned reset routes, SMTP delivery and session admission regressions."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

from job_hunting_agent.auth import hash_password
from job_hunting_agent.background_tasks import dispatch_due_account_emails
from job_hunting_agent.config import PlatformAuthSettings
from job_hunting_agent.platform_auth import PlatformAuthClient, PlatformAuthUnavailableError
from job_hunting_agent.platform_email import deliver_platform_account_email
from job_hunting_agent.web import create_web_app


def test_reset_client_uses_authenticated_internal_contract(monkeypatch):
    def post(url, **kwargs):
        assert url == "http://java/internal/v1/auth/password-reset/confirm"
        assert kwargs["headers"]["X-Internal-Service-Token"] == "internal"
        assert kwargs["json"] == {"token": "opaque", "new_password": "new-password-123"}
        return httpx.Response(200, json={"account_id": 1})

    monkeypatch.setattr(httpx, "post", post)
    client = PlatformAuthClient(PlatformAuthSettings(True, "http://java", "internal", 5))
    assert client.password_reset("confirm", token="opaque", new_password="new-password-123") == {"account_id": 1}


def test_reset_routes_fail_closed_without_python_token_or_password_writes(tmp_path):
    app = create_web_app(env_file=tmp_path / "missing.env")
    store = app.state.backend.store
    account = store.create_account(email="a@example.com", password_hash="unchanged-hash")

    class Unavailable:
        def password_reset(self, operation, **data):
            raise PlatformAuthUnavailableError()

    app.state.backend.platform_auth_client = Unavailable()
    with TestClient(app) as client:
        assert client.post("/api/auth/password-reset/request", json={"email": account.email}).status_code == 503
        assert client.post("/api/auth/password-reset/confirm", json={"token": "x" * 43, "new_password": "new-password-123"}).status_code == 503
    assert store.get_account_with_password(account.id)[1] == "unchanged-hash"
    assert store.list_account_email_outbox(limit=20) == []


@pytest.mark.parametrize("send_fails", [False, True])
def test_reset_worker_reports_only_fenced_low_sensitive_result(send_fails):
    calls = []

    class Client:
        def password_reset(self, operation, **data):
            calls.append((operation, data))
            if operation == "claim":
                return {"claim": {"claim_key": "lease", "recipient_email": "a@example.com", "action_url": "https://app/login?reset_password_token=opaque"}}
            return {"ok": True}

    class Sender:
        def send_password_reset(self, email, url):
            assert email == "a@example.com"
            assert "reset_password_token=" in url
            if send_fails:
                raise RuntimeError("private SMTP password must not be reported")

    result = deliver_platform_account_email(Client(), Sender(), 8, "reset_password")
    assert result["accepted"] is True
    assert calls[1] == ("finish", {"id": 8, "claim_key": "lease", "sent": not send_fails, "error_type": "SmtpDeliveryFailed" if send_fails else None})


def test_java_dispatch_does_not_process_legacy_mail_and_carries_only_ids(monkeypatch, tmp_path):
    import job_hunting_agent.background_tasks as tasks

    settings = PlatformAuthSettings(True, "http://java", "internal", 5)
    monkeypatch.setattr(tasks, "load_platform_auth_settings", lambda *args: settings)
    calls = []

    class Store:
        def list_due_account_email_outbox(self, *args, **kwargs):
            pytest.fail("Legacy mail must not be dispatched in Java mode")

    class Client:
        def __init__(self, settings):
            pass

        def email_verification(self, operation):
            return {"records": [{"id": 1, "attempt_count": 0}]}

        def password_reset(self, operation):
            return {"records": [{"id": 2, "attempt_count": 1}]}

    monkeypatch.setattr(tasks, "PlatformAuthClient", Client)
    queue = SimpleNamespace(send_task=lambda *args, **kwargs: calls.append(kwargs))
    assert dispatch_due_account_emails(Store(), queue, tmp_path / "missing.env") == {"dispatched": 2, "dispatch_failed": 0}
    assert [(call["args"], call["kwargs"]) for call in calls] == [([1], {"purpose": "verify_email"}), ([2], {"purpose": "reset_password"})]


def test_java_login_failure_cannot_create_python_session(tmp_path):
    app = create_web_app(env_file=tmp_path / "missing.env")
    store = app.state.backend.store
    account = store.create_account(email="race@example.com", password_hash=hash_password("old-password-123"))

    class Unavailable:
        def session(self, operation, **kwargs):
            assert operation == "login"
            raise PlatformAuthUnavailableError()

    app.state.backend.platform_auth_client = Unavailable()
    with TestClient(app) as client:
        result = client.post("/api/auth/login", json={"email": account.email, "password": "old-password-123"})
        assert result.status_code == 503
        assert not result.cookies
    with store.connect() as conn:
        assert conn.execute("SELECT count(*) AS n FROM auth_sessions WHERE account_id = ?", (account.id,)).fetchone()["n"] == 0


def test_session_admission_accepts_current_credential_and_rejects_stale_snapshot(tmp_path):
    app = create_web_app(env_file=tmp_path / "missing.env")
    store = app.state.backend.store
    account = store.create_account(email="admission@example.com", password_hash="original-hash")
    now = datetime.now(UTC)
    kwargs = dict(account_id=account.id, created_at=now.isoformat(), last_seen_at=now.isoformat(),
                  expires_at=(now + timedelta(hours=1)).isoformat(), absolute_expires_at=(now + timedelta(days=1)).isoformat())
    session = store.save_auth_session(**kwargs, token_hash="a" * 64, expected_password_hash="original-hash")
    store.update_account_password_and_revoke_sessions(account.id, "changed-hash")
    assert store.get_auth_session(session.id).revoked_at is not None
    with pytest.raises(ValueError, match="credentials changed"):
        store.save_auth_session(**kwargs, token_hash="b" * 64, expected_password_hash="original-hash")
    assert store.get_auth_session_by_token_hash("b" * 64) is None
