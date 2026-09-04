from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path

import pytest

from job_hunting_agent.backup_storage import (
    COMPLETE_MARKER,
    BackupStorageSettings,
    download_backup,
    upload_backup,
)


class _Paginator:
    def __init__(self, client: _FakeS3Client) -> None:
        self.client = client

    def paginate(self, *, Bucket: str, Prefix: str):  # noqa: N803
        del Bucket
        yield {
            "Contents": [
                {"Key": key} for key in sorted(self.client.objects) if key.startswith(Prefix)
            ]
        }


class _FakeS3Client:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.metadata: dict[str, dict[str, str]] = {}
        self.deleted: list[str] = []

    def upload_file(self, filename: str, bucket: str, key: str, *, ExtraArgs):
        del bucket
        self.objects[key] = Path(filename).read_bytes()
        self.metadata[key] = ExtraArgs["Metadata"]

    def head_object(self, *, Bucket: str, Key: str):  # noqa: N803
        del Bucket
        return {
            "ContentLength": len(self.objects[Key]),
            "Metadata": self.metadata[Key],
        }

    def get_object(self, *, Bucket: str, Key: str):  # noqa: N803
        del Bucket
        return {"Body": io.BytesIO(self.objects[Key])}

    def download_file(self, bucket: str, key: str, filename: str):
        del bucket
        Path(filename).write_bytes(self.objects[key])

    def put_object(self, *, Bucket: str, Key: str, Body: bytes, **kwargs):  # noqa: N803
        del Bucket, kwargs
        self.objects[Key] = Body
        self.metadata[Key] = {}

    def get_paginator(self, name: str):
        assert name == "list_objects_v2"
        return _Paginator(self)

    def delete_objects(self, *, Bucket: str, Delete):  # noqa: N803
        del Bucket
        for item in Delete["Objects"]:
            self.deleted.append(item["Key"])
            self.objects.pop(item["Key"], None)
        return {}


def _settings(*, retention: int = 2) -> BackupStorageSettings:
    return BackupStorageSettings(
        bucket="private-backups",
        prefix="job-agent/production",
        region="us-east-1",
        access_key="access",
        secret_key="secret",
        endpoint="https://backup.example.invalid",
        force_path_style=True,
        server_side_encryption="AES256",
        kms_key_id=None,
        verify_download=True,
        remote_retention_count=retention,
    )


def _write_backup(directory: Path) -> None:
    postgres = b"postgres-backup"
    minio = b"minio-backup"
    directory.mkdir()
    (directory / "postgres.dump").write_bytes(postgres)
    (directory / "minio-data.tar.gz").write_bytes(minio)
    manifest = {
        "postgres_dump": "postgres.dump",
        "minio_archive": "minio-data.tar.gz",
        "postgres_sha256": hashlib.sha256(postgres).hexdigest(),
        "minio_sha256": hashlib.sha256(minio).hexdigest(),
    }
    (directory / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")


def test_settings_require_private_destination_credentials():
    with pytest.raises(ValueError, match="JOB_AGENT_BACKUP_OFFSITE_BUCKET"):
        BackupStorageSettings.from_environment({})


def test_settings_reject_plain_http_endpoint():
    with pytest.raises(ValueError, match="must be an HTTPS endpoint"):
        BackupStorageSettings.from_environment(
            {
                "JOB_AGENT_BACKUP_OFFSITE_BUCKET": "private-backups",
                "JOB_AGENT_BACKUP_OFFSITE_ACCESS_KEY": "access",
                "JOB_AGENT_BACKUP_OFFSITE_SECRET_KEY": "secret",
                "JOB_AGENT_BACKUP_OFFSITE_ENDPOINT": "http://backup.example.invalid",
            }
        )


def test_upload_verifies_content_and_prunes_only_completed_old_backups(tmp_path: Path):
    backup_id = "20260904-120000-aaaaaaaaaaaa"
    backup_directory = tmp_path / backup_id
    _write_backup(backup_directory)
    client = _FakeS3Client()
    for old_id in (
        "20260901-120000-bbbbbbbbbbbb",
        "20260902-120000-cccccccccccc",
    ):
        prefix = f"job-agent/production/{old_id}"
        client.objects[f"{prefix}/postgres.dump"] = b"old"
        client.objects[f"{prefix}/{COMPLETE_MARKER}"] = b"{}"
    client.objects["job-agent/production/incomplete/postgres.dump"] = b"partial"

    result = upload_backup(backup_directory, backup_id, _settings(), client=client)

    assert result["uploaded_files"] == 3
    assert result["verified_by_download"] is True
    assert result["pruned_backup_ids"] == ["20260901-120000-bbbbbbbbbbbb"]
    assert any(key.endswith(f"/{COMPLETE_MARKER}") for key in client.objects)
    assert "job-agent/production/incomplete/postgres.dump" in client.objects
    assert all("20260901-120000-bbbbbbbbbbbb" not in key for key in client.objects)


def test_upload_rejects_tampered_backup_before_contacting_storage(tmp_path: Path):
    backup_id = "20260904-120000-aaaaaaaaaaaa"
    backup_directory = tmp_path / backup_id
    _write_backup(backup_directory)
    (backup_directory / "postgres.dump").write_bytes(b"tampered")

    with pytest.raises(ValueError, match="PostgreSQL backup SHA-256"):
        upload_backup(backup_directory, backup_id, _settings(), client=_FakeS3Client())


def test_download_requires_complete_marker_and_rechecks_hashes(tmp_path: Path):
    backup_id = "20260904-120000-aaaaaaaaaaaa"
    source = tmp_path / "source"
    _write_backup(source)
    client = _FakeS3Client()
    upload_backup(source, backup_id, _settings(), client=client)

    result = download_backup(tmp_path / "downloaded", backup_id, _settings(), client=client)

    restored = Path(result["destination"])
    assert result["sha256_verified"] is True
    assert (restored / "COMPLETE").is_file()
    assert (restored / "postgres.dump").read_bytes() == b"postgres-backup"


def test_download_removes_partial_directory_after_integrity_failure(tmp_path: Path):
    backup_id = "20260904-120000-aaaaaaaaaaaa"
    source = tmp_path / "source"
    _write_backup(source)
    client = _FakeS3Client()
    upload_backup(source, backup_id, _settings(), client=client)
    client.objects[f"job-agent/production/{backup_id}/postgres.dump"] = b"tampered"

    with pytest.raises(RuntimeError, match="Downloaded content verification"):
        download_backup(tmp_path / "downloaded", backup_id, _settings(), client=client)

    assert not (tmp_path / "downloaded" / f".{backup_id}.partial").exists()
