import copy
import json
import asyncio
from decimal import Decimal

import httpx
import pytest
from fastapi.testclient import TestClient

from job_hunting_agent import account_export
from job_hunting_agent.config import PlatformAuthSettings
from job_hunting_agent.platform_auth import PlatformAuthClient, PlatformAuthError
from job_hunting_agent.web import create_web_app


def export_fixture():
    return {
        "exported_at": "2026-10-17T00:00:00Z",
        "account": {"id": 42, "email": "owner@example.com", "display_name": "Owner", "role": "user",
                    "status": "active", "must_change_password": False, "email_verified_at": None, "deleted_at": None,
                    "created_at": "2026-10-17T00:00:00Z", "updated_at": "2026-10-17T00:00:00Z"},
        "consents": [], "account_balance": {"account_id": 42},
        **{table: [] for table in (*account_export.DIRECT_TABLES, *account_export.JOINED_TABLES)},
    }


def java_client():
    return PlatformAuthClient(PlatformAuthSettings(True, "http://java", "service-secret", 5))


@pytest.mark.parametrize("mutation", ["owner", "balance", "secret", "root", "parent", "consent", "duplicate", "trailing"])
def test_export_projection_rejects_cross_account_and_malformed_responses(mutation):
    value = export_fixture()
    value["jobs"] = [{"id": 1, "account_id": 42, "text": "password_hash in user content is retained"}]
    if mutation == "owner":
        value["jobs"][0]["account_id"] = 43
    elif mutation == "balance":
        value["account_balance"]["account_id"] = 43
    elif mutation == "secret":
        value["jobs"][0]["token_hash"] = "secret"
    elif mutation == "root":
        value["raw_secret"] = "secret"
    elif mutation == "parent":
        value["knowledge_asset_versions"] = [{"asset_id": 99}]
    elif mutation == "consent":
        value["consents"] = [{"secret": "secret"}]
    body = json.dumps(value)
    if mutation == "duplicate":
        body = body.replace('"id": 42', '"id": 42, "id": 42')
    elif mutation == "trailing":
        body += " {}"
    with pytest.raises(ValueError):
        account_export.decode_export(body.encode())


def test_export_preserves_user_content_and_rejects_oversize_without_truncation(monkeypatch):
    value = export_fixture()
    value["jobs"] = [{"id": 1, "account_id": 42, "text": "password_hash API token 是用户文本"}]
    assert account_export.decode_export(account_export.encode_export(value, 42)) == value
    value["account_balance"]["exact_amount"] = Decimal("1234567890.123456")
    assert account_export.decode_export(account_export.encode_export(value, 42))["account_balance"]["exact_amount"] == "1234567890.123456"
    monkeypatch.setattr(account_export, "MAX_EXPORT_BYTES", 100)
    with pytest.raises(OverflowError):
        account_export.encode_export(value, 42)


@pytest.mark.parametrize("status,expected", [(200, 200), (401, 401), (413, 413), (302, 503), (500, 503), (503, 503)])
def test_export_compatibility_client_is_bounded_and_never_reflects_errors(monkeypatch, status, expected):
    real_client = httpx.Client

    def handler(request):
        assert request.url == "http://java/internal/v1/accounts/export"
        assert json.loads(request.content) == {"session_token": "real-session"}
        assert request.headers["X-Internal-Service-Token"] == "service-secret"
        return httpx.Response(status, json=export_fixture() if status == 200 else {"detail": "raw-secret"},
                              headers={"Location": "http://evil.example"})

    def client(**kwargs):
        assert kwargs["trust_env"] is False and kwargs["follow_redirects"] is False
        return real_client(**kwargs, transport=httpx.MockTransport(handler))

    monkeypatch.setattr(httpx, "Client", client)
    if expected == 200:
        assert java_client().export_account("real-session")["account"]["id"] == 42
    else:
        with pytest.raises(PlatformAuthError) as error:
            java_client().export_account("real-session")
        assert error.value.status_code == expected
        assert "raw-secret" not in str(error.value)


