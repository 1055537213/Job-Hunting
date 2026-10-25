import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import pytest
from fastapi.testclient import TestClient

from job_hunting_agent.config import PlatformAuthSettings
from job_hunting_agent.models import CandidateProfileInput, ProjectExperienceCard
from job_hunting_agent.platform_auth import PlatformAuthClient, PlatformAuthError
from job_hunting_agent.project_deletion import cleanup, resume_pending
from job_hunting_agent.task_queue import TaskQueueError
from job_hunting_agent.web import create_web_app


@pytest.fixture
def projects(tmp_path, monkeypatch):
    app = create_web_app(env_file=tmp_path / "missing.env", resume_dir=tmp_path / "files")
    backend = app.state.backend
    owners, candidates, records = [], [], []
    for index in (1, 2):
        owner = backend.store.create_account(email=f"projects-{index}@example.com", password_hash="hash", display_name="Owner")
        owners.append(owner.id)
        candidate = backend.save_candidate_profile(CandidateProfileInput(name=f"Owner {index}", status="student", education="bachelor",
            experience_years=0, skills={}, preferred_cities=[], salary_floor_k=None, expected_salary_k=None, target_directions=[]), account_id=owner.id)
        candidates.append(candidate)
        card = ProjectExperienceCard("project", f"Project {index}", ["README.md"], {}, ["Python"], ["Search"], [], [], [], [])
        records.append(backend.store.save_project_card(candidate, card, account_id=owner.id))
    backend.platform_auth_client = PlatformAuthClient(PlatformAuthSettings(True, "http://java", "service-secret", 5))
    def account(operation, **data):
        if data["session_token"] == "expired":
            raise PlatformAuthError("SESSION_EXPIRED", "private session", 401)
        value = asdict(backend.store.get_account(owners[int(data["session_token"][-1]) - 1]))
        value["account_id"] = value.pop("id")
        return {"account": value}
    monkeypatch.setattr(backend.platform_auth_client, "account", account)
    with TestClient(app) as browser:
        yield app, browser, owners, candidates, records


def submit(browser, operation, **data):
    return browser.post(f"/internal/v1/projects/{operation}", headers={"X-Internal-Service-Token": "service-secret"},
                        json={"session_token": "session-1", **data})


def test_scope_credentials_confirmation_idempotency_conflict_and_delete(projects):
    app, browser, owners, candidates, records = projects
    record = records[0]
    assert browser.post("/internal/v1/projects/list", json={"session_token": "session-1", "candidate_id": candidates[0]}).status_code == 401
    assert submit(browser, "list", candidate_id=candidates[0], session_token="expired").status_code == 401
    assert submit(browser, "list", candidate_id=candidates[1]).status_code == 404
    assert submit(browser, "confirm", record_id=records[1].id, confirmed_summary="Mine").status_code == 404
    assert submit(browser, "delete", record_id=records[1].id).status_code == 404
    listed = submit(browser, "list", candidate_id=candidates[0])
    assert listed.status_code == 200 and listed.json()["project_cards"][0] == asdict(record), listed.text
    assert listed.headers["cache-control"] == "no-store"
    backend = app.state.backend
    queued = []
    backend.task_queue = SimpleNamespace(enqueue=queued.append)
    confirmed = submit(browser, "confirm", record_id=record.id, confirmed_summary="I built search.")
    assert confirmed.status_code == 200, confirmed.text
    again = submit(browser, "confirm", record_id=record.id, confirmed_summary="I built search.")
    assert again.status_code == 200 and again.json()["task"]["task_key"] == confirmed.json()["task"]["task_key"]
    assert len(queued) == 1
    assert submit(browser, "confirm", record_id=record.id, confirmed_summary="Different content").status_code == 409
    with backend.store.connect() as conn:
        assert conn.execute("SELECT count(*) AS n FROM long_texts WHERE entity_type='project_experience_card' AND entity_id=?", (record.id,)).fetchone()["n"] == 1
    with backend.store.account_work.acquire(owners[0]):
        assert submit(browser, "delete", record_id=record.id).json()["code"] == "PROJECT_BUSY"
    deleted = submit(browser, "delete", record_id=record.id)
    assert deleted.status_code == 200 and deleted.json()["cleanup_status"] == "completed", deleted.text
    assert submit(browser, "delete", record_id=record.id).json() == deleted.json()
    assert submit(browser, "confirm", record_id=record.id, confirmed_summary="I built search.").status_code == 404
    assert submit(browser, "list", candidate_id=candidates[0]).json()["project_cards"] == []
    assert backend.get_candidate_profile(candidates[0], account_id=owners[0]).skills == {}


def test_concurrent_confirmation_creates_one_evidence_and_rejects_other_summary(projects):
    app, browser, _, _, records = projects
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(lambda text: submit(browser, "confirm", record_id=records[0].id, confirmed_summary=text), ["First", "Second"]))
    assert sorted(r.status_code for r in responses) == [200, 409]
    with app.state.backend.store.connect() as conn:
        assert conn.execute("SELECT count(*) AS n FROM long_texts WHERE entity_type='project_experience_card' AND entity_id=?", (records[0].id,)).fetchone()["n"] == 1


