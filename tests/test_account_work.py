import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from threading import Event
from types import SimpleNamespace

import pytest
import sqlalchemy as sa
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage
from starlette.requests import ClientDisconnect

from job_hunting_agent.account_work import AccountWorkBusy, AccountWorkDenied, AccountWorkGuard, AccountWorkRoute, AccountWorkStreamingResponse
from job_hunting_agent.account_deletion import register_deletion_task
from job_hunting_agent.agent import JobHuntingAgent
from job_hunting_agent.background_tasks import run_registered_task
from job_hunting_agent.sqlalchemy_store import SQLAlchemyStore
from job_hunting_agent.web import ChatPayload, create_web_app, stream_web_chat_events


@pytest.fixture
def store(database_url):
    store = SQLAlchemyStore(database_url)
    account = store.create_account(email="drain@example.com", password_hash="hash", display_name="Drain")
    store.owner = account.id
    yield store
    store.close()


def test_independent_guards_share_account_lock_but_not_other_accounts(store, database_url, temporary_database_url):
    other = store.create_account(email="other@example.com", password_hash="hash", display_name="Other")
    guard = AccountWorkGuard(database_url)
    with store.account_work.acquire(store.owner), guard.acquire(store.owner):
        with pytest.raises(AccountWorkBusy):
            guard.acquire(store.owner, deletion=True)
        with guard.deletion(other.id):
            pass
        with AccountWorkGuard(temporary_database_url).deletion(store.owner):
            pass
    with guard.deletion(store.owner):
        with pytest.raises(AccountWorkBusy):
            store.account_work.acquire(store.owner)
    with store.account_work.acquire(store.owner):
        pass


def test_disabled_account_cannot_enter_even_after_cleaner_releases_lock(store):
    with store.connect() as conn:
        conn.execute("UPDATE accounts SET status='disabled' WHERE id=?", (store.owner,))
    for _ in range(10):
        with pytest.raises(AccountWorkDenied):
            store.account_work.acquire(store.owner)
    with store.account_work.deletion(store.owner):
        pass


def test_guard_connection_budget_and_lease_release_are_bounded_and_idempotent(store, database_url):
    guard = AccountWorkGuard(database_url, max_connections=1)
    lease = guard.acquire(store.owner)
    try:
        with pytest.raises(AccountWorkBusy):
            guard.acquire(store.owner)
        # The guard does not keep an idle transaction open during a model call.
        pid = lease._connection.execute(sa.text("SELECT pg_backend_pid()")).scalar_one()
        with store.connect() as conn:
            row = conn.execute("SELECT state,xact_start IS NULL AS no_transaction FROM pg_stat_activity WHERE pid=?", (pid,)).fetchone()
            assert row["state"] == "idle" and row["no_transaction"] is True
    finally:
        lease.release()
        lease.release()
    with guard.deletion(store.owner):
        pass


def test_failed_connection_releases_capacity_and_redacts_http_error(store, monkeypatch):
    guard = AccountWorkGuard(store.database_url, max_connections=1)
    original = guard._engine.connect
    def unavailable():
        raise RuntimeError("private-database-password")
    monkeypatch.setattr(guard._engine, "connect", unavailable)
    for _ in range(2):
        with pytest.raises(HTTPException) as error:
            guard.protect_request(Request({"type": "http", "state": {}}), store.owner)
        assert error.value.status_code == 503 and "private" not in error.value.detail
    monkeypatch.setattr(guard._engine, "connect", original)
    with guard.acquire(store.owner):
        pass


@pytest.mark.parametrize("outcome", ["success", "exception", "stream", "cancel"])
def test_route_keeps_guard_until_stream_finishes_and_releases_on_every_exit(store, outcome):
    app = FastAPI()
    app.router.route_class = AccountWorkRoute
    @app.get("/work")
    async def work(request: Request):
        store.account_work.protect_request(request, store.owner)
        # Calling current_account twice must not consume a second slot/lock.
        store.account_work.protect_request(request, store.owner)
        if outcome == "exception":
            raise HTTPException(400, "rejected")
        async def body():
            yield b"first"
            with pytest.raises(AccountWorkBusy):
                store.account_work.acquire(store.owner, deletion=True)
            if outcome == "cancel":
                raise asyncio.CancelledError()
            yield b"last"
        if outcome in {"stream", "cancel"}:
            return StreamingResponse(body())
        return {"ok": True}
    with TestClient(app) as client:
        if outcome == "cancel":
            client.get("/work")
        else:
            response = client.get("/work")
            assert response.status_code == (400 if outcome == "exception" else 200)
            if outcome == "stream":
                assert response.content == b"firstlast"
    with store.account_work.deletion(store.owner):
        pass


