from __future__ import annotations

import httpx
import pytest
from fastapi.testclient import TestClient

from job_hunting_agent.config import PlatformAuthSettings, load_platform_auth_settings
from job_hunting_agent.platform_auth import (
    PlatformAuthClient,
    PlatformAuthError,
    PlatformAuthUnavailableError,
)
from job_hunting_agent.web import create_web_app


@pytest.mark.parametrize("resource", ["summary", "events"])
@pytest.mark.parametrize("status", [200, 403, 503])
def test_usage_adapter_bypasses_cache_local_reads_and_cleanup(tmp_path, monkeypatch, resource, status):
    app = create_web_app(env_file=tmp_path / "missing.env")
    with TestClient(app) as browser:
        credentials = {"email": "usage-adapter@example.com", "password": "password-123"}
        assert browser.post("/api/auth/register", json=credentials).status_code == 200
        assert browser.post("/api/auth/login", json=credentials).status_code == 200
        payload = {"page_size": 100, "max_pages": 5}
        if resource == "summary":
            payload.update(summary={}, by_account=[], tool_calls_by_account=[], billing={"summary": {}, "settings": {}, "by_account": []})
        else:
            payload.update(events=[], total=0, limit=2, offset=4)

        def request(url, **kwargs):
            if url.endswith("/internal/v1/auth/accounts/me"):
                return httpx.Response(401, json={"code": "SESSION_EXPIRED", "message": "expired"})
            assert url.endswith(f"/internal/v1/admin/usage/{resource}")
            assert kwargs["json"]["session_token"]
            assert kwargs["headers"]["X-Trace-Id"] == "usage-adapter-trace"
            if resource == "events":
                assert kwargs["json"]["account_id"] == 3 and kwargs["json"]["candidate_id"] == 7
                assert kwargs["json"]["session_id"] == "a'b"
            return httpx.Response(status, json=payload if status == 200 else {"code": "ADMIN_REQUIRED", "message": "not allowed"})

        monkeypatch.setattr(httpx, "post", request)
        app.state.backend.platform_auth_client = PlatformAuthClient(PlatformAuthSettings(True, "http://java", "secret", 5))
        for method in ("summarize_usage", "list_usage_events", "prune_usage_events_to_limit", "prune_tool_call_traces_to_limit"):
            monkeypatch.setattr(app.state.backend.store, method, lambda *a, **k: pytest.fail("Local usage fallback"))
        monkeypatch.setattr(app.state.backend, "cached_admin_usage_summary", lambda *a: pytest.fail("Stale cache"))
        query = "?account_id=3&candidate_id=7&session_id=a%27b&limit=2&offset=4" if resource == "events" else ""
        response = browser.get(f"/api/admin/usage/{resource}{query}", headers={"X-Request-ID": "usage-adapter-trace"})
        assert response.status_code == status, response.text
        if status == 200:
            assert response.json() == payload


@pytest.mark.parametrize("response", [httpx.Response(200, text="html"), httpx.Response(200, json={}),
                                     httpx.Response(200, json={"events": [], "total": True, "limit": 1, "offset": 0, "page_size": 100, "max_pages": 5})])
def test_usage_client_rejects_malformed_success(monkeypatch, response):
    monkeypatch.setattr(httpx, "post", lambda *a, **k: response)
    client = PlatformAuthClient(PlatformAuthSettings(True, "http://java", "secret", 5))
    with pytest.raises(PlatformAuthError) as error:
        client.admin_usage("events", session_token="opaque", limit=1, offset=0)
    assert error.value.status_code == 502


def test_usage_client_rejects_missing_session_and_network_failure(monkeypatch):
    client = PlatformAuthClient(PlatformAuthSettings(True, "http://java", "secret", 5))
    with pytest.raises(PlatformAuthError) as error:
        client.admin_usage("summary", session_token=None)
    assert error.value.status_code == 401
    def request(*a, **k):
        raise httpx.ConnectError("offline")
    monkeypatch.setattr(httpx, "post", request)
    with pytest.raises(PlatformAuthUnavailableError):
        client.admin_usage("summary", session_token="opaque")


