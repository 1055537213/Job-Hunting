"""Hybrid session authority, HTTP contracts and outage behavior."""

import httpx
import pytest
from fastapi.testclient import TestClient
from job_hunting_agent.config import PlatformAuthSettings
from job_hunting_agent.platform_auth import PlatformAuthClient, PlatformAuthError, PlatformAuthUnavailableError
from job_hunting_agent.web import create_web_app


@pytest.mark.parametrize("operation,payload", [
    ("login", {"account_id": True, "session_token": "a" * 64}),
    ("login", {"account_id": 1, "session_token": "bad;cookie"}),
    ("resolve", {}), ("resolve", {"account_id": -1}),
    ("logout", {"ok": False}), ("logout-all", {"ok": True, "revoked_sessions": True}),
])
def test_session_client_rejects_malformed_success(monkeypatch, operation, payload):
    monkeypatch.setattr(httpx, "post", lambda *a, **k: httpx.Response(200, json=payload))
    client = PlatformAuthClient(PlatformAuthSettings(True, "http://java", "secret", 5))
    with pytest.raises(PlatformAuthError) as error:
        client.session(operation, session_token="opaque")
    assert error.value.status_code == 502


def test_session_client_authenticates_and_maps_errors(monkeypatch):
    def post(url, **kwargs):
        assert url == "http://java/internal/v1/auth/sessions/resolve"
        assert kwargs["headers"] == {"X-Internal-Service-Token": "secret", "X-Trace-Id": "trace"}
        assert kwargs["json"] == {"session_token": "opaque"}
        return httpx.Response(200, json={"account_id": None})
    monkeypatch.setattr(httpx, "post", post)
    client = PlatformAuthClient(PlatformAuthSettings(True, "http://java", "secret", 5))
    assert client.session("resolve", trace_id="trace", session_token="opaque") == {"account_id": None}
    monkeypatch.setattr(httpx, "post", lambda *a, **k: httpx.Response(401, json={"code": "SESSION_EXPIRED", "message": "expired"}))
    with pytest.raises(PlatformAuthError) as error:
        client.session("resolve", session_token="opaque")
    assert error.value.status_code == 401


def test_web_uses_java_for_all_session_operations_without_python_writes(tmp_path, monkeypatch):
    app = create_web_app(env_file=tmp_path / "missing.env")
    store = app.state.backend.store
    account = store.create_account(email="session@example.com", password_hash="unused")
    calls = []

    class Java:
        def session(self, operation, **data):
            calls.append((operation, data))
            if operation == "login":
                return {"account_id": account.id, "session_token": "a" * 64}
            if operation == "resolve":
                return {"account_id": account.id}
            if operation == "logout-all":
                return {"ok": True, "revoked_sessions": 2}
            return {"ok": True}

        def account(self, operation, **data):
            calls.append(("account", data))
            if operation == "profile":
                return {"account": {
                    "account_id": account.id,
                    "email": "java-authoritative@example.com",
                    "display_name": data["display_name"],
                    "role": account.role,
                    "status": account.status,
                    "must_change_password": account.must_change_password,
                    "email_verified_at": account.email_verified_at,
                    "deleted_at": account.deleted_at,
                    "created_at": account.created_at,
                    "updated_at": account.updated_at,
                }}
            assert operation == "me"
            return {"account": {
                "account_id": account.id,
                "email": "java-authoritative@example.com",
                "display_name": "Java authoritative profile",
                "role": account.role,
                "status": account.status,
                "must_change_password": account.must_change_password,
                "email_verified_at": account.email_verified_at,
                "deleted_at": account.deleted_at,
                "created_at": account.created_at,
                "updated_at": account.updated_at,
            }}

        def email_change(self, operation, **data):
            calls.append(("email_change", data))
            assert operation == "request"
            assert data["session_token"] == "a" * 64
            assert data["email"] == "new-java@example.com"
            return {"ok": True}

    app.state.backend.platform_auth_client = Java()
    def forbidden(*a, **k):
        pytest.fail("Hybrid authentication must not read/write Python sessions or password hashes")
    for method in ("save_auth_session", "touch_auth_session", "get_auth_session_by_token_hash",
                   "get_account_by_email", "get_account_with_password", "touch_account_login",
                   "revoke_auth_session", "revoke_all_auth_sessions", "update_account_password_and_revoke_sessions"):
        monkeypatch.setattr(store, method, forbidden)
    with TestClient(app) as client:
        logged_in = client.post("/api/auth/login", json={"email": account.email, "password": "password-123"})
        assert logged_in.status_code == 200
        assert "HttpOnly" in logged_in.headers["set-cookie"]
        before = len(calls)
        auth_me = client.get("/api/auth/me").json()
        assert auth_me["authenticated"] is True
        assert auth_me["account"]["email"] == "java-authoritative@example.com"
        assert [c[0] for c in calls[before:]] == ["account"]
        profile = client.patch(
            "/api/account/profile",
            headers={"X-CSRF-Token": auth_me["csrf_token"]},
            json={"display_name": "Updated User"},
        )
        assert profile.status_code == 200
        assert profile.json()["account"]["display_name"] == "Updated User"
        email_change = client.patch(
            "/api/account/email",
            headers={"X-CSRF-Token": auth_me["csrf_token"]},
            json={"new_email": "new-java@example.com"},
        )
        assert email_change.status_code == 200
        assert client.post("/api/account/password", json={"current_password": "password-123", "new_password": "new-password-123"}).status_code == 200
        assert "job_agent_session" not in client.cookies
        client.cookies.set("job_agent_session", "a" * 64)
        assert client.post("/api/auth/logout-all").json()["revoked_sessions"] == 2
        client.cookies.set("job_agent_session", "a" * 64)
        assert client.post("/api/auth/logout").status_code == 200
    assert {c[0] for c in calls} == {"login", "account", "email_change", "change-password", "logout", "logout-all"}


def test_java_outage_is_not_reported_as_logged_out_or_fallback_auth(tmp_path, monkeypatch):
    app = create_web_app(env_file=tmp_path / "missing.env")
    class Java:
        def session(self, *a, **k):
            raise PlatformAuthUnavailableError()

        def account(self, *a, **k):
            raise PlatformAuthUnavailableError()
    app.state.backend.platform_auth_client = Java()
    def forbidden(*a, **k):
        pytest.fail("Must not fall back to local authentication")
    monkeypatch.setattr(app.state.backend.store, "get_auth_session_by_token_hash", forbidden)
    with TestClient(app) as client:
        client.cookies.set("job_agent_session", "a" * 64)
        assert client.get("/api/auth/me").status_code == 503
        assert client.get("/api/me/balance").status_code == 503
        assert client.post("/api/auth/logout").status_code == 503
