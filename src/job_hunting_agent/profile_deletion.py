"""Python-owned atomic fact deletion and resumable, idempotent object cleanup."""

import json
import logging
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from .account_work import AccountWorkBusy

logger = logging.getLogger(__name__)


def _timestamp(value):
    return datetime.fromisoformat(value) if isinstance(value, str) else value


def projection(candidate_id, status):
    return {"deleted": True, "candidate_id": candidate_id, "cleanup_status": status, "rag_cleanup": "database_cascade"}


def request_deletion(backend, owner: int, candidate_id: int):
    # Exclude already-admitted HTTP writes. The account row lock below also serializes Worker claims.
    with backend.store.account_work.deletion(owner):
        with backend.store.connect() as conn:
            conn.execute("SET LOCAL lock_timeout = '1000ms'")
            conn.execute("SET LOCAL statement_timeout = '1500ms'")
            account = conn.execute("SELECT status,deleted_at FROM accounts WHERE id=? FOR UPDATE", (owner,)).fetchone()
            if account is None or account["status"] != "active" or account["deleted_at"] is not None:
                raise PermissionError("账号已被停用，请重新登录。")
            receipt = conn.execute("SELECT status FROM profile_deletions WHERE account_id=? AND candidate_id=?", (owner, candidate_id)).fetchone()
            if receipt is not None:
                result = projection(candidate_id, receipt["status"])
            else:
                profile = conn.execute("SELECT id FROM candidate_profiles WHERE account_id=? AND id=? FOR UPDATE", (owner, candidate_id)).fetchone()
                if profile is None:
                    raise KeyError(candidate_id)
                running = conn.execute("SELECT 1 FROM background_tasks WHERE account_id=? AND status='running' LIMIT 1", (owner,)).fetchone()
                if running is not None:
                    raise AccountWorkBusy("请等待当前任务完成后再删除档案。")
                deleted = backend.store.delete_candidate_profile(candidate_id, account_id=owner, _connection=conn)
                keys = sorted(set(deleted["storage_keys"]))
                status = "pending" if keys else "completed"
                conn.execute("""INSERT INTO profile_deletions(account_id,candidate_id,status,storage_keys_json)
                                VALUES (?,?,?,?) RETURNING 1 AS receipt""", (owner, candidate_id, status, json.dumps(keys)))
                result = projection(candidate_id, status)
        # Invalidate even on replay: a previous response may have been lost just after commit.
        backend.invalidate_profile_cache(owner, candidate_id)
        return result


def cleanup(backend, owner: int, candidate_id: int, *, force=False, batch_size=20):
    try:
        return _cleanup(backend, owner, candidate_id, force=force, batch_size=batch_size)
    except Exception:  # noqa: BLE001 - Celery tracebacks must not include manifest SQL parameters.
        raise RuntimeError("Profile attachment cleanup temporarily unavailable") from None


def _cleanup(backend, owner: int, candidate_id: int, *, force=False, batch_size=20):
    """Lease a bounded batch; never hold a database transaction across object storage I/O."""
    if not 1 <= batch_size <= 100:
        raise ValueError("Invalid cleanup batch size")
    now = datetime.now(UTC)
    token = uuid4().hex
    with backend.store.connect() as conn:
        row = conn.execute("SELECT * FROM profile_deletions WHERE account_id=? AND candidate_id=? FOR UPDATE SKIP LOCKED",
                           (owner, candidate_id)).fetchone()
        if row is None:
            return projection(candidate_id, "pending")
        if row["status"] == "completed":
            return projection(candidate_id, "completed")
        if (row["lease_until"] is not None and _timestamp(row["lease_until"]) > now) or (not force and _timestamp(row["next_attempt_at"]) > now):
            return projection(candidate_id, "pending")
        keys = row["storage_keys_json"]
        if isinstance(keys, str):
            keys = json.loads(keys)
        if not isinstance(keys, list) or any(not isinstance(key, str) for key in keys):
            raise ValueError("Invalid profile cleanup manifest")
        keys = list(keys)
        attempts = int(row["attempts"]) + 1
        conn.execute("""UPDATE profile_deletions SET lease_token=?,lease_until=?,attempts=?,updated_at=?
                        WHERE account_id=? AND candidate_id=?""", (token, now + timedelta(minutes=5), attempts, now, owner, candidate_id))
    failed = False
    # Keys are server-collected immutable object locators. Concurrent account cleanup can safely
    # delete them again; it includes the durable remaining manifest before removing this receipt.
    for key in keys[:batch_size]:
        try:
            backend.resume_files.delete(key)
        except Exception:  # noqa: BLE001 - no object keys, credentials or provider errors in logs.
            failed = True
            logger.warning("Profile attachment cleanup deferred")
            break
        keys.remove(key)
    status = "pending" if keys else "completed"
    now = datetime.now(UTC)
    delay = min(3600, 30 * 2 ** min(attempts, 7)) if failed else 0
    with backend.store.connect() as conn:
        updated = conn.execute("""UPDATE profile_deletions SET status=?,storage_keys_json=?,next_attempt_at=?,lease_token=NULL,
                        lease_until=NULL,last_error=?,updated_at=? WHERE account_id=? AND candidate_id=? AND lease_token=?""",
                     (status, json.dumps(keys), now + timedelta(seconds=delay), "对象清理失败，将自动重试。" if failed else None,
                      now, owner, candidate_id, token))
    return projection(candidate_id, status if updated.rowcount == 1 else "pending")


def resume_pending(backend, limit=10):
    now = datetime.now(UTC)
    with backend.store.connect() as conn:
        rows = conn.execute("""SELECT account_id,candidate_id FROM profile_deletions WHERE status='pending'
                               AND next_attempt_at<=? AND (lease_until IS NULL OR lease_until<=?)
                               ORDER BY next_attempt_at,account_id,candidate_id LIMIT ?""", (now, now, limit)).fetchall()
    completed = sum(cleanup(backend, row["account_id"], row["candidate_id"])["cleanup_status"] == "completed" for row in rows)
    return {"profile_deletions_completed": completed, "profile_deletions_deferred": len(rows) - completed}
