"""工具调用审计轨迹的共享辅助函数。"""

from __future__ import annotations

from collections.abc import Mapping

from .job_hunting_tools import job_hunting_tool_catalog
from .models import ToolCallTraceRecord
from .task_registry import background_task_catalog

AUDIT_SKIP_STEP_NAMES = {"compose_reply"}

_AGENT_TOOL_METADATA = {item.name: item for item in job_hunting_tool_catalog()}
_BACKGROUND_TASK_METADATA = {
    item.task_type: item for item in background_task_catalog()
}

TOOL_STEP_LABELS = {
    **{name: item.audit_label for name, item in _AGENT_TOOL_METADATA.items()},
    **{
        task_type: item.audit_label
        for task_type, item in _BACKGROUND_TASK_METADATA.items()
    },
    "compose_reply": "整理回复",
}


def tool_step_label(tool_name: str) -> str:
    """把内部工具名转换成稳定的用户可读步骤标题。"""

    return TOOL_STEP_LABELS.get(tool_name, tool_name.replace("_", " ").strip() or "执行任务")


def background_task_tool_name(task_type: str) -> str:
    """把后台任务类型映射为审计里的工具步骤名。"""

    return task_type if task_type in _BACKGROUND_TASK_METADATA else task_type or "background_task"


def audited_steps(trace: Mapping[str, object] | None) -> list[dict[str, object]]:
    """返回应该写入管理端审计的步骤。"""

    if not isinstance(trace, Mapping):
        return []
    steps = trace.get("steps")
    if not isinstance(steps, list):
        return []
    result: list[dict[str, object]] = []
    for step in steps:
        if not isinstance(step, dict):
            continue
        name = str(step.get("name") or "task_step")
        if name in AUDIT_SKIP_STEP_NAMES:
            continue
        result.append(step)
    return result


def has_audited_steps(trace: Mapping[str, object] | None) -> bool:
    """判断这条轨迹是否包含需要持久化的真实步骤。"""

    return bool(audited_steps(trace))


def tool_trace_title(tool_names: list[str]) -> str:
    """根据真实工具名称生成可读标题。"""

    candidates: list[tuple[int, str]] = []
    for tool_name in tool_names:
        metadata = _AGENT_TOOL_METADATA.get(tool_name)
        if metadata is not None and metadata.trace_priority > 0:
            candidates.append((metadata.trace_priority, metadata.audit_label))
            continue
        task_metadata = _BACKGROUND_TASK_METADATA.get(tool_name)
        if task_metadata is not None:
            candidates.append((task_metadata.trace_priority, task_metadata.audit_label))
    if candidates:
        return max(candidates, key=lambda item: item[0])[1]
    return "本次任务"


def tool_trace_step_count(trace: Mapping[str, object] | None) -> int:
    """统计一条轨迹中的真实步骤数量。"""

    return len(audited_steps(trace))


def tool_trace_attempt_count(trace: Mapping[str, object] | None) -> int:
    """统计一条轨迹中的尝试次数。"""

    attempts = 0
    for step in audited_steps(trace):
        step_attempts = step.get("attempts")
        if isinstance(step_attempts, list) and step_attempts:
            attempts += len([item for item in step_attempts if isinstance(item, dict)])
        else:
            attempts += 1
    return attempts


def tool_trace_last_step_name(trace: Mapping[str, object] | None) -> str | None:
    """返回最后一个真实步骤的工具名。"""

    for step in reversed(audited_steps(trace)):
        name = step.get("name")
        if name:
            return str(name)
    return None


def tool_trace_last_error_summary(trace: Mapping[str, object] | None) -> str | None:
    """返回最近一次失败的摘要。"""

    for step in reversed(audited_steps(trace)):
        attempts = step.get("attempts")
        if isinstance(attempts, list):
            for attempt in reversed(attempts):
                if not isinstance(attempt, dict):
                    continue
                if str(attempt.get("status") or "") not in {"failed", "cancelled"}:
                    continue
                summary = attempt.get("summary") or attempt.get("error")
                if summary:
                    return str(summary)
        if str(step.get("status") or "") not in {"failed", "cancelled"}:
            continue
        summary = step.get("summary")
        if summary:
            return str(summary)
        error = step.get("error")
        if error:
            return str(error)
    return None


def tool_trace_status(trace: Mapping[str, object] | None) -> str:
    """返回轨迹的最终状态。"""

    if not isinstance(trace, Mapping):
        return "running"
    status = str(trace.get("status") or "running")
    if status:
        return status
    return "running"


def build_tool_trace_record(
    trace: Mapping[str, object],
    *,
    account_id: int,
    candidate_id: int | None,
    session_id: str | None,
    source: str = "chat",
) -> ToolCallTraceRecord:
    """把内存中的任务轨迹整理成数据库记录。"""

    normalized_trace = dict(trace)
    root_request_id = str(normalized_trace.get("root_request_id") or "")
    if not root_request_id:
        raise ValueError("工具审计轨迹缺少 root_request_id。")
    tool_names = [str(step.get("name") or "") for step in audited_steps(normalized_trace)]
    title = str(normalized_trace.get("title") or tool_trace_title([name for name in tool_names if name]))
    created_at = str(normalized_trace.get("created_at") or normalized_trace.get("started_at") or "")
    started_at = normalized_trace.get("started_at")
    finished_at = normalized_trace.get("finished_at")
    updated_at = str(normalized_trace.get("updated_at") or finished_at or started_at or created_at or "")
    return ToolCallTraceRecord(
        id=0,
        account_id=account_id,
        candidate_id=candidate_id,
        session_id=session_id,
        root_request_id=root_request_id,
        title=title,
        status=tool_trace_status(normalized_trace),
        source=str(normalized_trace.get("source") or source or "chat"),
        step_count=tool_trace_step_count(normalized_trace),
        attempt_count=tool_trace_attempt_count(normalized_trace),
        last_step_name=tool_trace_last_step_name(normalized_trace),
        last_error_summary=tool_trace_last_error_summary(normalized_trace),
        trace=normalized_trace,
        created_at=created_at,
        started_at=str(started_at) if started_at is not None else None,
        finished_at=str(finished_at) if finished_at is not None else None,
        updated_at=updated_at,
    )
