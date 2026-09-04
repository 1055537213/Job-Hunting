"""Upload verified production backup artifacts to private S3-compatible storage."""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlparse

import boto3
from botocore.config import Config

BACKUP_ID_PATTERN = re.compile(r"^\d{8}-\d{6}-[0-9a-f]{12}$")
REQUIRED_FILES = ("postgres.dump", "minio-data.tar.gz", "manifest.json")
COMPLETE_MARKER = "remote-complete.json"


def _parse_bool(value: str, *, name: str) -> bool:
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be true or false")


def _required(values: Mapping[str, str], name: str) -> str:
    value = values.get(name, "").strip()
    if not value:
        raise ValueError(f"{name} is required when offsite backup is enabled")
    return value


@dataclass(frozen=True)
class BackupStorageSettings:
    bucket: str
    prefix: str
    region: str
    access_key: str
    secret_key: str
    endpoint: str | None
    force_path_style: bool
    server_side_encryption: str | None
    kms_key_id: str | None
    verify_download: bool
    remote_retention_count: int

    @classmethod
    def from_environment(
        cls, values: Mapping[str, str] | None = None
    ) -> BackupStorageSettings:
        env = os.environ if values is None else values
        bucket = _required(env, "JOB_AGENT_BACKUP_OFFSITE_BUCKET")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{1,61}[A-Za-z0-9]", bucket):
            raise ValueError("JOB_AGENT_BACKUP_OFFSITE_BUCKET has an invalid format")
        prefix = env.get(
            "JOB_AGENT_BACKUP_OFFSITE_PREFIX", "job-hunting-agent/production"
        ).strip(" /")
        if (
            not prefix
            or "\\" in prefix
            or any(part in {"", ".", ".."} for part in prefix.split("/"))
            or any(ord(character) < 32 for character in prefix)
        ):
            raise ValueError("JOB_AGENT_BACKUP_OFFSITE_PREFIX has an invalid format")
        region = env.get("JOB_AGENT_BACKUP_OFFSITE_REGION", "us-east-1").strip()
        access_key = _required(env, "JOB_AGENT_BACKUP_OFFSITE_ACCESS_KEY")
        secret_key = _required(env, "JOB_AGENT_BACKUP_OFFSITE_SECRET_KEY")
        endpoint = env.get("JOB_AGENT_BACKUP_OFFSITE_ENDPOINT", "").strip() or None
        if endpoint:
            parsed_endpoint = urlparse(endpoint)
            if (
                parsed_endpoint.scheme != "https"
                or not parsed_endpoint.netloc
                or parsed_endpoint.query
                or parsed_endpoint.fragment
            ):
                raise ValueError(
                    "JOB_AGENT_BACKUP_OFFSITE_ENDPOINT must be an HTTPS endpoint"
                )
        force_path_style = _parse_bool(
            env.get("JOB_AGENT_BACKUP_OFFSITE_FORCE_PATH_STYLE", "false"),
            name="JOB_AGENT_BACKUP_OFFSITE_FORCE_PATH_STYLE",
        )
        verify_download = _parse_bool(
            env.get("JOB_AGENT_BACKUP_OFFSITE_VERIFY_DOWNLOAD", "true"),
            name="JOB_AGENT_BACKUP_OFFSITE_VERIFY_DOWNLOAD",
        )
        encryption_value = env.get(
            "JOB_AGENT_BACKUP_OFFSITE_SERVER_SIDE_ENCRYPTION", "AES256"
        ).strip()
        if encryption_value.lower() == "none":
            encryption = None
        elif encryption_value in {"AES256", "aws:kms"}:
            encryption = encryption_value
        else:
            raise ValueError(
                "JOB_AGENT_BACKUP_OFFSITE_SERVER_SIDE_ENCRYPTION must be "
                "AES256, aws:kms, or none"
            )
        kms_key_id = env.get("JOB_AGENT_BACKUP_OFFSITE_KMS_KEY_ID", "").strip() or None
        if encryption == "aws:kms" and not kms_key_id:
            raise ValueError(
                "JOB_AGENT_BACKUP_OFFSITE_KMS_KEY_ID is required for aws:kms"
            )
        try:
            retention = int(
                env.get("JOB_AGENT_BACKUP_OFFSITE_RETENTION_COUNT", "30")
            )
        except ValueError as exc:
            raise ValueError(
                "JOB_AGENT_BACKUP_OFFSITE_RETENTION_COUNT must be an integer"
            ) from exc
        if not 2 <= retention <= 365:
            raise ValueError(
                "JOB_AGENT_BACKUP_OFFSITE_RETENTION_COUNT must be between 2 and 365"
            )
        return cls(
            bucket=bucket,
            prefix=prefix,
            region=region,
            access_key=access_key,
            secret_key=secret_key,
            endpoint=endpoint,
            force_path_style=force_path_style,
            server_side_encryption=encryption,
            kms_key_id=kms_key_id,
            verify_download=verify_download,
            remote_retention_count=retention,
        )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _load_verified_files(directory: Path) -> dict[str, tuple[Path, str, int]]:
    if not directory.is_dir():
        raise ValueError(f"Backup directory does not exist: {directory}")
    paths = {name: directory / name for name in REQUIRED_FILES}
    for name, path in paths.items():
        if not path.is_file() or path.stat().st_size == 0:
            raise ValueError(f"Required backup artifact is missing or empty: {name}")

    try:
        manifest = json.loads(paths["manifest.json"].read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("Backup manifest is not valid JSON") from exc
    if manifest.get("postgres_dump") != "postgres.dump":
        raise ValueError("Backup manifest contains an unexpected PostgreSQL filename")
    if manifest.get("minio_archive") != "minio-data.tar.gz":
        raise ValueError("Backup manifest contains an unexpected MinIO filename")

    files: dict[str, tuple[Path, str, int]] = {}
    for name, path in paths.items():
        digest = _sha256(path)
        size = path.stat().st_size
        files[name] = (path, digest, size)
    if manifest.get("postgres_sha256") != files["postgres.dump"][1]:
        raise ValueError("PostgreSQL backup SHA-256 does not match the manifest")
    if manifest.get("minio_sha256") != files["minio-data.tar.gz"][1]:
        raise ValueError("MinIO backup SHA-256 does not match the manifest")
    return files


def _build_client(settings: BackupStorageSettings):
    addressing_style = "path" if settings.force_path_style else "virtual"
    return boto3.client(
        "s3",
        endpoint_url=settings.endpoint,
        region_name=settings.region,
        aws_access_key_id=settings.access_key,
        aws_secret_access_key=settings.secret_key,
        config=Config(
            signature_version="s3v4",
            s3={"addressing_style": addressing_style},
            retries={"max_attempts": 5, "mode": "standard"},
        ),
    )


def _object_key(settings: BackupStorageSettings, backup_id: str, name: str) -> str:
    parts = [part for part in (settings.prefix, backup_id, name) if part]
    return "/".join(parts)


def _encryption_arguments(settings: BackupStorageSettings) -> dict[str, str]:
    if not settings.server_side_encryption:
        return {}
    arguments = {"ServerSideEncryption": settings.server_side_encryption}
    if settings.server_side_encryption == "aws:kms" and settings.kms_key_id:
        arguments["SSEKMSKeyId"] = settings.kms_key_id
    return arguments


def _hash_remote_body(body: Any) -> str:
    digest = hashlib.sha256()
    try:
        while chunk := body.read(1024 * 1024):
            digest.update(chunk)
    finally:
        close = getattr(body, "close", None)
        if close is not None:
            close()
    return digest.hexdigest()


def _read_remote_body(body: Any) -> bytes:
    try:
        return body.read()
    finally:
        close = getattr(body, "close", None)
        if close is not None:
            close()


def _list_completed_backup_ids(client: Any, settings: BackupStorageSettings) -> list[str]:
    root_prefix = f"{settings.prefix}/" if settings.prefix else ""
    paginator = client.get_paginator("list_objects_v2")
    completed: set[str] = set()
    for page in paginator.paginate(Bucket=settings.bucket, Prefix=root_prefix):
        for item in page.get("Contents", []):
            key = str(item.get("Key", ""))
            relative = key.removeprefix(root_prefix)
            backup_id, separator, name = relative.partition("/")
            if (
                separator
                and name == COMPLETE_MARKER
                and BACKUP_ID_PATTERN.fullmatch(backup_id)
            ):
                completed.add(backup_id)
    return sorted(completed, reverse=True)


def _delete_backup_prefix(client: Any, settings: BackupStorageSettings, backup_id: str) -> None:
    prefix = _object_key(settings, backup_id, "")
    if prefix:
        prefix += "/"
    paginator = client.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=settings.bucket, Prefix=prefix):
        keys = [{"Key": item["Key"]} for item in page.get("Contents", [])]
        for offset in range(0, len(keys), 1000):
            response = client.delete_objects(
                Bucket=settings.bucket,
                Delete={"Objects": keys[offset : offset + 1000], "Quiet": True},
            )
            if response.get("Errors"):
                raise RuntimeError("Offsite backup retention deletion was incomplete")