def test_stream_disconnect_releases_guard(store):
    app = FastAPI()
    app.router.route_class = AccountWorkRoute
    @app.get("/stream")
    async def stream(request: Request):
        store.account_work.protect_request(request, store.owner)
        async def body():
            yield b"first"
            await asyncio.sleep(3600)
        return StreamingResponse(body())
    async def run():
        sent = asyncio.Event()
        async def send(message):
            if message["type"] == "http.response.body":
                with pytest.raises(AccountWorkBusy):
                    store.account_work.acquire(store.owner, deletion=True)
                sent.set()
        async def receive():
            await sent.wait()
            return {"type": "http.disconnect"}
        await asyncio.wait_for(app({"type": "http", "method": "GET", "path": "/stream", "headers": [],
                                   "query_string": b"", "state": {}, "asgi": {"version": "3.0", "spec_version": "2.0"}}, receive, send), 5)
    asyncio.run(run())
    with store.account_work.deletion(store.owner):
        pass


def test_disconnect_does_not_release_guard_while_sync_stream_thread_is_working(store):
    app = FastAPI()
    app.router.route_class = AccountWorkRoute
    started, finish = Event(), Event()
    @app.get("/stream")
    def stream(request: Request):
        store.account_work.protect_request(request, store.owner)
        def body():
            yield b"first"
            started.set()
            assert finish.wait(5)
            yield b"last"
        return StreamingResponse(body())
    async def run():
        disconnect = asyncio.Event()
        async def send(message):
            pass
        async def receive():
            await disconnect.wait()
            return {"type": "http.disconnect"}
        task = asyncio.create_task(app({"type": "http", "method": "GET", "path": "/stream", "headers": [],
                                        "query_string": b"", "state": {}, "asgi": {"version": "3.0", "spec_version": "2.0"}}, receive, send))
        try:
            assert await asyncio.to_thread(started.wait, 3)
            disconnect.set()
            await asyncio.sleep(0.05)
            with pytest.raises(AccountWorkBusy):
                store.account_work.acquire(store.owner, deletion=True)
        finally:
            finish.set()
            await asyncio.wait_for(task, 5)
    asyncio.run(run())
    with store.account_work.deletion(store.owner):
        pass


def test_disconnect_explicitly_closes_web_agent_and_graph_before_unlock(store):
    closed = []
    def graph_stream():
        try:
            yield AIMessage(content="partial"), {}
            pytest.fail("Disconnected stream must not request another graph event")
        finally:
            with pytest.raises(AccountWorkBusy):
                store.account_work.acquire(store.owner, deletion=True)
            closed.append("graph")
    graph_events = graph_stream()
    agent = SimpleNamespace(
        app=SimpleNamespace(store=SimpleNamespace(assert_account_can_spend=lambda *a: None)), build_turn_messages=lambda *a: [],
        _route_message=lambda *a, **kw: None, _execute_direct_route=lambda *a, **kw: None,
        routing_metrics=SimpleNamespace(record_decision=lambda *a, **kw: None),
        graph=SimpleNamespace(stream=lambda *a, **kw: graph_events), tool_llm_available=False,
    )
    agent_events = JobHuntingAgent.stream_chat(agent, "hello", candidate_id=1, account_id=store.owner)
    web_events = stream_web_chat_events(SimpleNamespace(store=store), SimpleNamespace(stream_chat=lambda *a, **kw: agent_events),
                                       ChatPayload(candidate_id=1, message="hello", use_env_llm=True), "hello", "session", store.owner)
    app = FastAPI()
    app.router.route_class = AccountWorkRoute
    @app.get("/stream")
    async def stream(request: Request):
        store.account_work.protect_request(request, store.owner)
        return AccountWorkStreamingResponse(web_events)
    async def run():
        async def send(message):
            if message["type"] == "http.response.body" and b"event: token" in message.get("body", b""):
                raise OSError("client disconnected")
        async def receive():
            await asyncio.sleep(3600)
        with pytest.raises(ClientDisconnect):
            await app({"type": "http", "method": "GET", "path": "/stream", "headers": [], "query_string": b"",
                       "state": {}, "asgi": {"version": "3.0", "spec_version": "2.4"}}, receive, send)
    # Keep all three generators referenced: closure must not depend on their GC.
    asyncio.run(run())
    assert closed == ["graph"]
    with store.account_work.deletion(store.owner):
        pass


