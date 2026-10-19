import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from pathlib import Path

import httpx
import pytest
import yaml
from fastapi.testclient import TestClient

from job_hunting_agent.config import PlatformAuthSettings
from job_hunting_agent.models import CandidateProfileInput
from job_hunting_agent.platform_auth import PlatformAuthClient, PlatformAuthError
from job_hunting_agent.storage import ProfileCreateConflict
from job_hunting_agent.web import ProfileCreatePayload, create_web_app


def test_profile_creation_contract_matches_input_fields():
    contract = yaml.safe_load((Path(__file__).resolve().parents[1] / "platform-service/src/main/resources/profile-create.openapi.yaml").read_text(encoding="utf-8"))
    assert set(contract["components"]["schemas"]["ProfileInput"]["properties"]) == set(ProfileCreatePayload.model_fields)
    assert set(contract["paths"]["/api/profiles"]) == {"post"}


@pytest.fixture
def creation(tmp_path, monkeypatch):
    app = create_web_app(env_file=tmp_path / "missing.env")
    backend = app.state.backend
    owners = [backend.store.create_account(email=f"create-{i}@example.com", password_hash="hash", display_name="Owner") for i in (1, 2)]
    backend.platform_auth_client = PlatformAuthClient(PlatformAuthSettings(True, "http://java", "service-secret", 5))
    def account(operation, **data):
        assert operation == "me" and set(data) == {"session_token", "trace_id"}
        if data["session_token"] == "expired":
            raise PlatformAuthError("SESSION_EXPIRED", "private-session", 401)
        row = asdict(owners[int(data["session_token"][-1]) - 1])
        row["account_id"] = row.pop("id")
        return {"account": row}
    monkeypatch.setattr(backend.platform_auth_client, "account", account)
    with TestClient(app) as browser:
        yield app, browser, owners


def send(browser, profile=None, key="create-intent-1", session="session-1", headers=None):
    return browser.post("/internal/v1/profiles/create", json={"session_token": session, "idempotency_key": key,
        "profile": profile or {"name": "Candidate", "skills": {"python": "熟练"}, "preferred_cities": ["杭州市"]}},
        headers=headers if headers is not None else {"X-Internal-Service-Token": "service-secret"})


def test_real_session_owner_idempotent_replay_conflict_and_delete_tombstone(creation):
    app, browser, owners = creation
    assert send(browser, headers={}).status_code == 401
    assert send(browser, session="expired").status_code == 401
    first = send(browser)
    assert first.status_code == 200, first.text
    assert first.headers["cache-control"] == "no-store"
    assert first.json()["account_id"] == owners[0].id
    assert first.json()["profile"]["preferred_cities"] == ["杭州"]
    assert first.json()["profile"]["skills"] == {"Python": "熟练"}
    assert send(browser).json() == first.json()
    assert send(browser, profile={"name": "Different"}).status_code == 409
    other = send(browser, session="session-2")
    assert other.status_code == 200 and other.json()["candidate_id"] != first.json()["candidate_id"]
    assert other.json()["account_id"] == owners[1].id
    assert send(browser, key="other-key").json()["code"] == "DUPLICATE_RESOURCE"
    candidate = first.json()["candidate_id"]
    app.state.backend.delete_candidate_profile(candidate, account_id=owners[0].id)
    assert send(browser).json()["code"] == "PROFILE_CREATE_CONFLICT"
    assert send(browser, key="new-intent-after-delete").status_code == 200
    with app.state.backend.store.connect() as conn:
        rows = conn.execute("SELECT * FROM profile_create_commands WHERE account_id=?", (owners[0].id,)).fetchall()
        assert len(rows) == 2 and sum(row["candidate_id"] is None for row in rows) == 1
        assert "create-intent-1" not in str([dict(row) for row in rows])


@pytest.mark.parametrize("profile", [{"name": "X", "account_id": 99}, {"name": 123}, {"name": "x" * 129},
    {"name": "X", "skills": {"Java": 1}}, {"name": "X", "experience_years": "1"},
    {"name": "X", "preferred_cities": "杭州"}, {"name": "X", "salary_floor_k": 2**31}])
def test_invalid_callback_input_never_reflects_personal_fields(creation, profile):
    _, browser, _ = creation
    response = send(browser, profile=profile)
    assert response.status_code == 422 and response.json()["code"] == "INVALID_REQUEST"
    assert profile["name"] not in response.text if isinstance(profile["name"], str) else True