def test_audit_client_forwards_real_session_and_trace_and_validates_page(monkeypatch):
    captured = {}
    result = {"events": [{"id": 1, "details": {}, "created_at": "2026-10-16T00:00:00+00:00"}], "limit": 2, "offset": 4}

    def request(url, **kwargs):
        captured.update(url=url, **kwargs)
        return httpx.Response(200, json=result)

    monkeypatch.setattr(httpx, "post", request)
    client = PlatformAuthClient(PlatformAuthSettings(True, "http://platform-service:8081", "secret", 5))
    assert client.audit_events(session_token="session", limit=2, offset=4, trace_id="audit-trace") == result
    assert captured["url"].endswith("/internal/v1/auth/admin/audit/events")
    assert captured["json"] == {"session_token": "session", "limit": 2, "offset": 4}
    assert captured["headers"]["X-Trace-Id"] == "audit-trace"
    for field, invalid in (("events", None), ("events", [{"id": True, "details": {}, "created_at": "date"}]), ("limit", 3)):
        previous = result[field]
        result[field] = invalid
        with pytest.raises(PlatformAuthError) as error:
            client.audit_events(session_token="session", limit=2, offset=4)
        assert error.value.status_code == 502
        result[field] = previous


@pytest.mark.parametrize("status", [200, 403, 503])
def test_java_audit_web_adapter_never_reads_local_audit(tmp_path, monkeypatch, status):
    app = create_web_app(env_file=tmp_path / "missing.env")
    with TestClient(app) as browser:
        credentials = {"email": "audit-adapter@example.com", "password": "password-123"}
        assert browser.post("/api/auth/register", json=credentials).status_code == 200
        assert browser.post("/api/auth/login", json=credentials).status_code == 200

        def request(url, **kwargs):
            assert kwargs["json"]["session_token"]
            assert kwargs["headers"]["X-Trace-Id"] == "audit-adapter-trace"
            return httpx.Response(status, json={"events": [], "limit": 2, "offset": 4,
                                                "code": "ADMIN_REQUIRED", "message": "not allowed"})

        monkeypatch.setattr(httpx, "post", request)
        app.state.backend.platform_auth_client = PlatformAuthClient(
            PlatformAuthSettings(True, "http://platform-service:8081", "secret", 5))
        monkeypatch.setattr(app.state.backend.store, "list_admin_audit_events", lambda *args, **kwargs: pytest.fail("Local audit fallback"))
        response = browser.get("/api/admin/audit/events?limit=2&offset=4", headers={"X-Request-ID": "audit-adapter-trace"})
        assert response.status_code == status, response.text


def test_audit_client_maps_html_outage_and_malformed_success(monkeypatch):
    client = PlatformAuthClient(PlatformAuthSettings(True, "http://platform-service:8081", "secret", 5))
    monkeypatch.setattr(httpx, "post", lambda *args, **kwargs: httpx.Response(503, text="<html>Error</html>"))
    with pytest.raises(PlatformAuthUnavailableError):
        client.audit_events(session_token="session", limit=50, offset=0)
    monkeypatch.setattr(httpx, "post", lambda *args, **kwargs: httpx.Response(200, text="<html>Error</html>"))
    with pytest.raises(PlatformAuthError) as error:
        client.audit_events(session_token="session", limit=50, offset=0)
    assert error.value.status_code == 502


def test_platform_auth_settings_are_disabled_and_validated_by_default(tmp_path):
    disabled = load_platform_auth_settings(tmp_path / "missing.env", environ={})

    assert disabled == PlatformAuthSettings()

    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            [
                "JOB_AGENT_JAVA_AUTH_ENABLED=true",
                "JOB_AGENT_JAVA_AUTH_BASE_URL=http://platform-service:8081/",
                "JOB_AGENT_JAVA_AUTH_INTERNAL_TOKEN=platform-secret",
                "JOB_AGENT_JAVA_AUTH_TIMEOUT_SECONDS=7",
            ]
        ),
        encoding="utf-8",
    )
    enabled = load_platform_auth_settings(env_file, environ={})

    assert enabled.enabled is True
    assert enabled.base_url == "http://platform-service:8081/"
    assert enabled.timeout_seconds == 7


