"""Bounded projections for chat-session management across the platform boundary."""

import re

MAX_CHAT_BYTES = 4 * 1024 * 1024
SESSION_FIELDS = {"id", "session_id", "account_id", "candidate_id", "job_id", "title", "status",
                  "created_at", "updated_at", "archived_at"}
MESSAGE_FIELDS = {"id", "candidate_id", "session_id", "role", "content", "metadata", "created_at"}


def valid_session_key(value):
    return isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,128}", value) is not None


def positive_id(value):
    return type(value) is int and 0 < value <= 2**63 - 1


def validate_chat_projection(value, operation, *, account_id=None, candidate_id=None, session_id=None, limit=100,
                             include_archived=False):
    if not isinstance(value, dict):
        raise ValueError("Invalid chat projection")
    expected = {"list": {"sessions"}, "create": {"session"}, "archive": {"session"},
                "delete": {"deleted", "candidate_id", "session_id"},
                "history": {"candidate_id", "session_id", "messages"}}[operation]
    if set(value) != expected:
        raise ValueError("Unexpected chat fields")
    if operation in {"list", "create", "archive"}:
        rows = value["sessions"] if operation == "list" else [value["session"]]
        if not isinstance(rows, list):
            raise ValueError("Invalid sessions")
        ids, keys = set(), set()
        for row in rows:
            if (not isinstance(row, dict) or set(row) != SESSION_FIELDS or not positive_id(row["id"])
                    or not positive_id(row["account_id"]) or not positive_id(row["candidate_id"])
                    or not valid_session_key(row["session_id"]) or row["status"] not in {"active", "archived"}
                    or not all(isinstance(row[k], str) for k in ("title", "created_at", "updated_at"))
                    or (row["job_id"] is not None and not positive_id(row["job_id"]))
                    or (row["archived_at"] is not None and not isinstance(row["archived_at"], str))
                    or (account_id is not None and row["account_id"] != account_id)
                    or (candidate_id is not None and row["candidate_id"] != candidate_id)
                    or (session_id is not None and row["session_id"] != session_id)
                    or row["id"] in ids or row["session_id"] in keys):
                raise ValueError("Invalid session scope")
            if (operation == "archive" and row["status"] != "archived"
                    or operation == "create" and row["status"] != "active"
                    or operation == "list" and not include_archived and row["status"] != "active"):
                raise ValueError("Invalid session status")
            ids.add(row["id"])
            keys.add(row["session_id"])
    else:
        if (not positive_id(value["candidate_id"]) or not valid_session_key(value["session_id"])
                or (candidate_id is not None and value["candidate_id"] != candidate_id)
                or (session_id is not None and value["session_id"] != session_id)):
            raise ValueError("Invalid chat scope")
        if operation == "delete":
            if value["deleted"] is not True:
                raise ValueError("Invalid deletion receipt")
        else:
            messages = value["messages"]
            if not isinstance(messages, list) or len(messages) > limit:
                raise ValueError("Invalid history")
            ids = set()
            for row in messages:
                if (not isinstance(row, dict) or set(row) != MESSAGE_FIELDS or not positive_id(row["id"])
                        or row["id"] in ids or row["candidate_id"] != value["candidate_id"]
                        or row["session_id"] != value["session_id"] or row["role"] not in {"user", "assistant"}
                        or not isinstance(row["content"], str) or not isinstance(row["created_at"], str)
                        or not isinstance(row["metadata"], dict) or not set(row["metadata"]) <= {"task_trace"}):
                    raise ValueError("Invalid message projection")
                ids.add(row["id"])
    return value
