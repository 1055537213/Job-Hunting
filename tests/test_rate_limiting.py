"""Redis 分布式请求限流回归。"""

from __future__ import annotations

import asyncio
import threading
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.requests import Request

from job_hunting_agent.config import (
    WebSecuritySettings,
    load_web_security_settings,
    masked_web_security_settings,
)
from job_hunting_agent.rate_limiting import (
    RateLimitBackendUnavailable,
    RedisRateLimiter,
)
from job_hunting_agent.web import create_web_app
from job_hunting_agent.web_hardening import (
    format_prometheus_request_metrics,
    install_web_hardening,
    rate_limit_group,
)


@dataclass
class SharedRedisState:
    """只模拟 Lua 脚本的共享原子状态，不实现通用 Redis。"""

    now_ms: int = 1_000_000
    buckets: dict[str, list[tuple[int, str]]] = field(default_factory=dict)
    seen_keys: list[str] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def evaluate(self, key: str, window_ms: int, limit: int, member: str) -> list[int]:
        with self.lock:
            self.seen_keys.append(key)
            cutoff = self.now_ms - window_ms
            bucket = [
                item
                for item in self.buckets.get(key, [])
                if item[0] > cutoff
            ]
            self.buckets[key] = bucket
            if len(bucket) >= limit:
                retry_ms = max(1, bucket[0][0] + window_ms - self.now_ms)
                return [0, retry_ms]
            bucket.append((self.now_ms, member))
            bucket.sort()
            return [1, 0]


class FakeRedisClient:
    """让不同客户端实例共享同一 Redis 状态。"""

    def __init__(self, state: SharedRedisState) -> None:
        self.state = state

    def eval(self, script: str, numkeys: int, *keys_and_args: object) -> object:
        assert "redis.call('TIME')" in script
        assert numkeys == 1
        key, window_ms, limit, member = keys_and_args
        return self.state.evaluate(
            str(key),
            int(window_ms),
            int(limit),
            str(member),
        )


class BrokenRedisClient:
    def eval(self, script: str, numkeys: int, *keys_and_args: object) -> object:
        raise ConnectionError("redis unavailable")


class FailingRateLimiter:
    async def check(self, *, client_id: str, group: str) -> int | None:
        raise RateLimitBackendUnavailable("test outage")


class RecordingRateLimiter:
    def __init__(self) -> None:
        self.identities: list[str] = []

    async def check(self, *, client_id: str, group: str) -> int | None:
        self.identities.append(client_id)
        return None


def redis_settings(**overrides: object) -> WebSecuritySettings:
    values: dict[str, object] = {
        "rate_limit_backend": "redis",
        "rate_limit_redis_url": "redis://redis:6379/1",
        "rate_limit_window_seconds": 60,
        "rate_limit_auth_requests": 2,
        "rate_limit_default_requests": 4,
        "rate_limit_model_requests": 3,
        "rate_limit_upload_requests": 2,
        "rate_limit_admin_requests": 3,
        "rate_limit_write_requests": 3,
    }
    values.update(overrides)
    return WebSecuritySettings(**values)


def request_for(path: str, method: str = "GET") -> Request:
    return Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": method,
            "scheme": "http",
            "path": path,
            "raw_path": path.encode("ascii"),
            "query_string": b"",
            "headers": [],
            "client": ("127.0.0.1", 12345),
            "server": ("testserver", 80),
        }
    )


def test_two_redis_limiter_instances_share_one_atomic_window() -> None:
    state = SharedRedisState()
    settings = redis_settings()
    first = RedisRateLimiter(settings, redis_client=FakeRedisClient(state))
    second = RedisRateLimiter(settings, redis_client=FakeRedisClient(state))

    async def exercise() -> list[int | None]:
        return [
            await first.check(client_id="account:42", group="auth"),
            await second.check(client_id="account:42", group="auth"),
            await first.check(client_id="account:42", group="auth"),
        ]

    assert asyncio.run(exercise()) == [None, None, 60]
    assert all("account:42" not in key for key in state.seen_keys)


def test_redis_limiter_is_atomic_under_concurrency_and_isolates_keys() -> None:
    state = SharedRedisState()
    settings = redis_settings(rate_limit_model_requests=5)
    limiters = [
        RedisRateLimiter(settings, redis_client=FakeRedisClient(state))
        for _ in range(2)
    ]

    async def exercise() -> list[int | None]:
        return await asyncio.gather(
            *(
                limiters[index % 2].check(
                    client_id="account:7",
                    group="model",
                )
                for index in range(20)
            )
        )

    decisions = asyncio.run(exercise())
    assert decisions.count(None) == 5
    assert len([decision for decision in decisions if decision is not None]) == 15
    assert asyncio.run(
        limiters[0].check(client_id="account:8", group="model")
    ) is None
    assert asyncio.run(
        limiters[0].check(client_id="account:7", group="default")
    ) is None


