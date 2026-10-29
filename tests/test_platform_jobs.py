from dataclasses import asdict
from pathlib import Path
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

from job_hunting_agent.config import PlatformAuthSettings
from job_hunting_agent.models import CandidateProfileInput, ImportedJob
from job_hunting_agent.platform_auth import PlatformAuthClient, PlatformAuthError
from job_hunting_agent.web import create_web_app


def png_bytes() -> bytes:
    return bytes.fromhex(
        "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
        "0000000d49444154789c6360f8cf00000004000101e2 7f".replace(" ", "")
    )


@pytest.fixture
def jobs(tmp_path, monkeypatch):
    app = create_web_app(env_file=tmp_path / "missing.env", resume_dir=tmp_path / "files")
    backend = app.state.backend
    owner = backend.store.create_account(email="job-platform@example.com", password_hash="hash", display_name="Owner")
    backend.save_candidate_profile(
        CandidateProfileInput(
            name="Owner", status="student", education="bachelor", experience_years=0,
            skills={}, preferred_cities=[], salary_floor_k=None, expected_salary_k=None,
            target_directions=[], unacceptable=[],
        ),
        account_id=owner.id,
    )
    backend.platform_auth_client = PlatformAuthClient(PlatformAuthSettings(True, "http://java", "service-secret", 5))

    def account(operation, **data):
        if data["session_token"] == "expired":
            raise PlatformAuthError("SESSION_EXPIRED", "private session", 401)
        value = asdict(backend.store.get_account(owner.id))
        value["account_id"] = value.pop("id")
        return {"account": value}

    monkeypatch.setattr(backend.platform_auth_client, "account", account)
    job = ImportedJob(
        id=7, raw_text="Python backend engineer", source_url="https://example.com/job", title="Python backend engineer",
        city="杭州", salary_min_k=15, salary_max_k=25, salary_months=12, salary_unit="月薪",
        experience_min_years=1, experience_max_years=3, experience_label="1-3年", education="本科",
        company_name="Example", industry="Software", company_size="100-499", skills=["Python"],
        description_text="负责后端接口开发。", field_confidence={"title": 0.9}, uncertainty_notes=[],
        skill_requirements=[], import_method="screenshot", captured_at="now",
    )
    monkeypatch.setattr(backend, "import_job_screenshots", Mock(return_value=job))
    with TestClient(app) as browser:
        yield app, browser


def upload(browser, *, session="session-1", token="service-secret"):
    return browser.post(
        "/internal/v1/jobs/screenshots",
        headers={"X-Internal-Service-Token": token},
        data={"session_token": session, "source_url": "https://example.com/job"},
        files={"screenshots": ("job.png", png_bytes(), "image/png")},
    )


def upload_text(browser, *, session="session-1", token="service-secret"):
    return browser.post(
        "/internal/v1/jobs/text",
        headers={"X-Internal-Service-Token": token},
        json={"session_token": session, "raw_text": "Python backend engineer\n杭州\n15-20K\n任职要求：负责 Python 后端开发。",
              "source_url": "https://example.com/job"},
    )


def test_internal_job_screenshot_callback_rechecks_session_and_returns_safe_projection(jobs):
    _, browser = jobs
    assert upload(browser, token="wrong").status_code == 401
    assert upload(browser, session="expired").status_code == 401
    response = upload(browser)
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["account_id"] == 1
    assert payload["job"]["import_method"] == "screenshot"
    assert payload["extraction"] == {"source": "screenshot", "screenshot_count": 1}
    assert "private" not in response.text


def test_hybrid_python_public_job_screenshot_route_fails_closed(jobs):
    _, browser = jobs
    browser.cookies.set("job_agent_session", "session-1")
    response = browser.post(
        "/api/jobs/screenshots",
        files={"screenshots": ("job.png", png_bytes(), "image/png")},
    )
    assert response.status_code == 503
    assert "统一入口" in response.text


def test_internal_job_text_callback_rechecks_session_and_returns_safe_projection(jobs, monkeypatch):
    app, browser = jobs
    job = ImportedJob(
        id=8, raw_text="Python backend engineer", source_url="https://example.com/job",
        title="Python backend engineer", city="杭州", salary_min_k=15, salary_max_k=25,
        salary_months=12, salary_unit="月薪", experience_min_years=1, experience_max_years=3,
        experience_label="1-3年", education="本科", company_name="Example", industry="Software",
        company_size="100-499", skills=["Python"], description_text="负责后端接口开发。",
        field_confidence={"title": 0.9}, uncertainty_notes=[], skill_requirements=[],
        import_method="text", captured_at="now",
    )
    monkeypatch.setattr(app.state.backend, "import_job_text", Mock(return_value=job))
    assert upload_text(browser, token="wrong").status_code == 401
    assert upload_text(browser, session="expired").status_code == 401
    response = upload_text(browser)
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["account_id"] == 1
    assert payload["job"]["import_method"] == "text"
    assert "private" not in response.text


def test_hybrid_python_public_job_text_route_fails_closed(jobs):
    _, browser = jobs
    browser.cookies.set("job_agent_session", "session-1")
    response = browser.post("/api/jobs", json={"raw_text": "Python backend engineer"})
    assert response.status_code == 503
    assert "统一入口" in response.text


def test_hybrid_python_public_job_management_routes_fail_closed(jobs):
    _, browser = jobs
    browser.cookies.set("job_agent_session", "session-1")
    assert browser.get("/api/jobs").status_code == 503
    assert browser.put("/api/jobs/7/skill-requirements", json={"requirements": []}).status_code == 503
    assert browser.delete("/api/jobs/7").status_code == 503


