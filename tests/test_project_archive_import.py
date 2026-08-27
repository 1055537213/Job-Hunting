"""整包项目导入、文件类型路由和生命周期测试。"""

from __future__ import annotations

import hashlib
from io import BytesIO
from pathlib import Path
from zipfile import ZipFile

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from job_hunting_agent.app import JobHuntingApp
from job_hunting_agent.background_tasks import run_registered_task
from job_hunting_agent.deduplication import DuplicateResourceError
from job_hunting_agent.models import CandidateProfileInput
from job_hunting_agent.project_archive import analyze_project_archive
from job_hunting_agent.project_visual import (
    ProjectVisualAnalysisResult,
    ProjectVisualFinding,
)
from job_hunting_agent.resume_document import ResumeFileStore
from job_hunting_agent.web import create_web_app


class RecordingQueue:
    """记录 task_key，验证队列消息不携带项目正文。"""

    def __init__(self) -> None:
        self.task_keys: list[str] = []

    def health_check(self) -> None:
        return None

    def enqueue(self, task_key: str) -> None:
        self.task_keys.append(task_key)


class ArchiveVisualAnalyzer:
    max_pdf_pages = 4

    def analyze(
        self,
        inputs: list[object],
        *,
        account_id: int | None,
        candidate_id: int | None,
    ) -> ProjectVisualAnalysisResult:
        assert account_id == 7
        assert candidate_id == 11
        return ProjectVisualAnalysisResult(
            findings={
                item.source_id: ProjectVisualFinding(
                    source_id=item.source_id,
                    confidence=0.9,
                    summary="压力曲线显示验收工况稳定。",
                    element_relationships=("曲线保持在 16 MPa 验收线上方",),
                )
                for item in inputs
            },
            status="succeeded",
        )


def build_cross_industry_project_zip() -> bytes:
    """生成同时包含文本、工业文档、图像、表格和敏感文件的项目包。"""

    output = BytesIO()
    with ZipFile(output, "w") as archive:
        archive.writestr("pump-station/README.md", "Industrial pump monitoring project with Python.")
        archive.writestr("pump-station/src/analyze.py", "import pandas\n")
        archive.writestr("pump-station/specs/tolerance.pdf", b"%PDF-1.4 test")
        archive.writestr("pump-station/charts/pressure.png", b"PNG test")
        archive.writestr("pump-station/data/parameters.xlsx", b"XLSX test")
        archive.writestr("pump-station/reports/acceptance.docx", b"DOCX test")
        archive.writestr("pump-station/drawings/shaft.step", b"STEP test")
        archive.writestr("pump-station/.env", b"SECRET=not-read")
        archive.writestr("pump-station/node_modules/generated.js", b"ignored")
    return output.getvalue()


def build_same_card_project_zip(secret_value: str) -> bytes:
    """敏感文件内容改变会形成新快照，但不能改变可确认项目卡片。"""

    output = BytesIO()
    with ZipFile(output, "w") as archive:
        archive.writestr("service/README.md", "Python FastAPI candidate service")
        archive.writestr("service/.env", f"TOKEN={secret_value}")
    return output.getvalue()


def candidate_input() -> CandidateProfileInput:
    return CandidateProfileInput(
        name="整包项目候选人",
        status="待补充",
        education="本科",
        experience_years=3,
        salary_floor_k=None,
        expected_salary_k=None,
        skills={},
        preferred_cities=[],
        target_directions=[],
        unacceptable=[],
    )


def test_project_archive_routes_non_text_files_without_discarding_them() -> None:
    analysis = analyze_project_archive(
        filename="pump-station.zip",
        content=build_cross_industry_project_zip(),
    )

    assert analysis.card.project_name == "pump-station"
    assert analysis.card.read_files == [
        "README.md",
        "src/analyze.py",
        "drawings/shaft.step",
    ]
    assert analysis.card.discovered_file_kinds == {
        "source_text": 3,
        "pdf": 1,
        "image": 1,
        "spreadsheet": 1,
        "document": 1,
        "engineering_drawing": 1,
        "unsupported": 1,
    }
    assert set(analysis.card.deferred_files) == {
        "specs/tolerance.pdf",
        "charts/pressure.png",
        "data/parameters.xlsx",
        "reports/acceptance.docx",
    }
    files = {item.relative_path: item for item in analysis.files}
    assert files["specs/tolerance.pdf"].analysis_status == "failed"
    assert files["charts/pressure.png"].file_kind == "image"
    assert files["data/parameters.xlsx"].file_kind == "spreadsheet"
    assert files[".env"].analysis_status == "skipped"
    assert files[".env"].sha256 is None
    assert files["node_modules/generated.js"].skip_reason == "dir:node_modules"


