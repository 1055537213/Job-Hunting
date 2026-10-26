"""Scoped local collection management; parsing and file writes remain Python owned."""

import json
import re
from dataclasses import asdict

from .account_work import AccountWorkBusy, AccountWorkDenied
from .project_deletion import collection_projection
from .chat_session_query import positive_id

MAX_COLLECTION_REQUEST_BYTES = 4 * 1024 * 1024
MAX_COLLECTION_BYTES = 16 * 1024 * 1024
COLLECTION_FIELDS = {"id", "candidate_id", "project_card_id", "project_name", "source_type", "manifest_fingerprint", "status",
    "file_count", "selected_file_count", "uploaded_file_count", "total_size", "selected_size", "error_summary", "created_at", "updated_at"}
FILE_FIELDS = {"id", "collection_id", "relative_path", "file_kind", "media_type", "file_size", "client_sha256", "server_sha256",
    "selection_status", "selection_reason", "extraction_method", "text_length", "long_text_id", "metadata", "created_at", "updated_at"}


class CollectionConflict(ValueError):
    pass


class CollectionIncomplete(ValueError):
    pass


def validate_collection(value, operation, *, candidate_id=None, collection_id=None):
    if operation == "local-cancel":
        if (not isinstance(value, dict) or set(value) != {"deleted", "collection_id", "candidate_id", "cleanup_status", "rag_cleanup"}
                or value["deleted"] is not True or value["collection_id"] != collection_id or not positive_id(value["collection_id"])
                or not positive_id(value["candidate_id"]) or value["cleanup_status"] not in {"pending", "completed"}
                or value["rag_cleanup"] != "database_cascade"):
            raise ValueError("Invalid collection cancellation")
        return value
    fields = {"collection", "files", "selected_files", "resumed"} if operation == "local-manifest" else {"collection", "files"}
    if not isinstance(value, dict) or set(value) != fields:
        raise ValueError("Invalid collection fields")
    c = value["collection"]
    if (not isinstance(c, dict) or set(c) != COLLECTION_FIELDS or not positive_id(c["id"]) or not positive_id(c["candidate_id"])
            or (collection_id is not None and c["id"] != collection_id) or (candidate_id is not None and c["candidate_id"] != candidate_id)
            or (c["project_card_id"] is not None and not positive_id(c["project_card_id"]))
            or c["source_type"] != "local_directory_collection" or c["status"] not in {"planned", "uploading", "processing", "ready", "failed", "cancelled"}
            or not all(isinstance(c[k], str) for k in ("project_name", "manifest_fingerprint", "created_at", "updated_at"))
            or re.fullmatch(r"[a-f0-9]{64}", c["manifest_fingerprint"]) is None
            or any(type(c[k]) is not int or not 0 <= c[k] <= 2**63-1 for k in ("file_count", "selected_file_count", "uploaded_file_count", "total_size", "selected_size"))
            or c["error_summary"] not in {None, "采集未完成，请重新选择目录后重试。"}):
        raise ValueError("Invalid collection scope")
    files = value["files"]
    if not isinstance(files, list) or not 1 <= len(files) <= 20000 or len(files) != c["file_count"]:
        raise ValueError("Invalid file list")
    ids, paths = set(), set()
    for row in files:
        if (not isinstance(row, dict) or set(row) != FILE_FIELDS or not positive_id(row["id"]) or row["id"] in ids
                or type(row["collection_id"]) is not int or row["collection_id"] != c["id"]
                or not all(isinstance(row[k], str) for k in ("relative_path", "file_kind", "media_type", "selection_status", "selection_reason", "created_at", "updated_at"))
                or not 1 <= len(row["relative_path"]) <= 2048 or row["relative_path"] in paths
                or row["selection_status"] not in {"selected", "skipped", "uploaded", "analyzed", "failed"}
                or any(type(row[k]) is not int or not 0 <= row[k] <= 2**63-1 for k in ("file_size", "text_length"))
                or any(row[k] is not None and (not isinstance(row[k], str) or re.fullmatch(r"[a-f0-9]{64}", row[k]) is None) for k in ("client_sha256", "server_sha256"))
                or row["extraction_method"] is not None and not isinstance(row["extraction_method"], str)
                or row["long_text_id"] is not None and not positive_id(row["long_text_id"])
                or not isinstance(row["metadata"], dict) or set(row["metadata"]) != {"last_modified"}
                or row["metadata"]["last_modified"] is not None and (type(row["metadata"]["last_modified"]) is not int or not 0 <= row["metadata"]["last_modified"] <= 2**63-1)):
            raise ValueError("Invalid file scope")
        ids.add(row["id"])
        paths.add(row["relative_path"])
    if (c["total_size"] != sum(row["file_size"] for row in files) or c["selected_file_count"] > c["file_count"]
            or c["uploaded_file_count"] > c["selected_file_count"] or c["selected_size"] > c["total_size"]):
        raise ValueError("Invalid collection counts")
    if operation == "local-manifest":
        if (value["selected_files"] != [row for row in files if row["selection_status"] == "selected"]
                or type(value["resumed"]) is not bool or value["resumed"] != (c["uploaded_file_count"] > 0 or any(row["selection_status"] == "analyzed" for row in files))):
            raise ValueError("Invalid collection plan")
    return value


