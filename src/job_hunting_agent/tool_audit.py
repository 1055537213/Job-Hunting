"""工具调用审计轨迹的共享辅助函数。"""

from __future__ import annotations

from collections.abc import Mapping

from .models import ToolCallTraceRecord

AUDIT_SKIP_STEP_NAMES = {"compose_reply"}

TOOL_STEP_LABELS = {
    "ingest_candidate_message": "整理并保存候选人资料",
    "get_current_candidate_profile": "读取当前候选人档案",
    "list_candidate_profiles": "读取候选人档案列表",
    "search_candidate_evidence": "检索相关项目经历",
    "import_job_from_text": "解析职位信息",
    "list_imported_jobs": "读取已导入职位",
    "match_all_jobs_for_candidate": "计算职位匹配结果",
    "list_project_cards_for_candidate": "读取项目经历卡片",
    "analyze_github_project_for_candidate": "分析项目经历",
    "confirm_project_card": "确认项目经历",
    "create_resume_draft_for_job": "生成职位定制简历草稿",
    "list_resume_artifacts_for_candidate": "读取已上传简历",
    "create_tailored_resume_from_upload": "生成职位定制简历",
    "resume_ocr": "识别扫描版简历",
    "rag_index": "更新 RAG 检索索引",
    "visual_index": "更新视觉知识索引",
    "resume_export": "生成职位定制简历文件",
    "github_project_analysis": "分析 GitHub 项目",
    "project_archive_analysis": "扫描项目整包",
    "system_probe": "检查 Worker 连通性",
    "compose_reply": "整理回复",
}


def tool_step_label(tool_name: str) -> str:
    """把内部工具名转换成稳定的用户可读步骤标题。"""

    return TOOL_STEP_LABELS.get(tool_name, tool_name.replace("_", " ").strip() or "执行任务")


def background_task_tool_name(task_type: str) -> str:
    """把后台任务类型映射为审计里的工具步骤名。"""

    aliases = {
        "github_project_analysis": "github_project_analysis",
        "project_archive_analysis": "project_archive_analysis",
        "resume_ocr": "resume_ocr",
        "rag_index": "rag_index",
        "visual_index": "visual_index",
        "resume_export": "resume_export",
        "system_probe": "system_probe",
    }
    return aliases.get(task_type, task_type or "background_task")


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

    priorities = [
        ("create_tailored_resume_from_upload", "生成职位定制简历"),
        ("create_resume_draft_for_job", "生成职位定制简历草稿"),
        ("analyze_github_project_for_candidate", "分析项目经历"),
        ("github_project_analysis", "分析 GitHub 项目"),
        ("project_archive_analysis", "扫描项目整包"),
        ("resume_ocr", "识别扫描版简历"),
        ("resume_export", "生成职位定制简历文件"),
        ("rag_index", "更新 RAG 检索索引"),
        ("visual_index", "更新视觉知识索引"),
        ("confirm_project_card", "确认项目经历"),
        ("match_all_jobs_for_candidate", "职位匹配分析"),
        ("import_job_from_text", "导入职位信息"),
        ("ingest_candidate_message", "整理候选人资料"),
        ("system_probe", "检查 Worker 连通性"),
    ]
    for tool_name, title in priorities:
        if tool_name in tool_names:
            return title
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
