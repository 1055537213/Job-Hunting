from dataclasses import asdict
from types import SimpleNamespace
from unittest.mock import Mock
import asyncio
from threading import Event
import json
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from job_hunting_agent.account_work import AccountWorkBusy
from job_hunting_agent.chat_session_query import validate_chat_projection
from job_hunting_agent.config import PlatformAuthSettings
from job_hunting_agent.models import CandidateProfileInput
from job_hunting_agent.models import AgentChatResult
from job_hunting_agent.storage import InsufficientBalanceError
from job_hunting_agent.platform_auth import PlatformAuthClient, PlatformAuthError
from job_hunting_agent.web import create_web_app, InternalChatInvokePayload, ChatPayload, stream_web_chat_events


@pytest.fixture
def chat_callback(tmp_path, monkeypatch):
    agent = SimpleNamespace(chat=Mock(return_value=AgentChatResult(reply="Done.", candidate_id=None,
        session_id="", mode="langchain_agent")))
    app = create_web_app(env_file=tmp_path / "missing.env", chat_agent=agent)
    app.state.test_agent = agent
    backend = app.state.backend
    owners, candidates = [], []
    for index in (1, 2):
        owner = backend.store.create_account(email=f"chat-{index}@example.com", password_hash="hash", display_name="Owner")
        owners.append(owner)
        candidates.append(backend.save_candidate_profile(CandidateProfileInput(name=f"Private {index}", status="student",
            education="bachelor", experience_years=0, skills={}, preferred_cities=[], salary_floor_k=None,
            expected_salary_k=None, target_directions=[]), account_id=owner.id))
    backend.platform_auth_client = PlatformAuthClient(PlatformAuthSettings(True, "http://java", "service-secret", 5))

    def resolve(operation, **data):
        assert operation == "me"
        if data["session_token"] == "expired":
            raise PlatformAuthError("SESSION_EXPIRED", "secret", 401)
        value = asdict(owners[int(data["session_token"][-1])-1])
        value["account_id"] = value.pop("id")
        return {"account": value}

    monkeypatch.setattr(backend.platform_auth_client, "account", resolve)
    with TestClient(app) as browser:
        yield app, browser, owners, candidates


def callback(browser, operation, **data):
    return browser.post(f"/internal/v1/chat/sessions/{operation}",
        json={"session_token": "session-1", **data}, headers={"X-Internal-Service-Token": "service-secret"})


def test_callback_ownership_history_sanitization_archive_and_delete(chat_callback):
    app, browser, owners, candidates = chat_callback
    assert browser.post("/internal/v1/chat/sessions/list", json={"session_token": "session-1"}).status_code == 401
    assert callback(browser, "list", session_token="expired").status_code == 401
    created = callback(browser, "create", candidate_id=candidates[0], title="Interview")
    assert created.status_code == 200, created.text
    key = created.json()["session"]["session_id"]
    backend = app.state.backend
    backend.save_chat_message(candidates[0], key, "assistant", "完成。", metadata={"private": "private-password"}, account_id=owners[0].id)
    history = callback(browser, "history", candidate_id=candidates[0], session_id=key)
    assert history.status_code == 200, history.text
    assert history.json()["messages"][0]["metadata"] == {} and "private-password" not in history.text
    assert history.headers["Cache-Control"] == "no-store"
    assert len(callback(browser, "list", candidate_id=candidates[0]).json()["sessions"]) == 1
    for operation in ("history", "archive", "delete"):
        args = {"session_id": key, "session_token": "session-2"}
        if operation == "history":
            args["candidate_id"] = candidates[1]
        response = callback(browser, operation, **args)
        assert response.status_code in (403, 404) or response.json().get("messages") == [], response.text
        assert "Interview" not in response.text and "完成" not in response.text
    assert callback(browser, "create", candidate_id=candidates[1]).status_code == 404
    assert callback(browser, "create", candidate_id=candidates[0], job_id=999999).status_code == 404
    assert callback(browser, "archive", session_id=key).json()["session"]["status"] == "archived"
    assert callback(browser, "archive", session_id=key).status_code == 200
    assert callback(browser, "list").json()["sessions"] == []
    assert len(callback(browser, "list", include_archived=True).json()["sessions"]) == 1
    assert callback(browser, "history", candidate_id=candidates[0], session_id=key).status_code == 409
    assert callback(browser, "delete", session_id=key).json()["deleted"] is True
    assert callback(browser, "delete", session_id=key).status_code == 404
    assert backend.list_chat_messages(candidates[0], key, account_id=owners[0].id) == []