def upload_backup(
    directory: Path,
    backup_id: str,
    settings: BackupStorageSettings,
    *,
    client: Any | None = None,
) -> dict[str, Any]:
    if not BACKUP_ID_PATTERN.fullmatch(backup_id):
        raise ValueError("Backup ID has an unsupported format")
    files = _load_verified_files(directory)
    s3 = _build_client(settings) if client is None else client
    encryption = _encryption_arguments(settings)

    uploaded: list[dict[str, Any]] = []
    marker_written = False
    try:
        for name in REQUIRED_FILES:
            path, digest, size = files[name]
            key = _object_key(settings, backup_id, name)
            extra_args: dict[str, Any] = {
                "Metadata": {"sha256": digest, "backup-id": backup_id},
                **encryption,
            }
            s3.upload_file(str(path), settings.bucket, key, ExtraArgs=extra_args)
            head = s3.head_object(Bucket=settings.bucket, Key=key)
            if int(head.get("ContentLength", -1)) != size:
                raise RuntimeError(f"Remote size verification failed for {name}")
            if head.get("Metadata", {}).get("sha256") != digest:
                raise RuntimeError(f"Remote metadata verification failed for {name}")
            if settings.verify_download:
                remote = s3.get_object(Bucket=settings.bucket, Key=key)
                if _hash_remote_body(remote["Body"]) != digest:
                    raise RuntimeError(f"Remote content verification failed for {name}")
            uploaded.append({"name": name, "sha256": digest, "size": size})

        marker = {
            "schema_version": 1,
            "backup_id": backup_id,
            "files": uploaded,
            "verified_by_download": settings.verify_download,
        }
        marker_body = json.dumps(marker, ensure_ascii=True, sort_keys=True).encode(
            "utf-8"
        )
        s3.put_object(
            Bucket=settings.bucket,
            Key=_object_key(settings, backup_id, COMPLETE_MARKER),
            Body=marker_body,
            ContentType="application/json",
            **encryption,
        )
        remote_marker = s3.get_object(
            Bucket=settings.bucket,
            Key=_object_key(settings, backup_id, COMPLETE_MARKER),
        )
        if _read_remote_body(remote_marker["Body"]) != marker_body:
            raise RuntimeError("Remote completion marker verification failed")
        marker_written = True
    except Exception:
        if not marker_written:
            with contextlib.suppress(Exception):
                _delete_backup_prefix(s3, settings, backup_id)
        raise

    completed = _list_completed_backup_ids(s3, settings)
    pruned: list[str] = []
    for old_backup_id in completed[settings.remote_retention_count :]:
        if old_backup_id == backup_id:
            continue
        _delete_backup_prefix(s3, settings, old_backup_id)
        pruned.append(old_backup_id)

    return {
        "backup_id": backup_id,
        "bucket": settings.bucket,
        "prefix": settings.prefix,
        "uploaded_files": len(uploaded),
        "verified_by_download": settings.verify_download,
        "pruned_backup_ids": pruned,
    }


