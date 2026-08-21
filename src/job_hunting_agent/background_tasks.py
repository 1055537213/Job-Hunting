"""后台任务执行器与 Celery 注册。

Web 只登记受控资源 ID，Worker 在独立进程中读取 PostgreSQL 事实源并执行耗时操作。
当前已接入 ``system_probe``、``resume_ocr``、``rag_index`` 和公开 GitHub 项目分析；
简历导出会继续沿用同一任务边界逐个迁移。
"""

from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
from typing import Any

from .app import JobHuntingApp
from .auth import iso_utc
from .config import DEFAULT_ENV_PATH
from .deduplication import DuplicateResourceError, github_project_content_fingerprint
from .github_project import (
    GitHubRepositoryError,
    GitHubRepositoryUnavailableError,
    InvalidGitHubRepositoryUrlError,
    normalize_public_github_repository_url,
)
from .models import BackgroundTaskRecord
from .resume_document import ResumeDocumentError
from .task_queue import (
    BACKGROUND_TASK_NAME,
    GITHUB_PROJECT_ANALYSIS_TASK_TYPE,
    RAG_INDEX_TASK_TYPE,
    RESUME_OCR_TASK_TYPE,
    TOOL_AUDIT_RETENTION_TASK_NAME,
)
from .tool_audit import (
    background_task_tool_name,
    build_tool_trace_record,
    tool_step_label,
)

SYSTEM_PROBE_TASK_TYPE = "system_probe"


def _background_task_root_request_id(record: BackgroundTaskRecord) -> str:
    """读取任务链路 ID；缺失时用 task_key 保证后台任务仍可审计。"""

    root_request_id = record.payload.get("root_request_id")
    if isinstance(root_request_id, str) and root_request_id.strip():
        return root_request_id.strip()[:128]
    return record.task_key


def _record_background_task_trace(
    backend: JobHuntingApp,
    record: BackgroundTaskRecord,
    *,
    step_status: str,
    trace_status: str,
    attempt_status: str,
    summary: str | None,
    result: dict[str, object] | None = None,
    finish_attempt: bool = False,
) -> None:
    """把后台任务执行状态合并进工具调用审计轨迹。"""

    root_request_id = _background_task_root_request_id(record)
    now = iso_utc()
    try:
        existing = backend.store.get_tool_call_trace(
            root_request_id,
            account_id=record.account_id,
        )
        trace = dict(existing.trace)
        steps = trace.setdefault("steps", [])
        if not isinstance(steps, list):
            steps = []
            trace["steps"] = steps
        created_at = existing.created_at
        started_at = existing.started_at or record.started_at or now
        source = existing.source
    except KeyError:
        trace = {
            "version": 1,
            "root_request_id": root_request_id,
            "title": tool_step_label(background_task_tool_name(record.task_type)),
            "status": "running",
            "source": "background_task",
            "duration_ms": None,
            "created_at": record.created_at or now,
            "started_at": record.started_at or now,
            "finished_at": None,
            "updated_at": now,
            "steps": [],
            "approval": None,
        }
        steps = trace["steps"]
        created_at = str(trace["created_at"])
        started_at = str(trace["started_at"])
        source = "background_task"

    assert isinstance(steps, list)
    tool_name = background_task_tool_name(record.task_type)
    step = next(
        (
            item
            for item in steps
            if isinstance(item, dict) and item.get("background_task_key") == record.task_key
        ),
        None,
    )
    if step is None:
        step = {
            "id": f"step-{len(steps) + 1}",
            "name": tool_name,
            "label": tool_step_label(tool_name),
            "status": "running",
            "summary": None,
            "result": None,
            "started_at": record.started_at or now,
            "finished_at": None,
            "background_task_key": record.task_key,
            "attempts": [],
        }
        steps.append(step)
    step["name"] = str(step.get("name") or tool_name)
    step["label"] = str(step.get("label") or tool_step_label(step["name"]))
    step["background_task_key"] = record.task_key
    step["status"] = step_status
    step["summary"] = summary
    step["result"] = result or {
        "ok": step_status not in {"failed", "cancelled"},
        "task_key": record.task_key,
        "task_type": record.task_type,
        "status": record.status,
        "progress": record.progress,
        "error_summary": record.error_summary,
    }
    if step_status in {"completed", "failed", "cancelled"}:
        step["finished_at"] = record.finished_at or now

    attempts = step.setdefault("attempts", [])
    if not isinstance(attempts, list):
        attempts = []
        step["attempts"] = attempts
    attempt_number = max(1, int(record.attempt or 1))
    attempt = next(
        (
            item
            for item in attempts
            if isinstance(item, dict)
            and int(item.get("attempt") or 0) == attempt_number
            and item.get("phase") == "background_task"
        ),
        None,
    )
    if attempt is None:
        attempt = {
            "attempt": attempt_number,
            "phase": "background_task",
            "started_at": record.started_at or now,
            "finished_at": None,
        }
        attempts.append(attempt)
    attempt["status"] = attempt_status
    attempt["summary"] = summary
    attempt["result"] = step["result"]
    if finish_attempt or attempt_status in {"completed", "failed", "cancelled"}:
        attempt["finished_at"] = record.finished_at or now

    trace["root_request_id"] = root_request_id
    trace["source"] = source
    trace["created_at"] = created_at
    trace["started_at"] = started_at
    trace["status"] = trace_status
    trace["updated_at"] = now
    if trace_status in {"completed", "failed", "cancelled"}:
        trace["finished_at"] = record.finished_at or now
    elif trace.get("finished_at") and trace_status == "running":
        trace["finished_at"] = None
    backend.store.record_tool_call_trace(
        build_tool_trace_record(
            trace,
            account_id=record.account_id,
            candidate_id=record.candidate_id,
            session_id=record.session_id,
            source=source,
        )
    )


