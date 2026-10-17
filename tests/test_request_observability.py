import asyncio
from copy import deepcopy
from types import SimpleNamespace

import httpx
import anyio
import pytest
from fastapi.testclient import TestClient

from job_hunting_agent.config import PlatformAuthSettings
from job_hunting_agent.platform_auth import PlatformAuthClient, PlatformAuthError, PlatformAuthUnavailableError
from job_hunting_agent.web_hardening import RequestMetrics

from job_hunting_agent.web import create_web_app


def test_service_snapshot_is_private_and_separates_api_from_internal_calls(tmp_path):
    app = create_web_app(env_file=tmp_path / "missing.env")

    class Auth:
        internal_token = "private-service-secret"

    with TestClient(app) as browser:
        app.state.backend.platform_auth_client = Auth()
        response = browser.get("/internal/v1/observability/requests")
        assert response.status_code == 401
        browser.get("/api/health").raise_for_status()
        headers = {"X-Internal-Service-Token": Auth.internal_token}
        snapshot = browser.get("/internal/v1/observability/requests", headers=headers)
        assert snapshot.status_code == 200
        data = snapshot.json()
        assert data["status"] == "available"
        assert data["api"]["total_requests"] == 1
        assert data["internal"]["total_requests"] == 1
        assert data["internal"]["error_requests"] == 1
        assert data["account_email"]["status"] == "available"
        assert snapshot.headers["cache-control"] == "no-store"


def test_private_callback_does_not_need_a_free_public_sync_worker(tmp_path):
    app = create_web_app(env_file=tmp_path / "missing.env")
    with TestClient(app):
        async def scenario():
            limiter = anyio.to_thread.current_default_thread_limiter()
            previous = limiter.total_tokens
            limiter.total_tokens = 1
            try:
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as browser:
                    loop = asyncio.get_running_loop()

                    class Auth:
                        internal_token = "private-service-secret"

                        def observability(self, **kw):
                            # The compatibility request occupies the only synchronous handler slot.
                            future = asyncio.run_coroutine_threadsafe(browser.get(
                                "/internal/v1/observability/requests", headers={
                                    "X-Internal-Service-Token": self.internal_token}), loop)
                            return future.result(timeout=3).json()

                    app.state.backend.platform_auth_client = Auth()
                    response = await browser.get("/api/admin/observability/requests")
                    assert response.status_code == 200, response.text
                    assert response.json()["status"] == "available"
            finally:
                limiter.total_tokens = previous

        asyncio.run(scenario())


def test_private_snapshot_does_not_expose_paths_queries_or_raw_mail(tmp_path, monkeypatch):
    app = create_web_app(env_file=tmp_path / "missing.env")
    with TestClient(app) as browser:
        app.state.backend.platform_auth_client = SimpleNamespace(internal_token="private-service-secret")
        record = SimpleNamespace(id=1, account_id=2, purpose="verify_email", recipient_email="owner@example.com",
            status="failed", attempt_count=1, max_attempts=5, last_error_type="SMTP: private credential",
            created_at="2026-10-17T00:00:00Z", updated_at="2026-10-17T00:00:00Z")
        monkeypatch.setattr(app.state.backend.store, "list_account_email_outbox", lambda **kw: [record])
        for index in range(24):
            assert browser.get(f"/api/private-path-{index}?token=never-expose-query").status_code == 404
        data = browser.get("/internal/v1/observability/requests", headers={
            "X-Internal-Service-Token": "private-service-secret"}).json()
        assert data["api"]["endpoint_counts"] == {"unmatched": 24}
        assert len(data["api"]["recent_errors"]) == 20
        assert data["account_email"]["records"][0]["recipient_email"] == "o***@example.com"
        assert data["account_email"]["records"][0]["last_error_type"] == "DeliveryError"
        serialized = str(data)
        assert all(secret not in serialized for secret in ("private-path", "never-expose-query", "owner@example.com", "private credential"))


def test_legacy_mail_outage_preserves_request_snapshot(tmp_path, monkeypatch):
    app = create_web_app(env_file=tmp_path / "missing.env")
    with TestClient(app) as browser:
        app.state.backend.platform_auth_client = SimpleNamespace(internal_token="private-service-secret")
        def failed(**kw):
            raise RuntimeError("database credential must not leak")
        monkeypatch.setattr(app.state.backend.store, "list_account_email_outbox", failed)
        data = browser.get("/internal/v1/observability/requests", headers={
            "X-Internal-Service-Token": "private-service-secret"}).json()
        assert data["status"] == "available"
        assert data["api"]["total_requests"] == 0
        assert data["account_email"] == {"status": "unavailable", "summary": None, "records": []}
        assert "credential" not in str(data)


