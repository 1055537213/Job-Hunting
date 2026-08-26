"""统一知识文件资产和版本管理测试。"""

from __future__ import annotations

import pytest

from job_hunting_agent.deduplication import DuplicateResourceError
from job_hunting_agent.models import CandidateProfileInput
from job_hunting_agent.sqlalchemy_store import SQLAlchemyStore


def _candidate(store: SQLAlchemyStore, account_id: int, name: str = "测试候选人") -> int:
    return store.save_candidate_profile(
        CandidateProfileInput(
            name=name,
            status="在职",
            education="本科",
            experience_years=2,
            salary_floor_k=None,
            expected_salary_k=None,
            skills={"Python": "项目使用"},
            preferred_cities=["杭州"],
            target_directions=["后端开发"],
        ),
        account_id=account_id,
    )


def test_knowledge_asset_versions_keep_one_current_version_and_account_boundary(database_url) -> None:
    store = SQLAlchemyStore(database_url)
    store.initialize()
    owner = store.create_account("asset-owner@example.com", "hashed-password")
    other = store.create_account("asset-other@example.com", "hashed-password")
    candidate_id = _candidate(store, owner.id)

    asset, first = store.register_knowledge_asset(
        account_id=owner.id,
        candidate_id=candidate_id,
        asset_kind="industrial_document",
        title="主轴设计规范",
        original_filename="main-shaft-rev-a.pdf",
        storage_key="knowledge/owner/main-shaft-rev-a.pdf",
        media_type="application/pdf",
        file_size=1024,
        sha256="a" * 64,
        source_kind="upload",
        processing_status="ready",
        scan_status="clean",
        revision_label="A",
        metadata={"document_number": "SPEC-001"},
    )
    second = store.add_knowledge_asset_version(
        asset.id,
        account_id=owner.id,
        original_filename="main-shaft-rev-b.pdf",
        storage_key="knowledge/owner/main-shaft-rev-b.pdf",
        media_type="application/pdf",
        file_size=2048,
        sha256="b" * 64,
        source_kind="upload",
        processing_status="processing",
        scan_status="clean",
        revision_label="B",
    )

    refreshed = store.get_knowledge_asset(asset.id, account_id=owner.id)
    versions = store.list_knowledge_asset_versions(asset.id, account_id=owner.id)

    assert first.version_number == 1
    assert second.version_number == 2
    assert refreshed.current_version_id == second.id
    assert [(item.version_number, item.is_current) for item in versions] == [(1, False), (2, True)]
    assert refreshed.metadata == {"document_number": "SPEC-001"}
    with pytest.raises(DuplicateResourceError, match="知识文件版本"):
        store.add_knowledge_asset_version(
            asset.id,
            account_id=owner.id,
            original_filename="duplicate.pdf",
            storage_key="knowledge/owner/duplicate.pdf",
            media_type="application/pdf",
            file_size=1024,
            sha256="b" * 64,
        )
    with pytest.raises(KeyError):
        store.get_knowledge_asset(asset.id, account_id=other.id)


def test_archived_knowledge_asset_cannot_receive_a_new_version(database_url) -> None:
    store = SQLAlchemyStore(database_url)
    store.initialize()
    account = store.create_account("archived-asset@example.com", "hashed-password")
    candidate_id = _candidate(store, account.id)
    asset, _ = store.register_knowledge_asset(
        account_id=account.id,
        candidate_id=candidate_id,
        asset_kind="project_document",
        title="项目验收资料",
        original_filename="acceptance.pdf",
        storage_key="knowledge/acceptance.pdf",
        media_type="application/pdf",
        file_size=512,
        sha256="c" * 64,
    )

    archived = store.archive_knowledge_asset(asset.id, account_id=account.id)

    assert archived.lifecycle_status == "archived"
    assert store.list_knowledge_assets(account_id=account.id) == []
    assert [item.id for item in store.list_knowledge_assets(account_id=account.id, include_archived=True)] == [
        asset.id
    ]
    with pytest.raises(ValueError, match="已归档"):
        store.add_knowledge_asset_version(
            asset.id,
            account_id=account.id,
            original_filename="acceptance-v2.pdf",
            storage_key="knowledge/acceptance-v2.pdf",
            media_type="application/pdf",
            file_size=600,
            sha256="d" * 64,
        )


def test_source_resume_lifecycle_is_mirrored_to_knowledge_asset(database_url) -> None:
    store = SQLAlchemyStore(database_url)
    store.initialize()
    account = store.create_account("resume-asset@example.com", "hashed-password")
    candidate_id = _candidate(store, account.id)

    artifact = store.save_resume_artifact(
        account_id=account.id,
        candidate_id=candidate_id,
        artifact_type="source",
        original_filename="resume.pdf",
        download_filename="resume.pdf",
        storage_key="resumes/resume.pdf",
        media_type="application/pdf",
        file_size=4096,
        sha256="e" * 64,
        extraction_method="scan_pending",
        extracted_text="",
        page_count=None,
        status="scanning",
        scan_status="pending",
    )

    assert artifact.knowledge_asset_id is not None
    assert artifact.knowledge_asset_version_id is not None
    asset = store.get_knowledge_asset(artifact.knowledge_asset_id, account_id=account.id)
    pending_version = store.get_knowledge_asset_version(
        artifact.knowledge_asset_version_id,
        account_id=account.id,
    )
    assert asset.asset_kind == "resume"
    assert asset.current_version_id == pending_version.id
    assert pending_version.processing_status == "scanning"
    assert pending_version.scan_status == "pending"

    store.complete_resume_artifact_scan(
        artifact.id,
        next_status="ready",
        extraction_method="docx",
        page_count=1,
        scan_engine="local-safety",
        account_id=account.id,
    )
    completed_version = store.get_knowledge_asset_version(
        artifact.knowledge_asset_version_id,
        account_id=account.id,
    )
    assert completed_version.processing_status == "ready"
    assert completed_version.scan_status == "clean"
    assert completed_version.scan_engine == "local-safety"

    deleted = store.delete_resume_artifact(artifact.id, account_id=account.id)

    assert deleted["knowledge_asset_id"] == asset.id
    with pytest.raises(KeyError):
        store.get_knowledge_asset(asset.id, account_id=account.id)

    quarantined_artifact = store.save_resume_artifact(
        account_id=account.id,
        candidate_id=candidate_id,
        artifact_type="source",
        original_filename="infected.pdf",
        download_filename="infected.pdf",
        storage_key="resumes/infected.pdf",
        media_type="application/pdf",
        file_size=2048,
        sha256="f" * 64,
        extraction_method="scan_pending",
        extracted_text="",
        page_count=None,
        status="scanning",
        scan_status="pending",
    )
    store.quarantine_resume_artifact(
        quarantined_artifact.id,
        scan_status="infected",
        scan_engine="clamav",
        scan_reason="test signature",
        account_id=account.id,
    )
    quarantined_version = store.get_knowledge_asset_version(
        quarantined_artifact.knowledge_asset_version_id,
        account_id=account.id,
    )
    assert quarantined_version.processing_status == "quarantined"
    assert quarantined_version.scan_status == "infected"
    assert quarantined_version.scan_reason == "test signature"
