from dataclasses import asdict

import httpx
import pytest
from fastapi.testclient import TestClient

from job_hunting_agent.account_work import AccountWorkBusy
from job_hunting_agent.chat_session_query import validate_chat_projection
from job_hunting_agent.config import PlatformAuthSettings
from job_hunting_agent.models import CandidateProfileInput
from job_hunting_agent.platform_auth import PlatformAuthClient, PlatformAuthError
from job_hunting_agent.web import create_web_app


@pytest.fixture
def chat_callback(tmp_path, monkeypatch):
    app = create_web_app(env_file=tmp_path / "missing.env")
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
