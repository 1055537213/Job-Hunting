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