def purge_old_tool_call_traces(backend: JobHuntingApp) -> int:
    """删除超出分页保留窗口的工具调用审计记录。"""

    return backend.store.prune_tool_call_traces_to_limit()


def purge_old_operational_audit_records(backend: JobHuntingApp) -> dict[str, int]:
    """删除超出分页保留窗口的后台运维记录。"""

    deleted_tool_call_traces = backend.store.prune_tool_call_traces_to_limit()
    deleted_usage_events = backend.store.prune_usage_events_to_limit()
    return {
        "deleted_tool_call_traces": deleted_tool_call_traces,
        "deleted_usage_events": deleted_usage_events,
    }


class NonRetryableTaskError(RuntimeError):
    """任务类型或参数不可恢复，不应浪费队列重试次数。"""


def _rag_task_payload(record: BackgroundTaskRecord) -> tuple[list[int], str | None]:
    """校验 RAG 任务 payload，只接受长文本资源 ID 和可选链路 ID。"""

    raw_ids = record.payload.get("long_text_ids")
    if not isinstance(raw_ids, list) or not raw_ids:
        raise NonRetryableTaskError("RAG 任务缺少长文本 ID。")

    normalized_ids: set[int] = set()
    for raw_id in raw_ids:
        # bool 是 int 的子类，但不应被当成长文本主键接受。
        if isinstance(raw_id, bool):
            raise NonRetryableTaskError("RAG 任务包含无效长文本 ID。")
        if isinstance(raw_id, int):
            long_text_id = raw_id
        elif isinstance(raw_id, str) and raw_id.strip().isdigit():
            long_text_id = int(raw_id.strip())
        else:
            raise NonRetryableTaskError("RAG 任务包含无效长文本 ID。")
        if long_text_id <= 0:
            raise NonRetryableTaskError("RAG 任务包含无效长文本 ID。")
        normalized_ids.add(long_text_id)

    root_request_id = record.payload.get("root_request_id")
    if root_request_id is not None:
        if not isinstance(root_request_id, str) or not root_request_id.strip():
            raise NonRetryableTaskError("RAG 任务包含无效请求链路 ID。")
        root_request_id = root_request_id.strip()[:128]
    return sorted(normalized_ids), root_request_id


