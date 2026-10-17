import httpx
import pytest
from fastapi.testclient import TestClient

from job_hunting_agent.config import PlatformAuthSettings
from job_hunting_agent.platform_auth import PlatformAuthClient
from job_hunting_agent.web import create_web_app


@pytest.mark.parametrize("resource", ["profile", "email"])
@pytest.mark.parametrize("status", [200, 400, 401, 409, 422, 503])
def test_account_compatibility_routes_only_forward_to_java(tmp_path, monkeypatch, resource, status):
    app = create_web_app(env_file=tmp_path / "missing.env")
    with TestClient(app) as browser:
        browser.cookies.set("job_agent_session", "opaque-session")
        backend = app.state.backend
        backend.platform_auth_client = PlatformAuthClient(PlatformAuthSettings(True, "http://java", "secret", 5))

        def forbidden(*a, **kw):
            pytest.fail("Python must not resolve account facts or write migrated account data")

        for method in ("get_account", "get_account_by_email", "get_account_with_password", "update_account_display_name",
                       "get_auth_session_by_token_hash", "list_account_email_outbox"):
            monkeypatch.setattr(backend.store, method, forbidden)
        monkeypatch.setattr(backend.auth, "normalize_email", forbidden)
        raw_value = " Updated " if resource == "profile" else " NEW@Example.com "
        account = {"account_id": 42, "email": "owner@example.com", "display_name": "Updated", "role": "user",
                   "status": "active", "must_change_password": False, "email_verified_at": None, "deleted_at": None,
                   "created_at": "2026-10-17T00:00:00Z", "updated_at": "2026-10-17T00:00:00Z"}

        def request(url, **kw):
            if url.endswith("/internal/v1/auth/accounts/me"):
                return httpx.Response(200, json={"account": account})
            expected = "/internal/v1/auth/accounts/profile" if resource == "profile" else "/internal/v1/auth/email-change/request"
            assert url.endswith(expected)
            assert kw["json"]["session_token"] == "opaque-session"
            assert kw["json"]["display_name" if resource == "profile" else "email"] == raw_value
            assert "account_id" not in kw["json"]
            if resource == "profile":
                assert kw["headers"]["X-Trace-Id"] == "profile-route-trace"
            if status == 503:
                return httpx.Response(503, text="unavailable")
            result = {"account": account} if resource == "profile" else {"ok": True}
            return httpx.Response(status, json=result if status == 200 else {"code": "REJECTED", "message": "rejected"})

        monkeypatch.setattr(httpx, "post", request)
        response = browser.patch(f"/api/account/{resource}", json={
            "display_name" if resource == "profile" else "new_email": raw_value, "account_id": 999},
            headers={"X-Request-ID": "profile-route-trace"})
        assert response.status_code == status, response.text
        if status == 200 and resource == "profile":
            assert response.json()["account"]["id"] == 42
            assert response.json()["account"]["display_name"] == "Updated"
            assert "password_hash" not in response.json()["account"]
        elif status == 200:
            assert response.json() == {"message": "确认邮件已发送，请打开新邮箱中的链接完成变更。"}


@pytest.mark.parametrize("resource", ["profile", "email"])
def test_account_compatibility_routes_require_session_and_fail_closed(tmp_path, monkeypatch, resource):
    app = create_web_app(env_file=tmp_path / "missing.env")
    with TestClient(app) as browser:
        app.state.backend.platform_auth_client = PlatformAuthClient(PlatformAuthSettings(True, "http://java", "secret", 5))
        body = {"display_name": "Updated"} if resource == "profile" else {"new_email": "new@example.com"}
        assert browser.patch(f"/api/account/{resource}", json=body).status_code == 401
        browser.cookies.set("job_agent_session", "opaque-session")

        def offline(*a, **kw):
            raise httpx.ConnectError("offline")

        monkeypatch.setattr(httpx, "post", offline)
        assert browser.patch(f"/api/account/{resource}", json=body).status_code == 503
