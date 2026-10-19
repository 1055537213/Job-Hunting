import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import asdict
from datetime import UTC, datetime
from threading import Event
from types import SimpleNamespace
from pathlib import Path

import httpx
import pytest
import yaml
from fastapi.testclient import TestClient

from job_hunting_agent.app import JobHuntingApp
from job_hunting_agent.config import PlatformAuthSettings
from job_hunting_agent.models import CandidateProfileInput
from job_hunting_agent.platform_auth import PlatformAuthClient, PlatformAuthError
from job_hunting_agent.profile_deletion import cleanup, resume_pending
from job_hunting_agent.web import create_web_app


def test_frozen_deletion_receipt_is_small_and_contains_no_object_locators():
    contract = yaml.safe_load((Path(__file__).resolve().parents[1] / "platform-service/src/main/resources/profile-delete.openapi.yaml").read_text(encoding="utf-8"))
    schema = contract["components"]["schemas"]["Receipt"]
    assert set(schema["properties"]) == set(schema["required"]) == {"deleted", "candidate_id", "cleanup_status", "rag_cleanup"}
    assert schema["additionalProperties"] is False


@pytest.fixture
def deletion(tmp_path, monkeypatch):
    app = create_web_app(env_file=tmp_path / "missing.env", resume_dir=tmp_path / "files")
    backend = app.state.backend
    owners, candidates = [], []
    for i in (1, 2):
        owner = backend.store.create_account(email=f"delete-profile-{i}@example.com", password_hash="hash", display_name="Owner")
        owners.append(owner)
        candidates.append(backend.save_candidate_profile(CandidateProfileInput(name=f"Profile {i}", status="student",
            education="bachelor", experience_years=0, skills={}, preferred_cities=[], salary_floor_k=None,
            expected_salary_k=None, target_directions=[]), account_id=owner.id))
    backend.platform_auth_client = PlatformAuthClient(PlatformAuthSettings(True, "http://java", "service-secret", 5))
    def account(operation, **data):
        assert operation == "me" and set(data) == {"session_token", "trace_id"}
        if data["session_token"] == "expired":
            raise PlatformAuthError("SESSION_EXPIRED", "private-session", 401)
        owner = owners[int(data["session_token"][-1])-1]
        value = asdict(owner)
        value["account_id"] = value.pop("id")
        return {"account": value}
    monkeypatch.setattr(backend.platform_auth_client, "account", account)
    with TestClient(app) as browser:
        yield app, browser, owners, candidates


def submit(browser, candidate, session="session-1", **extra):
    return browser.post("/internal/v1/profiles/delete", json={"session_token": session, "candidate_id": candidate, **extra},
                        headers={"X-Internal-Service-Token": "service-secret"})


def asset_files(backend, owner, candidate):
    first = backend.resume_files.save(account_id=owner, candidate_id=candidate, filename="v1.txt", content=b"private v1")
    asset, _ = backend.store.register_knowledge_asset(account_id=owner, candidate_id=candidate, asset_kind="document", title="Private",
        original_filename="v1.txt", storage_key=first.storage_key, media_type="text/plain", file_size=first.file_size, sha256=first.sha256)
    second = backend.resume_files.save(account_id=owner, candidate_id=candidate, filename="v2.txt", content=b"private v2")
    backend.store.add_knowledge_asset_version(asset.id, account_id=owner, original_filename="v2.txt", storage_key=second.storage_key,
        media_type="text/plain", file_size=second.file_size, sha256=second.sha256)
    return sorted([first.storage_key, second.storage_key])


def test_owner_scope_session_service_credentials_replay_and_safe_receipt(deletion):
    app, browser, owners, candidates = deletion
    assert browser.post("/internal/v1/profiles/delete", json={"session_token": "session-1", "candidate_id": candidates[0]}).status_code == 401
    assert submit(browser, candidates[0], "expired").status_code == 401
    assert submit(browser, candidates[1]).status_code == 404
    assert submit(browser, 987654321).status_code == 404
    response = submit(browser, candidates[0])
    assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
    assert response.json() == {"account_id": owners[0].id, "deleted": True, "candidate_id": candidates[0],
                               "cleanup_status": "completed", "rag_cleanup": "database_cascade"}
    assert submit(browser, candidates[0]).json() == response.json()
    assert submit(browser, candidates[0], "session-2").status_code == 404
    assert app.state.backend.list_candidate_profiles(account_id=owners[0].id) == []
    assert app.state.backend.get_candidate_profile(candidates[1], account_id=owners[1].id).id == candidates[1]
    with app.state.backend.store.connect() as conn:
        conn.execute("UPDATE accounts SET status='disabled' WHERE id=?", (owners[0].id,))
    assert submit(browser, candidates[0]).status_code == 403


