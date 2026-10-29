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
    evidence_task = backend.store.create_background_task(account_id=owner, candidate_id=candidate, task_type="rag_index",
        idempotency_key=f"project-archive-evidence-rag:{imported.id}")
    visual_task = backend.store.create_background_task(account_id=owner, candidate_id=candidate, task_type="visual_index",
        idempotency_key=f"project-archive-visual:{imported.id}")
    assert submit(browser, "confirm", record_id=record.id, confirmed_summary="My industrial project").status_code == 200
    text = backend.store.get_long_text_for_entity("project_experience_card", record.id, source_label="confirmed", account_id=owner)
    with backend.store.connect() as conn:
        conn.execute("""INSERT INTO rag_chunks(id,account_id,candidate_id,long_text_id,entity_type,entity_id,source_label,chunk_index,
            content,content_sha256,embedding,embedding_model,embedding_dimensions,created_at,updated_at)
            VALUES ('delete-project',?,?,?,'project_experience_card',?,'confirmed',0,'evidence',?,'[0.1,0.2,0.3]'::vector,'test',3,now(),now()) RETURNING 1 AS fixture""",
            (owner, candidate, text.id, record.id, "0"*64))
    result = submit(browser, "delete", record_id=record.id)
    assert result.status_code == 200 and result.json()["cleanup_status"] == "pending", result.text
    assert backend.store.get_background_task(evidence_task.task_key).status == "cancelled"
    assert backend.store.get_background_task(visual_task.task_key).status == "cancelled"
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
    ("POST", "/api/projects/1/confirm", {"confirmed_summary": "Mine"}, "confirm"), ("DELETE", "/api/projects/1", None, "delete"),
    ("POST", "/api/projects/github", {"candidate_id": 1, "repository_url": "https://github.com/example/repo"}, "github")])
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


def github_submit(browser, candidate, **data):
    return submit(browser, "github", candidate_id=candidate, repository_url="https://github.com/example/project", **data)


def test_github_submission_scope_replay_canonicalization_and_conflict(projects):
    app, browser, owners, candidates, _ = projects
    calls = []
    app.state.backend.task_queue = SimpleNamespace(enqueue=calls.append)
    assert github_submit(browser, candidates[1]).status_code == 404
    assert github_submit(browser, candidates[0], session_token="expired").status_code == 401
    key = "github:request-0001"
    first = github_submit(browser, candidates[0], idempotency_key=key)
    assert first.status_code == 200, first.text
    payload = first.json()
    again = submit(browser, "github", candidate_id=candidates[0], repository_url="https://www.github.com/EXAMPLE/Project.git/", idempotency_key=key)
    assert again.status_code == 200 and again.json() == payload
    implicit = github_submit(browser, candidates[0])
    assert implicit.status_code == 200 and implicit.json() == payload
    assert calls == [payload["task"]["task_key"]]
    conflict = submit(browser, "github", candidate_id=candidates[0], repository_url="https://github.com/example/other", idempotency_key=key)
    assert conflict.status_code == 409 and conflict.json()["code"] == "PROJECT_SUBMISSION_CONFLICT"
    task = app.state.backend.store.get_background_task(calls[0], account_id=owners[0])
    assert task.payload["repository_url"] == "https://github.com/example/project"
    assert payload["root_request_id"] == task.payload["root_request_id"]
    assert payload["task"]["result"] is None and payload["processing_async"] is True and payload["project_card"] is None
    with app.state.backend.store.connect() as conn:
        assert conn.execute("SELECT count(*) AS n FROM background_tasks").fetchone()["n"] == 1
        assert conn.execute("SELECT key_hash FROM project_submissions WHERE key_hash IS NOT NULL").fetchone()["key_hash"] != key