def test_python_export_compatibility_only_forwards_real_session(tmp_path, monkeypatch):
    app = create_web_app(env_file=tmp_path / "missing.env")
    with TestClient(app) as browser:
        app.state.backend.platform_auth_client = java_client()
        expected = export_fixture()
        calls = []
        monkeypatch.setattr(app.state.backend.platform_auth_client, "export_account",
                            lambda token: calls.append(token) or expected)
        browser.cookies.set("job_agent_session", "real-session")
        response = browser.get("/api/account/export?account_id=999")
        assert response.status_code == 200
        assert response.json() == expected
        assert response.headers["Content-Disposition"] == 'attachment; filename="job-agent-account-42-export.json"'
        assert response.headers["Cache-Control"] == "no-store"
        assert calls == ["real-session"]


@pytest.mark.parametrize("service_token,session,status", [(None, "real", 401), ("wrong", "real", 401),
                                                         ("service-secret", "expired", 401), ("service-secret", "real", 200)])
def test_private_export_requires_service_and_real_session_and_no_python_account_read(tmp_path, monkeypatch, service_token, session, status):
    app = create_web_app(env_file=tmp_path / "missing.env")
    with TestClient(app) as browser:
        assert browser.post("/api/auth/register", json={"email": "owner@example.com", "password": "password-123"}).status_code == 200
        store = app.state.backend.store
        native = store.get_account_by_email("owner@example.com")[0]
        account_export.encode_export(store.export_account_data(native.id, account=native), native.id)
        account = copy.deepcopy(export_fixture()["account"])
        account["account_id"] = native.id
        del account["id"]
        app.state.backend.platform_auth_client = java_client()

        def current(url, **kw):
            assert url.endswith("/internal/v1/auth/accounts/me")
            assert kw["json"] == {"session_token": session}
            return httpx.Response(200, json={"account": account}) if session == "real" else httpx.Response(
                401, json={"code": "SESSION_EXPIRED", "message": "expired"})

        def forbidden(*a, **kw):
            pytest.fail("Java-mode export must not re-read Python-owned account credentials")

        monkeypatch.setattr(httpx, "post", current)
        monkeypatch.setattr(store, "get_account", forbidden)
        response = browser.post("/internal/v1/accounts/export", json={"session_token": session},
                                headers={} if service_token is None else {"X-Internal-Service-Token": service_token})
        assert response.status_code == status, response.text
        if status == 200:
            assert response.json()["account"]["id"] == native.id
            assert response.headers["Cache-Control"] == "no-store"
            account_export.validate_export(response.json(), native.id)
            assert "password_hash" not in response.text and "token_hash" not in response.text
        assert browser.post("/internal/v1/accounts/export", json={"session_token": session, "account_id": 999},
                            headers={"X-Internal-Service-Token": "service-secret"}).status_code == 422
        invalid = browser.post("/internal/v1/accounts/export", json={"session_token": "private-value" * 100},
                               headers={"X-Internal-Service-Token": "service-secret"})
        assert invalid.status_code == 422 and "private-value" not in invalid.text


def test_export_callbacks_do_not_wait_on_fastapi_sync_pool(tmp_path, monkeypatch):
    import anyio.to_thread

    app = create_web_app(env_file=tmp_path / "missing.env")
    with TestClient(app) as browser:
        backend = app.state.backend
        backend.platform_auth_client = java_client()
        value = export_fixture()
        account = dict(value["account"])
        account["account_id"] = account.pop("id")
        monkeypatch.setattr(httpx, "post", lambda *a, **kw: httpx.Response(200, json={"account": account}))
        monkeypatch.setattr(backend.store, "export_account_data", lambda *a, **kw: value)

        async def setup():
            limiter = anyio.to_thread.current_default_thread_limiter()
            previous = limiter.total_tokens
            limiter.total_tokens = 1
            return asyncio.get_running_loop(), limiter, previous

        loop, limiter, previous = browser.portal.call(setup)

        def forward(token):
            async def callback():
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://callback") as client:
                    return await client.post("/internal/v1/accounts/export", json={"session_token": token},
                                             headers={"X-Internal-Service-Token": "service-secret"})
            result = asyncio.run_coroutine_threadsafe(callback(), loop).result(timeout=5)
            assert result.status_code == 200, result.text
            return result.json()

        monkeypatch.setattr(backend.platform_auth_client, "export_account", forward)
        browser.cookies.set("job_agent_session", "real-session")
        try:
            assert browser.get("/api/account/export").status_code == 200
        finally:
            async def restore():
                limiter.total_tokens = previous
            browser.portal.call(restore)