@pytest.mark.parametrize("extra", [{"candidate_id": 0}, {"candidate_id": True}, {"candidate_id": "1"},
                                   {"candidate_id": 2**63}, {"account_id": 999}, {"session_token": "private" * 100}])
def test_invalid_callback_never_echoes_input(deletion, extra):
    _, browser, _, candidates = deletion
    response = submit(browser, candidates[0], **extra)
    assert response.status_code == 422 and response.json()["code"] == "INVALID_REQUEST"
    assert "private" not in response.text


def test_all_asset_versions_survive_as_manifest_then_cleanup_recovers_after_restart(deletion, monkeypatch, tmp_path):
    app, browser, owners, candidates = deletion
    backend, owner, candidate = app.state.backend, owners[0].id, candidates[0]
    keys = asset_files(backend, owner, candidate)
    other = asset_files(backend, owners[1].id, candidates[1])
    response = submit(browser, candidate)
    assert response.status_code == 200 and response.json()["cleanup_status"] == "pending"
    assert all(key not in response.text for key in keys)
    with backend.store.connect() as conn:
        row = conn.execute("SELECT storage_keys_json FROM profile_deletions WHERE account_id=? AND candidate_id=?", (owner, candidate)).fetchone()
        assert json.loads(row["storage_keys_json"]) == keys
        assert conn.execute("SELECT COUNT(*) AS n FROM knowledge_assets WHERE account_id=?", (owner,)).fetchone()["n"] == 0
    original = backend.resume_files.delete
    calls = []
    def flaky(key):
        calls.append(key)
        if key == keys[1]:
            raise RuntimeError("private-storage-credential")
        original(key)
    monkeypatch.setattr(backend.resume_files, "delete", flaky)
    assert cleanup(backend, owner, candidate, force=True)["cleanup_status"] == "pending"
    with backend.store.connect() as conn:
        row = conn.execute("SELECT * FROM profile_deletions WHERE account_id=? AND candidate_id=?", (owner, candidate)).fetchone()
        assert json.loads(row["storage_keys_json"]) == [keys[1]] and row["lease_token"] is None
        assert "private" not in row["last_error"] and datetime.fromisoformat(row["next_attempt_at"]) > datetime.now(UTC)
        conn.execute("UPDATE profile_deletions SET next_attempt_at=now() WHERE account_id=?", (owner,))
    recovered = JobHuntingApp(env_path=tmp_path / "missing.env", resume_dir=tmp_path / "files")
    try:
        recovered.initialize()
        assert resume_pending(recovered)["profile_deletions_completed"] == 1
    finally:
        recovered.store.close()
    assert submit(browser, candidate).json()["cleanup_status"] == "completed"
    assert all(not backend.resume_files.path_for(key).exists() for key in keys)
    assert all(backend.resume_files.path_for(key).exists() for key in other)


def test_database_failure_rolls_back_profile_vectors_and_manifest(deletion, monkeypatch):
    app, browser, owners, candidates = deletion
    backend = app.state.backend
    original = backend.store.delete_candidate_profile
    def fail(*args, **kw):
        original(*args, **kw)
        raise RuntimeError("private-db-password")
    monkeypatch.setattr(backend.store, "delete_candidate_profile", fail)
    response = submit(browser, candidates[0])
    assert response.status_code == 503 and "private-db-password" not in response.text
    assert backend.get_candidate_profile(candidates[0], account_id=owners[0].id).id == candidates[0]
    with backend.store.connect() as conn:
        assert conn.execute("SELECT COUNT(*) AS n FROM profile_deletions").fetchone()["n"] == 0
        assert conn.execute("SELECT COUNT(*) AS n FROM long_texts WHERE candidate_id=?", (candidates[0],)).fetchone()["n"] > 0


def test_inflight_request_and_running_worker_prevent_deletion_without_partial_changes(deletion):
    app, browser, owners, candidates = deletion
    backend, owner, candidate = app.state.backend, owners[0].id, candidates[0]
    with backend.store.account_work.acquire(owner):
        response = submit(browser, candidate)
        assert response.status_code == 409 and response.json()["code"] == "PROFILE_DELETE_BUSY"
    task = backend.store.create_background_task(account_id=owner, candidate_id=candidate, task_type="system_probe")
    assert backend.store.claim_background_task(task.task_key).status == "running"
    assert submit(browser, candidate).status_code == 409
    backend.store.complete_background_task(task.task_key, {})
    assert submit(browser, candidate).status_code == 200