def test_queue_failure_reuses_committed_confirmation_and_original_task(projects):
    app, browser, owners, _, records = projects
    enqueue = Mock(side_effect=TaskQueueError("private broker credential"))
    app.state.backend.task_queue = SimpleNamespace(enqueue=enqueue)
    response = submit(browser, "confirm", record_id=records[0].id, confirmed_summary="My work")
    assert response.status_code == 503 and "private broker" not in response.text
    enqueue.side_effect = None
    assert submit(browser, "confirm", record_id=records[0].id, confirmed_summary="My work").status_code == 200
    with app.state.backend.store.connect() as conn:
        assert conn.execute("SELECT count(*) AS n FROM background_tasks WHERE account_id=?", (owners[0],)).fetchone()["n"] == 1
        assert conn.execute("SELECT count(*) AS n FROM long_texts WHERE entity_type='project_experience_card' AND entity_id=?", (records[0].id,)).fetchone()["n"] == 1


def test_deletion_is_atomic_and_running_task_prevents_it(projects, monkeypatch):
    app, browser, owners, _, records = projects
    backend = app.state.backend
    task = backend.store.create_background_task(account_id=owners[0], task_type="system_probe")
    backend.store.claim_background_task(task.task_key)
    assert submit(browser, "delete", record_id=records[0].id).json()["code"] == "PROJECT_BUSY"
    backend.store.complete_background_task(task.task_key, {})
    original = backend.store.delete_project_card
    def fail(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError("private SQL failure after removal")
    monkeypatch.setattr(backend.store, "delete_project_card", fail)
    response = submit(browser, "delete", record_id=records[0].id)
    assert response.status_code == 503 and "private SQL" not in response.text
    assert backend.store.get_project_card(records[0].id, account_id=owners[0]).id == records[0].id
    with backend.store.connect() as conn:
        assert conn.execute("SELECT count(*) AS n FROM project_deletions").fetchone()["n"] == 0


def test_frozen_contract_matches_whitelisted_card_fields():
    from pathlib import Path
    import yaml
    from job_hunting_agent.project_contract import CARD_FIELDS, RECORD_FIELDS
    contract = yaml.safe_load((Path(__file__).resolve().parents[1] / "platform-service/src/main/resources/projects.openapi.yaml").read_text(encoding="utf-8"))
    for name, fields in (("Record", RECORD_FIELDS), ("Card", CARD_FIELDS)):
        schema = contract["components"]["schemas"][name]
        assert set(schema["required"]) == set(schema["properties"]) == fields
        assert schema["additionalProperties"] is False


def test_deletion_cancels_only_queued_work_for_this_project(projects):
    app, browser, owners, candidates, records = projects
    store = app.state.backend.store
    records[0].card.source_url = "https://github.com/example/project"
    with store.connect() as conn:
        conn.execute("UPDATE project_experience_cards SET card_json=? WHERE id=?", (json.dumps(asdict(records[0].card)), records[0].id))
    related = store.create_background_task(account_id=owners[0], candidate_id=candidates[0], task_type="github_project_analysis",
        payload={"repository_url": "https://github.com/EXAMPLE/Project"})
    other = store.create_background_task(account_id=owners[0], candidate_id=candidates[0], task_type="github_project_analysis",
        payload={"repository_url": "https://github.com/example/other"})
    assert submit(browser, "delete", record_id=records[0].id).status_code == 200
    assert store.get_background_task(related.task_key, account_id=owners[0]).status == "cancelled"
    assert store.get_background_task(other.task_key, account_id=owners[0]).status == "queued"
    with store.connect() as conn:
        conn.execute("UPDATE accounts SET status='disabled' WHERE id=?", (owners[0],))
    assert submit(browser, "delete", record_id=records[0].id).status_code == 403


def test_all_versions_manifest_survives_storage_failure_and_is_collected_for_account_deletion(projects, monkeypatch):
    app, browser, owners, candidates, records = projects
    backend, owner, candidate, record = app.state.backend, owners[0], candidates[0], records[0]
    files = [backend.resume_files.save(account_id=owner, candidate_id=candidate, filename="project.zip", content=b"archive" + bytes([n])) for n in (1, 2)]
    imported = backend.store.register_project_archive_import(account_id=owner, candidate_id=candidate, original_filename="project.zip",
        storage_key=files[0].storage_key, file_size=files[0].file_size, sha256=files[0].sha256, scan_engine="test")
    backend.store.add_knowledge_asset_version(imported.knowledge_asset_id, account_id=owner, original_filename="project.zip",
        storage_key=files[1].storage_key, media_type="application/zip", file_size=files[1].file_size, sha256=files[1].sha256)
    with backend.store.connect() as conn:
        conn.execute("UPDATE project_archive_imports SET project_card_id=? WHERE id=?", (record.id, imported.id))
    assert submit(browser, "confirm", record_id=record.id, confirmed_summary="My industrial project").status_code == 200
    text = backend.store.get_long_text_for_entity("project_experience_card", record.id, source_label="confirmed", account_id=owner)
    with backend.store.connect() as conn:
        conn.execute("""INSERT INTO rag_chunks(id,account_id,candidate_id,long_text_id,entity_type,entity_id,source_label,chunk_index,
            content,content_sha256,embedding,embedding_model,embedding_dimensions,created_at,updated_at)
            VALUES ('delete-project',?,?,?,'project_experience_card',?,'confirmed',0,'evidence',?,'[0.1,0.2,0.3]'::vector,'test',3,now(),now()) RETURNING 1 AS fixture""",
            (owner, candidate, text.id, record.id, "0"*64))
    result = submit(browser, "delete", record_id=record.id)
    assert result.status_code == 200 and result.json()["cleanup_status"] == "pending", result.text
    assert "storage_keys" not in result.text
    assert sorted(backend.store.collect_account_deletion_storage_keys(owner)) == sorted(f.storage_key for f in files)
    original = backend.resume_files.delete
    monkeypatch.setattr(backend.resume_files, "delete", Mock(side_effect=RuntimeError("private credential")))
    assert cleanup(backend, owner, record.id, force=True)["cleanup_status"] == "pending"
    with backend.store.connect() as conn:
        assert conn.execute("SELECT count(*) AS n FROM rag_chunks WHERE id='delete-project'").fetchone()["n"] == 0
        manifest = conn.execute("SELECT storage_keys_json FROM project_deletions WHERE account_id=?", (owner,)).fetchone()["storage_keys_json"]
        assert sorted(json.loads(manifest)) == sorted(f.storage_key for f in files)
        conn.execute("UPDATE project_deletions SET next_attempt_at=now() WHERE account_id=?", (owner,))
    monkeypatch.setattr(backend.resume_files, "delete", original)
    assert resume_pending(backend)["project_deletions_completed"] == 1
    assert all(not backend.resume_files.path_for(f.storage_key).exists() for f in files)
    assert submit(browser, "delete", record_id=record.id).json()["cleanup_status"] == "completed"


@pytest.mark.parametrize("operation,data", [("list", {"candidate_id": True}), ("list", {"candidate_id": 0}),
    ("delete", {"record_id": "1"}), ("list", {"candidate_id": 1, "account_id": 2}),
    ("confirm", {"record_id": 1, "confirmed_summary": "x"*20001}), ("delete", {"record_id": 1, "candidate_id": 1})])
def test_invalid_callbacks_are_redacted(projects, operation, data):
    response = submit(projects[1], operation, **data)
    assert response.status_code == 422 and response.json()["code"] == "INVALID_REQUEST"
    assert "x"*100 not in response.text


@pytest.mark.parametrize("method,path,data,operation", [("GET", "/api/projects?candidate_id=1", None, "list"),
    ("POST", "/api/projects/1/confirm", {"confirmed_summary": "Mine"}, "confirm"), ("DELETE", "/api/projects/1", None, "delete")])
def test_compatibility_only_forwards_and_never_falls_back(projects, monkeypatch, method, path, data, operation):
    app, browser, _, _, _ = projects
    browser.cookies.set("job_agent_session", "session-1")
    monkeypatch.setattr(app.state.backend, "list_project_cards", Mock(side_effect=AssertionError("local fallback")))
    monkeypatch.setattr(app.state.backend, "delete_project_card", Mock(side_effect=AssertionError("local fallback")))
    monkeypatch.setattr(app.state.backend, "confirm_project_card_and_enqueue_rag", Mock(side_effect=AssertionError("local fallback")))
    forwarded = Mock(side_effect=PlatformAuthError("PROJECT_UNAVAILABLE", "Unavailable", 503))
    monkeypatch.setattr(app.state.backend.platform_auth_client, "projects", forwarded)
    response = browser.request(method, path, **({"json": data} if data is not None else {}))
    assert response.status_code == 503
    assert forwarded.call_args.args == (operation, "session-1")


def test_platform_client_bounds_and_validates_projection(monkeypatch):
    state = {"body": {"project_cards": []}, "status": 200}
    def respond(request):
        return httpx.Response(state["status"], json=state["body"])
    real = httpx.Client
    monkeypatch.setattr(httpx, "Client", lambda **kwargs: real(transport=httpx.MockTransport(respond), **kwargs))
    client = PlatformAuthClient(PlatformAuthSettings(True, "http://java", "secret", 5))
    assert client.projects("list", "session", candidate_id=1) == {"project_cards": []}
    for body, status in [({"project_cards": [], "account_id": 2}, 200), ({"detail": "private credential"}, 500),
        ({"project_cards": "x"*(4*1024*1024+1)}, 200)]:
        state.update(body=body, status=status)
        with pytest.raises(PlatformAuthError) as error:
            client.projects("list", "session", candidate_id=1)
        assert error.value.status_code == 503 and "private credential" not in str(error.value)