def projection():
    metrics = RequestMetrics(safe_routes=True).snapshot()
    return {"status": "available", "requests": metrics, "services": {
        name: {"status": "available", "api": deepcopy(metrics), "internal": deepcopy(metrics),
               "observed_at": "2026-10-17T00:00:00Z"} for name in ("java", "python")},
        "account_email": {"status": "available", "summary": {key: 0 for key in (
            "pending", "sending", "retrying", "sent", "failed", "cancelled")}, "records": []}}


@pytest.mark.parametrize("status", [200, 401, 403, 503])
def test_java_compatibility_route_never_reads_local_metrics_or_mail(tmp_path, monkeypatch, status):
    app = create_web_app(env_file=tmp_path / "missing.env")
    with TestClient(app) as browser:
        credentials = {"email": "observe@example.com", "password": "password-123"}
        browser.post("/api/auth/register", json=credentials).raise_for_status()
        browser.post("/api/auth/login", json=credentials).raise_for_status()
        payload = projection()
        def request(url, **kw):
            if url.endswith("/internal/v1/auth/accounts/me"):
                return httpx.Response(401, json={"message": "expired"})
            assert url.endswith("/internal/v1/observability/requests")
            assert kw["json"] == {"session_token": browser.cookies["job_agent_session"]}
            assert kw["headers"]["X-Trace-Id"] == "observation-trace"
            return httpx.Response(status, json=payload if status == 200 else {"message": "unavailable"})
        monkeypatch.setattr(httpx, "post", request)
        app.state.backend.platform_auth_client = PlatformAuthClient(PlatformAuthSettings(True, "http://java", "secret", 5))
        for method in ("list_account_email_outbox", "summarize_account_email_outbox"):
            monkeypatch.setattr(app.state.backend.store, method, lambda **kw: pytest.fail("Local observability fallback"))
        response = browser.get("/api/admin/observability/requests", headers={"X-Request-ID": "observation-trace"})
        assert response.status_code == status, response.text
        if status == 200:
            assert response.json() == payload
            assert response.headers["cache-control"] == "no-store"


@pytest.mark.parametrize("invalid", ["html", "extra", "missing", "nonfinite", "private-email", "bad-service"])
def test_observability_client_rejects_invalid_snapshots(monkeypatch, invalid):
    data = projection()
    if invalid == "extra":
        data["cookie"] = "private"
    elif invalid == "missing":
        del data["services"]
    elif invalid == "nonfinite":
        data["requests"]["total_duration_ms"] = -1
    elif invalid == "private-email":
        data["account_email"]["records"] = [{"recipient_email": "private@example.com"}]
    elif invalid == "bad-service":
        data["services"]["python"]["status"] = "unknown"
    response = httpx.Response(200, text="html") if invalid == "html" else httpx.Response(200, json=data)
    monkeypatch.setattr(httpx, "post", lambda *a, **kw: response)
    client = PlatformAuthClient(PlatformAuthSettings(True, "http://java", "secret", 5))
    with pytest.raises(PlatformAuthError) as error:
        client.observability(session_token="opaque")
    assert error.value.status_code == 502


def test_observability_client_preserves_partial_and_fails_closed(monkeypatch):
    client = PlatformAuthClient(PlatformAuthSettings(True, "http://java", "secret", 5))
    with pytest.raises(PlatformAuthError) as error:
        client.observability(session_token=None)
    assert error.value.status_code == 401
    data = projection()
    data.update(status="partial", requests=None)
    data["services"]["python"] = {"status": "unavailable", "api": None, "internal": None, "observed_at": None}
    data["account_email"].update(status="partial", summary=None)
    monkeypatch.setattr(httpx, "post", lambda *a, **kw: httpx.Response(200, json=data))
    assert client.observability(session_token="opaque") == data
    def offline(*a, **kw):
        raise httpx.ConnectError("offline")
    monkeypatch.setattr(httpx, "post", offline)
    with pytest.raises(PlatformAuthUnavailableError):
        client.observability(session_token="opaque")