def test_platform_auth_requires_endpoint_and_token(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("JOB_AGENT_JAVA_AUTH_ENABLED=true\n", encoding="utf-8")

    with pytest.raises(ValueError, match="BASE_URL"):
        load_platform_auth_settings(env_file, environ={})

    env_file.write_text(
        "JOB_AGENT_JAVA_AUTH_ENABLED=true\n"
        "JOB_AGENT_JAVA_AUTH_BASE_URL=http://platform-service:8081\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="INTERNAL_TOKEN"):
        load_platform_auth_settings(env_file, environ={})


def test_platform_auth_client_sends_internal_request_without_logging_password(monkeypatch):
    calls: list[dict[str, object]] = []

    def fake_post(url, *, headers, json=None, timeout):
        calls.append({"url": url, "headers": headers, "json": json, "timeout": timeout})
        return httpx.Response(200, json={"account_id": 42, "session_token": "a" * 64})

    monkeypatch.setattr(httpx, "post", fake_post)
    client = PlatformAuthClient(
        PlatformAuthSettings(True, "http://platform-service:8081/", "secret", 7)
    )

    result = client.session(
        "login",
        email="user@example.com",
        password="password-123",
        email_verification_required=True,
        trace_id="trace-42",
    )

    assert result["account_id"] == 42
    assert calls == [
        {
            "url": "http://platform-service:8081/internal/v1/auth/sessions/login",
            "headers": {"X-Internal-Service-Token": "secret", "X-Trace-Id": "trace-42"},
            "json": {
                "email": "user@example.com",
                "password": "password-123",
                "email_verification_required": True,
            },
            "timeout": 7,
        }
    ]


def test_platform_auth_client_registers_account_and_passes_consents(monkeypatch):
    calls: list[dict[str, object]] = []

    def fake_post(url, *, headers, json=None, timeout):
        calls.append({"url": url, "headers": headers, "json": json, "timeout": timeout})
        return httpx.Response(200, json={"account_id": 43})

    monkeypatch.setattr(httpx, "post", fake_post)
    client = PlatformAuthClient(
        PlatformAuthSettings(True, "http://platform-service:8081", "secret", 5)
    )

    result = client.register_account(
        email="user@example.com",
        password="password-123",
        display_name="Test User",
        email_verified=False,
        consents=[
            {
                "document_type": "terms",
                "version": "2026-01",
                "ip_address": "127.0.0.1",
                "user_agent": "pytest",
            }
        ],
        trace_id="trace-register-43",
    )

    assert result.account_id == 43
    assert calls[0]["url"] == "http://platform-service:8081/internal/v1/auth/register"
    assert calls[0]["headers"] == {
        "X-Internal-Service-Token": "secret",
        "X-Trace-Id": "trace-register-43",
    }
    assert calls[0]["json"]["consents"][0]["document_type"] == "terms"
    assert calls[0]["json"]["password"] == "password-123"


def test_platform_auth_client_maps_business_and_network_errors(monkeypatch):
    client = PlatformAuthClient(
        PlatformAuthSettings(True, "http://platform-service:8081", "secret", 5)
    )

    monkeypatch.setattr(
        httpx,
        "post",
        lambda *args, **kwargs: httpx.Response(
            401,
            json={"code": "INVALID_CREDENTIALS", "message": "邮箱或密码错误。"},
        ),
    )
    with pytest.raises(PlatformAuthError) as error:
        client.session(
            "login",
            email="missing@example.com",
            password="wrong-password",
            email_verification_required=False,
        )
    assert error.value.code == "INVALID_CREDENTIALS"
    assert error.value.status_code == 401

    def unavailable(*args, **kwargs):
        raise httpx.ConnectTimeout("test timeout")

    monkeypatch.setattr(httpx, "post", unavailable)
    with pytest.raises(PlatformAuthUnavailableError):
        client.session(
            "login",
            email="user@example.com",
            password="password-123",
            email_verification_required=False,
        )


def test_platform_auth_client_maps_malformed_success_response_to_bad_gateway(monkeypatch):
    monkeypatch.setattr(httpx, "post", lambda *args, **kwargs: httpx.Response(200, json={}))
    client = PlatformAuthClient(
        PlatformAuthSettings(True, "http://platform-service:8081", "secret", 5)
    )

    with pytest.raises(PlatformAuthError) as error:
        client.session(
            "login",
            email="user@example.com",
            password="password-123",
            email_verification_required=False,
        )
    assert error.value.status_code == 502


def test_platform_auth_client_validates_java_admin_account_projection(monkeypatch):
    safe_account = {
        "account_id": 42,
        "email": "admin@example.com",
        "display_name": "Admin",
        "role": "admin",
        "status": "active",
        "must_change_password": False,
        "email_verified_at": "2026-10-10T00:00:00Z",
        "deleted_at": None,
        "created_at": "2026-10-10T00:00:00Z",
        "updated_at": "2026-10-10T00:00:00Z",
    }
    calls: list[dict[str, object]] = []

    def fake_post(url, *, headers, json=None, timeout):
        calls.append({"url": url, "headers": headers, "json": json, "timeout": timeout})
        return httpx.Response(200, json={"accounts": [safe_account]})

    monkeypatch.setattr(httpx, "post", fake_post)
    client = PlatformAuthClient(
        PlatformAuthSettings(True, "http://platform-service:8081", "secret", 5)
    )

    result = client.account("list", session_token="opaque", trace_id="trace-admin")

    assert result["accounts"][0] == safe_account
    assert calls[0]["url"].endswith("/internal/v1/auth/accounts/list")
    assert calls[0]["json"] == {"session_token": "opaque"}
    assert "password" not in str(calls[0])


def test_platform_auth_client_reads_current_account_projection(monkeypatch):
    account = {
        "account_id": 42,
        "email": "user@example.com",
        "display_name": "User",
        "role": "user",
        "status": "active",
        "must_change_password": False,
        "email_verified_at": None,
        "deleted_at": None,
        "created_at": "2026-10-10T00:00:00Z",
        "updated_at": "2026-10-10T00:00:00Z",
    }
    calls: list[dict[str, object]] = []

    def fake_post(url, *, headers, json=None, timeout):
        calls.append({"url": url, "headers": headers, "json": json, "timeout": timeout})
        return httpx.Response(200, json={"account": account})

    monkeypatch.setattr(httpx, "post", fake_post)
    client = PlatformAuthClient(
        PlatformAuthSettings(True, "http://platform-service:8081", "secret", 5)
    )

    assert client.account("me", session_token="opaque", trace_id="trace-me")["account"] == account
    assert calls[0]["url"].endswith("/internal/v1/auth/accounts/me")
    assert calls[0]["json"] == {"session_token": "opaque"}


def test_platform_auth_client_completes_account_deletion(monkeypatch):
    calls: list[dict[str, object]] = []

    def fake_post(url, *, headers, json=None, timeout):
        calls.append({"url": url, "headers": headers, "json": json, "timeout": timeout})
        return httpx.Response(200, json={"account_id": 42, "deleted": True})

    monkeypatch.setattr(httpx, "post", fake_post)
    client = PlatformAuthClient(PlatformAuthSettings(True, "http://java", "secret", 5))

    result = client.account(
        "delete-complete",
        account_id=42,
        task_key="task-delete",
        trace_id="trace-delete-complete",
    )

    assert result == {"account_id": 42, "deleted": True}
    assert calls[0]["url"].endswith("/internal/v1/auth/accounts/delete-complete")
    assert calls[0]["json"] == {"account_id": 42, "task_key": "task-delete"}


def test_platform_auth_client_updates_current_account_profile(monkeypatch):
    account = {
        "account_id": 42,
        "email": "user@example.com",
        "display_name": "Updated User",
        "role": "user",
        "status": "active",
        "must_change_password": False,
        "email_verified_at": None,
        "deleted_at": None,
        "created_at": "2026-10-10T00:00:00Z",
        "updated_at": "2026-10-11T00:00:00Z",
    }
    calls = []

    def fake_post(url, *, headers, json=None, timeout):
        calls.append({"url": url, "headers": headers, "json": json, "timeout": timeout})
        return httpx.Response(200, json={"account": account})

    monkeypatch.setattr(httpx, "post", fake_post)
    client = PlatformAuthClient(PlatformAuthSettings(True, "http://java", "secret", 5))

    result = client.account(
        "profile", session_token="opaque", display_name="Updated User", trace_id="trace-profile"
    )

    assert result["account"] == account
    assert calls[0]["url"].endswith("/internal/v1/auth/accounts/profile")
    assert calls[0]["headers"]["X-Trace-Id"] == "trace-profile"
    assert calls[0]["json"] == {"session_token": "opaque", "display_name": "Updated User"}



def test_platform_auth_client_rejects_account_projection_with_password(monkeypatch):
    account = {
        "account_id": 42,
        "email": "admin@example.com",
        "display_name": None,
        "role": "admin",
        "status": "active",
        "must_change_password": False,
        "email_verified_at": None,
        "deleted_at": None,
        "created_at": "2026-10-10T00:00:00Z",
        "updated_at": "2026-10-10T00:00:00Z",
        "password_hash": "must-not-cross-boundary",
    }
    monkeypatch.setattr(
        httpx,
        "post",
        lambda *args, **kwargs: httpx.Response(200, json={"accounts": [account]}),
    )
    client = PlatformAuthClient(
        PlatformAuthSettings(True, "http://platform-service:8081", "secret", 5)
    )

    with pytest.raises(PlatformAuthError) as error:
        client.account("list", session_token="opaque")
    assert error.value.status_code == 502