def test_concurrent_creation_and_atomic_rollback(creation, monkeypatch):
    app, _, owners = creation
    backend = app.state.backend
    profile = CandidateProfileInput(name="Parallel", status="student", education="bachelor", experience_years=0,
        skills={}, preferred_cities=[], salary_floor_k=None, expected_salary_k=None, target_directions=[])
    with ThreadPoolExecutor(max_workers=4) as pool:
        ids = list(pool.map(lambda _: backend.create_candidate_profile_once(profile, owners[0].id, "parallel-intent").id, range(4)))
    assert len(set(ids)) == 1 and len(backend.list_candidate_profiles(account_id=owners[0].id)) == 1
    original = backend.store._add_long_text
    def fail(*args, **kw):
        raise RuntimeError("private-db-error")
    monkeypatch.setattr(backend.store, "_add_long_text", fail)
    with pytest.raises(RuntimeError):
        backend.create_candidate_profile_once(profile, owners[1].id, "rollback-intent")
    with backend.store.connect() as conn:
        assert conn.execute("SELECT COUNT(*) AS n FROM profile_create_commands WHERE account_id=?", (owners[1].id,)).fetchone()["n"] == 0
        assert conn.execute("SELECT COUNT(*) AS n FROM candidate_profiles WHERE account_id=?", (owners[1].id,)).fetchone()["n"] == 0
    monkeypatch.setattr(backend.store, "_add_long_text", original)
    assert backend.create_candidate_profile_once(profile, owners[1].id, "rollback-intent").id > 0
    with pytest.raises(ProfileCreateConflict):
        backend.create_candidate_profile_once(CandidateProfileInput(**{**asdict(profile), "name": "Changed"}), owners[0].id, "parallel-intent")


def test_callback_faults_are_safe_and_disabled_account_cannot_write(creation, monkeypatch):
    app, browser, owners = creation
    def fail(*args, **kw):
        raise RuntimeError("private-db-password")
    with monkeypatch.context() as patch:
        patch.setattr(app.state.backend, "create_candidate_profile_once", fail)
        response = send(browser)
        assert response.status_code == 503 and response.headers["retry-after"] == "3"
        assert "private-db-password" not in response.text
    assert send(browser).status_code == 200
    with app.state.backend.store.connect() as conn:
        conn.execute("UPDATE accounts SET status='disabled' WHERE id=?", (owners[0].id,))
    assert send(browser, key="disabled").status_code == 403


def test_commit_without_response_is_recovered_by_same_intent(creation, monkeypatch):
    app, browser, owners = creation
    backend = app.state.backend
    original = backend.create_candidate_profile_once
    def commit_then_fail(*args):
        original(*args)
        raise RuntimeError("response-lost-after-commit")
    with monkeypatch.context() as patch:
        patch.setattr(backend, "create_candidate_profile_once", commit_then_fail)
        assert send(browser).status_code == 503
    replay = send(browser)
    assert replay.status_code == 200
    assert [p.id for p in backend.list_candidate_profiles(account_id=owners[0].id)] == [replay.json()["candidate_id"]]
    assert send(browser, profile={"name": "Too large", "skills": {"Skill": "x" * 65536}}).status_code == 413


def test_native_profile_creation_still_works_and_optional_key_is_idempotent(tmp_path):
    app = create_web_app(env_file=tmp_path / "missing.env")
    with TestClient(app) as browser:
        assert browser.post("/api/auth/register", json={"email": "native-create@example.com", "password": "test-native-password"}).status_code == 200
        assert browser.post("/api/auth/login", json={"email": "native-create@example.com", "password": "test-native-password"}).status_code == 200
        headers = {"Idempotency-Key": "native-intent"}
        created = browser.post("/api/profiles", json={"name": "Native"}, headers=headers)
        assert created.status_code == 200
        assert browser.post("/api/profiles", json={"name": "Native"}, headers=headers).json() == created.json()
        assert browser.post("/api/profiles", json={"name": "Native"}).status_code == 409
        assert browser.post("/api/profiles", json={"name": "Native without key"}).status_code == 200


