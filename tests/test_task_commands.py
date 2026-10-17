from concurrent.futures import ThreadPoolExecutor

import pytest
import httpx
from fastapi.testclient import TestClient

from job_hunting_agent.app import JobHuntingApp
from job_hunting_agent.task_commands import TaskCommands, TaskCommandConflict
from job_hunting_agent.task_queue import TaskQueueError
from job_hunting_agent.config import PlatformAuthSettings
from job_hunting_agent.platform_auth import PlatformAuthClient, PlatformAuthError, PlatformAuthUnavailableError
from job_hunting_agent.web import create_web_app


class Queue:
    def __init__(self):
        self.sent = []
        self.offline = False

    def health_check(self):
        pass

    def enqueue(self, key):
        if self.offline:
            raise TaskQueueError("private broker detail")
        self.sent.append(key)


def setup_commands(tmp_path):
    app = JobHuntingApp(env_path=tmp_path / "missing.env")
    app.initialize()
    actor = app.store.create_account(email="commands@example.com", password_hash="hash", display_name="Admin", role="admin")
    queue = Queue()
    return app, actor, queue, TaskCommands(app.store, queue)


def test_probe_command_replay_has_one_task_delivery_and_audit(tmp_path):
    app, actor, queue, commands = setup_commands(tmp_path)
    try:
        first = commands.submit(actor.id, "a" * 64, "probe", None, "command-trace-123")
        replay = commands.submit(actor.id, "a" * 64, "probe", None, "replay-trace-123")
        assert first.task_key == replay.task_key
        assert queue.sent == [first.task_key]
        audits = app.store.list_admin_audit_events(limit=100)
        assert len(audits) == 1 and audits[0].request_id == "command-trace-123"
        assert first.payload == {"purpose": "admin_runtime_probe"}
    finally:
        app.store.close()


def test_failed_publish_survives_restart_and_replay_does_not_duplicate_audit(tmp_path):
    app, actor, queue, commands = setup_commands(tmp_path)
    try:
        queue.offline = True
        with pytest.raises(TaskQueueError):
            commands.submit(actor.id, "b" * 64, "probe", None, "first-trace-123")
        tasks = app.store.list_background_tasks(account_id=actor.id)
        assert len(tasks) == 1 and tasks[0].status == "queued"
        queue.offline = False
        recovered = TaskCommands(app.store, queue)
        assert recovered.dispatch_pending()["dispatched_commands"] == 1
        replay = recovered.submit(actor.id, "b" * 64, "probe", None, "replay-trace-123")
        assert replay.task_key == tasks[0].task_key and queue.sent == [replay.task_key]
        assert len(app.store.list_admin_audit_events(limit=100)) == 1
        with pytest.raises(TaskCommandConflict):
            commands.submit(actor.id, "b" * 64, "retry", replay.task_key, "conflict-trace-123")
    finally:
        app.store.close()


def test_retry_replay_never_resets_running_or_completed_task(tmp_path):
    app, actor, queue, commands = setup_commands(tmp_path)
    try:
        failed = app.store.create_background_task(account_id=actor.id, task_type="system_probe")
        app.store.fail_queued_background_task(failed.task_key, "test failure")
        retried = commands.submit(actor.id, "c" * 64, "retry", failed.task_key, "retry-trace-123")
        assert retried.status == "queued" and queue.sent == [failed.task_key]
        assert app.store.claim_background_task(failed.task_key) is not None
        app.store.update_background_task_progress(failed.task_key, 60)
        assert commands.submit(actor.id, "c" * 64, "retry", failed.task_key, "replay-trace-123").progress == 60
        app.store.complete_background_task(failed.task_key, {"ok": True})
        assert commands.submit(actor.id, "c" * 64, "retry", failed.task_key, "replay-trace-123").status == "succeeded"
        assert queue.sent == [failed.task_key]
        assert len(app.store.list_admin_audit_events(limit=100)) == 1
        with pytest.raises(TaskCommandConflict):
            commands.submit(actor.id, "d" * 64, "retry", failed.task_key, "new-trace-123")
    finally:
        app.store.close()


def test_concurrent_same_command_creates_one_task_and_audit(tmp_path):
    app, actor, queue, commands = setup_commands(tmp_path)
    def submit(_):
        try:
            return commands.submit(actor.id, "e" * 64, "probe", None, "concurrent-trace-123").task_key
        except TaskQueueError:
            return None
    try:
        with ThreadPoolExecutor(max_workers=4) as pool:
            keys = list(pool.map(submit, range(4)))
        assert len({key for key in keys if key is not None}) == 1
        assert len(queue.sent) == 1
        assert len(app.store.list_background_tasks(account_id=actor.id)) == 1
        assert len(app.store.list_admin_audit_events(limit=100)) == 1
    finally:
        app.store.close()


