"""Transactional GitHub submission receipts and recoverable, at-least-once dispatch."""

import hashlib
import json
import re
from uuid import uuid4

from .account_work import AccountWorkDenied
from .deduplication import github_project_content_fingerprint
from .github_project import normalize_public_github_repository_url
from .storage import now_iso
from .task_queue import TaskQueueError


class ProjectSubmissionConflict(ValueError):
    pass


class ProjectSubmissions:
    def __init__(self, store, queue):
        self.store, self.queue = store, queue

    def submit(self, owner, candidate, repository_url, idempotency_key=None):
        if self.queue is None:
            raise TaskQueueError("Project queue unavailable")
        reference = normalize_public_github_repository_url(repository_url)
        # GitHub owner/repository names are case insensitive; Worker validates again.
        canonical = reference.canonical_url.lower()
        fingerprint = github_project_content_fingerprint(canonical)
        payload_hash = hashlib.sha256(f"{candidate}:{canonical}".encode()).hexdigest()
        key_hash = hashlib.sha256(idempotency_key.encode()).hexdigest() if idempotency_key else None
        task_idempotency = f"github-project:{candidate}:{fingerprint[:40]}"
        now = now_iso()
        with self.store.connect() as conn:
            conn.execute("SET LOCAL lock_timeout='1s'")
            conn.execute("SET LOCAL statement_timeout='3s'")
            account = conn.execute("SELECT status,deleted_at FROM accounts WHERE id=? FOR UPDATE", (owner,)).fetchone()
            if account is None or account["status"] != "active" or account["deleted_at"] is not None:
                raise AccountWorkDenied()
            if conn.execute("SELECT id FROM candidate_profiles WHERE id=? AND account_id=?", (candidate, owner)).fetchone() is None:
                raise KeyError("Profile not found")
            existing = conn.execute("SELECT * FROM project_submissions WHERE account_id=? AND key_hash=?", (owner, key_hash)).fetchone() if key_hash else None
            if existing:
                if existing["payload_hash"] != payload_hash:
                    raise ProjectSubmissionConflict()
                command_id, task_key = existing["command_id"], existing["task_key"]
            else:
                task = conn.execute("SELECT * FROM background_tasks WHERE account_id=? AND idempotency_key=? FOR UPDATE", (owner, task_idempotency)).fetchone()
                if task and (task["candidate_id"] != candidate or task["task_type"] != "github_project_analysis"):
                    raise ProjectSubmissionConflict()
                if task and task["status"] not in {"queued", "running"}:
                    conn.execute("UPDATE background_tasks SET idempotency_key=NULL WHERE task_key=?", (task["task_key"],))
                    task = None
                command_id = uuid4().hex
                if task is None:
                    task_key = uuid4().hex
                    payload = json.dumps({"repository_url": canonical, "root_request_id": uuid4().hex})
                    conn.execute("""INSERT INTO background_tasks(task_key,account_id,candidate_id,session_id,task_type,
                        status,progress,attempt,max_attempts,idempotency_key,payload_json,result_json,created_at,updated_at)
                        VALUES (?,?,?,?,'github_project_analysis','queued',0,0,3,?,?,'{}',?,?)""",
                        (task_key, owner, candidate, f"github-project-candidate-{candidate}", task_idempotency, payload, now, now))
                else:
                    task_key = task["task_key"]
                    payload = json.loads(task["payload_json"])
                    if not isinstance(payload.get("root_request_id"), str) or not re.fullmatch(r"[a-f0-9]{32}", payload["root_request_id"]):
                        payload["root_request_id"] = task_key
                        conn.execute("UPDATE background_tasks SET payload_json=? WHERE task_key=?", (json.dumps(payload), task_key))
                conn.execute("""INSERT INTO project_submissions(command_id,account_id,candidate_id,key_hash,payload_hash,task_key,
                    status,attempts,next_dispatch_at,created_at,updated_at) VALUES (?,?,?,?,?,?,'pending',0,?,?,?)""",
                    (command_id, owner, candidate, key_hash, payload_hash, task_key, now, now, now))
        if not self.dispatch(command_id):
            with self.store.connect() as conn:
                receipt = conn.execute("SELECT status FROM project_submissions WHERE command_id=?", (command_id,)).fetchone()
            if receipt is None or receipt["status"] == "superseded":
                raise ProjectSubmissionConflict()
            raise TaskQueueError("Project dispatch unconfirmed")
        return self.store.get_background_task(task_key, account_id=owner)

    def dispatch(self, command_id):
        if self.queue is None:
            return False
        with self.store.connect() as conn:
            conn.execute("SET LOCAL lock_timeout='1s'")
            conn.execute("SET LOCAL statement_timeout='5s'")
            # Match Worker/deletion lock order. Never publish a task cancelled by deletion.
            account = conn.execute("""SELECT a.status,a.deleted_at FROM accounts a JOIN project_submissions s
                ON s.account_id=a.id WHERE s.command_id=? FOR SHARE OF a""", (command_id,)).fetchone()
            row = conn.execute("SELECT * FROM project_submissions WHERE command_id=? FOR UPDATE SKIP LOCKED", (command_id,)).fetchone()
            if row is None:
                return False
            task = conn.execute("SELECT status FROM background_tasks WHERE task_key=? FOR UPDATE", (row["task_key"],)).fetchone()
            if account is None or account["status"] != "active" or account["deleted_at"] is not None or task is None or task["status"] == "cancelled":
                conn.execute("UPDATE project_submissions SET status='superseded',updated_at=? WHERE command_id=?", (now_iso(), command_id))
                conn.execute("UPDATE background_tasks SET status='cancelled',finished_at=?,updated_at=? WHERE task_key=? AND status='queued'", (now_iso(), now_iso(), row["task_key"]))
                return False
            if row["status"] != "pending":
                return row["status"] == "dispatched"
            delivered = conn.execute("SELECT id FROM project_submissions WHERE task_key=? AND status='dispatched' LIMIT 1", (row["task_key"],)).fetchone()
            if task["status"] == "queued" and delivered is None:
                try:
                    getattr(self.queue, "enqueue_command", self.queue.enqueue)(row["task_key"])
                except TaskQueueError:
                    conn.execute("""UPDATE project_submissions SET attempts=attempts+1,
                        next_dispatch_at=now()+make_interval(secs => LEAST(3600, 10 * power(2, LEAST(attempts, 8)))::int),
                        updated_at=? WHERE command_id=?""", (now_iso(), command_id))
                    return False
            conn.execute("UPDATE project_submissions SET status='dispatched',updated_at=? WHERE command_id=?", (now_iso(), command_id))
        return True

    def dispatch_pending(self, limit=20):
        with self.store.connect() as conn:
            rows = conn.execute("SELECT command_id FROM project_submissions WHERE status='pending' AND next_dispatch_at<=now() ORDER BY next_dispatch_at,id LIMIT ?", (limit,)).fetchall()
        delivered = 0
        for row in rows:
            try:
                delivered += self.dispatch(row["command_id"])
            except Exception:  # noqa: BLE001 - one unavailable receipt must not starve maintenance.
                continue
        return {"dispatched_projects": delivered}