def test_internal_job_management_callbacks_return_safe_scoped_projections(jobs, monkeypatch):
    app, browser = jobs
    backend = app.state.backend
    monkeypatch.setattr(backend, "list_jobs", Mock(return_value=[]))
    assert browser.post(
        "/internal/v1/jobs/list",
        headers={"X-Internal-Service-Token": "wrong"},
        json={"session_token": "session-1"},
    ).status_code == 401
    assert browser.post(
        "/internal/v1/jobs/list",
        headers={"X-Internal-Service-Token": "service-secret"},
        json={"session_token": "expired"},
    ).status_code == 401
    response = browser.post(
        "/internal/v1/jobs/list",
        headers={"X-Internal-Service-Token": "service-secret"},
        json={"session_token": "session-1"},
    )
    assert response.status_code == 200, response.text
    assert response.json() == {"account_id": 1, "jobs": []}

    job = ImportedJob(
        id=7, raw_text="Python backend engineer", source_url="https://example.com/job",
        title="Python backend engineer", city="杭州", salary_min_k=15, salary_max_k=25,
        salary_months=12, salary_unit="月薪", experience_min_years=1, experience_max_years=3,
        experience_label="1-3年", education="本科", company_name="Example", industry="Software",
        company_size="100-499", skills=["Python"], description_text="负责后端接口开发。",
        field_confidence={"title": 0.9}, uncertainty_notes=[], skill_requirements=[],
        import_method="text", captured_at="now",
    )
    monkeypatch.setattr(backend, "update_job_skill_requirements", Mock(return_value=job))
    response = browser.post(
        "/internal/v1/jobs/skill-requirements",
        headers={"X-Internal-Service-Token": "service-secret"},
        json={"session_token": "session-1", "job_id": 7,
              "requirements": [{"name": "Python", "category": "core", "confidence": 0.9, "evidence": "backend"}]},
    )
    assert response.status_code == 200, response.text
    assert response.json()["account_id"] == 1
    assert "private" not in response.text

    monkeypatch.setattr(backend, "delete_job", Mock(return_value={
        "job_id": 7, "storage_keys": ["private/object"], "long_text_ids": [9],
    }))
    response = browser.post(
        "/internal/v1/jobs/delete",
        headers={"X-Internal-Service-Token": "service-secret"},
        json={"session_token": "session-1", "job_id": 7},
    )
    assert response.status_code == 200, response.text
    assert response.json() == {"account_id": 1, "deleted": True, "job_id": 7}
    assert "storage_keys" not in response.text and "long_text_ids" not in response.text


def test_job_screenshot_contract_and_gateway_routes():
    import yaml

    root = Path(__file__).resolve().parents[1]
    contract = yaml.safe_load((root / "platform-service/src/main/resources/job-screenshots-upload.openapi.yaml").read_text(encoding="utf-8"))
    schema = contract["paths"]["/api/jobs/screenshots"]["post"]["requestBody"]["content"]["multipart/form-data"]["schema"]
    assert schema["required"] == ["screenshots"]
    assert schema["additionalProperties"] is False
    description = contract["paths"]["/api/jobs/screenshots"]["post"]["description"]
    assert "8 MiB" in description and "20 MiB" in description
    for name in ("Caddyfile.hybrid", "Caddyfile.hybrid.local"):
        text = (root / "deploy" / name).read_text(encoding="utf-8")
        assert "path /api/jobs/screenshots" in text
        assert "max_size 20MB" in text
        assert "reverse_proxy @job_screenshot_upload platform-service:8081" in text
    nginx = (root / "deploy/nginx/coexist-hybrid-ip-https.conf.template").read_text(encoding="utf-8")
    route = nginx.split("location = /api/jobs/screenshots {", 1)[1].split("\n    }", 1)[0]
    assert "client_max_body_size 20m;" in route
    assert "proxy_request_buffering off;" in route
    assert "proxy_next_upstream off;" in route

    text_contract = yaml.safe_load((root / "platform-service/src/main/resources/job-text-import.openapi.yaml").read_text(encoding="utf-8"))
    text_schema = text_contract["paths"]["/api/jobs"]["post"]["requestBody"]["content"]["application/json"]["schema"]
    assert text_schema["required"] == ["raw_text"]
    assert text_schema["additionalProperties"] is False
    assert text_schema["properties"]["raw_text"]["maxLength"] == 65536
    for name in ("Caddyfile.hybrid", "Caddyfile.hybrid.local"):
        text = (root / "deploy" / name).read_text(encoding="utf-8")
        assert "path /api/jobs" in text
        assert "max_size 256KB" in text
        assert "reverse_proxy @job_text_import platform-service:8081" in text
    nginx_text = (root / "deploy/nginx/coexist-hybrid-ip-https.conf.template").read_text(encoding="utf-8")
    text_route = nginx_text.split("location = /api/jobs {", 1)[1].split("\n    }", 1)[0]
    assert "client_max_body_size 256k;" in text_route
    assert "proxy_next_upstream off;" in text_route

    for name in ("Caddyfile.hybrid", "Caddyfile.hybrid.local"):
        text = (root / "deploy" / name).read_text(encoding="utf-8")
        assert "method GET" in text and "path /api/jobs" in text
        assert "path_regexp jobSkillUpdate ^/api/jobs/[0-9]+/skill-requirements$" in text
        assert "path_regexp jobDelete ^/api/jobs/[0-9]+$" in text
    nginx = (root / "deploy/nginx/coexist-hybrid-ip-https.conf.template").read_text(encoding="utf-8")
    job_route = nginx.split("location = /api/jobs {", 1)[1].split("\n    }", 1)[0]
    assert "^(GET|POST)$" in job_route
    assert "location ~ ^/api/jobs/[0-9]+/skill-requirements$" in nginx
    assert "location ~ ^/api/jobs/[0-9]+$" in nginx
