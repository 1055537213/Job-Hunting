import asyncio
import copy
import json
from dataclasses import asdict
from pathlib import Path

import httpx
import pytest
import yaml
from fastapi.testclient import TestClient

from job_hunting_agent import profile_query
from job_hunting_agent.config import PlatformAuthSettings
from job_hunting_agent.models import CandidateProfile, CandidateProfileInput
from job_hunting_agent.platform_auth import PlatformAuthClient, PlatformAuthError
from job_hunting_agent.web import create_web_app


def profile_fixture(owner_id=1):
    return asdict(CandidateProfile(name="Owner", status="student", education="bachelor", experience_years=1.5,
        skills={"Python": "熟练"}, preferred_cities=["杭州"], salary_floor_k=10, expected_salary_k=None,
        target_directions=["后端开发"], id=owner_id))


def test_frozen_profile_contract_matches_existing_domain_fields():
    contract = yaml.safe_load((Path(__file__).resolve().parents[1] / "platform-service/src/main/resources/profile-query.openapi.yaml").read_text(encoding="utf-8"))
    schema = contract["components"]["schemas"]["Profile"]
    assert set(schema["required"]) == set(schema["properties"]) == profile_query.PROFILE_FIELDS
    assert schema["additionalProperties"] is False
    for path in ("/api/profiles", "/api/profiles/{candidate_id}"):
        assert set(contract["paths"][path]) == {"get"}


@pytest.mark.parametrize("invalid", ["extra", "missing", "id", "bool", "duplicate", "skills", "number", "list", "weights", "salary", "wrong-id"])
def test_profile_projection_rejects_corruption_not_user_content(invalid):
    row = profile_fixture()
    value = {"profiles": [row]}
    candidate_id = None
    if invalid == "extra":
        row["token_hash"] = "private"
    elif invalid == "missing":
        del row["skills"]
    elif invalid == "id":
        row["id"] = 0
    elif invalid == "bool":
        row["id"] = True
    elif invalid == "duplicate":
        value["profiles"].append(copy.deepcopy(row))
    elif invalid == "skills":
        row["skills"]["Python"] = 1
    elif invalid == "number":
        row["experience_years"] = float("nan")
    elif invalid == "list":
        row["preferred_cities"] = "杭州"
    elif invalid == "weights":
        row["preference_weights"]["city"] = True
    elif invalid == "salary":
        row["salary_floor_k"] = "10"
    else:
        value, candidate_id = {"profile": row}, 2
    with pytest.raises(ValueError):
        profile_query.validate_projection(value, candidate_id)


def test_projection_preserves_contract_and_rejects_duplicate_json_and_oversize():
    value = {"profiles": [profile_fixture()]}
    value["profiles"][0]["name"] = "token_hash 是用户填写的文本"
    assert profile_query.decode_projection(json.dumps(value).encode()) == value
    assert profile_query.encode_callback(42, value).decode().startswith('{"account_id": 42,')
    for invalid in (b'{"profiles":[],"profiles":[]}', json.dumps(value).encode() + b" {}", b"x" * (profile_query.MAX_PROFILE_QUERY_BYTES + 1)):
        with pytest.raises(ValueError):
            profile_query.decode_projection(invalid)


@pytest.fixture
def callback(tmp_path, monkeypatch):
    app = create_web_app(env_file=tmp_path / "missing.env")
    backend = app.state.backend
    accounts, candidates = [], []
    for index in (1, 2):
        owner = backend.store.create_account(email=f"profile-{index}@example.com", password_hash="hash", display_name="Owner")
        accounts.append(owner)
        candidates.append(backend.save_candidate_profile(CandidateProfileInput(name=f"Private {index}", status="student", education="bachelor",
            experience_years=0, skills={}, preferred_cities=[], salary_floor_k=None, expected_salary_k=None, target_directions=[]), account_id=owner.id))
    backend.platform_auth_client = PlatformAuthClient(PlatformAuthSettings(True, "http://java", "service-secret", 5))
    def resolve(operation, **data):
        assert operation == "me" and set(data) == {"session_token", "trace_id"}
        if data["session_token"] == "expired":
            raise PlatformAuthError("SESSION_EXPIRED", "private-session", 401)
        owner = accounts[int(data["session_token"][-1]) - 1]
        value = asdict(owner)
        value["account_id"] = value.pop("id")
        return {"account": value}
    monkeypatch.setattr(backend.platform_auth_client, "account", resolve)
    with TestClient(app) as browser:
        yield app, browser, accounts, candidates


