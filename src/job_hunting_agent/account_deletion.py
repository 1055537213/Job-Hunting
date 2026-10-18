"""Recover Java-admitted account deletion without depending on a live browser session."""

import logging

from .storage import now_iso

logger = logging.getLogger(__name__)


def register_deletion_task(store, request):
    owner, key = request["account_id"], request["task_key"]
    now = now_iso()
    with store.connect() as conn:
        account = conn.execute("SELECT status,deleted_at FROM accounts WHERE id=? FOR SHARE", (owner,)).fetchone()
        if account is None or account["status"] != "disabled":
            raise ValueError("注销账号状态无效。")
        conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(?,0))", (f"account-deletion:{owner}",))
        existing = conn.execute("SELECT * FROM background_tasks WHERE task_key=? FOR UPDATE", (key,)).fetchone()
        if existing is not None:
            if existing["account_id"] != owner or existing["task_type"] != "account_deletion":
                raise ValueError("注销任务归属不匹配。")
        else:
            conn.execute("""INSERT INTO background_tasks(task_key,account_id,task_type,status,progress,attempt,
                            max_attempts,payload_json,result_json,idempotency_key,created_at,updated_at)
                            VALUES (?,?,'account_deletion','queued',0,0,8,'{"storage_keys":[]}','{}',?,?,?)""",
                         (key, owner, f"account-deletion:{owner}", now, now))
        conn.execute("""UPDATE background_tasks SET status='cancelled',finished_at=?,updated_at=?
                        WHERE account_id=? AND task_key<>? AND status='queued'""", (now, now, owner, key))
    return store.get_background_task(key)


def dispatch_account_deletions(backend):
    """At-least-once dispatch; failed tasks keep their evidence for administrator retry."""
    counts = {"deletion_dispatched": 0, "deletion_deferred": 0}
    if backend.platform_auth_client is None or backend.task_queue is None:
        return counts
    requests = backend.platform_auth_client.deletion_requests("due", limit=20)["requests"]
    for request in requests:
        try:
            task = register_deletion_task(backend.store, request)
            if task.status == "queued":
                publish = getattr(backend.task_queue, "enqueue_command", backend.task_queue.enqueue)
                publish(task.task_key)
                counts["deletion_dispatched"] += 1
            elif task.status == "succeeded":
                backend.platform_auth_client.account("delete-complete", account_id=task.account_id, task_key=task.task_key)
        except Exception as error:  # noqa: BLE001 - one failing account must not starve later requests.
            counts["deletion_deferred"] += 1
            logger.warning("Account deletion deferred account_id=%s error_type=%s", request["account_id"], type(error).__name__)
    return counts