def test_audit_failure_rolls_back_intent_and_task(tmp_path, monkeypatch):
    app, actor, queue, commands = setup_commands(tmp_path)
    def fail(*args):
        raise RuntimeError("audit unavailable")
    try:
        monkeypatch.setattr(app.store, "_insert_admin_audit_event", fail)
        with pytest.raises(RuntimeError):
            commands.submit(actor.id, "f" * 64, "probe", None, "audit-trace-123")
        assert app.store.list_background_tasks(account_id=actor.id) == []
        assert commands.dispatch_pending()["dispatched_commands"] == 0 and queue.sent == []
    finally:
        app.store.close()


def test_pending_command_is_cancelled_if_actor_disabled(tmp_path):
    app, actor, queue, commands = setup_commands(tmp_path)
    try:
        queue.offline = True
        with pytest.raises(TaskQueueError):
            commands.submit(actor.id, "1" * 64, "probe", None, "disable-trace-123")
        with app.store.connect() as conn:
            conn.execute("UPDATE accounts SET status='disabled' WHERE id=?", (actor.id,))
        queue.offline = False
        assert commands.dispatch_pending()["dispatched_commands"] == 0 and queue.sent == []
        assert app.store.list_background_tasks(account_id=actor.id)[0].status == "failed"
        with pytest.raises(PermissionError):
            commands.submit(actor.id, "1" * 64, "probe", None, "replay-trace-123")
    finally:
        app.store.close()


@pytest.mark.parametrize("terminal", ["failed", "cancelled"])
def test_undispatched_terminal_task_is_not_reported_as_success(tmp_path, terminal):
    app, actor, queue, commands = setup_commands(tmp_path)
    try:
        queue.offline = True
        with pytest.raises(TaskQueueError):
            commands.submit(actor.id, "4" * 64, "probe", None, "terminal-trace-123")
        task = app.store.list_background_tasks(account_id=actor.id)[0]
        if terminal == "failed":
            app.store.fail_queued_background_task(task.task_key, "cancelled by maintenance")
        else:
            app.store.cancel_background_task(task.task_key, actor.id)
        queue.offline = False
        with pytest.raises(TaskCommandConflict):
            commands.submit(actor.id, "4" * 64, "probe", None, "replay-trace-123")
        assert queue.sent == []
    finally:
        app.store.close()


def test_deletion_cleanup_of_disabled_account_can_be_retried_and_recovered(tmp_path):
    app, actor, queue, commands = setup_commands(tmp_path)
    try:
        target = app.store.create_account(email="deleted@example.com", password_hash="hash", display_name="Target")
        deletion = app.store.create_background_task(account_id=target.id, task_type="account_deletion")
        other = app.store.create_background_task(account_id=target.id, task_type="system_probe")
        for task in (deletion, other):
            app.store.fail_queued_background_task(task.task_key, "test failure")
        with app.store.connect() as conn:
            conn.execute("UPDATE accounts SET status='disabled' WHERE id=?", (target.id,))
        with pytest.raises(TaskCommandConflict):
            commands.submit(actor.id, "5" * 64, "retry", other.task_key, "disabled-trace-123")
        queue.offline = True
        with pytest.raises(TaskQueueError):
            commands.submit(actor.id, "6" * 64, "retry", deletion.task_key, "deletion-trace-123")
        queue.offline = False
        assert commands.dispatch_pending()["dispatched_commands"] == 1
        replay = commands.submit(actor.id, "6" * 64, "retry", deletion.task_key, "replay-trace-123")
        assert replay.status == "queued" and queue.sent == [deletion.task_key]
    finally:
        app.store.close()


def test_uncertain_publish_after_worker_completion_does_not_execute_twice(tmp_path):
    app, actor, queue, commands = setup_commands(tmp_path)
    class AmbiguousQueue:
        calls = 0
        def enqueue(self, key):
            self.calls += 1
            assert app.store.claim_background_task(key) is not None
            app.store.complete_background_task(key, {"ok": True})
            raise TaskQueueError("lost publish acknowledgement")
    try:
        broker = AmbiguousQueue()
        commands = TaskCommands(app.store, broker)
        with pytest.raises(TaskQueueError):
            commands.submit(actor.id, "2" * 64, "probe", None, "uncertain-trace-123")
        replay = commands.submit(actor.id, "2" * 64, "probe", None, "replay-trace-123")
        assert replay.status == "succeeded" and broker.calls == 1
    finally:
        app.store.close()