def test_callback_requires_service_and_real_session_and_enforces_tenant_scope(callback):
    app, browser, accounts, candidates = callback
    headers = {"X-Internal-Service-Token": "service-secret", "X-Trace-Id": "profile-query-trace"}
    path = "/internal/v1/profiles/query"
    assert browser.post(path, json={"session_token": "session-1"}).status_code == 401
    assert browser.post(path, json={"session_token": "expired"}, headers=headers).status_code == 401
    for index in (0, 1):
        value = browser.post(path, json={"session_token": f"session-{index+1}"}, headers=headers)
        assert value.status_code == 200 and value.headers["Cache-Control"] == "no-store"
        assert value.json()["account_id"] == accounts[index].id
        assert [row["id"] for row in value.json()["profiles"]] == [candidates[index]]
        own = browser.post(path, json={"session_token": f"session-{index+1}", "candidate_id": candidates[index]}, headers=headers)
        assert own.json()["profile"]["name"] == f"Private {index+1}"
    forbidden = browser.post(path, json={"session_token": "session-1", "candidate_id": candidates[1]}, headers=headers)
    assert forbidden.status_code == 404 and forbidden.json()["code"] == "PROFILE_NOT_FOUND" and "Private 2" not in forbidden.text
    assert forbidden.headers["Cache-Control"] == "no-store"
    with app.state.backend.store.connect() as conn:
        conn.execute("UPDATE accounts SET status='disabled' WHERE id=?", (accounts[0].id,))
    disabled = browser.post(path, json={"session_token": "session-1"}, headers=headers)
    assert disabled.status_code == 403 and disabled.json()["code"] == "ACCOUNT_DISABLED"


@pytest.mark.parametrize("invalid", [{"account_id": 2}, {"candidate_id": 0}, {"candidate_id": True},
                                    {"candidate_id": "1"}, {"candidate_id": 2**63}, {"session_token": "private-value" * 50}])
def test_callback_invalid_requests_do_not_reflect_input(callback, invalid):
    _, browser, _, _ = callback
    response = browser.post("/internal/v1/profiles/query", json={"session_token": "session-1", **invalid},
                            headers={"X-Internal-Service-Token": "service-secret"})
    assert response.status_code == 422 and "private-value" not in response.text


def test_callback_faults_and_oversize_are_not_empty_success_and_capacity_recovers(callback, monkeypatch):
    app, browser, _, _ = callback
    backend = app.state.backend
    path, headers = "/internal/v1/profiles/query", {"X-Internal-Service-Token": "service-secret"}
    original = backend.list_candidate_profiles
    def broken(**kw):
        raise RuntimeError("private-db-password")
    monkeypatch.setattr(backend, "list_candidate_profiles", broken)
    response = browser.post(path, json={"session_token": "session-1"}, headers=headers)
    assert response.status_code == 503 and "private-db-password" not in response.text and "profiles" not in response.json()
    monkeypatch.setattr(backend, "list_candidate_profiles", original)
    monkeypatch.setattr(profile_query, "MAX_PROFILE_QUERY_BYTES", 10)
    assert browser.post(path, json={"session_token": "session-1"}, headers=headers).status_code == 413
    monkeypatch.setattr(profile_query, "MAX_PROFILE_QUERY_BYTES", 4 * 1024 * 1024)
    assert browser.post(path, json={"session_token": "session-1"}, headers=headers).status_code == 200