def download_backup(
    destination_root: Path,
    backup_id: str,
    settings: BackupStorageSettings,
    *,
    client: Any | None = None,
) -> dict[str, Any]:
    if not BACKUP_ID_PATTERN.fullmatch(backup_id):
        raise ValueError("Backup ID has an unsupported format")
    destination_root.mkdir(parents=True, exist_ok=True)
    destination = destination_root / backup_id
    partial = destination_root / f".{backup_id}.partial"
    if destination.exists() or partial.exists():
        raise ValueError("Backup destination already exists")
    s3 = _build_client(settings) if client is None else client
    marker_key = _object_key(settings, backup_id, COMPLETE_MARKER)
    try:
        marker_object = s3.get_object(Bucket=settings.bucket, Key=marker_key)
        marker = json.loads(_read_remote_body(marker_object["Body"]).decode("utf-8"))
    except (KeyError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("Remote completion marker is invalid") from exc
    if marker.get("schema_version") != 1 or marker.get("backup_id") != backup_id:
        raise ValueError("Remote completion marker identity does not match the request")
    expected_files = marker.get("files")
    if not isinstance(expected_files, list):
        raise ValueError("Remote completion marker does not contain a file list")
    expected = {
        item.get("name"): item
        for item in expected_files
        if isinstance(item, dict) and item.get("name") in REQUIRED_FILES
    }
    if set(expected) != set(REQUIRED_FILES):
        raise ValueError("Remote completion marker is missing required artifacts")

    partial.mkdir(mode=0o700)
    try:
        for name in REQUIRED_FILES:
            path = partial / name
            s3.download_file(
                settings.bucket,
                _object_key(settings, backup_id, name),
                str(path),
            )
            digest = _sha256(path)
            if digest != expected[name].get("sha256"):
                raise RuntimeError(f"Downloaded content verification failed for {name}")
            if path.stat().st_size != int(expected[name].get("size", -1)):
                raise RuntimeError(f"Downloaded size verification failed for {name}")
            path.chmod(0o600)
        _load_verified_files(partial)
        (partial / "COMPLETE").touch(mode=0o600)
        partial.rename(destination)
    except Exception:
        shutil.rmtree(partial, ignore_errors=True)
        raise
    return {
        "backup_id": backup_id,
        "destination": str(destination),
        "downloaded_files": len(REQUIRED_FILES),
        "sha256_verified": True,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("upload", "download"))
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--backup-id", required=True)
    arguments = parser.parse_args()

    settings = BackupStorageSettings.from_environment()
    if arguments.command == "upload":
        result = upload_backup(arguments.directory, arguments.backup_id, settings)
    else:
        result = download_backup(arguments.directory, arguments.backup_id, settings)
    print(json.dumps(result, ensure_ascii=True, sort_keys=True))


if __name__ == "__main__":
    main()