def project_collection_view(session, files, *, manifest=False):
    collection = asdict(session)
    collection.pop("account_id")
    collection["error_summary"] = "采集未完成，请重新选择目录后重试。" if session.error_summary else None
    rows = []
    for item in files:
        row = asdict(item)
        modified = item.metadata.get("last_modified")
        row["metadata"] = {"last_modified": modified if type(modified) is int and 0 <= modified <= 2**63-1 else None}
        if item.selection_status == "failed":
            row["selection_reason"] = "file_processing_failed"
        rows.append(row)
    result = {"collection": collection, "files": rows}
    if manifest:
        result.update(selected_files=[row for row in rows if row["selection_status"] == "selected"],
            resumed=session.uploaded_file_count > 0 or any(row["selection_status"] == "analyzed" for row in rows))
    return result


def get_collection_view(backend, owner, collection_id):
    with backend.store.connect() as conn:
        conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        row = conn.execute("SELECT * FROM project_collection_sessions WHERE id=? AND account_id=?", (collection_id, owner)).fetchone()
        if row is None:
            raise KeyError(collection_id)
        files = conn.execute("SELECT * FROM project_collection_files WHERE collection_id=? ORDER BY id", (collection_id,)).fetchall()
        return project_collection_view(backend.store._project_collection_session_from_row(row),
            [backend.store._project_collection_file_from_row(item) for item in files])


def cancel_collection(backend, owner, collection_id):
    with backend.store.account_work.deletion(owner):
        with backend.store.connect() as conn:
            conn.execute("SET LOCAL lock_timeout='1s'")
            conn.execute("SET LOCAL statement_timeout='3s'")
            account = conn.execute("SELECT status,deleted_at FROM accounts WHERE id=? FOR UPDATE", (owner,)).fetchone()
            if account is None or account["status"] != "active" or account["deleted_at"] is not None:
                raise AccountWorkDenied()
            receipt = conn.execute("SELECT * FROM project_collection_deletions WHERE account_id=? AND collection_id=?", (owner, collection_id)).fetchone()
            if receipt:
                return collection_projection(collection_id, receipt["candidate_id"], receipt["status"])
            if conn.execute("SELECT id FROM project_collection_sessions WHERE id=? AND account_id=?", (collection_id, owner)).fetchone() is None:
                raise KeyError(collection_id)
            if conn.execute("SELECT 1 FROM background_tasks WHERE account_id=? AND status='running' LIMIT 1", (owner,)).fetchone():
                raise AccountWorkBusy()
            try:
                result = backend.store.delete_incomplete_project_collection(collection_id, account_id=owner, _connection=conn)
            except ValueError:
                raise CollectionConflict() from None
            keys = sorted(set(result["storage_keys"]))
            status = "pending" if keys else "completed"
            conn.execute("""INSERT INTO project_collection_deletions(account_id,collection_id,candidate_id,status,storage_keys_json)
                VALUES (?,?,?,?,?) RETURNING 1 AS receipt""", (owner, collection_id, result["candidate_id"], status, json.dumps(keys)))
            return collection_projection(collection_id, result["candidate_id"], status)