def test_callback_busy_failure_and_recovery(chat_callback, monkeypatch):
    app, browser, owners, candidates = chat_callback
    key = callback(browser, "create", candidate_id=candidates[0]).json()["session"]["session_id"]
    guard = app.state.backend.store.account_work
    with guard.acquire(owners[0].id):
        blocked = callback(browser, "delete", session_id=key)
        assert blocked.status_code == 409 and blocked.json()["code"] == "CHAT_BUSY"
    original = app.state.backend.store.list_chat_sessions

    def broken(*args, **kwargs):
        raise RuntimeError("secret-database-error")

    monkeypatch.setattr(app.state.backend.store, "list_chat_sessions", broken)
    response = callback(browser, "list")
    assert response.status_code == 503 and "secret-database-error" not in response.text
    monkeypatch.setattr(app.state.backend.store, "list_chat_sessions", original)
    assert callback(browser, "list").status_code == 200
    assert callback(browser, "delete", session_id=key).status_code == 200
    with pytest.raises(AccountWorkBusy):
        with guard.acquire(owners[0].id):
            guard.acquire(owners[0].id, deletion=True)


@pytest.mark.parametrize("data", [{"account_id": 2}, {"candidate_id": True}, {"candidate_id": 0},
    {"session_id": "../private-value"}, {"limit": 501}, {"title": "x" * 201}])
def test_callback_rejects_invalid_fields(chat_callback, data):
    _, browser, _, _ = chat_callback
    response = callback(browser, "list", **data)
    assert response.status_code == 422 and response.json()["code"] == "INVALID_REQUEST" and "private-value" not in response.text


@pytest.mark.parametrize("path,method,body,operation", [
    ("/api/chat/sessions?candidate_id=7", "get", None, "list"),
    ("/api/chat/history?candidate_id=7", "get", None, "history"),
    ("/api/chat/sessions", "post", {"candidate_id": 7}, "create"),
    ("/api/chat/sessions/chat-1/archive", "post", None, "archive"),
    ("/api/chat/sessions/chat-1", "delete", None, "delete"),
])
def test_compatibility_routes_only_forward_and_fail_closed(chat_callback, monkeypatch, path, method, body, operation):
    app, browser, _, _ = chat_callback
    browser.cookies.set("job_agent_session", "session-1")

    def forbidden(*args, **kwargs):
        pytest.fail("Compatibility route must not read/write local chat facts")

    monkeypatch.setattr(app.state.backend.store, "list_chat_sessions", forbidden)
    monkeypatch.setattr(app.state.backend.store, "get_candidate_profile", forbidden)
    monkeypatch.setattr(app.state.backend.store, "create_chat_session", forbidden)
    monkeypatch.setattr(app.state.backend.store, "archive_chat_session", forbidden)
    monkeypatch.setattr(app.state.backend, "delete_chat_session", forbidden)

    def offline(actual, token, **data):
        assert actual == operation and token == "session-1"
        raise PlatformAuthError("CHAT_UNAVAILABLE", "unavailable", 503)

    monkeypatch.setattr(app.state.backend.platform_auth_client, "chat_sessions", offline)
    response = browser.request(method, path, **({"json": body} if body is not None else {}))
    assert response.status_code == 503 and response.headers["Retry-After"] == "3"


def test_python_client_bounds_errors_and_foreign_projection(monkeypatch):
    auth = PlatformAuthClient(PlatformAuthSettings(True, "http://java", "secret", 5))
    def handler(request):
        assert request.url.path == "/internal/v1/chat/sessions/history"
        return httpx.Response(200, json={"candidate_id": 99, "session_id": "other", "messages": []})
    original = httpx.Client
    monkeypatch.setattr(httpx, "Client", lambda **kw: original(transport=httpx.MockTransport(handler), **kw))
    with pytest.raises(PlatformAuthError) as failure:
        auth.chat_sessions("history", "real-session", candidate_id=7, session_id="chat-1")
    assert failure.value.status_code == 503
    with pytest.raises(ValueError):
        validate_chat_projection({"candidate_id": 7, "session_id": "chat-1", "messages": [{"private": "x"}]}, "history")