def test_cleanup_lease_and_batch_bound_and_account_deletion_manifest(deletion, monkeypatch):
    app, browser, owners, candidates = deletion
    backend, owner, candidate = app.state.backend, owners[0].id, candidates[0]
    keys = asset_files(backend, owner, candidate)
    assert submit(browser, candidate).json()["cleanup_status"] == "pending"
    started, finish = Event(), Event()
    original = backend.resume_files.delete
    def blocked(key):
        started.set()
        assert finish.wait(5)
        original(key)
    monkeypatch.setattr(backend.resume_files, "delete", blocked)
    with ThreadPoolExecutor(max_workers=1) as threads:
        pending = threads.submit(cleanup, backend, owner, candidate, force=True, batch_size=1)
        try:
            assert started.wait(3)
            assert cleanup(backend, owner, candidate, force=True)["cleanup_status"] == "pending"
        finally:
            finish.set()
        assert pending.result(timeout=5)["cleanup_status"] == "pending"
    assert not backend.resume_files.path_for(keys[0]).exists() and backend.resume_files.path_for(keys[1]).exists()
    assert backend.store.prepare_account_deletion(owner) == [keys[1]]


def test_hybrid_compatibility_only_forwards_and_preserves_pending_status(deletion, monkeypatch):
    app, browser, _, candidates = deletion
    expected = {"deleted": True, "candidate_id": candidates[0], "cleanup_status": "pending", "rag_cleanup": "database_cascade"}
    def forbidden(*a, **kw):
        pytest.fail("Hybrid compatibility must not delete locally")
    monkeypatch.setattr(app.state.backend, "request_candidate_profile_deletion", forbidden)
    def forward(session, candidate, **kw):
        assert session == "session-1" and candidate == candidates[0]
        return expected
    monkeypatch.setattr(app.state.backend.platform_auth_client, "delete_profile", forward)
    browser.cookies.set("job_agent_session", "session-1")
    response = browser.delete(f"/api/profiles/{candidates[0]}?account_id=999")
    assert response.status_code == 202 and response.json() == expected
    def offline(*a, **kw):
        raise PlatformAuthError("PROFILE_DELETE_UNAVAILABLE", "safe", 503)
    monkeypatch.setattr(app.state.backend.platform_auth_client, "delete_profile", offline)
    assert browser.delete(f"/api/profiles/{candidates[0]}").status_code == 503


def test_client_rejects_cross_resource_extra_fields_unknown_errors_and_redirects(deletion, monkeypatch):
    app, _, _, candidates = deletion
    value = {"deleted": True, "candidate_id": candidates[0], "cleanup_status": "pending", "rag_cleanup": "database_cascade"}
    current = [httpx.Response(200, json=value)]
    real = httpx.Client
    def handler(request):
        assert request.url.path == "/internal/v1/profiles/delete"
        assert json.loads(request.content) == {"session_token": "session-1", "candidate_id": candidates[0]}
        return current[0]
    monkeypatch.setattr(httpx, "Client", lambda **kw: real(**kw, transport=httpx.MockTransport(handler)))
    client = app.state.backend.platform_auth_client
    assert client.delete_profile("session-1", candidates[0]) == value
    for bad in (httpx.Response(200, json={**value, "candidate_id": candidates[1]}), httpx.Response(200, json={**value, "storage_keys": []}),
                httpx.Response(302), httpx.Response(401, json={"code": "INTERNAL_UNAUTHORIZED"}), httpx.Response(404, text="private")):
        current[0] = bad
        with pytest.raises(PlatformAuthError) as error:
            client.delete_profile("session-1", candidates[0])
        assert error.value.status_code == 503
    current[0] = httpx.Response(404, json={"code": "PROFILE_NOT_FOUND", "detail": "private upstream"})
    with pytest.raises(PlatformAuthError) as error:
        client.delete_profile("session-1", candidates[0])
    assert error.value.status_code == 404 and "private" not in str(error.value)


