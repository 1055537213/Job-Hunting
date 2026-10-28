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