def test_github_dispatch_failure_survives_restart_and_unknown_ack_does_not_duplicate_work(projects):
    from job_hunting_agent.project_submission import ProjectSubmissions
    app, browser, owners, candidates, _ = projects
    backend = app.state.backend
    calls = []
    def lost_ack(task_key):
        calls.append(task_key)
        raise TaskQueueError("private broker credentials")
    backend.task_queue = SimpleNamespace(enqueue=lost_ack)
    first = github_submit(browser, candidates[0], idempotency_key="github:restart-test")
    assert first.status_code == 503 and "private broker" not in first.text
    with backend.store.connect() as conn:
        row = conn.execute("SELECT * FROM project_submissions").fetchone()
        assert row["status"] == "pending" and row["attempts"] == 1
        assert conn.execute("SELECT status FROM background_tasks").fetchone()["status"] == "queued"
        conn.execute("UPDATE project_submissions SET next_dispatch_at=now()")
    # A new object represents a new Web/maintenance process using only persisted state.
    resumed = ProjectSubmissions(backend.store, SimpleNamespace(enqueue=calls.append))
    assert resumed.dispatch_pending()["dispatched_projects"] == 1
    assert calls == [row["task_key"], row["task_key"]]
    assert backend.store.claim_background_task(row["task_key"]) is not None
    assert backend.store.claim_background_task(row["task_key"]) is None
    backend.store.complete_background_task(row["task_key"], {"project_card_id": 1, "private": "hidden"})
    backend.task_queue = SimpleNamespace(enqueue=calls.append)
    replay = github_submit(browser, candidates[0], idempotency_key="github:restart-test")
    assert replay.status_code == 200 and replay.json()["task"]["status"] == "succeeded"
    assert replay.json()["task"]["result"] is None
    assert len(calls) == 2
    # A deliberate new submission has a new key; old receipts remain immutable.
    fresh = github_submit(browser, candidates[0], idempotency_key="github:deliberate-new")
    assert fresh.status_code == 200 and fresh.json()["task"]["task_key"] != row["task_key"]
    assert backend.store.get_background_task(row["task_key"], account_id=owners[0]).status == "succeeded"


def test_github_submission_is_atomic_and_rejects_missing_queue(projects, monkeypatch):
    from contextlib import contextmanager
    app, browser, _, candidates, _ = projects
    backend = app.state.backend
    assert github_submit(browser, candidates[0]).status_code == 503
    calls = []
    backend.task_queue = SimpleNamespace(enqueue=calls.append)
    connect = backend.store.connect
    @contextmanager
    def broken_receipt():
        with connect() as conn:
            execute = conn.execute
            def fail(query, *args, **kwargs):
                if "INSERT INTO project_submissions" in query:
                    raise RuntimeError("private database failure")
                return execute(query, *args, **kwargs)
            conn.execute = fail
            yield conn
    monkeypatch.setattr(backend.store, "connect", broken_receipt)
    response = github_submit(browser, candidates[0])
    assert response.status_code == 503 and "private database" not in response.text
    with connect() as conn:
        assert conn.execute("SELECT count(*) AS n FROM background_tasks").fetchone()["n"] == 0
        assert conn.execute("SELECT count(*) AS n FROM project_submissions").fetchone()["n"] == 0
    assert calls == []


def test_concurrent_github_submissions_share_one_task(projects):
    app, browser, _, candidates, _ = projects
    calls = []
    app.state.backend.task_queue = SimpleNamespace(enqueue=calls.append)
    with ThreadPoolExecutor(max_workers=4) as pool:
        responses = list(pool.map(lambda i: github_submit(browser, candidates[0], idempotency_key=f"github:concurrent-{i}"), range(4)))
    assert [r.status_code for r in responses] == [200]*4, [r.text for r in responses]
    assert len({r.json()["task"]["task_key"] for r in responses}) == 1
    assert len(calls) == 1


@pytest.mark.parametrize("cancel", ["delete", "disable"])
def test_pending_github_dispatch_cannot_revive_deleted_or_disabled_work(projects, cancel):
    from job_hunting_agent.project_submission import ProjectSubmissions
    app, browser, owners, candidates, records = projects
    backend = app.state.backend
    backend.task_queue = SimpleNamespace(enqueue=Mock(side_effect=TaskQueueError("offline")))
    assert github_submit(browser, candidates[0], idempotency_key="github:pending-cancel").status_code == 503
    with backend.store.connect() as conn:
        row = conn.execute("SELECT * FROM project_submissions").fetchone()
        if cancel == "delete":
            card = asdict(records[0].card)
            card["source_url"] = "https://github.com/Example/Project"
            conn.execute("UPDATE project_experience_cards SET card_json=? WHERE id=?", (json.dumps(card), records[0].id))
        else:
            conn.execute("UPDATE accounts SET status='disabled' WHERE id=?", (owners[0],))
        conn.execute("UPDATE project_submissions SET next_dispatch_at=now()")
    if cancel == "delete":
        assert submit(browser, "delete", record_id=records[0].id).status_code == 200
    calls = []
    assert ProjectSubmissions(backend.store, SimpleNamespace(enqueue=calls.append)).dispatch_pending()["dispatched_projects"] == 0
    assert calls == []
    assert backend.store.get_background_task(row["task_key"]).status == "cancelled"
    with backend.store.connect() as conn:
        assert conn.execute("SELECT status FROM project_submissions").fetchone()["status"] == "superseded"


@pytest.mark.parametrize("url", ["https://gitlab.com/a/b", "https://github.com@evil.test/a/b", "https://github.com/a/b?token=private", "https://github.com/a/b/tree/main"])
def test_github_invalid_repository_never_creates_task(projects, url):
    app, browser, _, candidates, _ = projects
    app.state.backend.task_queue = SimpleNamespace(enqueue=Mock())
    response = submit(browser, "github", candidate_id=candidates[0], repository_url=url)
    assert response.status_code == 400 and response.json()["code"] == "PROJECT_REPOSITORY_INVALID"
    assert "token=private" not in response.text
    with app.state.backend.store.connect() as conn:
        assert conn.execute("SELECT count(*) AS n FROM background_tasks").fetchone()["n"] == 0