def test_project_archive_uses_shared_visual_analyzer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    image = BytesIO()
    Image.new("RGB", (160, 100), "white").save(image, format="PNG")
    output = BytesIO()
    with ZipFile(output, "w") as archive:
        archive.writestr("pump/README.md", "Industrial pressure acceptance")
        archive.writestr("pump/charts/pressure.png", image.getvalue())
    monkeypatch.setattr("job_hunting_agent.project_evidence.run_rapidocr", lambda _image: "")

    analysis = analyze_project_archive(
        filename="pump.zip",
        content=output.getvalue(),
        visual_analyzer=ArchiveVisualAnalyzer(),
        account_id=7,
        candidate_id=11,
    )

    image_file = next(item for item in analysis.files if item.file_kind == "image")
    image_evidence = next(
        item for item in analysis.evidence if item.relative_path == "charts/pressure.png"
    )
    assert image_file.analysis_status == "analyzed"
    assert image_file.metadata["extraction_method"] == "image_vlm"
    assert "16 MPa" in image_evidence.text
    assert len(analysis.visual_artifacts) == 1
    assert analysis.visual_artifacts[0].relative_path == "charts/pressure.png"
    assert analysis.visual_artifacts[0].artifact.source_id == "image-1"


def test_project_archive_worker_persists_manifest_and_delete_cleans_all_layers(
    database_url: str,
    account_id: int,
    tmp_path: Path,
) -> None:
    queue = RecordingQueue()
    file_store = ResumeFileStore(tmp_path / "objects")
    app = JobHuntingApp(
        database_url=database_url,
        object_storage=file_store,
        task_queue=queue,
        semantic_matching=False,
    )
    candidate_id = app.save_candidate_profile(candidate_input(), account_id=account_id)
    content = build_cross_industry_project_zip()
    project_import = app.upload_project_archive(
        candidate_id,
        "pump-station.zip",
        content,
        account_id=account_id,
    )
    version = app.store.get_knowledge_asset_version(
        project_import.knowledge_asset_version_id,
        account_id=account_id,
    )
    task = app.enqueue_project_archive_analysis_task(
        project_archive_id=project_import.id,
        account_id=account_id,
        candidate_id=candidate_id,
    )

    assert task.payload == {"project_archive_id": project_import.id}
    assert queue.task_keys == [task.task_key]
    assert "Industrial pump" not in str(task.payload)
    assert file_store.path_for(version.storage_key).is_file()

    completed = run_registered_task(app, task.task_key)
    refreshed = app.store.get_project_archive_import(project_import.id, account_id=account_id)
    manifest = app.list_project_archive_files(project_import.id, account_id=account_id)
    other_account = app.store.create_account("archive-other@example.com", "hashed-password")

    assert completed["status"] == "succeeded"
    assert refreshed.status == "ready"
    assert refreshed.project_card_id is not None
    assert len(manifest) == 9
    assert {item.file_kind for item in manifest} >= {
        "source_text",
        "pdf",
        "image",
        "spreadsheet",
        "document",
        "engineering_drawing",
    }
    with pytest.raises(KeyError):
        app.list_project_archive_files(project_import.id, account_id=other_account.id)

    result = app.delete_project_card(refreshed.project_card_id, account_id=account_id)

    assert result["storage_keys"] == [version.storage_key]
    assert not file_store.path_for(version.storage_key).exists()
    with pytest.raises(KeyError):
        app.store.get_project_archive_import(project_import.id, account_id=account_id)
    with pytest.raises(KeyError):
        app.store.get_knowledge_asset(project_import.knowledge_asset_id, account_id=account_id)


def test_project_archive_duplicate_is_scoped_to_candidate(
    database_url: str,
    account_id: int,
    tmp_path: Path,
) -> None:
    app = JobHuntingApp(
        database_url=database_url,
        object_storage=ResumeFileStore(tmp_path / "objects"),
        semantic_matching=False,
    )
    first_candidate = app.save_candidate_profile(candidate_input(), account_id=account_id)
    second_input = candidate_input()
    second_input.name = "另一位候选人"
    second_candidate = app.save_candidate_profile(second_input, account_id=account_id)
    content = build_cross_industry_project_zip()

    app.upload_project_archive(
        first_candidate,
        "pump-station.zip",
        content,
        account_id=account_id,
    )
    with pytest.raises(DuplicateResourceError, match="项目压缩包"):
        app.upload_project_archive(
            first_candidate,
            "renamed.zip",
            content,
            account_id=account_id,
        )
    other = app.upload_project_archive(
        second_candidate,
        "pump-station.zip",
        content,
        account_id=account_id,
    )

    assert other.candidate_id == second_candidate
    other_version = app.store.get_knowledge_asset_version(
        other.knowledge_asset_version_id,
        account_id=account_id,
    )
    other_path = app.resume_files.path_for(other_version.storage_key)
    assert other_path.is_file()

    app.delete_candidate_profile(second_candidate, account_id=account_id)

    assert not other_path.exists()
    with pytest.raises(KeyError):
        app.store.get_project_archive_import(other.id, account_id=account_id)