def test_hybrid_compatibility_entry_never_writes_local_and_keeps_same_key(creation, monkeypatch):
    app, browser, _ = creation
    expected = send(browser).json()
    expected.pop("account_id")
    def forbidden(*a, **kw):
        pytest.fail("Compatibility must not save a local profile")
    monkeypatch.setattr(app.state.backend, "save_candidate_profile", forbidden)
    monkeypatch.setattr(app.state.backend, "create_candidate_profile_once", forbidden)
    def forward(session, profile, key, **kw):
        assert session == "session-1" and profile["name"] == "Forward" and key == "stable-intent"
        return expected
    monkeypatch.setattr(app.state.backend.platform_auth_client, "create_profile", forward)
    assert browser.post("/api/profiles", json={"name": "Forward"}).status_code == 401
    browser.cookies.set("job_agent_session", "session-1")
    response = browser.post("/api/profiles", json={"name": "Forward"}, headers={"Idempotency-Key": "stable-intent"})
    assert response.status_code == 200 and response.json() == expected
    def offline(*a, **kw):
        raise PlatformAuthError("PROFILE_CREATE_UNAVAILABLE", "safe", 503)
    monkeypatch.setattr(app.state.backend.platform_auth_client, "create_profile", offline)
    assert browser.post("/api/profiles", json={"name": "Forward"}).status_code == 503
    def conflict(*a, **kw):
        raise PlatformAuthError("PROFILE_CREATE_CONFLICT", "safe conflict", 409)
    monkeypatch.setattr(app.state.backend.platform_auth_client, "create_profile", conflict)
    assert browser.post("/api/profiles", json={"name": "Forward"}).json()["code"] == "PROFILE_CREATE_CONFLICT"


def test_python_client_validates_created_id_and_only_known_errors(creation, monkeypatch):
    app, browser, _ = creation
    value = send(browser).json()
    value.pop("account_id")
    current = [httpx.Response(200, json=value)]
    real = httpx.Client
    def handler(request):
        assert request.url.path == "/internal/v1/profiles/create"
        assert json.loads(request.content) == {"session_token": "session-1", "profile": {"name": "Candidate"}, "idempotency_key": "stable-intent"}
        return current[0]
    monkeypatch.setattr(httpx, "Client", lambda **kw: real(**kw, transport=httpx.MockTransport(handler)))
    client = app.state.backend.platform_auth_client
    assert client.create_profile("session-1", {"name": "Candidate"}, "stable-intent") == value
    for bad in (httpx.Response(200, json={**value, "candidate_id": 999}), httpx.Response(302),
                httpx.Response(401, json={"code": "INTERNAL_UNAUTHORIZED"}), httpx.Response(404, text="private-error")):
        current[0] = bad
        with pytest.raises(PlatformAuthError) as error:
            client.create_profile("session-1", {"name": "Candidate"}, "stable-intent")
        assert error.value.status_code == 503 and "private" not in str(error.value)
    current[0] = httpx.Response(409, json={"code": "PROFILE_CREATE_CONFLICT", "detail": "private-error"})
    with pytest.raises(PlatformAuthError) as error:
        client.create_profile("session-1", {"name": "Candidate"}, "stable-intent")
    assert error.value.status_code == 409 and "private-error" not in str(error.value)


def test_create_callback_does_not_wait_for_busy_compatibility_sync_pool(creation, monkeypatch):
    import anyio.to_thread
    app, browser, _ = creation
    async def setup():
        limiter = anyio.to_thread.current_default_thread_limiter()
        previous = limiter.total_tokens
        limiter.total_tokens = 1
        return asyncio.get_running_loop(), limiter, previous
    loop, limiter, previous = browser.portal.call(setup)
    def forward(session, profile, key, **kw):
        async def call():
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://callback") as client:
                return await client.post("/internal/v1/profiles/create", json={"session_token": session,
                    "profile": profile, "idempotency_key": key}, headers={"X-Internal-Service-Token": "service-secret"})
        response = asyncio.run_coroutine_threadsafe(call(), loop).result(timeout=5)
        assert response.status_code == 200, response.text
        value = response.json()
        value.pop("account_id")
        return value
    monkeypatch.setattr(app.state.backend.platform_auth_client, "create_profile", forward)
    browser.cookies.set("job_agent_session", "session-1")
    try:
        assert browser.post("/api/profiles", json={"name": "One sync slot"}, headers={"Idempotency-Key": "one-sync-slot"}).status_code == 200
    finally:
        async def restore():
            limiter.total_tokens = previous
        browser.portal.call(restore)


def test_account_cleanup_removes_receipts_without_touching_other_owners(creation):
    app, browser, owners = creation
    assert send(browser).status_code == 200
    assert send(browser, session="session-2").status_code == 200
    app.state.backend.store.prepare_account_deletion(owners[0].id)
    app.state.backend.store.finalize_account_deletion(owners[0].id, "removed@example.com", "unusable", preserve_account=True)
    with app.state.backend.store.connect() as conn:
        rows = conn.execute("SELECT account_id FROM profile_create_commands").fetchall()
        assert [row["account_id"] for row in rows] == [owners[1].id]