@pytest.mark.parametrize("fields", [{"candidate_id": True}, {"idempotency_key": "short"},
    {"root_request_id": "a"*32}, {"account_id": 99}, {"repository_url": "x"*513}])
def test_github_callback_strictly_rejects_extra_and_unsafe_fields(projects, fields):
    _, browser, _, candidates, _ = projects
    data = {"candidate_id": candidates[0], "repository_url": "https://github.com/example/project", **fields}
    response = submit(browser, "github", **data)
    assert response.status_code == 422 and response.json()["code"] == "INVALID_REQUEST"
    assert "x"*100 not in response.text


def test_github_contract_and_both_gateway_topologies_are_aligned():
    from pathlib import Path
    import yaml
    from job_hunting_agent.project_contract import TASK_FIELDS
    root = Path(__file__).resolve().parents[1]
    contract = yaml.safe_load((root / "platform-service/src/main/resources/projects.openapi.yaml").read_text(encoding="utf-8"))
    schema = contract["components"]["schemas"]["GitHubTask"]
    assert set(schema["required"]) == set(schema["properties"]) == TASK_FIELDS
    assert schema["additionalProperties"] is False
    assert contract["paths"]["/api/projects/github"]["post"]["requestBody"]["content"]["application/json"]["schema"]["additionalProperties"] is False
    for name in ("Caddyfile.hybrid", "Caddyfile.hybrid.local"):
        text = (root / "deploy" / name).read_text(encoding="utf-8")
        assert "@project_github {\n\t\t\tmethod POST\n\t\t\tpath /api/projects/github" in text
        assert "reverse_proxy @project_github platform-service:8081" in text
    nginx = (root / "deploy/nginx/coexist-hybrid-ip-https.conf.template").read_text(encoding="utf-8")
    block = nginx.split("location = /api/projects/github {", 1)[1].split("\n    }", 1)[0]
    assert "proxy_pass http://platform-service:8081;" in block and "proxy_next_upstream off;" in block


def manifest(browser, candidate, **data):
    import hashlib
    return submit(browser, "local-manifest", **{"candidate_id": candidate, "project_name": "Local design",
        "files": [{"relative_path": "README.md", "file_size": 9, "sha256": hashlib.sha256(b"prototype").hexdigest()}], **data})


def completion_collection(projects, *, pending=False):
    import hashlib
    from job_hunting_agent.project_evidence import ProjectManifestItem
    app, _, owners, candidates, _ = projects
    backend = app.state.backend
    entries = [ProjectManifestItem("README.md", 9, hashlib.sha256(b"prototype").hexdigest())]
    if pending:
        entries.append(ProjectManifestItem("notes.txt", 9, hashlib.sha256(b"prototype").hexdigest()))
    collection, files = backend.create_local_project_collection(candidates[0], "Completion", entries, account_id=owners[0])
    backend.process_local_project_collection_file(collection.id, files[0].id, b"prototype", account_id=owners[0])
    return backend, owners[0], collection


def test_local_completion_does_not_leave_card_when_uploads_are_pending(projects):
    backend, owner, collection = completion_collection(projects, pending=True)
    before = backend.store.list_project_cards(collection.candidate_id, account_id=owner)
    with pytest.raises(ValueError):
        backend.complete_local_project_collection(collection.id, account_id=owner)
    assert backend.store.list_project_cards(collection.candidate_id, account_id=owner) == before
    assert backend.store.get_project_collection(collection.id, account_id=owner).project_card_id is None