@pytest.mark.parametrize("candidate_id", [None, 7])
def test_compatibility_route_only_forwards_real_session_and_never_reads_local_profiles(tmp_path, monkeypatch, candidate_id):
    app = create_web_app(env_file=tmp_path / "missing.env")
    backend = app.state.backend
    backend.platform_auth_client = PlatformAuthClient(PlatformAuthSettings(True, "http://java", "service-secret", 5))
    def forbidden(*args, **kw):
        pytest.fail("Hybrid compatibility query must not read local profiles/accounts")
    for method in ("get_account", "get_auth_session_by_token_hash", "get_candidate_profile", "list_candidate_profiles"):
        monkeypatch.setattr(backend.store, method, forbidden)
    expected = {"profiles": [profile_fixture(7)]} if candidate_id is None else {"profile": profile_fixture(7)}
    with TestClient(app) as browser:
        endpoint = "/api/profiles" + ("" if candidate_id is None else f"/{candidate_id}")
        assert browser.get(endpoint).status_code == 401
        browser.cookies.set("job_agent_session", "opaque-session")
        def forward(session, candidate, **kw):
            assert session == "opaque-session" and candidate == candidate_id and kw["trace_id"] == "profile-route-trace"
            return expected
        monkeypatch.setattr(backend.platform_auth_client, "query_profiles", forward)
        response = browser.get(endpoint + "?account_id=999", headers={"X-Request-ID": "profile-route-trace"})
        assert response.status_code == 200 and response.json() == expected
        assert response.headers["Cache-Control"] == "no-store"
        def offline(*a, **kw):
            raise PlatformAuthError("PROFILE_QUERY_UNAVAILABLE", "档案查询暂时不可用，请稍后重试。", 503)
        monkeypatch.setattr(backend.platform_auth_client, "query_profiles", offline)
        assert browser.get(endpoint).status_code == 503


def test_profile_client_bounds_and_validates_java_response_and_known_errors(monkeypatch):
    real_client = httpx.Client
    response = [httpx.Response(200, json={"profile": profile_fixture(7)})]
    def handler(request):
        assert request.url.path == "/internal/v1/profiles/query"
        assert json.loads(request.content) == {"session_token": "real-session", "candidate_id": 7}
        assert request.headers["X-Trace-Id"] == "profile-client-trace"
        return response[0]
    monkeypatch.setattr(httpx, "Client", lambda **kw: real_client(**kw, transport=httpx.MockTransport(handler)))
    client = PlatformAuthClient(PlatformAuthSettings(True, "http://java", "service-secret", 5))
    assert client.query_profiles("real-session", 7, trace_id="profile-client-trace") == response[0].json()
    for bad in [httpx.Response(302, headers={"Location": "http://evil.example"}), httpx.Response(200, content=b"x" * (4*1024*1024+1)),
                httpx.Response(200, json={"profile": profile_fixture(8)}), httpx.Response(200, json={"profile": {"secret": "private"}}),
                httpx.Response(401, json={"code": "INTERNAL_UNAUTHORIZED"}), httpx.Response(404, text="Not Found")]:
        response[0] = bad
        with pytest.raises(PlatformAuthError) as error:
            client.query_profiles("real-session", 7, trace_id="profile-client-trace")
        assert error.value.status_code == 503 and "private" not in str(error.value)
    response[0] = httpx.Response(404, json={"code": "PROFILE_NOT_FOUND", "detail": "private upstream text"})
    with pytest.raises(PlatformAuthError) as error:
        client.query_profiles("real-session", 7, trace_id="profile-client-trace")
    assert error.value.status_code == 404 and "private" not in str(error.value)


def test_callback_does_not_deadlock_when_compatibility_sync_pool_is_full(callback, monkeypatch):
    import anyio.to_thread
    app, browser, _, _ = callback
    backend = app.state.backend
    async def setup():
        limiter = anyio.to_thread.current_default_thread_limiter()
        previous = limiter.total_tokens
        limiter.total_tokens = 1
        return asyncio.get_running_loop(), limiter, previous
    loop, limiter, previous = browser.portal.call(setup)
    def forward(session, candidate=None, **kw):
        async def request():
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://callback") as client:
                return await client.post("/internal/v1/profiles/query", json={"session_token": session},
                                         headers={"X-Internal-Service-Token": "service-secret"})
        response = asyncio.run_coroutine_threadsafe(request(), loop).result(timeout=5)
        assert response.status_code == 200, response.text
        return {"profiles": response.json()["profiles"]}
    monkeypatch.setattr(backend.platform_auth_client, "query_profiles", forward)
    browser.cookies.set("job_agent_session", "session-1")
    try:
        assert browser.get("/api/profiles").status_code == 200
    finally:
        async def restore():
            limiter.total_tokens = previous
        browser.portal.call(restore)