def _resume_ocr_task_payload(record: BackgroundTaskRecord) -> tuple[int, str | None]:
    """校验 OCR 任务只引用一份待处理简历，不接收文件正文或路径。"""

    raw_artifact_id = record.payload.get("artifact_id")
    if isinstance(raw_artifact_id, bool):
        raise NonRetryableTaskError("OCR 任务包含无效简历 ID。")
    if isinstance(raw_artifact_id, int):
        artifact_id = raw_artifact_id
    elif isinstance(raw_artifact_id, str) and raw_artifact_id.strip().isdigit():
        artifact_id = int(raw_artifact_id.strip())
    else:
        raise NonRetryableTaskError("OCR 任务缺少有效简历 ID。")
    if artifact_id <= 0:
        raise NonRetryableTaskError("OCR 任务包含无效简历 ID。")

    root_request_id = record.payload.get("root_request_id")
    if root_request_id is not None:
        if not isinstance(root_request_id, str) or not root_request_id.strip():
            raise NonRetryableTaskError("OCR 任务包含无效请求链路 ID。")
        root_request_id = root_request_id.strip()[:128]
    return artifact_id, root_request_id


def _github_project_task_payload(record: BackgroundTaskRecord) -> tuple[str, str | None]:
    """校验 GitHub 分析任务只引用规范化后的公开仓库首页地址。"""

    raw_url = record.payload.get("repository_url")
    if not isinstance(raw_url, str):
        raise NonRetryableTaskError("GitHub 项目分析任务缺少仓库链接。")
    try:
        repository_url = normalize_public_github_repository_url(raw_url).canonical_url
    except InvalidGitHubRepositoryUrlError as error:
        raise NonRetryableTaskError("GitHub 项目分析任务包含无效仓库链接。") from error

    root_request_id = record.payload.get("root_request_id")
    if root_request_id is not None:
        if not isinstance(root_request_id, str) or not root_request_id.strip():
            raise NonRetryableTaskError("GitHub 项目分析任务包含无效请求链路 ID。")
        root_request_id = root_request_id.strip()[:128]
    return repository_url, root_request_id


def _mark_resume_ocr_artifact_failed(backend: JobHuntingApp, task_key: str) -> None:
    """在 OCR 任务终止时同步文件状态，避免页面永久显示“处理中”。"""

    try:
        record = backend.store.get_background_task(task_key)
        if record.task_type != RESUME_OCR_TASK_TYPE:
            return
        artifact_id, _ = _resume_ocr_task_payload(record)
        backend.fail_resume_ocr_artifact(artifact_id=artifact_id, account_id=record.account_id)
    except (KeyError, NonRetryableTaskError, RuntimeError, ValueError):
        # 文件可能已由用户删除；这里不应盖过原始任务错误或导致 Worker 再次失败。
        return


