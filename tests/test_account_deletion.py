from types import SimpleNamespace
from concurrent.futures import ThreadPoolExecutor
import json

import httpx
import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient

from job_hunting_agent.account_deletion import dispatch_account_deletions, register_deletion_task
from job_hunting_agent.app import JobHuntingApp
from job_hunting_agent.background_tasks import run_registered_task
from job_hunting_agent.config import PlatformAuthSettings
from job_hunting_agent.database_migrations import upgrade_database
from job_hunting_agent.platform_auth import PlatformAuthClient, PlatformAuthError
from job_hunting_agent.object_storage import ObjectNotFoundError
from job_hunting_agent.task_queue import TaskQueueError
from job_hunting_agent.web import create_web_app


@pytest.fixture
def backend(tmp_path):
    app = JobHuntingApp(env_path=tmp_path / "missing.env", resume_dir=tmp_path / "files")
    app.initialize()
    owner = app.store.create_account(email="delete@example.com", password_hash="hash", display_name="Owner")
    app.store.prepare_account_deletion(owner.id)
    app.owner = owner.id
    yield app
    app.store.close()


def intent(owner, key="d" * 32):
    return {"account_id": owner, "request_key": "a" * 32, "task_key": key, "request_id": "delete-request"}


def stale_task(backend):
    with backend.store.connect() as conn:
        conn.execute("UPDATE accounts SET status='active' WHERE id=?", (backend.owner,))
    task = backend.store.create_background_task(account_id=backend.owner, task_type="system_probe")
    with backend.store.connect() as conn:
        conn.execute("UPDATE accounts SET status='disabled' WHERE id=?", (backend.owner,))
    return task


def test_task_registration_is_atomic_replayable_and_cancels_other_work(backend):
    stale = stale_task(backend)
    with ThreadPoolExecutor(max_workers=4) as threads:
        results = list(threads.map(lambda _: register_deletion_task(backend.store, intent(backend.owner)), range(4)))
    assert len({row.task_key for row in results}) == 1
    assert backend.store.get_background_task(stale.task_key).status == "cancelled"
    task = backend.store.get_background_task("d" * 32)
    assert task.payload == {"storage_keys": []} and task.max_attempts == 8
    backend.store.fail_queued_background_task(task.task_key, "manual retry required")
    assert register_deletion_task(backend.store, intent(backend.owner)).status == "failed"


def test_disabled_account_cannot_claim_normal_work_but_can_claim_cleanup(backend):
    stale = stale_task(backend)
    assert backend.store.claim_background_task(stale.task_key) is None
    assert backend.store.get_background_task(stale.task_key).status == "cancelled"
    deletion = register_deletion_task(backend.store, intent(backend.owner))
    assert backend.store.claim_background_task(deletion.task_key).status == "running"


def test_broker_outage_keeps_queued_task_and_recovers_with_same_key(backend):
    sent = []
    def offline(key):
        raise TaskQueueError("private broker details")
    backend.task_queue = SimpleNamespace(enqueue=offline)
    backend.platform_auth_client = SimpleNamespace(deletion_requests=lambda *a, **kw: {"requests": [intent(backend.owner)]})
    assert dispatch_account_deletions(backend)["deletion_deferred"] == 1
    assert backend.store.get_background_task("d" * 32).status == "queued"
    backend.task_queue = SimpleNamespace(enqueue=sent.append)
    assert dispatch_account_deletions(backend)["deletion_dispatched"] == 1
    assert sent == ["d" * 32]
    backend.store.claim_background_task("d" * 32)
    assert dispatch_account_deletions(backend)["deletion_dispatched"] == 0


def test_late_objects_are_durably_recorded_before_deletion_and_retry(backend, monkeypatch):
    task = register_deletion_task(backend.store, intent(backend.owner))
    stored = backend.resume_files.save(account_id=backend.owner, candidate_id=1, filename="late.txt", content=b"late data")
    backend.store.register_knowledge_asset(account_id=backend.owner, candidate_id=None, asset_kind="document", title="Late",
                                          original_filename="late.txt", storage_key=stored.storage_key, media_type="text/plain",
                                          file_size=stored.file_size, sha256=stored.sha256)
    completed = []
    backend.platform_auth_client = SimpleNamespace(account=lambda *a, **kw: completed.append(kw) or {"deleted": True})
    original_delete = backend.resume_files.delete
    def unavailable(key):
        raise RuntimeError("private storage error")
    monkeypatch.setattr(backend.resume_files, "delete", unavailable)
    with pytest.raises(RuntimeError):
        run_registered_task(backend, task.task_key)
    assert backend.store.get_background_task(task.task_key).payload["storage_keys"] == [stored.storage_key]
    assert backend.store.get_account(backend.owner).deleted_at is None and not completed
    backend.store.requeue_background_task(task.task_key, "retry")
    monkeypatch.setattr(backend.resume_files, "delete", original_delete)
    assert run_registered_task(backend, task.task_key)["status"] == "succeeded"
    assert completed[0]["account_id"] == backend.owner and completed[0]["task_key"] == task.task_key
    with pytest.raises(ObjectNotFoundError):
        backend.resume_files.read(stored.storage_key)
    assert backend.store.list_knowledge_assets(account_id=backend.owner) == []


