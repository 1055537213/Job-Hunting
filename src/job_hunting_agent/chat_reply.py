"""Public non-stream chat contract; never transport raw tools or routing metadata."""

from .profile_query import validate_projection

REPLY_FIELDS = {"mode", "reply", "display_reply", "task_trace", "background_tasks", "profile"}
TRACE_FIELDS = {"version", "root_request_id", "title", "status", "duration_ms", "steps", "approval"}
STEP_FIELDS = {"id", "label", "status", "summary"}
APPROVAL_FIELDS = {"kind", "record_id", "title", "message", "items", "confirm_label", "cancel_label", "status"}
CHAT_UNCONFIRMED_MESSAGE = "对话结果暂未确认，请先查看历史记录，勿立即重复发送。"


def validate_reply(value, candidate_id):
    if not isinstance(value, dict) or set(value) != REPLY_FIELDS:
        raise ValueError("Invalid chat reply fields")
    if not all(isinstance(value[key], str) for key in ("mode", "reply", "display_reply")):
        raise ValueError("Invalid chat reply text")
    validate_projection({"profile": value["profile"]}, candidate_id)
    trace = value["task_trace"]
    if trace is not None:
        if (not isinstance(trace, dict) or set(trace) != TRACE_FIELDS or type(trace["version"]) is not int
                or not all(isinstance(trace[k], str) for k in ("root_request_id", "title", "status"))
                or not (trace["duration_ms"] is None or type(trace["duration_ms"]) is int)
                or not isinstance(trace["steps"], list)):
            raise ValueError("Invalid public trace")
        for step in trace["steps"]:
            if not isinstance(step, dict) or set(step) != STEP_FIELDS or not all(isinstance(v, str) for v in step.values()):
                raise ValueError("Invalid public step")
        approval = trace["approval"]
        if approval is not None:
            if (not isinstance(approval, dict) or set(approval) != APPROVAL_FIELDS
                    or not all(isinstance(approval[k], str) for k in APPROVAL_FIELDS - {"record_id", "items"})
                    or not (approval["record_id"] is None or type(approval["record_id"]) in (str, int))
                    or not isinstance(approval["items"], list)):
                raise ValueError("Invalid public approval")
            for item in approval["items"]:
                if not isinstance(item, dict) or set(item) != {"label", "value"} or not all(isinstance(v, str) for v in item.values()):
                    raise ValueError("Invalid approval item")
    if not isinstance(value["background_tasks"], list):
        raise ValueError("Invalid public tasks")
    for task in value["background_tasks"]:
        if (not isinstance(task, dict) or set(task) != {"task_key", "task_type", "status", "progress"}
                or not all(isinstance(task[k], str) for k in ("task_key", "task_type", "status"))
                or type(task["progress"]) is not int):
            raise ValueError("Invalid public task")
    return value