def invoke(browser, candidate, **data):
    return browser.post("/internal/v1/chat/invoke", headers={"X-Internal-Service-Token": "service-secret", "X-Trace-Id": "chat-invoke-trace"},
        json={"session_token": "session-1", "candidate_id": candidate, "message": "Hello", "auto_rag": False, **data})


def test_invoke_callback_scopes_success_and_persists_one_turn(chat_callback):
    app, browser, owners, candidates = chat_callback
    assert browser.post("/internal/v1/chat/invoke", json={"session_token": "session-1", "candidate_id": candidates[0], "message": "Hello"}).status_code == 401
    assert invoke(browser, candidates[0], session_token="expired").status_code == 401
    assert invoke(browser, candidates[1]).status_code == 404
    assert app.state.test_agent.chat.call_count == 0
    response = invoke(browser, candidates[0])
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["account_id"] == owners[0].id and result["result"]["profile"]["id"] == candidates[0]
    assert result["session_id"] == f"account-{owners[0].id}-candidate-{candidates[0]}"
    assert response.headers["Cache-Control"] == "no-store"
    assert set(result["result"]) == {"mode", "reply", "display_reply", "task_trace", "background_tasks", "profile"}
    call = app.state.test_agent.chat.call_args.kwargs
    assert len(call["root_request_id"]) == 32 and call["root_request_id"] != "chat-invoke-trace" and call["account_id"] == owners[0].id
    assert call["use_tool_llm"] is False and call["auto_rag"] is False
    messages = app.state.backend.list_chat_messages(candidates[0], result["session_id"], account_id=owners[0].id)
    assert [m.role for m in messages] == ["user", "assistant"]
    with app.state.backend.store.account_work.deletion(owners[0].id):
        pass
    assert invoke(browser, candidates[0], session_token="session-2", session_id=result["session_id"]).status_code == 404
    key = callback(browser, "create", candidate_id=candidates[0]).json()["session"]["session_id"]
    callback(browser, "archive", session_id=key)
    assert invoke(browser, candidates[0], session_id=key).status_code == 409
    assert app.state.test_agent.chat.call_count == 1


@pytest.mark.parametrize("data", [{"account_id": 99}, {"candidate_id": True}, {"message": "x" * 16001},
    {"message": {"private": "secret"}}, {"session_id": "../private"}, {"auto_rag": "false"}])
def test_invoke_callback_validation_redacts_private_inputs(chat_callback, data):
    app, browser, _, candidates = chat_callback
    response = invoke(browser, candidates[0], **data) if "candidate_id" not in data else invoke(browser, data["candidate_id"])
    assert response.status_code == 422 and response.json()["code"] == "INVALID_REQUEST", response.text
    assert "private" not in response.text and "secret" not in response.text
    assert app.state.test_agent.chat.call_count == 0


def test_invoke_balance_failure_and_ambiguous_failure_do_not_save_or_replay(chat_callback):
    app, browser, owners, candidates = chat_callback
    key = f"account-{owners[0].id}-candidate-{candidates[0]}"
    app.state.test_agent.chat.side_effect = InsufficientBalanceError()
    response = invoke(browser, candidates[0])
    assert response.status_code == 402 and response.json() == {"code": "INSUFFICIENT_BALANCE", "detail": "余额不足，请先充值后重试。"}
    app.state.test_agent.chat.side_effect = RuntimeError("private-model-url-and-key")
    response = invoke(browser, candidates[0])
    assert response.status_code == 502 and "private-model" not in response.text
    assert app.state.test_agent.chat.call_count == 2
    assert app.state.backend.list_chat_messages(candidates[0], key, account_id=owners[0].id) == []
    with app.state.backend.store.account_work.deletion(owners[0].id):
        pass
    app.state.test_agent.chat.side_effect = None
    assert invoke(browser, candidates[0]).status_code == 200


