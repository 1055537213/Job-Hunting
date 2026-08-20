"""SQLAlchemy 生产仓储适配层的回归测试。"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta

import pytest

from job_hunting_agent.app import JobHuntingApp
from job_hunting_agent.models import (
    AdminAuditEventRecord,
    CandidateProfileInput,
    ToolCallTraceRecord,
    UsageEventRecord,
)
from job_hunting_agent.sqlalchemy_store import SQLAlchemyStore
from job_hunting_agent.tool_audit import tool_audit_retention_cutoff


def test_sqlalchemy_store_runs_existing_profile_chat_and_usage_workflow(database_url):
    """现有业务方法直接在 Alembic 管理的 PostgreSQL schema 上运行。"""
    store = SQLAlchemyStore(database_url)
    store.initialize()

    account = store.create_account("owner@example.com", "hashed-password")
    candidate_id = store.save_candidate_profile(
        CandidateProfileInput(
            name="测试候选人",
            status="在职",
            education="本科",
            experience_years=2.0,
            skills={"Python": "项目使用"},
            preferred_cities=["杭州"],
            salary_floor_k=12,
            expected_salary_k=16,
            target_directions=["Python 后端开发"],
        ),
        account_id=account.id,
    )
    job = store.save_job_text(
        "Python 后端开发工程师\n12-18K\n杭州\n1-3年\n本科\n职位描述：负责 Python 服务开发。",
        account_id=account.id,
    )
    session = store.create_chat_session(
        "chat-production-adapter",
        account.id,
        candidate_id,
        "测试会话",
        job_id=job.id,
    )
    message = store.save_chat_message(
        candidate_id,
        session.session_id,
        "user",
        "我想了解这个岗位。",
        account_id=account.id,
    )
    usage = store.record_usage_event(
        UsageEventRecord(
            id=0,
            account_id=account.id,
            candidate_id=candidate_id,
            session_id=session.session_id,
            root_request_id="request-1",
            call_id="call-1",
            provider="test-provider",
            model="test-model",
            operation="chat",
            input_tokens=10,
            output_tokens=20,
            total_tokens=30,
            usage_source="provider",
            status="succeeded",
            attempt=1,
            provider_request_id=None,
            raw_usage={"total_tokens": 30},
            created_at="2026-08-07T00:00:00+00:00",
            billable=True,
            pricing_version="test-v1",
        )
    )

    assert store.get_candidate_profile(candidate_id, account_id=account.id).name == "测试候选人"
    assert store.get_job(job.id, account_id=account.id).title == "Python 后端开发工程师"
    assert message.content == "我想了解这个岗位。"
    assert usage.total_tokens == 30
    assert store.summarize_usage(account_id=account.id)["billable_tokens"] == 30
    store.close()


def test_app_uses_sqlalchemy_store_when_a_database_url_is_explicit(database_url):
    """Web 传入数据库 URL 时使用 PostgreSQL 仓储。"""

    app = JobHuntingApp(database_url=database_url)
    app.initialize()

    assert app.store.__class__.__name__ == "SQLAlchemyStore"
    app.store.close()


def test_sqlalchemy_store_records_lists_summarizes_and_purges_tool_call_traces(database_url):
    """工具调用审计表按任务 upsert，并支持管理端列表、详情、聚合和保留清理。"""

    store = SQLAlchemyStore(database_url)
    store.initialize()
    account = store.create_account("tools@example.com", "hashed-password")
    other = store.create_account("other-tools@example.com", "hashed-password")
    retention_cutoff = tool_audit_retention_cutoff()
    cutoff_at = datetime.fromisoformat(retention_cutoff)
    recent_started_at = (cutoff_at + timedelta(minutes=1)).isoformat()
    recent_finished_at = (cutoff_at + timedelta(minutes=2)).isoformat()
    other_started_at = (cutoff_at + timedelta(minutes=3)).isoformat()
    other_finished_at = (cutoff_at + timedelta(minutes=4)).isoformat()
    old_started_at = (cutoff_at - timedelta(seconds=1)).isoformat()
    old_finished_at = (cutoff_at + timedelta(seconds=10)).isoformat()

    first = store.record_tool_call_trace(
        ToolCallTraceRecord(
            id=0,
            account_id=account.id,
            candidate_id=None,
            session_id="session-1",
            root_request_id="root-1",
            title="导入职位信息",
            status="running",
            source="chat",
            step_count=1,
            attempt_count=1,
            last_step_name="import_job_from_text",
            last_error_summary=None,
            trace={
                "steps": [
                    {
                        "id": "step-1",
                        "name": "import_job_from_text",
                        "status": "running",
                        "attempts": [{"attempt": 1, "status": "running"}],
                    }
                ]
            },
            created_at=recent_started_at,
            started_at=recent_started_at,
            finished_at=None,
            updated_at=recent_started_at,
        )
    )
    updated = store.record_tool_call_trace(
        ToolCallTraceRecord(
            id=0,
            account_id=account.id,
            candidate_id=None,
            session_id=None,
            root_request_id="root-1",
            title="导入职位信息",
            status="completed",
            source="chat",
            step_count=1,
            attempt_count=1,
            last_step_name="import_job_from_text",
            last_error_summary=None,
            trace={
                "steps": [
                    {
                        "id": "step-1",
                        "name": "import_job_from_text",
                        "status": "completed",
                        "result": {"ok": True, "job_title": "Python 后端"},
                        "attempts": [{"attempt": 1, "status": "completed"}],
                    }
                ]
            },
            created_at=recent_started_at,
            started_at=recent_started_at,
            finished_at=recent_finished_at,
            updated_at=recent_finished_at,
        )
    )
    with pytest.raises(ValueError, match="其他账号"):
        store.record_tool_call_trace(
            replace(
                updated,
                id=0,
                account_id=other.id,
                title="不应覆盖其他账号的任务",
            )
        )
    assert store.get_tool_call_trace("root-1", account_id=account.id).account_id == account.id
    with pytest.raises(KeyError):
        store.get_tool_call_trace("root-1", account_id=other.id)

    store.record_tool_call_trace(
        ToolCallTraceRecord(
            id=0,
            account_id=other.id,
            candidate_id=None,
            session_id=None,
            root_request_id="root-other",
            title="分析项目经历",
            status="failed",
            source="background_task",
            step_count=1,
            attempt_count=2,
            last_step_name="github_project_analysis",
            last_error_summary="GitHub 不可访问",
            trace={"steps": [{"id": "step-1", "name": "github_project_analysis", "status": "failed"}]},
            created_at=other_started_at,
            started_at=other_started_at,
            finished_at=other_finished_at,
            updated_at=other_finished_at,
        )
    )
    old = store.record_tool_call_trace(
        ToolCallTraceRecord(
            id=0,
            account_id=account.id,
            candidate_id=None,
            session_id=None,
            root_request_id="root-old",
            title="旧任务",
            status="completed",
            source="chat",
            step_count=1,
            attempt_count=1,
            last_step_name="ingest_candidate_message",
            last_error_summary=None,
            trace={"steps": [{"id": "step-1", "name": "ingest_candidate_message", "status": "completed"}]},
            created_at=old_started_at,
            started_at=old_started_at,
            finished_at=old_finished_at,
            updated_at=old_finished_at,
        )
    )

    assert updated.id == first.id
    assert store.get_tool_call_trace("root-1").status == "completed"
    assert store.count_tool_call_traces(account.id, cutoff_iso=retention_cutoff) == 1
    assert [
        trace.root_request_id
        for trace in store.list_tool_call_traces(account.id, cutoff_iso=retention_cutoff)
    ] == ["root-1"]
    with pytest.raises(KeyError):
        store.get_tool_call_trace(old.root_request_id, cutoff_iso=retention_cutoff)
    summary = store.summarize_tool_call_traces_by_account(cutoff_iso=retention_cutoff)
    assert {"account_id": account.id, "trace_count": 1, "failed_trace_count": 0} in summary
    assert {"account_id": other.id, "trace_count": 1, "failed_trace_count": 1} in summary

    deleted = store.delete_tool_call_traces_before(retention_cutoff)

    assert deleted == 1
    assert store.count_tool_call_traces(account.id, cutoff_iso=retention_cutoff) == 1
    try:
        store.get_tool_call_trace(old.root_request_id)
    except KeyError:
        pass
    else:  # pragma: no cover
        raise AssertionError("旧工具调用记录没有被清理")
    store.close()


def test_sqlalchemy_store_records_admin_audit_events(database_url):
    """管理员审计表以追加方式保存低敏动作和 request_id。"""

    store = SQLAlchemyStore(database_url)
    store.initialize()
    actor = store.create_account("admin-audit@example.com", "hashed-password", role="admin")
    target = store.create_account("target-audit@example.com", "hashed-password")

    first = store.record_admin_audit_event(
        AdminAuditEventRecord(
            id=0,
            actor_account_id=actor.id,
            target_account_id=target.id,
            action="account.status_updated",
            target_type="account",
            target_id=str(target.id),
            outcome="succeeded",
            summary="账号状态从 active 更新为 disabled。",
            details={"previous_status": "active", "next_status": "disabled"},
            request_id="audit-request-1",
            created_at="2026-08-20T01:00:00+00:00",
        )
    )
    second = store.record_admin_audit_event(
        AdminAuditEventRecord(
            id=0,
            actor_account_id=actor.id,
            target_account_id=None,
            action="system.probe_enqueued",
            target_type="background_task",
            target_id="task-1",
            outcome="succeeded",
            summary="投递系统探针任务。",
            details={"task_type": "system_probe"},
            request_id="audit-request-2",
            created_at="2026-08-20T01:01:00+00:00",
        )
    )

    events = store.list_admin_audit_events(limit=10)

    assert [event.id for event in events] == [second.id, first.id]
    assert events[0].action == "system.probe_enqueued"
    assert events[0].request_id == "audit-request-2"
    assert events[1].target_account_id == target.id
    assert events[1].details == {"previous_status": "active", "next_status": "disabled"}
    store.close()


def test_account_status_and_admin_audit_are_committed_atomically(database_url, monkeypatch):
    """审计写入失败时，账号状态和 Session 撤销不能先行提交。"""

    store = SQLAlchemyStore(database_url)
    store.initialize()
    actor = store.create_account("atomic-admin@example.com", "hashed-password", role="admin")
    target = store.create_account("atomic-target@example.com", "hashed-password")
    event = AdminAuditEventRecord(
        id=0,
        actor_account_id=actor.id,
        target_account_id=target.id,
        action="account.status_updated",
        target_type="account",
        target_id=str(target.id),
        outcome="succeeded",
        summary="账号状态已更新。",
        details={"previous_status": "active", "next_status": "disabled"},
        request_id="atomic-request-1",
    )

    def fail_audit_insert(*_args, **_kwargs):
        raise RuntimeError("audit unavailable")

    monkeypatch.setattr(store, "_insert_admin_audit_event", fail_audit_insert)
    with pytest.raises(RuntimeError, match="audit unavailable"):
        store.update_account_status_with_audit(
            target.id,
            "disabled",
            event,
            revoke_sessions=True,
        )

    assert store.get_account(target.id).status == "active"
    store.close()


def test_revoke_all_auth_sessions_and_admin_audit_are_committed_atomically(
    database_url,
    monkeypatch,
):
    """撤销全部 Session 时，审计失败也不能留下半成品会话状态。"""

    store = SQLAlchemyStore(database_url)
    store.initialize()
    actor = store.create_account("atomic-admin-logout@example.com", "hashed-password", role="admin")
    target = store.create_account("atomic-target-logout@example.com", "hashed-password")
    session = store.save_auth_session(
        target.id,
        "token-hash-logout",
        "2026-08-20T01:00:00+00:00",
        "2026-08-20T01:00:00+00:00",
        "2026-08-21T01:00:00+00:00",
        "2026-08-22T01:00:00+00:00",
    )
    event = AdminAuditEventRecord(
        id=0,
        actor_account_id=actor.id,
        target_account_id=target.id,
        action="auth.logout_all_devices",
        target_type="account",
        target_id=str(target.id),
        outcome="succeeded",
        summary="撤销账号全部登录会话。",
        details={},
        request_id="atomic-request-logout",
    )

    def fail_audit_insert(*_args, **_kwargs):
        raise RuntimeError("audit unavailable")

    monkeypatch.setattr(store, "_insert_admin_audit_event", fail_audit_insert)
    with pytest.raises(RuntimeError, match="audit unavailable"):
        store.revoke_all_auth_sessions_with_audit(target.id, event)

    assert store.get_auth_session(session.id).revoked_at is None
    store.close()


def test_background_task_and_admin_audit_are_committed_atomically(database_url, monkeypatch):
    """后台任务登记和管理员审计必须同事务提交，否则不能留下孤立任务。"""

    store = SQLAlchemyStore(database_url)
    store.initialize()
    actor = store.create_account("probe-admin@example.com", "hashed-password", role="admin")
    event = AdminAuditEventRecord(
        id=0,
        actor_account_id=actor.id,
        target_account_id=None,
        action="system.probe_enqueued",
        target_type="background_task",
        target_id=None,
        outcome="succeeded",
        summary="投递系统探针任务。",
        details={},
        request_id="probe-request-1",
    )

    def fail_audit_insert(*_args, **_kwargs):
        raise RuntimeError("audit unavailable")

    monkeypatch.setattr(store, "_insert_admin_audit_event", fail_audit_insert)
    with pytest.raises(RuntimeError, match="audit unavailable"):
        store.create_background_task(
            account_id=actor.id,
            task_type="system_probe",
            payload={"purpose": "admin_runtime_probe"},
            idempotency_key="probe-once",
            max_attempts=1,
            audit_event=event,
        )

    assert store.get_background_task_by_idempotency(actor.id, "probe-once") is None
    store.close()
