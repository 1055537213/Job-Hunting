from dataclasses import asdict
from pathlib import Path
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

from job_hunting_agent.config import PlatformAuthSettings
from job_hunting_agent.models import CandidateProfileInput, ResumeArtifactRecord
from job_hunting_agent.platform_auth import PlatformAuthClient, PlatformAuthError
from job_hunting_agent.web import create_web_app


@pytest.fixture
def resumes(tmp_path, monkeypatch):
    app = create_web_app(
        env_file=tmp_path / "missing.env", resume_dir=tmp_path / "files"
    )
    backend = app.state.backend
    owner = backend.store.create_account(
        email="resume-platform@example.com", password_hash="hash", display_name="Owner"
    )
    candidate = backend.save_candidate_profile(
        CandidateProfileInput(
            name="Owner",
            status="student",
            education="bachelor",
            experience_years=0,
            skills={},
            preferred_cities=[],
            salary_floor_k=None,
            expected_salary_k=None,
            target_directions=[],
            unacceptable=[],
        ),
        account_id=owner.id,
    )
    backend.platform_auth_client = PlatformAuthClient(
        PlatformAuthSettings(True, "http://java", "service-secret", 5)
    )

    def account(operation, **data):
        if data["session_token"] == "expired":
            raise PlatformAuthError("SESSION_EXPIRED", "private session", 401)
        value = asdict(backend.store.get_account(owner.id))
        value["account_id"] = value.pop("id")
        return {"account": value}

    monkeypatch.setattr(backend.platform_auth_client, "account", account)
    artifact = ResumeArtifactRecord(
        id=11,
        account_id=owner.id,
        candidate_id=candidate,
        job_id=None,
        draft_id=None,
        parent_artifact_id=None,
        version=1,
        artifact_type="source",
        original_filename="resume.docx",
        download_filename="resume.docx",
        storage_key="private/storage-key",
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        file_size=6,
        sha256="a" * 64,
        extraction_method="docx",
        text_length=6,
        page_count=None,
        status="ready",
        long_text_id=None,
        created_at="now",
        knowledge_asset_id=None,
        knowledge_asset_version_id=None,
        scan_status="clean",
        scan_engine="clamav",
        scan_reason=None,
    )
    monkeypatch.setattr(backend, "upload_resume_document", Mock(return_value=artifact))
    with TestClient(app) as browser:
        yield app, browser, candidate, owner.id


def upload(browser, candidate, *, token="service-secret", session="session-1"):
    return browser.post(
        "/internal/v1/resumes/upload",
        headers={"X-Internal-Service-Token": token},
        data={"session_token": session, "candidate_id": str(candidate)},
        files={"file": ("resume.docx", b"resume", "application/octet-stream")},
    )


def test_internal_resume_callback_rechecks_session_and_returns_safe_projection(resumes):
    app, browser, candidate, owner = resumes
    assert upload(browser, candidate, token="wrong").status_code == 401
    assert upload(browser, candidate, session="expired").status_code == 401
    response = upload(browser, candidate)
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["account_id"] == owner
    assert payload["artifact"]["candidate_id"] == candidate
    assert "storage_key" not in response.text
    assert payload["artifact"]["scan_engine"] is None


def test_hybrid_python_public_resume_route_fails_closed(resumes):
    _, browser, candidate, _ = resumes
    browser.cookies.set("job_agent_session", "session-1")
    response = browser.post(
        "/api/resumes/upload",
        data={"candidate_id": str(candidate)},
        files={"file": ("resume.docx", b"resume", "application/octet-stream")},
    )
    assert response.status_code == 503
    assert "统一入口" in response.text


def test_resume_callback_contract_and_gateway_routes():
    import yaml

    root = Path(__file__).resolve().parents[1]
    contract = yaml.safe_load(
        (
            root / "platform-service/src/main/resources/resumes-upload.openapi.yaml"
        ).read_text(encoding="utf-8")
    )
    schema = contract["paths"]["/api/resumes/upload"]["post"]["requestBody"]["content"][
        "multipart/form-data"
    ]["schema"]
    assert schema["required"] == ["candidate_id", "file"]
    assert schema["additionalProperties"] is False
    assert "20 MiB" in contract["paths"]["/api/resumes/upload"]["post"]["description"]
    for name in ("Caddyfile.hybrid", "Caddyfile.hybrid.local"):
        text = (root / "deploy" / name).read_text(encoding="utf-8")
        assert "path /api/resumes/upload" in text
        assert "max_size 20MB" in text
        assert "reverse_proxy @resume_upload platform-service:8081" in text
    nginx = (root / "deploy/nginx/coexist-hybrid-ip-https.conf.template").read_text(
        encoding="utf-8"
    )
    upload_route = nginx.split("location = /api/resumes/upload {", 1)[1].split(
        "\n    }", 1
    )[0]
    assert "client_max_body_size 20m;" in upload_route
    assert "proxy_request_buffering off;" in upload_route
    assert "proxy_next_upstream off;" in upload_route