def test_repeated_http_trace_id_is_not_reused_as_billing_identity(chat_callback):
    app, browser, _, candidates = chat_callback
    assert invoke(browser, candidates[0]).status_code == 200
    assert invoke(browser, candidates[0]).status_code == 200
    roots = [call.kwargs["root_request_id"] for call in app.state.test_agent.chat.call_args_list]
    assert len(set(roots)) == 2 and "chat-invoke-trace" not in roots


def test_invoke_holds_account_guard_through_agent_and_rule_path_updates_profile(chat_callback):
    app, browser, owners, candidates = chat_callback
    def blocked(*args, **kwargs):
        with pytest.raises(AccountWorkBusy):
            app.state.backend.store.account_work.acquire(owners[0].id, deletion=True)
        raise RuntimeError("private-failure")
    app.state.test_agent.chat.side_effect = blocked
    assert invoke(browser, candidates[0]).status_code == 502
    result = invoke(browser, candidates[0], message="我的首选城市改为上海", use_env_llm=False)
    assert result.status_code == 200, result.text
    assert result.json()["result"]["profile"]["preferred_cities"] == ["上海"]


def test_invoke_compatibility_never_executes_locally(chat_callback, monkeypatch):
    app, browser, _, candidates = chat_callback
    browser.cookies.set("job_agent_session", "session-1")
    def unavailable(token, **data):
        assert token == "session-1" and data["candidate_id"] == candidates[0]
        raise PlatformAuthError("CHAT_EXECUTION_UNCONFIRMED", "查看历史后重试", 503)
    monkeypatch.setattr(app.state.backend.platform_auth_client, "chat_invoke", unavailable)
    app.state.test_agent.chat.side_effect = AssertionError("No local fallback")
    result = browser.post("/api/chat", json={"candidate_id": candidates[0], "message": "Hello"})
    assert result.status_code == 503 and result.json()["code"] == "CHAT_EXECUTION_UNCONFIRMED"
    assert app.state.test_agent.chat.call_count == 0


def test_invoke_python_adapter_errors_bounds_and_projection(chat_callback, monkeypatch):
    app, browser, _, candidates = chat_callback
    receipt = invoke(browser, candidates[0]).json()["result"]
    auth = app.state.backend.platform_auth_client
    calls, state = [], {"status": 200, "body": receipt, "mime": "application/json"}
    def handler(request):
        calls.append(request)
        assert request.url.path == "/internal/v1/chat/invoke"
        return httpx.Response(state["status"], json=state["body"], headers={"Content-Type": state["mime"]})
    original = httpx.Client
    monkeypatch.setattr(httpx, "Client", lambda **kw: original(transport=httpx.MockTransport(handler), **kw))
    assert auth.chat_invoke("session-1", candidate_id=candidates[0], message="Hello") == receipt
    for status, body, mime in [(200, {**receipt, "tool_outputs": "secret"}, "application/json"),
        (200, {**receipt, "profile": {**receipt["profile"], "id": candidates[1]}}, "application/json"),
        (200, {**receipt, "reply": "x" * (4 * 1024 * 1024)}, "application/json"),
        (302, {}, "application/json"), (200, receipt, "text/html"), (500, {"detail": "private"}, "application/json")]:
        state.update(status=status, body=body, mime=mime)
        previous = len(calls)
        with pytest.raises(PlatformAuthError) as failure:
            auth.chat_invoke("session-1", candidate_id=candidates[0], message="Hello")
        assert failure.value.code == "CHAT_EXECUTION_UNCONFIRMED" and len(calls) == previous + 1
    state.update(status=402, body={"code": "INSUFFICIENT_BALANCE", "detail": "private-upstream"}, mime="application/json")
    with pytest.raises(PlatformAuthError) as failure:
        auth.chat_invoke("session-1", candidate_id=candidates[0], message="Hello")
    assert str(failure.value) == "余额不足，请先充值后重试。"