def test_local_completion_rolls_back_card_and_collection_together(projects, monkeypatch):
    backend, owner, collection = completion_collection(projects)
    before = backend.store.list_project_cards(collection.candidate_id, account_id=owner)
    original = backend.store.complete_project_collection
    def interrupted(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError("SECRET_SQL_AFTER_FINALIZE")
    monkeypatch.setattr(backend.store, "complete_project_collection", interrupted)
    with pytest.raises(RuntimeError, match="SECRET_SQL_AFTER_FINALIZE"):
        backend.complete_local_project_collection(collection.id, account_id=owner)
    assert backend.store.list_project_cards(collection.candidate_id, account_id=owner) == before
    assert backend.store.get_project_collection(collection.id, account_id=owner).project_card_id is None


def test_local_completion_callback_scope_replay_and_confirmed_card(projects):
    backend, owner, collection = completion_collection(projects)
    browser = projects[1]
    assert submit(browser, "local-complete", collection_id=collection.id, session_token="session-2").status_code == 404
    assert submit(browser, "local-complete", collection_id=collection.id, session_token="expired").status_code == 401
    first = submit(browser, "local-complete", collection_id=collection.id)
    assert first.status_code == 200, first.text
    data = first.json()
    assert set(data) == {"account_id", "completion_collection_id", "project_card", "processing_async"}
    assert data["account_id"] == owner and data["completion_collection_id"] == collection.id
    assert data["processing_async"] is False
    card_id = data["project_card"]["id"]
    assert submit(browser, "local-complete", collection_id=collection.id).json() == data
    backend.store.confirm_project_card(card_id, "My verified work", account_id=owner)
    replay = submit(browser, "local-complete", collection_id=collection.id)
    assert replay.status_code == 200 and replay.json()["project_card"]["id"] == card_id
    assert replay.json()["project_card"]["status"] == "已确认"
    assert len(backend.store.list_project_cards(collection.candidate_id, account_id=owner)) == 2
    assert backend.get_candidate_profile(collection.candidate_id, account_id=owner).skills == {}
    assert submit(browser, "delete", record_id=card_id).status_code == 200
    assert submit(browser, "local-complete", collection_id=collection.id).status_code == 404


def test_local_completion_pending_empty_failed_and_disabled_are_not_success(projects):
    backend, owner, collection = completion_collection(projects, pending=True)
    browser = projects[1]
    response = submit(browser, "local-complete", collection_id=collection.id)
    assert response.status_code == 409 and response.json()["code"] == "PROJECT_COLLECTION_INCOMPLETE"
    with backend.store.connect() as conn:
        conn.execute("UPDATE project_collection_files SET selection_status='failed',selection_reason='SECRET_PARSER' WHERE collection_id=? AND selection_status='selected'", (collection.id,))
    completed = submit(browser, "local-complete", collection_id=collection.id)
    assert completed.status_code == 200 and completed.json()["project_card"]["card"]["deferred_files"]
    # Raw parser errors must never be copied into a project card's public skipped_summary.
    assert "SECRET_PARSER" not in completed.text
    with backend.store.connect() as conn:
        conn.execute("UPDATE accounts SET status='disabled' WHERE id=?", (owner,))
    assert submit(browser, "local-complete", collection_id=collection.id).status_code == 403


def test_local_completion_excludes_upload_cancel_resume_and_other_completion(projects, monkeypatch):
    from threading import Event
    backend, owner, collection = completion_collection(projects)
    browser = projects[1]
    entered, finish = Event(), Event()
    original = backend._complete_local_project_collection
    def held(*args, **kwargs):
        entered.set()
        assert finish.wait(10)
        return original(*args, **kwargs)
    monkeypatch.setattr(backend, "_complete_local_project_collection", held)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(submit, browser, "local-complete", collection_id=collection.id)
        try:
            assert entered.wait(10)
            assert submit(browser, "local-complete", collection_id=collection.id).json()["code"] == "PROJECT_BUSY"
            assert submit(browser, "local-cancel", collection_id=collection.id).json()["code"] == "PROJECT_BUSY"
            assert manifest(browser, collection.candidate_id).json()["code"] == "PROJECT_BUSY"
            from job_hunting_agent.account_work import AccountWorkBusy
            with pytest.raises(AccountWorkBusy):
                backend.store.account_work.acquire(owner)
        finally:
            finish.set()
        assert future.result(timeout=10).status_code == 200


def test_local_completion_callback_redacts_failure_and_recovers_after_retry(projects, monkeypatch):
    backend, owner, collection = completion_collection(projects)
    browser = projects[1]
    original = backend.store.complete_project_collection
    def interrupted(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError("SECRET_SQL_AFTER_FINALIZE")
    monkeypatch.setattr(backend.store, "complete_project_collection", interrupted)
    failed = submit(browser, "local-complete", collection_id=collection.id)
    assert failed.status_code == 503 and "SECRET_SQL" not in failed.text
    assert backend.store.get_project_collection(collection.id, account_id=owner).project_card_id is None
    assert len(backend.store.list_project_cards(collection.candidate_id, account_id=owner)) == 1
    monkeypatch.setattr(backend.store, "complete_project_collection", original)
    assert submit(browser, "local-complete", collection_id=collection.id).status_code == 200


@pytest.mark.parametrize("data", [{"collection_id": True}, {"collection_id": -1}, {"collection_id": 1, "account_id": 9}, {"collection_id": 1, "candidate_id": 9}])
def test_local_completion_rejects_spoofed_locators(projects, data):
    assert submit(projects[1], "local-complete", **data).status_code == 422


def test_local_upload_internal_callback_reuses_python_policy_and_redacts_metadata(projects):
    app, browser, owners, candidates, _ = projects
    data = manifest(browser, candidates[0]).json()
    collection = data["collection"]["id"]
    file_id = data["selected_files"][0]["id"]
    response = browser.post(
        f"/internal/v1/projects/local/{collection}/files",
        headers={"X-Internal-Service-Token": "service-secret"},
        data={"session_token": "session-1", "file_ids": json.dumps([file_id])},
        files=[("files", ("README.md", b"prototype", "text/markdown"))],
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["account_id"] == owners[0]
    assert body["processed_files"][0]["selection_status"] == "analyzed"
    assert "long_text_id" not in body["processed_files"][0]
    assert "metadata" not in body["processed_files"][0]
    assert "storage_key" not in response.text
    assert "SECRET" not in response.text


def test_local_upload_internal_callback_rejects_wrong_token_and_batch_shape(projects):
    app, browser, _, candidates, _ = projects
    data = manifest(browser, candidates[0]).json()
    path = f"/internal/v1/projects/local/{data['collection']['id']}/files"
    file_id = data["selected_files"][0]["id"]
    assert browser.post(path, data={"session_token": "session-1", "file_ids": json.dumps([file_id])},
                        files=[("files", ("README.md", b"prototype", "text/markdown"))]).status_code == 401
    assert browser.post(path, headers={"X-Internal-Service-Token": "service-secret"},
                        data={"session_token": "session-1", "file_ids": json.dumps([file_id, file_id])},
                        files=[("files", ("README.md", b"prototype", "text/markdown"))]).status_code == 400


def test_local_completion_empty_evidence_rejected_before_writing(projects):
    app, browser, owners, candidates, _ = projects
    backend = app.state.backend
    collection = manifest(browser, candidates[0]).json()["collection"]["id"]
    with backend.store.connect() as conn:
        conn.execute("UPDATE project_collection_files SET selection_status='failed',selection_reason='SECRET_PARSER' WHERE collection_id=?", (collection,))
    response = submit(browser, "local-complete", collection_id=collection)
    assert response.status_code == 409 and response.json()["code"] == "PROJECT_COLLECTION_INCOMPLETE"
    assert len(backend.store.list_project_cards(candidates[0], account_id=owners[0])) == 1


def test_local_completion_handles_unique_race_without_aborting_parent_transaction(projects, monkeypatch):
    backend, owner, collection = completion_collection(projects)
    original = backend.complete_local_project_collection(collection.id, account_id=owner)
    with backend.store.connect() as conn:
        conn.execute("UPDATE project_collection_sessions SET project_card_id=NULL,status='uploading' WHERE id=?", (collection.id,))
    find = backend.store.find_project_card_by_content_fingerprint
    calls = []
    def missed_then_visible(*args, **kwargs):
        calls.append(1)
        return None if len(calls) == 1 else find(*args, **kwargs)
    monkeypatch.setattr(backend.store, "find_project_card_by_content_fingerprint", missed_then_visible)
    replay = backend.complete_local_project_collection(collection.id, account_id=owner)
    assert replay.id == original.id
    assert backend.store.get_project_collection(collection.id, account_id=owner).project_card_id == original.id
    assert len(backend.store.list_project_cards(collection.candidate_id, account_id=owner)) == 2


def test_local_completion_python_compatibility_validates_public_projection(projects, monkeypatch):
    _, _, _, _, records = projects
    state = {"status": 200, "value": {"project_card": asdict(records[0]), "processing_async": False}}
    real = httpx.Client
    def respond(request):
        assert request.url.path == "/internal/v1/projects/local-complete"
        assert json.loads(request.content) == {"session_token": "session-1", "collection_id": 8}
        return httpx.Response(state["status"], json=state["value"])
    monkeypatch.setattr(httpx, "Client", lambda **kwargs: real(transport=httpx.MockTransport(respond), **kwargs))
    client = PlatformAuthClient(PlatformAuthSettings(True, "http://java", "secret", 5))
    assert client.projects("local-complete", "session-1", collection_id=8) == state["value"]
    for value in ({"project_card": None, "processing_async": False},
                  {"project_card": asdict(records[0]), "processing_async": True},
                  {**state["value"], "account_id": 1}):
        state["value"] = value
        with pytest.raises(PlatformAuthError) as error:
            client.projects("local-complete", "session-1", collection_id=8)
        assert error.value.status_code == 503
    state.update(status=409, value={"code": "PROJECT_COLLECTION_INCOMPLETE", "detail": "SECRET_PARSER"})
    with pytest.raises(PlatformAuthError) as error:
        client.projects("local-complete", "session-1", collection_id=8)
    assert error.value.status_code == 409 and "SECRET" not in str(error.value)


def test_local_manifest_resume_scope_and_cancel_receipt(projects):
    app, browser, owners, candidates, _ = projects
    assert manifest(browser, candidates[1]).status_code == 404
    assert manifest(browser, candidates[0], session_token="expired").status_code == 401
    first = manifest(browser, candidates[0])
    assert first.status_code == 200, first.text
    data = first.json()
    collection = data["collection"]["id"]
    assert "account_id" not in data["collection"]
    assert len(data["selected_files"]) == 1 and data["resumed"] is False
    again = manifest(browser, candidates[0])
    assert again.status_code == 200 and again.json()["collection"]["id"] == collection
    get = submit(browser, "local-get", collection_id=collection)
    assert get.status_code == 200 and get.json()["files"] == data["files"]
    assert submit(browser, "local-get", collection_id=collection, session_token="session-2").status_code == 404
    assert submit(browser, "local-cancel", collection_id=collection, session_token="session-2").status_code == 404
    backend = app.state.backend
    with backend.store.account_work.acquire(owners[0]):
        assert submit(browser, "local-get", collection_id=collection).status_code == 200
        assert submit(browser, "local-cancel", collection_id=collection).json()["code"] == "PROJECT_BUSY"
        assert manifest(browser, candidates[0]).json()["code"] == "PROJECT_BUSY"
    cancelled = submit(browser, "local-cancel", collection_id=collection)
    assert cancelled.status_code == 200 and cancelled.json()["cleanup_status"] == "completed", cancelled.text
    assert submit(browser, "local-cancel", collection_id=collection).json() == cancelled.json()
    assert submit(browser, "local-get", collection_id=collection).status_code == 404
    with pytest.raises(KeyError):
        backend.process_local_project_collection_file(collection, data["files"][0]["id"], b"prototype", account_id=owners[0])
    fresh = manifest(browser, candidates[0])
    assert fresh.status_code == 200 and fresh.json()["collection"]["id"] != collection
    with backend.store.connect() as conn:
        assert conn.execute("SELECT count(*) AS n FROM project_collection_sessions").fetchone()["n"] == 1
        conn.execute("UPDATE accounts SET status='disabled' WHERE id=?", (owners[0],))
    assert submit(browser, "local-cancel", collection_id=collection).status_code == 403


def test_local_file_policy_and_private_metadata_are_not_exposed(projects):
    app, browser, _, candidates, _ = projects
    response = manifest(browser, candidates[0], files=[{"relative_path": ".env", "file_size": 10, "sha256": "a"*64},
        {"relative_path": "README.md", "file_size": 10, "sha256": "b"*64}])
    assert response.status_code == 200, response.text
    data = response.json()
    assert len(data["selected_files"]) == 1 and data["selected_files"][0]["relative_path"] == "README.md"
    with app.state.backend.store.connect() as conn:
        conn.execute("UPDATE project_collection_files SET selection_status='failed',selection_reason=?,metadata_json=? WHERE id=?",
            ("SECRET_PROVIDER_ERROR", json.dumps({"last_modified": 12, "storage_key": "SECRET_OBJECT", "extracted_text": "SECRET_CONTENT"}), data["selected_files"][0]["id"]))
        conn.execute("UPDATE project_collection_sessions SET error_summary=? WHERE id=?", ("SECRET_SQL", data["collection"]["id"]))
    get = submit(browser, "local-get", collection_id=data["collection"]["id"])
    assert get.status_code == 200 and "SECRET" not in get.text
    assert get.json()["files"][1]["metadata"] == {"last_modified": 12}
    resumed = manifest(browser, candidates[0], files=[{"relative_path": "README.md", "file_size": 10, "sha256": "b"*64},
        {"relative_path": ".env", "file_size": 10, "sha256": "a"*64}])
    assert resumed.status_code == 200 and resumed.json()["collection"]["id"] == data["collection"]["id"]
    assert resumed.json()["selected_files"][0]["selection_status"] == "selected"


def test_local_cancel_manifest_atomicity_and_all_version_recovery(projects, monkeypatch):
    from job_hunting_agent.project_deletion import cleanup, resume_pending
    app, browser, owners, candidates, _ = projects
    backend, owner = app.state.backend, owners[0]
    response = manifest(browser, candidates[0]).json()
    collection, file_id = response["collection"]["id"], response["files"][0]["id"]
    processed = backend.process_local_project_collection_file(collection, file_id, b"prototype", account_id=owner)
    files = [backend.resume_files.save(account_id=owner, candidate_id=candidates[0], filename="design.txt", content=b"original"+bytes([i])) for i in (1, 2)]
    asset, version = backend.store.register_knowledge_asset(account_id=owner, candidate_id=candidates[0], asset_kind="document", title="Design",
        original_filename="design.txt", storage_key=files[0].storage_key, media_type="text/plain", file_size=files[0].file_size, sha256=files[0].sha256)
    backend.store.add_knowledge_asset_version(asset.id, account_id=owner, original_filename="design.txt", storage_key=files[1].storage_key,
        media_type="text/plain", file_size=files[1].file_size, sha256=files[1].sha256)
    queued = backend.store.create_background_task(account_id=owner, candidate_id=candidates[0], session_id=f"local-project-collection-{collection}", task_type="rag_index")
    with backend.store.connect() as conn:
        conn.execute("UPDATE project_collection_files SET knowledge_asset_id=?,knowledge_asset_version_id=?,storage_key=? WHERE id=?", (asset.id, version.id, files[0].storage_key, file_id))
    original = backend.store.delete_incomplete_project_collection
    def fail(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError("SECRET_SQL")
    monkeypatch.setattr(backend.store, "delete_incomplete_project_collection", fail)
    failed = submit(browser, "local-cancel", collection_id=collection)
    assert failed.status_code == 503 and "SECRET_SQL" not in failed.text
    assert backend.store.get_background_task(queued.task_key).status == "queued"
    assert backend.store.get_project_collection(collection, account_id=owner).id == collection
    monkeypatch.setattr(backend.store, "delete_incomplete_project_collection", original)
    result = submit(browser, "local-cancel", collection_id=collection)
    assert result.status_code == 200 and result.json()["cleanup_status"] == "pending", result.text
    assert backend.store.get_background_task(queued.task_key).status == "cancelled"
    assert sorted(backend.store.collect_account_deletion_storage_keys(owner)) == sorted(f.storage_key for f in files)
    deleted_profile = backend.store.delete_candidate_profile(candidates[0], account_id=owner)
    assert sorted(set(deleted_profile["storage_keys"])) == sorted(f.storage_key for f in files)
    with backend.store.connect() as conn:
        assert conn.execute("SELECT id FROM long_texts WHERE id=?", (processed.long_text_id,)).fetchone() is None
    remove = backend.resume_files.delete
    monkeypatch.setattr(backend.resume_files, "delete", Mock(side_effect=RuntimeError("SECRET_STORAGE")))
    assert cleanup(backend, owner, collection, force=True, collection=True)["cleanup_status"] == "pending"
    with backend.store.connect() as conn:
        conn.execute("UPDATE project_collection_deletions SET next_attempt_at=now()")
    monkeypatch.setattr(backend.resume_files, "delete", remove)
    assert resume_pending(backend, collection=True)["project_collection_deletions_completed"] == 1
    assert all(not backend.resume_files.path_for(f.storage_key).exists() for f in files)
    assert submit(browser, "local-cancel", collection_id=collection).json()["cleanup_status"] == "completed"


def test_local_cancel_rejects_ready_collection_and_running_worker(projects):
    app, browser, owners, candidates, records = projects
    backend = app.state.backend
    data = manifest(browser, candidates[0]).json()
    collection = data["collection"]["id"]
    task = backend.store.create_background_task(account_id=owners[0], task_type="system_probe")
    backend.store.claim_background_task(task.task_key)
    assert submit(browser, "local-cancel", collection_id=collection).json()["code"] == "PROJECT_BUSY"
    backend.store.complete_background_task(task.task_key, {})
    backend.process_local_project_collection_file(collection, data["files"][0]["id"], b"prototype", account_id=owners[0])
    backend.store.complete_project_collection(collection, account_id=owners[0], project_card_id=records[0].id)
    assert submit(browser, "local-cancel", collection_id=collection).json()["code"] == "PROJECT_COLLECTION_CONFLICT"
    assert manifest(browser, candidates[0]).json()["code"] == "PROJECT_COLLECTION_CONFLICT"


@pytest.mark.parametrize("file", [{"relative_path": "../private.txt", "file_size": 1, "sha256": "a"*64},
    {"relative_path": "README.md", "file_size": True}, {"relative_path": "README.md", "file_size": -1},
    {"relative_path": "README.md", "file_size": 1, "content": "SECRET_CONTENT"}])
def test_local_manifest_invalid_metadata_never_creates_collection(projects, file):
    app, browser, _, candidates, _ = projects
    response = manifest(browser, candidates[0], files=[file])
    assert response.status_code in {400, 422} and "SECRET" not in response.text
    with app.state.backend.store.connect() as conn:
        assert conn.execute("SELECT count(*) AS n FROM project_collection_sessions").fetchone()["n"] == 0


@pytest.mark.parametrize("method,path,data,operation", [
    ("POST", "/api/projects/local/manifest", {"candidate_id": 1,"project_name": "Project", "files": [{"relative_path": "README.md", "file_size": 1}]}, "local-manifest"),
    ("GET", "/api/projects/local/1", None, "local-get"), ("DELETE", "/api/projects/local/1", None, "local-cancel"),
    ("POST", "/api/projects/local/1/complete", None, "local-complete")])
def test_local_compatibility_fails_closed(projects, monkeypatch, method, path, data, operation):
    app, browser, _, _, _ = projects
    browser.cookies.set("job_agent_session", "session-1")
    forward = Mock(side_effect=PlatformAuthError("PROJECT_UNAVAILABLE", "Unavailable", 503))
    monkeypatch.setattr(app.state.backend.platform_auth_client, "projects", forward)
    monkeypatch.setattr(app.state.backend, "create_local_project_collection", Mock(side_effect=AssertionError("fallback")))
    monkeypatch.setattr(app.state.backend, "delete_incomplete_local_project_collection", Mock(side_effect=AssertionError("fallback")))
    monkeypatch.setattr(app.state.backend, "complete_local_project_collection", Mock(side_effect=AssertionError("fallback")))
    result = browser.request(method, path, **({"json": data} if data is not None else {}))
    assert result.status_code == 503 and forward.call_args.args == (operation, "session-1")


def test_local_large_manifest_rejected_before_creating_rows(projects):
    app, browser, _, candidates, _ = projects
    files = [{"relative_path": f"{i:04}-"+"p"*1000+".md", "file_size": 1, "sha256": "a"*64} for i in range(4000)]
    response = manifest(browser, candidates[0], files=files)
    assert response.status_code == 413, response.text[:200]
    with app.state.backend.store.connect() as conn:
        assert conn.execute("SELECT count(*) AS n FROM project_collection_sessions").fetchone()["n"] == 0


def test_local_contract_and_precise_gateway_methods():
    from pathlib import Path
    import yaml
    from job_hunting_agent.local_project_entry import COLLECTION_FIELDS, FILE_FIELDS
    root = Path(__file__).resolve().parents[1]
    contract = yaml.safe_load((root / "platform-service/src/main/resources/local-projects.openapi.yaml").read_text(encoding="utf-8"))
    for name, fields in (("Collection", COLLECTION_FIELDS), ("File", FILE_FIELDS)):
        schema = contract["components"]["schemas"][name]
        assert set(schema["required"]) == set(schema["properties"]) == fields
        assert schema["additionalProperties"] is False
    upload = contract["paths"]["/api/projects/local/{collection_id}/files"]["post"]
    assert upload["requestBody"]["content"]["multipart/form-data"]["schema"]["required"] == ["file_ids", "files"]
    result = contract["components"]["schemas"]["UploadResult"]
    assert set(result["required"]) == set(result["properties"]) and result["additionalProperties"] is False
    for name in ("Caddyfile.hybrid", "Caddyfile.hybrid.local"):
        text = (root / "deploy" / name).read_text(encoding="utf-8")
        assert "method POST\n\t\t\tpath /api/projects/local/manifest" in text
        assert "method GET DELETE\n\t\t\tpath_regexp localCollection ^/api/projects/local/[0-9]+$" in text
        assert "request_body @local_manifest" in text
        assert "method POST\n\t\t\tpath_regexp localComplete ^/api/projects/local/[0-9]+/complete$" in text
        assert "reverse_proxy @local_complete platform-service:8081" in text
        assert "method POST\n\t\t\tpath_regexp localFiles ^/api/projects/local/[0-9]+/files$" in text
        assert "request_body @local_files" in text and "max_size 167772160" in text
        assert "reverse_proxy @local_files platform-service:8081" in text
        assert "/api/projects/local/*" not in text
    hybrid = (root / "deploy/Caddyfile.hybrid").read_text(encoding="utf-8")
    assert "max_size 25MB" not in hybrid
    assert "max_size 167772160" in hybrid
    nginx = (root / "deploy/nginx/coexist-hybrid-ip-https.conf.template").read_text(encoding="utf-8")
    assert "location = /api/projects/local/manifest" in nginx and "client_max_body_size 4m;" in nginx
    assert "location ~ ^/api/projects/local/[0-9]+$" in nginx
    complete = nginx.split("location ~ ^/api/projects/local/[0-9]+/complete$ {", 1)[1].split("\n    }", 1)[0]
    assert "proxy_pass http://platform-service:8081;" in complete and "proxy_next_upstream off;" in complete
    schema = contract["paths"]["/api/projects/local/{collection_id}/complete"]["post"]["responses"]["200"]["content"]["application/json"]["schema"]
    assert set(schema["required"]) == set(schema["properties"]) == {"project_card", "processing_async"}
    assert schema["additionalProperties"] is False and schema["properties"]["processing_async"]["const"] is False
    upload = nginx.split("location ~ ^/api/projects/local/[0-9]+/files$ {", 1)[1].split("\n    }", 1)[0]
    assert "client_max_body_size 160m;" in upload and "proxy_request_buffering off;" in upload