def test_internal_commands_require_service_token_and_real_admin_session(tmp_path):
    queue = Queue()
    app = create_web_app(env_file=tmp_path / "missing.env", task_queue=queue)
    class Auth:
        internal_token = "internal-secret"
        role = "admin"
        revoked = False
        calls = 0
        def account(self, operation, **data):
            self.calls += 1
            if self.revoked or data["session_token"] != "actual-session":
                raise PlatformAuthError("SESSION_EXPIRED", "expired", 401)
            return {"account": {"account_id": actor.id, "role": self.role, "status": "active"}}
    with TestClient(app) as browser:
        actor = app.state.backend.store.create_account(email="internal@example.com", password_hash="hash", display_name="Admin", role="admin")
        auth = Auth()
        app.state.backend.platform_auth_client = auth
        body = {"command_id": "3" * 64, "operation": "probe", "task_key": None,
                "session_token": "actual-session", "request_id": "internal-command-trace"}
        path = "/internal/v1/task-commands"
        assert browser.post(path, json=body).status_code == 401
        assert auth.calls == 0 and queue.sent == []
        headers = {"X-Internal-Service-Token": auth.internal_token}
        assert browser.post(path, json={**body, "actor_account_id": actor.id}, headers=headers).status_code == 422
        auth.role = "user"
        assert browser.post(path, json=body, headers=headers).status_code == 403
        auth.role = "admin"
        response = browser.post(path, json=body, headers=headers)
        assert response.status_code == 200 and len(response.json()["task"]) == 12, response.text
        assert browser.post(path, json=body, headers=headers).json() == response.json()
        assert len(queue.sent) == 1
        auth.revoked = True
        assert browser.post(path, json=body, headers=headers).status_code == 401
        assert len(queue.sent) == 1


@pytest.mark.parametrize("status", [401, 403, 404, 409, 422, 503])
def test_command_compatibility_never_performs_local_write(tmp_path, monkeypatch, status):
    app = create_web_app(env_file=tmp_path / "missing.env", task_queue=Queue())
    with TestClient(app) as browser:
        credentials = {"email": "compat@example.com", "password": "password-123"}
        browser.post("/api/auth/register", json=credentials).raise_for_status()
        browser.post("/api/auth/login", json=credentials).raise_for_status()
        def request(url, **kwargs):
            if url.endswith("/internal/v1/auth/accounts/me"):
                return httpx.Response(401, json={"code": "SESSION_EXPIRED", "message": "expired"})
            assert url.endswith("/internal/v1/tasks/admin/command")
            assert kwargs["json"]["idempotency_key"] == "stable-command-key"
            assert kwargs["json"]["session_token"] == browser.cookies["job_agent_session"]
            return httpx.Response(status, json={"code": "TASK_COMMAND_ERROR", "message": "request rejected"})
        monkeypatch.setattr(httpx, "post", request)
        app.state.backend.platform_auth_client = PlatformAuthClient(PlatformAuthSettings(True, "http://java", "secret", 5))
        def forbidden(*a, **k):
            pytest.fail("Local command fallback")
        monkeypatch.setattr(app.state.backend, "retry_background_task", forbidden)
        monkeypatch.setattr(app.state.backend, "enqueue_system_probe", forbidden)
        for path in ("/api/admin/tasks/probe", "/api/admin/tasks/task-1/retry"):
            result = browser.post(path, headers={"Idempotency-Key": "stable-command-key"})
            assert result.status_code == status, result.text


def test_command_client_rejects_malformed_success_and_transport_errors(monkeypatch):
    client = PlatformAuthClient(PlatformAuthSettings(True, "http://java", "secret", 5))
    for response in (httpx.Response(200, text="html"), httpx.Response(200, json={}), httpx.Response(200, json={"task": {"payload": "secret"}})):
        monkeypatch.setattr(httpx, "post", lambda *a, **k: response)
        with pytest.raises(PlatformAuthError) as error:
            client.task_command(session_token="session", operation="probe", task_key=None, idempotency_key="stable-key")
        assert error.value.status_code == 502
    def unavailable(*a, **k):
        raise httpx.ConnectError("private downstream detail")
    monkeypatch.setattr(httpx, "post", unavailable)
    with pytest.raises(PlatformAuthUnavailableError):
        client.task_command(session_token="session", operation="probe", task_key=None, idempotency_key="stable-key")
