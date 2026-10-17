"""Python-owned transactional task intents; Java is only the authenticated command entry."""

from uuid import uuid4

from .models import AdminAuditEventRecord
from .storage import now_iso
from .task_queue import TaskQueueError


class TaskCommandConflict(ValueError):
    pass


class TaskCommands:
    def __init__(self, store, queue):
        self.store = store
        self.queue = queue

    def submit(self, actor_id: int, command_id: str, operation: str, task_key: str | None, request_id: str):
        if self.queue is None:
            raise TaskQueueError("当前运行环境未启用后台任务队列。")
        if operation not in {"probe", "retry"} or (operation == "probe") != (task_key is None):
            raise ValueError("无效的任务操作。")
        now = now_iso()
        with self.store.connect() as conn:
            # Serialize identical intents across Web processes without storing sessions or raw keys.
            conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(?, 0))", (command_id,))
            actor = conn.execute("SELECT role, status FROM accounts WHERE id = ?", (actor_id,)).fetchone()
            if actor is None or actor["role"] != "admin" or actor["status"] != "active":
                raise PermissionError("需要有效的管理员账号。")
            existing = conn.execute("SELECT * FROM task_commands WHERE command_id = ?", (command_id,)).fetchone()
            if existing is not None:
                if (existing["actor_account_id"] != actor_id or existing["operation"] != operation
                        or (operation == "retry" and existing["task_key"] != task_key)):
                    raise TaskCommandConflict("幂等键已用于不同的任务操作。")
                if existing["status"] == "superseded":
                    raise TaskCommandConflict("原投递请求已失效，请刷新后重新操作。")
                task_key = existing["task_key"]
            else:
                if operation == "retry":
                    row = conn.execute("SELECT * FROM background_tasks WHERE task_key = ? FOR UPDATE", (task_key,)).fetchone()
                    if row is None:
                        raise KeyError("后台任务不存在。")
                    if row["status"] != "failed":
                        raise TaskCommandConflict("只有失败状态的后台任务可以人工重试。")
                    target = conn.execute("SELECT status FROM accounts WHERE id = ?", (row["account_id"],)).fetchone()
                    if target is None or (target["status"] != "active" and row["task_type"] != "account_deletion"):
                        raise TaskCommandConflict("已停用账号的任务不能重试。")
                    conn.execute("""UPDATE background_tasks SET status='queued',progress=0,attempt=0,result_json='{}',
                                    error_summary=NULL,started_at=NULL,finished_at=NULL,updated_at=? WHERE task_key=?""", (now, task_key))
                    target_id = row["account_id"]
                    action, summary = "background_task.retry_requested", "管理员请求重新投递失败后台任务。"
                else:
                    task_key = uuid4().hex
                    target_id = actor_id
                    conn.execute("""INSERT INTO background_tasks(task_key,account_id,task_type,status,progress,attempt,
                                    max_attempts,payload_json,result_json,created_at,updated_at)
                                    VALUES (?,?,'system_probe','queued',0,0,1,?, '{}',?,?)""",
                                 (task_key, actor_id, '{"purpose":"admin_runtime_probe"}', now, now))
                    action, summary = "system.probe_enqueued", "管理员登记 Worker 探针投递请求。"
                conn.execute("""INSERT INTO task_commands(command_id,actor_account_id,operation,task_key,request_id,status,created_at,updated_at)
                                VALUES (?,?,?,?,?,'pending',?,?)""", (command_id, actor_id, operation, task_key, request_id, now, now))
                self.store._insert_admin_audit_event(conn, AdminAuditEventRecord(
                    id=0, actor_account_id=actor_id, target_account_id=target_id, action=action,
                    target_type="background_task", target_id=task_key, outcome="succeeded", summary=summary,
                    details={"command_id": command_id, "dispatch_status": "pending"}, request_id=request_id))
        if not self.dispatch(command_id):
            with self.store.connect() as conn:
                command = conn.execute("SELECT status FROM task_commands WHERE command_id=?", (command_id,)).fetchone()
            if command is not None and command["status"] == "superseded":
                raise TaskCommandConflict("原投递请求已失效，请刷新后重新操作。")
            raise TaskQueueError("后台任务投递尚未确认，请稍后使用相同请求重试。")
        return self.store.get_background_task(task_key)

    def dispatch(self, command_id: str) -> bool:
        if self.queue is None:
            return False
        with self.store.connect() as conn:
            row = conn.execute("SELECT * FROM task_commands WHERE command_id=? FOR UPDATE SKIP LOCKED", (command_id,)).fetchone()
            if row is None:
                return False
            if row["status"] != "pending":
                return row["status"] == "dispatched"
            newer = conn.execute("SELECT id FROM task_commands WHERE task_key=? AND id>? LIMIT 1", (row["task_key"], row["id"])).fetchone()
            if newer is not None:
                conn.execute("UPDATE task_commands SET status='superseded',updated_at=? WHERE command_id=?", (now_iso(), command_id))
                return False
            task = conn.execute("""SELECT t.status,t.attempt,t.task_type,a.status AS account_status FROM background_tasks t
                                   JOIN accounts a ON a.id=t.account_id WHERE t.task_key=?""", (row["task_key"],)).fetchone()
            actor = conn.execute("SELECT status,role FROM accounts WHERE id=?", (row["actor_account_id"],)).fetchone()
            allowed = actor is not None and actor["status"] == "active" and actor["role"] == "admin"
            target_allowed = task is not None and (task["account_status"] == "active" or task["task_type"] == "account_deletion")
            if not allowed or not target_allowed:
                conn.execute("UPDATE task_commands SET status='superseded',updated_at=? WHERE command_id=?", (now_iso(), command_id))
                conn.execute("""UPDATE background_tasks SET status='failed',error_summary=?,finished_at=?,updated_at=?
                                WHERE task_key=? AND status='queued'""",
                             ("账号状态已变化，投递请求已取消。", now_iso(), now_iso(), row["task_key"]))
                return False
            if task["status"] == "cancelled" or (task["status"] == "failed" and task["attempt"] == 0):
                conn.execute("UPDATE task_commands SET status='superseded',updated_at=? WHERE command_id=?", (now_iso(), command_id))
                return False
            if task["status"] == "queued":
                try:
                    publish = getattr(self.queue, "enqueue_command", self.queue.enqueue)
                    publish(row["task_key"])
                except TaskQueueError:
                    conn.execute("UPDATE task_commands SET updated_at=? WHERE command_id=?", (now_iso(), command_id))
                    return False
            # If a previous publish timed out after broker acknowledgement, Worker may already
            # have claimed/finished it. Do not reset or republish that task on HTTP replay.
            conn.execute("UPDATE task_commands SET status='dispatched',updated_at=? WHERE command_id=?", (now_iso(), command_id))
        return True

    def dispatch_pending(self, limit: int = 20) -> dict[str, int]:
        with self.store.connect() as conn:
            rows = conn.execute("SELECT command_id FROM task_commands WHERE status='pending' ORDER BY updated_at,id LIMIT ?", (limit,)).fetchall()
        delivered = sum(self.dispatch(row["command_id"]) for row in rows)
        with self.store.connect() as conn:
            pending = conn.execute("SELECT COUNT(*) AS total FROM task_commands WHERE status='pending'").fetchone()["total"]
        return {"dispatched_commands": delivered, "pending_commands": int(pending)}