def test_cached_java_active_session_cannot_admit_write_after_disable(tmp_path):
    app = create_web_app(env_file=tmp_path / "missing.env")
    backend = app.state.backend
    owner = backend.store.create_account(email="java-stale@example.com", password_hash="hash", display_name="Owner")
    snapshot = asdict(owner)
    snapshot["account_id"] = snapshot.pop("id")
    calls = []
    def resolve(*args, **kwargs):
        calls.append(args)
        backend.store.prepare_account_deletion(owner.id)
        return {"account": snapshot}
    backend.platform_auth_client = SimpleNamespace(account=resolve, internal_token="service-secret")
    with TestClient(app) as browser:
        browser.cookies.set("job_agent_session", "opaque-session")
        response = browser.post("/internal/v1/profiles/create", json={"session_token": "opaque-session",
            "idempotency_key": "stale-intent", "profile": {"name": "Stale authentication"}},
            headers={"X-Internal-Service-Token": "service-secret"})
        assert response.status_code == 403 and len(calls) == 1
        assert backend.store.list_candidate_profiles(account_id=owner.id) == []
    with backend.store.account_work.deletion(owner.id):
        pass


def test_real_profile_write_drains_before_cleanup_and_no_rows_survive(tmp_path, monkeypatch):
    app = create_web_app(env_file=tmp_path / "missing.env")
    backend = app.state.backend
    started, finish = Event(), Event()
    with TestClient(app) as client:
        credentials = {"email": "drain-profile@example.com", "password": "password-123"}
        owner = client.post("/api/auth/register", json=credentials).json()["account"]["id"]
        assert client.post("/api/auth/login", json=credentials).status_code == 200
        original = backend.save_candidate_profile
        def slow_write(*args, **kwargs):
            started.set()
            assert finish.wait(10)
            return original(*args, **kwargs)
        monkeypatch.setattr(backend, "save_candidate_profile", slow_write)
        with ThreadPoolExecutor(max_workers=1) as threads:
            pending = threads.submit(client.post, "/api/profiles", json={"name": "Late profile"})
            try:
                assert started.wait(5)
                backend.store.prepare_account_deletion(owner)
                task = register_deletion_task(backend.store, {"account_id": owner, "task_key": "e" * 32})
                with pytest.raises(AccountWorkBusy):
                    run_registered_task(backend, task.task_key)
                assert backend.store.get_account(owner).deleted_at is None
            finally:
                finish.set()
            assert pending.result(timeout=10).status_code == 200
        backend.store.requeue_background_task(task.task_key, "drained")
        assert run_registered_task(backend, task.task_key)["status"] == "succeeded"
        assert backend.store.list_candidate_profiles(account_id=owner) == []
        assert client.post("/api/profiles", json={"name": "Too late"}).status_code == 401


def test_closed_database_session_releases_advisory_lock(store, database_url):
    guard = AccountWorkGuard(database_url)
    lease = guard.acquire(store.owner)
    try:
        pid = lease._connection.execute(sa.text("SELECT pg_backend_pid()")).scalar_one()
        with store.connect() as conn:
            conn.execute("SELECT pg_terminate_backend(?)", (pid,))
        with store.account_work.deletion(store.owner):
            pass
    finally:
        lease.release()
