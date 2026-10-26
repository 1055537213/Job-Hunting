"""Strict, bounded public projections shared by both Python project adapters."""

import re
from .chat_session_query import positive_id

MAX_PROJECT_BYTES = 4 * 1024 * 1024
PROJECT_UNAVAILABLE = "项目操作结果暂未确认，请刷新项目列表核实后重试。"
RECORD_FIELDS = {"id", "candidate_id", "status", "card", "confirmed_summary", "created_at", "confirmed_at"}
CARD_LISTS = {"read_files", "detected_tech_stack", "detected_core_features", "responsibility_draft", "highlight_draft",
              "resume_expression_draft", "questions_for_candidate", "deferred_files"}
CARD_FIELDS = CARD_LISTS | {"card_type", "project_name", "skipped_summary", "source_type", "source_url", "source_ref", "discovered_file_kinds"}
TASK_FIELDS = {"task_key", "task_type", "status", "progress", "attempt", "max_attempts", "result", "error_summary",
               "created_at", "started_at", "finished_at", "updated_at"}


def validate_projection(value, operation, *, candidate_id=None, record_id=None, collection_id=None):
    if operation in {"local-manifest", "local-get", "local-cancel"}:
        from .local_project_entry import validate_collection
        return validate_collection(value, operation, candidate_id=candidate_id, collection_id=collection_id)
    expected = {"list": {"project_cards"}, "confirm": {"project_card", "task", "root_request_id"},
                "delete": {"deleted", "project_card_id", "candidate_id", "cleanup_status", "rag_cleanup"},
                "github": {"task", "project_card", "processing_async", "root_request_id"},
                "local-complete": {"project_card", "processing_async"}}[operation]
    if not isinstance(value, dict) or set(value) != expected:
        raise ValueError("Invalid project fields")
    if operation == "github":
        if (value["project_card"] is not None or value["processing_async"] is not True
                or not isinstance(value["root_request_id"], str) or re.fullmatch(r"[a-f0-9]{32}", value["root_request_id"]) is None):
            raise ValueError("Invalid project submission")
        validate_task(value["task"], "github_project_analysis")
        return value
    if operation == "delete":
        if (value["deleted"] is not True or not positive_id(value["project_card_id"]) or value["project_card_id"] != record_id or not positive_id(value["candidate_id"])
                or value["cleanup_status"] not in {"pending", "completed"} or value["rag_cleanup"] != "database_cascade"):
            raise ValueError("Invalid project deletion")
        return value
    records = value["project_cards"] if operation == "list" else [value["project_card"]]
    if not isinstance(records, list):
        raise ValueError("Invalid project list")
    seen = set()
    for row in records:
        if (not isinstance(row, dict) or set(row) != RECORD_FIELDS or not positive_id(row["id"])
                or row["id"] in seen or not positive_id(row["candidate_id"])
                or (candidate_id is not None and row["candidate_id"] != candidate_id)
                or (record_id is not None and row["id"] != record_id) or row["status"] not in {"待确认", "已确认"}
                or not isinstance(row["created_at"], str)
                or any(row[key] is not None and not isinstance(row[key], str) for key in ("confirmed_summary", "confirmed_at"))):
            raise ValueError("Invalid project scope")
        seen.add(row["id"])
        card = row["card"]
        if (not isinstance(card, dict) or set(card) != CARD_FIELDS
                or not all(isinstance(card[k], str) for k in ("card_type", "project_name", "source_type"))
                or any(card[k] is not None and not isinstance(card[k], str) for k in ("source_url", "source_ref"))
                or any(not isinstance(card[k], list) or any(not isinstance(x, str) for x in card[k]) for k in CARD_LISTS)
                or any(not isinstance(card[k], dict) or any(type(n) is not int or n < 0 for n in card[k].values())
                       for k in ("skipped_summary", "discovered_file_kinds"))):
            raise ValueError("Invalid project card")
    if operation == "local-complete":
        if value["processing_async"] is not False:
            raise ValueError("Invalid local completion")
    if operation == "confirm":
        task = value["task"]
        if (records[0]["status"] != "已确认" or not records[0]["confirmed_summary"] or not isinstance(records[0]["confirmed_at"], str)
                or not isinstance(value["root_request_id"], str)
                or re.fullmatch(r"[a-f0-9]{32}", value["root_request_id"]) is None
                or (task is not None and (not isinstance(task, dict) or set(task) != TASK_FIELDS
                    or not isinstance(task["task_key"], str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", task["task_key"])
                    or task["task_type"] != "rag_index" or task["status"] not in {"queued", "running", "succeeded", "failed", "cancelled"}
                    or task["result"] is not None or type(task["progress"]) is not int or not 0 <= task["progress"] <= 100
                    or type(task["attempt"]) is not int or task["attempt"] < 0
                    or not positive_id(task["max_attempts"])
                    or not all(isinstance(task[k], str) for k in ("created_at", "updated_at"))
                    or any(task[k] is not None and not isinstance(task[k], str) for k in ("started_at", "finished_at", "error_summary"))))):
            raise ValueError("Invalid confirmation")
    return value


def validate_task(task, task_type):
    if (not isinstance(task, dict) or set(task) != TASK_FIELDS
            or not isinstance(task["task_key"], str) or re.fullmatch(r"[A-Za-z0-9_-]{1,64}", task["task_key"]) is None
            or task["task_type"] != task_type or task["status"] not in {"queued", "running", "succeeded", "failed", "cancelled"}
            or task["result"] is not None or type(task["progress"]) is not int or not 0 <= task["progress"] <= 100
            or type(task["attempt"]) is not int or task["attempt"] < 0 or not positive_id(task["max_attempts"])
            or not all(isinstance(task[k], str) for k in ("created_at", "updated_at"))
            or any(task[k] is not None and not isinstance(task[k], str) for k in ("started_at", "finished_at", "error_summary"))):
        raise ValueError("Invalid project task")