def test_disconnect_does_not_release_account_guard_before_execution_finishes(chat_callback):
    app, _, owners, candidates = chat_callback
    entered, release, finished = Event(), Event(), Event()
    def pending(*args, **kwargs):
        entered.set()
        try:
            assert release.wait(5)
            raise RuntimeError("model failed after disconnect")
        finally:
            finished.set()
    app.state.test_agent.chat.side_effect = pending
    endpoint = next(route.endpoint for route in app.routes if getattr(route, "path", None) == "/internal/v1/chat/invoke")
    request = SimpleNamespace(headers={"X-Internal-Service-Token": "service-secret", "X-Trace-Id": "cancelled-chat-trace"})
    payload = InternalChatInvokePayload(session_token="session-1", candidate_id=candidates[0], message="Hello")
    async def scenario():
        task = asyncio.create_task(endpoint(payload, request))
        try:
            assert await asyncio.to_thread(entered.wait, 3)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            with pytest.raises(AccountWorkBusy):
                app.state.backend.store.account_work.acquire(owners[0].id, deletion=True)
        finally:
            release.set()
            await asyncio.to_thread(finished.wait, 3)
    asyncio.run(scenario())
    with app.state.backend.store.account_work.deletion(owners[0].id):
        pass
    assert app.state.test_agent.chat.call_count == 1


def stream_invoke(browser, candidate, **data):
    return browser.post("/internal/v1/chat/stream", headers={"X-Internal-Service-Token": "service-secret"},
        json={"session_token": "session-1", "candidate_id": candidate, "message": "Hello", "auto_rag": False, **data})


def parse_events(text):
    events = []
    for frame in text.split("\n\n"):
        if frame.startswith("event: "):
            name, value = frame.split("\ndata: ", 1)
            events.append((name.removeprefix("event: "), json.loads(value)))
    return events


def install_stream_agent(app, action=None):
    def stream(*args, **kwargs):
        if action is not None:
            yield from action(*args, **kwargs)
        else:
            yield {"type": "token", "content": "Partial"}
            yield {"type": "final", "result": AgentChatResult(reply="Done.", candidate_id=kwargs["candidate_id"],
                session_id=kwargs["session_id"], mode="langchain_agent")}
    app.state.test_agent.stream_chat = Mock(side_effect=stream)


def test_stream_callback_scope_events_history_and_preflight_rejections(chat_callback, monkeypatch):
    app, browser, owners, candidates = chat_callback
    install_stream_agent(app)
    assert browser.post("/internal/v1/chat/stream", json={"session_token": "session-1", "candidate_id": candidates[0], "message": "Hello"}).status_code == 401
    assert stream_invoke(browser, candidates[0], session_token="expired").status_code == 401
    assert stream_invoke(browser, candidates[1]).status_code == 404
    assert stream_invoke(browser, candidates[0], account_id=99).status_code == 422
    response = stream_invoke(browser, candidates[0], execution_id="a" * 32)
    assert response.status_code == 200 and response.headers["content-type"].startswith("text/event-stream"), response.text
    events = parse_events(response.text)
    assert events[0] == ("scope", {"account_id": owners[0].id, "candidate_id": candidates[0],
        "session_id": f"account-{owners[0].id}-candidate-{candidates[0]}", "execution_id": "a" * 32})
    assert [v["content"] for name, v in events if name == "token"] == ["Partial"]
    assert sum(name == "final" for name, _ in events) == 1 and events[-1][0] == "final"
    assert next(v for name, v in events if name == "task_started")["task_trace"]["root_request_id"] == "a" * 32
    assert len(app.state.backend.list_chat_messages(candidates[0], events[0][1]["session_id"], account_id=owners[0].id)) == 2
    monkeypatch.setattr(app.state.backend.store, "assert_account_can_spend", Mock(side_effect=InsufficientBalanceError()))
    denied = stream_invoke(browser, candidates[0])
    assert denied.status_code == 402 and denied.json()["detail"] == "余额不足，请先充值后重试。"
    assert app.state.test_agent.stream_chat.call_count == 1


def test_stream_errors_are_redacted_and_never_persist_success(chat_callback):
    app, browser, owners, candidates = chat_callback
    def failure(*args, **kwargs):
        yield {"type": "token", "content": "Partial"}
        raise RuntimeError("private-model-url-and-api-key")
    install_stream_agent(app, failure)
    response = stream_invoke(browser, candidates[0])
    events = parse_events(response.text)
    assert response.status_code == 200 and events[-1][0] == "error"
    assert "private-model-url" not in response.text and not any(name == "final" for name, _ in events)
    assert app.state.backend.list_chat_messages(candidates[0], events[0][1]["session_id"], account_id=owners[0].id) == []