def run_registered_task(
    backend: JobHuntingApp,
    task_key: str,
    *,
    claimed_record: BackgroundTaskRecord | None = None,
) -> dict[str, object]:
    """认领并执行一个已登记任务，返回只含摘要的结果。

    单元测试和同步维护入口可以直接调用本函数，由它完成认领。Celery 外层需要先认领
    再检查外部依赖时，则传入 ``claimed_record``，保证同一消息全程只认领一次。
    """

    record = claimed_record or backend.store.claim_background_task(task_key)
    if record is None:
        # 重复消息没有取得执行权，只返回权威状态，绝不再次执行任务正文。
        current_task = backend.store.get_background_task(task_key)
        return {
            "task_key": current_task.task_key,
            "status": current_task.status,
            "result": current_task.result,
        }

    _record_background_task_trace(
        backend,
        record,
        step_status="running",
        trace_status="running",
        attempt_status="running",
        summary="后台任务已开始执行",
        result={
            "ok": True,
            "task_key": record.task_key,
            "task_type": record.task_type,
            "status": record.status,
            "progress": record.progress,
        },
    )

    if record.task_type == RESUME_OCR_TASK_TYPE:
        artifact_id, root_request_id = _resume_ocr_task_payload(record)
        if record.candidate_id is None:
            raise NonRetryableTaskError("OCR 任务缺少候选人归属。")
        # PDFium 渲染与 RapidOCR 都可能占用较长时间，因此把进度拆成 OCR 和 RAG 两段。
        backend.store.update_background_task_progress(task_key, 10)
        try:
            artifact = backend.process_resume_ocr_artifact(
                artifact_id=artifact_id,
                account_id=record.account_id,
                candidate_id=record.candidate_id,
            )
        except KeyError as error:
            # 用户主动删除了原件时没有可恢复的资源，不应继续消耗 OCR 重试次数。
            raise NonRetryableTaskError("OCR 原始简历已不存在。") from error
        except ValueError as error:
            # ResumeDocumentError 可能来自短暂的 OCR/运行时故障，仍保留 Celery 重试。
            if isinstance(error, ResumeDocumentError):
                raise
            raise NonRetryableTaskError("OCR 简历状态不允许继续处理。") from error
        if artifact.long_text_id is None:
            raise RuntimeError("OCR 完成后没有登记可索引的简历长文本。")
        backend.store.update_background_task_progress(task_key, 70)
        rag_task = backend.enqueue_rag_index_task(
            long_text_ids=[artifact.long_text_id],
            account_id=record.account_id,
            candidate_id=record.candidate_id,
            session_id=record.session_id,
            root_request_id=root_request_id or record.task_key,
            idempotency_key=f"resume-rag:{artifact.id}",
        )
        backend.store.update_background_task_progress(task_key, 90)
        completed = backend.store.complete_background_task(
            task_key,
            {
                "artifact_id": artifact.id,
                "long_text_id": artifact.long_text_id,
                "rag_task_key": rag_task.task_key,
            },
        )
        _record_background_task_trace(
            backend,
            completed,
            step_status="completed",
            trace_status="running",
            attempt_status="completed",
            summary="OCR 已完成，RAG 索引任务已排队",
            result={
                "ok": True,
                "task_key": completed.task_key,
                "task_type": completed.task_type,
                "status": completed.status,
                "artifact_id": artifact.id,
                "long_text_id": artifact.long_text_id,
                "rag_task_key": rag_task.task_key,
            },
            finish_attempt=True,
        )
        return {
            "task_key": completed.task_key,
            "status": completed.status,
            "result": completed.result,
        }

    if record.task_type == GITHUB_PROJECT_ANALYSIS_TASK_TYPE:
        repository_url, _root_request_id = _github_project_task_payload(record)
        if record.candidate_id is None:
            raise NonRetryableTaskError("GitHub 项目分析任务缺少候选人归属。")
        # 网络读取和 ZIP 筛选都在 Worker 内执行；任务 payload 中从不保存源码正文。
        backend.store.update_background_task_progress(task_key, 10)
        try:
            project_card = backend.analyze_github_project_for_candidate(
                record.candidate_id,
                repository_url,
                account_id=record.account_id,
            )
        except DuplicateResourceError as error:
            # 提交阶段已经去重；若 Worker 重投或与同步入口发生竞态，复用已保存的卡片。
            existing_card = backend.store.find_project_card_by_content_fingerprint(
                record.account_id,
                record.candidate_id,
                github_project_content_fingerprint(repository_url),
            )
            if existing_card is None:
                raise NonRetryableTaskError(str(error)) from error
            project_card = existing_card
        except GitHubRepositoryError as error:
            # 链接错误、仓库删除或私有仓库不会因重试而恢复，直接结束任务。
            if isinstance(error, GitHubRepositoryUnavailableError):
                raise
            raise NonRetryableTaskError("GitHub 仓库不可读取。") from error
        backend.store.update_background_task_progress(task_key, 90)
        completed = backend.store.complete_background_task(
            task_key,
            {
                "project_card_id": project_card.id,
                "project_name": project_card.card.project_name,
                "source_url": project_card.card.source_url,
            },
        )
        _record_background_task_trace(
            backend,
            completed,
            step_status="completed",
            trace_status="completed",
            attempt_status="completed",
            summary=f"已生成项目经历卡片：{project_card.card.project_name}",
            result={
                "ok": True,
                "task_key": completed.task_key,
                "task_type": completed.task_type,
                "status": completed.status,
                "project_card_id": project_card.id,
                "project_name": project_card.card.project_name,
                "source_url": project_card.card.source_url,
            },
            finish_attempt=True,
        )
        return {
            "task_key": completed.task_key,
            "status": completed.status,
            "result": completed.result,
        }

    if record.task_type == RAG_INDEX_TASK_TYPE:
        long_text_ids, root_request_id = _rag_task_payload(record)
        # Embedding 是耗时和可能失败的外部调用，先反馈已开始再更新到完成前的进度。
        backend.store.update_background_task_progress(task_key, 10)
        stats = backend.index_rag_long_texts(
            long_text_ids,
            account_id=record.account_id,
            candidate_id=record.candidate_id,
            session_id=record.session_id,
            root_request_id=root_request_id or record.task_key,
        )
        backend.store.update_background_task_progress(task_key, 90)
        completed = backend.store.complete_background_task(
            task_key,
            {
                "long_text_ids": long_text_ids,
                "index_stats": asdict(stats),
            },
        )
        _record_background_task_trace(
            backend,
            completed,
            step_status="completed",
            trace_status="completed",
            attempt_status="completed",
            summary=f"已更新 {stats.chunk_count} 个检索切片",
            result={
                "ok": True,
                "task_key": completed.task_key,
                "task_type": completed.task_type,
                "status": completed.status,
                "long_text_ids": long_text_ids,
                "index_stats": asdict(stats),
            },
            finish_attempt=True,
        )
        return {
            "task_key": completed.task_key,
            "status": completed.status,
            "result": completed.result,
        }

    if record.task_type != SYSTEM_PROBE_TASK_TYPE:
        raise NonRetryableTaskError(f"暂不支持的后台任务类型：{record.task_type}")

    # 探针不读取用户数据，只确认 Worker 能够完成一次数据库状态更新。
    backend.store.update_background_task_progress(task_key, 50)
    completed = backend.store.complete_background_task(
        task_key,
        {"worker": "ready", "task_type": SYSTEM_PROBE_TASK_TYPE},
    )
    _record_background_task_trace(
        backend,
        completed,
        step_status="completed",
        trace_status="completed",
        attempt_status="completed",
        summary="Worker 探针已完成",
        result={
            "ok": True,
            "task_key": completed.task_key,
            "task_type": completed.task_type,
            "status": completed.status,
            "worker": "ready",
        },
        finish_attempt=True,
    )
    return {
        "task_key": completed.task_key,
        "status": completed.status,
        "result": completed.result,
    }