def test_distinct_archive_revisions_can_share_one_card_and_delete_all_sources(
    database_url: str,
    account_id: int,
    tmp_path: Path,
) -> None:
    file_store = ResumeFileStore(tmp_path / "revision-objects")
    app = JobHuntingApp(
        database_url=database_url,
        object_storage=file_store,
        semantic_matching=False,
    )
    candidate_id = app.save_candidate_profile(candidate_input(), account_id=account_id)
    first = app.upload_project_archive(
        candidate_id,
        "service-v1.zip",
        build_same_card_project_zip("first"),
        account_id=account_id,
    )
    first_card = app.analyze_project_archive_for_candidate(first.id, account_id=account_id)
    second = app.upload_project_archive(
        candidate_id,
        "service-v2.zip",
        build_same_card_project_zip("second"),
        account_id=account_id,
    )
    second_card = app.analyze_project_archive_for_candidate(second.id, account_id=account_id)
    first_version = app.store.get_knowledge_asset_version(
        first.knowledge_asset_version_id,
        account_id=account_id,
    )
    second_version = app.store.get_knowledge_asset_version(
        second.knowledge_asset_version_id,
        account_id=account_id,
    )

    assert first_card.id == second_card.id
    assert app.store.find_project_archive_import_by_project_card(
        first_card.id,
        account_id=account_id,
    ).id == second.id

    result = app.delete_project_card(first_card.id, account_id=account_id)

    assert set(result["storage_keys"]) == {
        first_version.storage_key,
        second_version.storage_key,
    }
    assert not file_store.path_for(first_version.storage_key).exists()
    assert not file_store.path_for(second_version.storage_key).exists()
    with pytest.raises(KeyError):
        app.store.get_project_archive_import(first.id, account_id=account_id)
    with pytest.raises(KeyError):
        app.store.get_project_archive_import(second.id, account_id=account_id)


def test_local_project_web_endpoint_collects_manifest_then_selected_files(
    database_url: str,
    tmp_path: Path,
) -> None:
    queue = RecordingQueue()
    client = TestClient(
        create_web_app(
            database_url=database_url,
            resume_dir=tmp_path / "web-objects",
            task_queue=queue,
        )
    )
    client.post(
        "/api/auth/register",
        json={"email": "archive-web@example.com", "password": "password-123"},
    )
    client.post(
        "/api/auth/login",
        json={"email": "archive-web@example.com", "password": "password-123"},
    )
    profile = client.post("/api/profiles", json={"name": "项目整包 Web"})
    candidate_id = int(profile.json()["candidate_id"])

    source = b"# Pump station\nFastAPI monitoring service"
    unsupported = client.post(
        "/api/projects/local/manifest",
        json={
            "candidate_id": candidate_id,
            "project_name": "pump-station",
            "preserve_originals": True,
            "files": [
                {
                    "relative_path": "README.md",
                    "file_size": len(source),
                    "sha256": hashlib.sha256(source).hexdigest(),
                    "media_type": "text/markdown",
                }
            ],
        },
    )
    assert unsupported.status_code == 422

    manifest = client.post(
        "/api/projects/local/manifest",
        json={
            "candidate_id": candidate_id,
            "project_name": "pump-station",
            "files": [
                {
                    "relative_path": "README.md",
                    "file_size": len(source),
                    "sha256": hashlib.sha256(source).hexdigest(),
                    "media_type": "text/markdown",
                },
                {
                    "relative_path": ".env",
                    "file_size": 10,
                    "sha256": None,
                    "media_type": "text/plain",
                },
            ],
        },
    )
    assert manifest.status_code == 200, manifest.text
    manifest_payload = manifest.json()
    selected = manifest_payload["selected_files"]
    assert len(selected) == 1
    assert selected[0]["relative_path"] == "README.md"
    assert next(
        item for item in manifest_payload["files"] if item["relative_path"] == ".env"
    )["selection_reason"] == "sensitive_path"

    collection_id = manifest_payload["collection"]["id"]
    uploaded = client.post(
        f"/api/projects/local/{collection_id}/files",
        data={"file_ids": f"[{selected[0]['id']}]"},
        files=[("files", ("README.md", source, "text/markdown"))],
    )
    assert uploaded.status_code == 200, uploaded.text
    assert uploaded.json()["processed_files"][0]["long_text_id"] is not None
    assert uploaded.json()["rag_task"]["task_type"] == "rag_index"

    completed = client.post(f"/api/projects/local/{collection_id}/complete")
    assert completed.status_code == 200, completed.text
    assert completed.json()["project_card"]["card"]["project_name"] == "pump-station"
    assert len(queue.task_keys) == 1