def test_stream_disconnect_drains_blocking_worker_before_unlock_and_suppresses_final(chat_callback):
    from starlette.requests import ClientDisconnect
    app, _, owners, candidates = chat_callback
    entered, release, closed = Event(), Event(), Event()
    def slow(*args, **kwargs):
        try:
            yield {"type": "step_started", "name": "ingest_candidate_message"}
            yield {"type": "step_completed", "name": "ingest_candidate_message", "data": {"status": "saved"}}
            yield {"type": "token", "content": "Partial"}
            entered.set()
            assert release.wait(5)
            yield {"type": "final", "result": AgentChatResult(reply="Must not persist", candidate_id=kwargs["candidate_id"],
                session_id=kwargs["session_id"], mode="langchain_agent")}
        finally:
            closed.set()
    install_stream_agent(app, slow)
    endpoint = next(r.endpoint for r in app.routes if getattr(r, "path", None) == "/internal/v1/chat/stream")
    async def scenario():
        response = await endpoint(InternalChatInvokePayload(session_token="session-1", candidate_id=candidates[0], message="Hello"),
            SimpleNamespace(headers={"X-Internal-Service-Token": "service-secret"}))
        async def receive():
            await asyncio.sleep(60)
        async def send(message):
            if message["type"] == "http.response.body" and b"Partial" in message.get("body", b""):
                assert await asyncio.to_thread(entered.wait, 2)
                raise OSError("browser disconnected")
        try:
            with pytest.raises(ClientDisconnect):
                await response({"type": "http", "method": "POST", "asgi": {"version": "3.0", "spec_version": "2.4"}}, receive, send)
            with pytest.raises(AccountWorkBusy):
                app.state.backend.store.account_work.acquire(owners[0].id, deletion=True)
        finally:
            release.set()
            assert await asyncio.to_thread(closed.wait, 3)
    asyncio.run(scenario())
    assert app.state.backend.list_chat_messages(candidates[0], f"account-{owners[0].id}-candidate-{candidates[0]}", account_id=owners[0].id) == []
    deadline = time.monotonic() + 3
    while True:
        try:
            app.state.backend.store.account_work.acquire(owners[0].id, deletion=True).release()
            break
        except AccountWorkBusy:
            assert time.monotonic() < deadline
            time.sleep(0.01)
    traces = app.state.backend.store.list_tool_call_traces(account_id=owners[0].id)
    assert len(traces) == 1 and traces[0].status == "cancelled"


def test_stream_compatibility_fails_closed_and_never_executes_python_agent(chat_callback, monkeypatch):
    from contextlib import asynccontextmanager
    app, browser, _, candidates = chat_callback
    browser.cookies.set("job_agent_session", "session-1")
    install_stream_agent(app)
    @asynccontextmanager
    async def offline(token, **kwargs):
        raise PlatformAuthError("CHAT_EXECUTION_UNCONFIRMED", "Check history", 503)
        yield  # pragma: no cover
    monkeypatch.setattr(app.state.backend.platform_auth_client, "chat_stream", offline)
    response = browser.post("/api/chat/stream", json={"candidate_id": candidates[0], "message": "Hello"})
    assert response.status_code == 503 and app.state.test_agent.stream_chat.call_count == 0


def test_rule_stream_cancellation_retains_completed_tool_audit_without_success_history(chat_callback, monkeypatch):
    app, _, owners, candidates = chat_callback
    cancelled = Event()
    original = app.state.backend.ingest_conversation_message
    def ingest(*args, **kwargs):
        result = original(*args, **kwargs)
        cancelled.set()
        return result
    monkeypatch.setattr(app.state.backend, "ingest_conversation_message", ingest)
    payload = ChatPayload(candidate_id=candidates[0], message="我的首选城市改为上海", use_env_llm=False, auto_rag=False)
    events = list(stream_web_chat_events(app.state.backend, None, payload, payload.message, "rule-session", owners[0].id,
        cancelled=cancelled, safe_errors=True))
    assert not any("event: final" in event for event in events)
    traces = app.state.backend.store.list_tool_call_traces(account_id=owners[0].id)
    assert len(traces) == 1 and traces[0].status == "cancelled"
    assert app.state.backend.list_chat_messages(candidates[0], "rule-session", account_id=owners[0].id) == []
