"""Atomic project deletion with a durable, resumable object manifest."""

import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from .account_work import AccountWorkBusy


def _timestamp(value):
    return datetime.fromisoformat(value) if isinstance(value, str) else value


def projection(record_id, candidate_id, status):
    return {"deleted": True, "project_card_id": record_id, "candidate_id": candidate_id,
            "cleanup_status": status, "rag_cleanup": "database_cascade"}


def request_deletion(backend, owner, record_id):
    with backend.store.account_work.deletion(owner):
        with backend.store.connect() as conn:
            conn.execute("SET LOCAL lock_timeout='1000ms'")
            conn.execute("SET LOCAL statement_timeout='2000ms'")
            account = conn.execute("SELECT status,deleted_at FROM accounts WHERE id=? FOR UPDATE", (owner,)).fetchone()
            if account is None or account["status"] != "active" or account["deleted_at"] is not None:
                raise PermissionError()
            receipt = conn.execute("SELECT * FROM project_deletions WHERE account_id=? AND project_card_id=?", (owner, record_id)).fetchone()
            if receipt:
                return projection(record_id, receipt["candidate_id"], receipt["status"])
            if conn.execute("SELECT 1 FROM background_tasks WHERE account_id=? AND status='running' LIMIT 1", (owner,)).fetchone():
                raise AccountWorkBusy()
            record = backend.store.get_project_card(record_id, account_id=owner)
            # Claims take a shared account row lock; none can cross this cancellation/delete transaction.
            conn.execute("""UPDATE background_tasks SET status='cancelled',finished_at=now(),updated_at=now()
                WHERE account_id=? AND candidate_id=? AND status='queued' AND (
                    (task_type='rag_index' AND idempotency_key=?) OR
                    (task_type='github_project_analysis' AND lower(CAST(payload_json AS jsonb)->>'repository_url')=lower(?)) OR
                    (task_type='project_archive_analysis' AND CAST(payload_json AS jsonb)->>'project_archive_id' IN
                        (SELECT CAST(id AS text) FROM project_archive_imports WHERE account_id=? AND project_card_id=?)))""",
                (owner, record.candidate_id, f"project-rag:{record_id}", record.card.source_url, owner, record_id))
            deleted = backend.store.delete_project_card(record_id, account_id=owner, _connection=conn)
            keys = sorted(set(deleted["storage_keys"]))
            status = "pending" if keys else "completed"
            conn.execute("""INSERT INTO project_deletions(account_id,project_card_id,candidate_id,status,storage_keys_json)
                            VALUES (?,?,?,?,?) RETURNING 1 AS receipt""",
                         (owner, record_id, deleted["candidate_id"], status, json.dumps(keys)))
            return projection(record_id, deleted["candidate_id"], status)


def cleanup(backend, owner, record_id, *, force=False, batch_size=20):
    try:
        return _cleanup(backend, owner, record_id, force=force, batch_size=batch_size)
    except Exception:  # noqa: BLE001 - object locators/SQL parameters are private.
        raise RuntimeError("Project attachment cleanup temporarily unavailable") from None


def _cleanup(backend, owner, record_id, *, force, batch_size):
    if not 1 <= batch_size <= 100:
        raise ValueError("Invalid cleanup batch")
    now, token = datetime.now(UTC), uuid4().hex
    with backend.store.connect() as conn:
        row = conn.execute("SELECT * FROM project_deletions WHERE account_id=? AND project_card_id=? FOR UPDATE SKIP LOCKED",
                           (owner, record_id)).fetchone()
        if row is None:
            return None
        result = projection(record_id, row["candidate_id"], row["status"])
        if row["status"] == "completed":
            return result
        if ((row["lease_until"] and _timestamp(row["lease_until"]) > now) or (not force and _timestamp(row["next_attempt_at"]) > now)):
            return result
        keys = row["storage_keys_json"]
        keys = json.loads(keys) if isinstance(keys, str) else list(keys)
        attempts = row["attempts"] + 1
        conn.execute("""UPDATE project_deletions SET lease_token=?,lease_until=?,attempts=?,updated_at=?
                        WHERE account_id=? AND project_card_id=?""", (token, now + timedelta(minutes=5), attempts, now, owner, record_id))
    failed = False
    for key in keys[:batch_size]:
        try:
            backend.resume_files.delete(key)
        except Exception:  # noqa: BLE001 - retain remaining keys; never echo storage credentials.
            failed = True
            break
        keys.remove(key)
    status = "pending" if keys else "completed"
    with backend.store.connect() as conn:
        updated = conn.execute("""UPDATE project_deletions SET status=?,storage_keys_json=?,lease_token=NULL,lease_until=NULL,
            next_attempt_at=?,last_error=?,updated_at=? WHERE account_id=? AND project_card_id=? AND lease_token=?""",
            (status, json.dumps(keys), now + timedelta(seconds=min(3600, 30 * 2 ** min(attempts, 7)) if failed else 0),
             "对象清理失败，将自动重试。" if failed else None, now, owner, record_id, token))
    return projection(record_id, row["candidate_id"], status if updated.rowcount == 1 else "pending")


def resume_pending(backend, limit=10):
    now = datetime.now(UTC)
    with backend.store.connect() as conn:
        rows = conn.execute("""SELECT account_id,project_card_id FROM project_deletions WHERE status='pending'
            AND next_attempt_at<=? AND (lease_until IS NULL OR lease_until<=?)
            ORDER BY next_attempt_at,account_id,project_card_id LIMIT ?""", (now, now, limit)).fetchall()
    completed = 0
    for row in rows:
        result = cleanup(backend, row["account_id"], row["project_card_id"])
        completed += result is not None and result["cleanup_status"] == "completed"
    return {"project_deletions_completed": completed, "project_deletions_deferred": len(rows) - completed}