def register_background_tasks(celery_app: Any, env_path: str | Path = DEFAULT_ENV_PATH) -> Any:
    """向一个 Celery 应用注册任务处理函数。"""

    @celery_app.task(
        bind=True,
        name=TOOL_AUDIT_RETENTION_TASK_NAME,
        ignore_result=True,
    )
    def purge_tool_call_traces(self: Any) -> dict[str, object]:
        """按上海自然日清理过期工具调用审计和 Token 用量记录。"""

        backend = JobHuntingApp(env_path=env_path)
        try:
            backend.initialize()
            deleted_counts = purge_old_operational_audit_records(backend)
            return {
                "status": "succeeded",
                "deleted_count": sum(deleted_counts.values()),
                **deleted_counts,
            }
        finally:
            backend.store.close()

    @celery_app.task(
        bind=True,
        name=BACKGROUND_TASK_NAME,
        acks_late=True,
        reject_on_worker_lost=True,
        ignore_result=True,
    )
    def execute_background_task(self: Any, task_key: str) -> dict[str, object]:
        """在独立 Worker 中执行任务，并把状态写回 PostgreSQL。"""

        backend = JobHuntingApp(env_path=env_path)
        try:
            # 先认领任务再做外部基础设施检查；检查失败时也能把 running 任务安全放回队列。
            claimed_record = backend.store.claim_background_task(task_key)
            if claimed_record is None:
                current_task = backend.store.get_background_task(task_key)
                return {
                    "task_key": current_task.task_key,
                    "status": current_task.status,
                    "result": current_task.result,
                }
            backend.initialize()
            return run_registered_task(backend, task_key, claimed_record=claimed_record)
        except NonRetryableTaskError as error:
            # 不把异常正文写入任务表，避免供应商或用户输入意外进入运维数据。
            try:
                _mark_resume_ocr_artifact_failed(backend, task_key)
                failed_record = backend.store.get_background_task(task_key)
                _record_background_task_trace(
                    backend,
                    failed_record,
                    step_status="failed",
                    trace_status="failed",
                    attempt_status="failed",
                    summary=str(error),
                    result={
                        "ok": False,
                        "task_key": task_key,
                        "task_type": failed_record.task_type,
                        "status": "failed",
                        "error": type(error).__name__,
                        "error_summary": str(error),
                    },
                    finish_attempt=True,
                )
                backend.store.fail_background_task(
                    task_key,
                    f"不可重试任务：{type(error).__name__}",
                )
            except (KeyError, RuntimeError):
                pass
            raise
        except Exception as error:
            # 数据库本身不可用时这里会直接抛出，由 Celery 的 broker 重试机制处理。
            record = backend.store.get_background_task(task_key)
            if record.task_type == RESUME_OCR_TASK_TYPE:
                safe_summary = "扫描版 PDF OCR 失败，请确认文件清晰且未加密后重试。"
            elif record.task_type == GITHUB_PROJECT_ANALYSIS_TASK_TYPE:
                safe_summary = "GitHub 仓库分析失败，请确认仓库公开可访问且未超出分析限制。"
            else:
                safe_summary = f"任务执行异常：{type(error).__name__}"
            if record.attempt < record.max_attempts:
                _record_background_task_trace(
                    backend,
                    record,
                    step_status="running",
                    trace_status="running",
                    attempt_status="failed",
                    summary=safe_summary,
                    result={
                        "ok": False,
                        "task_key": record.task_key,
                        "task_type": record.task_type,
                        "status": record.status,
                        "error": type(error).__name__,
                        "error_summary": safe_summary,
                        "retrying": True,
                    },
                    finish_attempt=True,
                )
                backend.store.requeue_background_task(task_key, safe_summary)
                # 退避时间随 Celery 重试次数增长，避免上游服务短暂故障时打满队列。
                countdown = min(60, 2 ** max(0, int(getattr(self.request, "retries", 0))))
                raise self.retry(exc=RuntimeError(safe_summary), countdown=countdown)
            _mark_resume_ocr_artifact_failed(backend, task_key)
            _record_background_task_trace(
                backend,
                record,
                step_status="failed",
                trace_status="failed",
                attempt_status="failed",
                summary=safe_summary,
                result={
                    "ok": False,
                    "task_key": record.task_key,
                    "task_type": record.task_type,
                    "status": "failed",
                    "error": type(error).__name__,
                    "error_summary": safe_summary,
                },
                finish_attempt=True,
            )
            backend.store.fail_background_task(task_key, safe_summary)
            raise
        finally:
            backend.store.close()

    return execute_background_task
