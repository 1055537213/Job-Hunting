from __future__ import annotations

import httpx
import pytest

from job_hunting_agent.config import PlatformAuthSettings, load_platform_auth_settings
from job_hunting_agent.platform_auth import (
    PlatformAuthClient,
    PlatformAuthError,
    PlatformAuthUnavailableError,
)


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