def test_waiting_for_requests_does_not_consume_java_cleanup_retry_budget(backend):
    with backend.store.connect() as conn:
        conn.execute("UPDATE accounts SET status='active' WHERE id=?", (backend.owner,))
    lease = backend.store.account_work.acquire(backend.owner)
    backend.store.prepare_account_deletion(backend.owner)
    task = register_deletion_task(backend.store, intent(backend.owner))
    completed = []
    backend.platform_auth_client = SimpleNamespace(account=lambda *a, **kw: completed.append(kw))
    try:
        for _ in range(12):
            result = run_registered_task(backend, task.task_key)
            assert result["status"] == "queued" and result["deferred"] is True
            assert backend.store.get_background_task(task.task_key).attempt == 0
            assert backend.store.get_account(backend.owner).deleted_at is None
        assert not completed
    finally:
        lease.release()
    assert run_registered_task(backend, task.task_key)["status"] == "succeeded"
    assert len(completed) == 1


@pytest.mark.parametrize("status", [202, 400, 401, 403, 409, 422, 503])
def test_compatibility_delete_only_forwards_and_clears_cookies_after_admission(tmp_path, monkeypatch, status):
    app = create_web_app(env_file=tmp_path / "missing.env")
    with TestClient(app) as browser:
        backend = app.state.backend
        backend.platform_auth_client = PlatformAuthClient(PlatformAuthSettings(True, "http://java", "service-secret", 5))
        browser.cookies.set("job_agent_session", "session-secret")
        def forbidden(*a, **kw):
            pytest.fail("Compatibility route must not mutate or clean up Python data")
        for method in ("prepare_account_deletion", "create_background_task", "collect_account_deletion_storage_keys"):
            monkeypatch.setattr(backend.store, method, forbidden)
        def submit(operation, **data):
            assert operation == "request"
            assert data == {"session_token": "session-secret", "current_password": "password-secret", "confirmation": "注销账号"}
            if status != 202:
                raise PlatformAuthError("REJECTED", "rejected", status)
            return {"ok": True, "status": "accepted", "task_key": "d" * 32}
        monkeypatch.setattr(backend.platform_auth_client, "deletion_requests", submit)
        result = browser.post("/api/account/delete", json={"current_password": "password-secret", "confirmation": "注销账号", "account_id": 999})
        assert result.status_code == status
        assert bool(result.headers.get("set-cookie")) == (status == 202)


@pytest.mark.parametrize("operation", ["request", "due"])
def test_deletion_client_limits_response_and_never_reflects_upstream_secrets(monkeypatch, operation):
    real_client = httpx.Client
    payload = {"ok": True, "status": "accepted", "task_key": "d" * 32} if operation == "request" else {"requests": [intent(1)]}
    response = [httpx.Response(202 if operation == "request" else 200, json=payload)]
    def handler(request):
        assert request.url.path == f"/internal/v1/accounts/deletions/{operation}"
        assert "account_id" not in json.loads(request.content)
        return response[0]
    monkeypatch.setattr(httpx, "Client", lambda **kwargs: real_client(**kwargs, transport=httpx.MockTransport(handler)))
    client = PlatformAuthClient(PlatformAuthSettings(True, "http://java", "service-secret", 5))
    data = {"session_token": "session-secret", "current_password": "password-secret", "confirmation": "注销账号"} if operation == "request" else {"limit": 20}
    assert client.deletion_requests(operation, **data) == payload
    for bad in [httpx.Response(302, text="private secret", headers={"location": "http://evil.example"}),
                httpx.Response(200, content=b"x" * 65537), httpx.Response(200, json={"password": "private secret"})]:
        response[0] = bad
        with pytest.raises(PlatformAuthError) as error:
            client.deletion_requests(operation, **data)
        assert error.value.status_code == 503 and "private secret" not in str(error.value)


def test_migration_recovers_only_explicit_admissions_and_preserves_existing_task(temporary_database_url):
    upgrade_database(temporary_database_url, "20261017_0024")
    engine = sa.create_engine(temporary_database_url)
    try:
        with engine.begin() as conn:
            for owner in (1, 2, 3, 4):
                conn.execute(sa.text("""INSERT INTO accounts(id,email,password_hash,role,status,created_at,updated_at)
                                        VALUES (:id,:email,'hash','user','disabled',now(),now())"""), {"id": owner, "email": f"old{owner}@example.com"})
            for owner in (1, 2, 4):
                conn.execute(sa.text("""INSERT INTO admin_audit_events(target_account_id,action,target_type,outcome,summary,details_json,created_at)
                                        VALUES (:id,'account.deletion_requested','account','succeeded','admitted','{}',now())"""), {"id": owner})
            conn.execute(sa.text("""INSERT INTO admin_audit_events(target_account_id,action,target_type,outcome,summary,details_json,created_at)
                                    VALUES (4,'account.status_updated','account','succeeded','admin intervention','{}',now())"""))
            conn.execute(sa.text("""INSERT INTO background_tasks(task_key,account_id,task_type,status,progress,attempt,max_attempts,
                                    payload_json,result_json,created_at,updated_at)
                                    VALUES ('legacy-task',2,'account_deletion','queued',0,0,8,'{"storage_keys":[]}','{}',now(),now())"""))
        upgrade_database(temporary_database_url)
        with engine.connect() as conn:
            rows = conn.execute(sa.text("SELECT account_id,task_key FROM platform_account_deletion_requests ORDER BY account_id")).all()
            assert [row.account_id for row in rows] == [1, 2]
            assert rows[1].task_key == "legacy-task"
    finally:
        engine.dispose()