def test_redis_limiter_releases_expired_window_entries() -> None:
    state = SharedRedisState()
    limiter = RedisRateLimiter(
        redis_settings(rate_limit_upload_requests=1),
        redis_client=FakeRedisClient(state),
    )

    assert asyncio.run(limiter.check(client_id="ip:127.0.0.1", group="upload")) is None
    assert asyncio.run(limiter.check(client_id="ip:127.0.0.1", group="upload")) == 60
    state.now_ms += 60_001
    assert asyncio.run(limiter.check(client_id="ip:127.0.0.1", group="upload")) is None


def test_redis_client_failure_is_mapped_to_backend_error() -> None:
    limiter = RedisRateLimiter(
        redis_settings(),
        redis_client=BrokenRedisClient(),
    )

    with pytest.raises(RateLimitBackendUnavailable):
        asyncio.run(limiter.check(client_id="account:1", group="default"))


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("/api/auth/login", "auth"),
        ("/api/admin/accounts", "admin"),
        ("/api/jobs/screenshots", "upload"),
        ("/api/projects/github", "upload"),
        ("/api/resumes/upload", "upload"),
        ("/api/chat/stream", "model"),
        ("/api/matches/12", "model"),
        ("/api/resumes/9/tailor", "model"),
        ("/api/profiles", "default"),
        ("/api/profiles", "write"),
    ],
)
def test_request_paths_use_low_cardinality_rate_limit_groups(
    path: str,
    expected: str,
) -> None:
    method = "POST" if expected == "write" else "GET"
    assert rate_limit_group(request_for(path, method)) == expected


def test_backend_outage_fails_closed_for_auth_but_open_for_default() -> None:
    app = FastAPI()

    @app.get("/api/ping")
    def ping() -> dict[str, bool]:
        return {"ok": True}

    @app.post("/api/auth/login")
    def login() -> dict[str, bool]:
        return {"ok": True}

    @app.post("/api/items")
    def create_item() -> dict[str, bool]:
        return {"ok": True}

    install_web_hardening(
        app,
        settings=redis_settings(csrf_enabled=False),
        session_cookie_name="session",
        rate_limiter=FailingRateLimiter(),
    )
    client = TestClient(app, raise_server_exceptions=False)

    ordinary = client.get("/api/ping")
    auth = client.post("/api/auth/login")
    write = client.post("/api/items")

    assert ordinary.status_code == 200
    assert auth.status_code == 503
    assert write.status_code == 503
    assert auth.headers["retry-after"] == "1"
    assert "暂时不可用" in auth.json()["detail"]
    metrics = app.state.request_metrics.snapshot()
    assert metrics["rate_limit_backend_errors"] == 3
    assert "job_agent_rate_limit_backend_errors_total 3" in (
        format_prometheus_request_metrics(metrics)
    )


def test_authenticated_requests_use_account_identity_instead_of_session_or_email() -> None:
    limiter = RecordingRateLimiter()
    client = TestClient(create_web_app(rate_limiter=limiter))
    registered = client.post(
        "/api/auth/register",
        json={"email": "rate-limit-account@example.com", "password": "password-123"},
    )
    assert registered.status_code == 200
    account_id = registered.json()["account"]["id"]
    assert client.post(
        "/api/auth/login",
        json={"email": "rate-limit-account@example.com", "password": "password-123"},
    ).status_code == 200

    assert client.get("/api/auth/me").status_code == 200
    assert f"account:{account_id}" in limiter.identities
    assert all("rate-limit-account@example.com" not in value for value in limiter.identities)


def test_production_requires_redis_rate_limit_backend(tmp_path: Path) -> None:
    env_path = tmp_path / ".env"
    env_path.write_text("", encoding="utf-8")

    with pytest.raises(ValueError, match="Redis 分布式请求限流"):
        load_web_security_settings(
            env_path,
            environ={
                "JOB_AGENT_ENVIRONMENT": "production",
                "JOB_AGENT_RATE_LIMIT_BACKEND": "memory",
            },
        )


def test_redis_rate_limit_configuration_does_not_expose_url(tmp_path: Path) -> None:
    env_path = tmp_path / ".env"
    env_path.write_text("", encoding="utf-8")
    settings = load_web_security_settings(
        env_path,
        environ={
            "JOB_AGENT_ENVIRONMENT": "production",
            "JOB_AGENT_RATE_LIMIT_BACKEND": "redis",
            "JOB_AGENT_RATE_LIMIT_REDIS_URL": "redis://:secret@redis:6379/1",
        },
    )

    masked = masked_web_security_settings(settings)
    assert masked["rate_limit_backend"] == "redis"
    assert masked["rate_limit_redis_configured"] is True
    assert "secret" not in str(masked)
    assert "redis_url" not in str(masked)