def test_busy_sync_pool_does_not_deadlock_delete_callback(deletion, monkeypatch):
    import anyio.to_thread
    app, browser, _, candidates = deletion
    async def setup():
        limiter = anyio.to_thread.current_default_thread_limiter()
        previous = limiter.total_tokens
        limiter.total_tokens = 1
        return asyncio.get_running_loop(), limiter, previous
    loop, limiter, previous = browser.portal.call(setup)
    def forward(session, candidate, **kw):
        async def call():
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://callback") as client:
                return await client.post("/internal/v1/profiles/delete", json={"session_token": session, "candidate_id": candidate},
                                         headers={"X-Internal-Service-Token": "service-secret"})
        response = asyncio.run_coroutine_threadsafe(call(), loop).result(timeout=5)
        assert response.status_code == 200, response.text
        value = response.json()
        value.pop("account_id")
        return value
    monkeypatch.setattr(app.state.backend.platform_auth_client, "delete_profile", forward)
    browser.cookies.set("job_agent_session", "session-1")
    try:
        assert browser.delete(f"/api/profiles/{candidates[0]}").status_code == 200
    finally:
        async def restore():
            limiter.total_tokens = previous
        browser.portal.call(restore)


def test_native_delete_returns_accepted_until_attachments_are_cleaned(tmp_path):
    app = create_web_app(env_file=tmp_path / "missing.env", resume_dir=tmp_path / "files")
    with TestClient(app) as browser:
        credentials = {"email": "native-delete@example.com", "password": "test-native-password"}
        owner = browser.post("/api/auth/register", json=credentials).json()["account"]["id"]
        assert browser.post("/api/auth/login", json=credentials).status_code == 200
        candidate = browser.post("/api/profiles", json={"name": "Native deletion"}).json()["candidate_id"]
        asset_files(app.state.backend, owner, candidate)
        response = browser.delete(f"/api/profiles/{candidate}")
        assert response.status_code == 202 and response.json()["cleanup_status"] == "pending"
        assert browser.get("/api/profiles").json() == {"profiles": []}
        assert browser.delete(f"/api/profiles/{candidate}").status_code == 202
        assert resume_pending(app.state.backend)["profile_deletions_completed"] == 1
        assert browser.delete(f"/api/profiles/{candidate}").status_code == 200


def test_lost_response_replays_and_stale_cache_cannot_resurrect_deleted_profile(deletion, monkeypatch):
    app, browser, owners, candidates = deletion
    backend, owner, candidate = app.state.backend, owners[0].id, candidates[0]
    profile = asdict(backend.get_candidate_profile(candidate, account_id=owner))
    values = {backend._profile_cache_key(owner, candidate): profile, backend._profile_list_cache_key(owner): [profile]}
    backend.business_cache = SimpleNamespace(get_json=values.get, delete=lambda *keys: None, set_json=lambda *a: None)
    original = backend.request_candidate_profile_deletion
    def lost(*args, **kw):
        original(*args, **kw)
        raise RuntimeError("response-lost-after-commit")
    with monkeypatch.context() as patch:
        patch.setattr(backend, "request_candidate_profile_deletion", lost)
        assert submit(browser, candidate).status_code == 503
    assert submit(browser, candidate).status_code == 200
    assert backend.list_candidate_profiles(account_id=owner) == []
    with pytest.raises(KeyError):
        backend.get_candidate_profile(candidate, account_id=owner)


def test_storage_success_without_database_ack_replays_after_lease_expiry(deletion, monkeypatch):
    app, browser, owners, candidates = deletion
    backend, owner, candidate = app.state.backend, owners[0].id, candidates[0]
    keys = asset_files(backend, owner, candidate)
    assert submit(browser, candidate).json()["cleanup_status"] == "pending"
    original = backend.store.connect
    @contextmanager
    def broken_connect():
        with original() as conn:
            class Proxy:
                def execute(self, sql, parameters=None):
                    if sql.startswith("UPDATE profile_deletions SET status="):
                        raise RuntimeError("progress acknowledgement lost")
                    return conn.execute(sql, parameters)
            yield Proxy()
    with monkeypatch.context() as patch:
        patch.setattr(backend.store, "connect", broken_connect)
        with pytest.raises(RuntimeError):
            cleanup(backend, owner, candidate, force=True)
    assert all(not backend.resume_files.path_for(key).exists() for key in keys)
    with backend.store.connect() as conn:
        row = conn.execute("SELECT status,storage_keys_json FROM profile_deletions WHERE account_id=? AND candidate_id=?", (owner, candidate)).fetchone()
        assert row["status"] == "pending" and sorted(json.loads(row["storage_keys_json"])) == keys
        conn.execute("UPDATE profile_deletions SET lease_until=now()-INTERVAL '1 second' WHERE account_id=?", (owner,))
    assert resume_pending(backend)["profile_deletions_completed"] == 1
