"""SQLAlchemy 生产仓储适配层的回归测试。"""

from __future__ import annotations

from dataclasses import replace

import pytest

from job_hunting_agent.app import JobHuntingApp
from job_hunting_agent.models import (
    AdminAuditEventRecord,
    CandidateProfileInput,
    ToolCallTraceRecord,
    UsageEventRecord,
)
from job_hunting_agent.sqlalchemy_store import SQLAlchemyStore


def ledger_timestamp(index: int) -> str:
    """生成稳定、按字典序递增的 ISO 时间，方便分页/排序测试。"""

    minute, second = divmod(index, 60)
    return f"2026-08-21T00:{minute:02d}:{second:02d}+00:00"


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
    """工具调用审计表按任务 upsert，并支持 5 页固定分页保留。"""

    store = SQLAlchemyStore(database_url)
    store.initialize()
    account = store.create_account("tools@example.com", "hashed-password")
    other = store.create_account("other-tools@example.com", "hashed-password")

    first = store.record_tool_call_trace(
        ToolCallTraceRecord(
            id=0,
            account_id=account.id,
            candidate_id=None,
            session_id="session-1",
            root_request_id="root-001",
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
            created_at=ledger_timestamp(1),
            started_at=ledger_timestamp(1),
            finished_at=None,
            updated_at=ledger_timestamp(1),
        )
    )
    updated = store.record_tool_call_trace(
        ToolCallTraceRecord(
            id=0,
            account_id=account.id,
            candidate_id=None,
            session_id=None,
            root_request_id="root-001",
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
            created_at=ledger_timestamp(2),
            started_at=ledger_timestamp(1),
            finished_at=ledger_timestamp(2),
            updated_at=ledger_timestamp(2),
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
    assert store.get_tool_call_trace("root-001").status == "completed"

    for index in range(2, 502):
        store.record_tool_call_trace(
            ToolCallTraceRecord(
                id=0,
                account_id=account.id,
                candidate_id=None,
                session_id=None,
                root_request_id=f"root-{index:03d}",
                title=f"任务 {index}",
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
                        }
                    ]
                },
                created_at=ledger_timestamp(index),
                started_at=ledger_timestamp(index),
                finished_at=ledger_timestamp(index),
                updated_at=ledger_timestamp(index),
            )
        )

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
            created_at=ledger_timestamp(503),
            started_at=ledger_timestamp(503),
            finished_at=ledger_timestamp(503),
            updated_at=ledger_timestamp(503),
        )
    )

    assert updated.id == first.id
    assert store.count_tool_call_traces(account.id) == 500
    assert store.count_tool_call_traces(other.id) == 1
    assert [
        trace.root_request_id
        for trace in store.list_tool_call_traces(account.id, limit=100, offset=0)
    ] == [f"root-{index:03d}" for index in range(501, 401, -1)]
    assert [
        trace.root_request_id
        for trace in store.list_tool_call_traces(account.id, limit=100, offset=400)
    ] == [f"root-{index:03d}" for index in range(101, 1, -1)]
    with pytest.raises(KeyError):
        store.get_tool_call_trace("root-001", account_id=account.id)

    summary = store.summarize_tool_call_traces_by_account()
    assert {"account_id": account.id, "trace_count": 500, "failed_trace_count": 0} in summary
    assert {"account_id": other.id, "trace_count": 1, "failed_trace_count": 1} in summary
    store.close()
def test_sqlalchemy_store_filters_and_purges_usage_events_by_retention_window(database_url):
    """Token 流水支持和工具调用相同的 5 页固定保留窗口。"""

    store = SQLAlchemyStore(database_url)
    store.initialize()
    account = store.create_account("usage-retention@example.com", "hashed-password")
    other = store.create_account("usage-retention-other@example.com", "hashed-password")

    def usage_event(
        *,
        owner_id: int,
        call_id: str,
        total_tokens: int,
        created_at: str,
    ) -> UsageEventRecord:
        return UsageEventRecord(
            id=0,
            account_id=owner_id,
            candidate_id=None,
            session_id=None,
            root_request_id=f"request-{call_id}",
            call_id=call_id,
            provider="test-provider",
            model="test-model",
            operation="agent_chat",
            input_tokens=total_tokens,
            output_tokens=0,
            total_tokens=total_tokens,
            usage_source="provider",
            status="succeeded",
            attempt=1,
            provider_request_id=None,
            raw_usage={"total_tokens": total_tokens},
            created_at=created_at,
            billable=True,
            pricing_version="test-v1",
        )

    for index in range(1, 502):
        store.record_usage_event(
            usage_event(
                owner_id=account.id,
                call_id=f"usage-{index:03d}",
                total_tokens=1,
                created_at=ledger_timestamp(index),
            )
        )
    store.record_usage_event(
        usage_event(
            owner_id=other.id,
            call_id="usage-other-recent",
            total_tokens=5,
            created_at=ledger_timestamp(503),
        )
    )

    assert store.count_usage_events(account.id) == 500
    assert store.count_usage_events(other.id) == 1
    assert [
        event.call_id
        for event in store.list_usage_events(account.id, limit=100, offset=0)
    ] == [f"usage-{index:03d}" for index in range(501, 401, -1)]
    assert [
        event.call_id
        for event in store.list_usage_events(account.id, limit=100, offset=400)
    ] == [f"usage-{index:03d}" for index in range(101, 1, -1)]
    assert store.summarize_usage(account.id)["total_tokens"] == 500
    assert store.summarize_usage()["total_tokens"] == 505
    by_account = store.summarize_usage_by_account()
    assert {
        "account_id": account.id,
        "input_tokens": 500,
        "output_tokens": 0,
        "total_tokens": 500,
        "billable_tokens": 500,
        "event_count": 500,
    } in by_account
    assert {
        "account_id": other.id,
        "input_tokens": 5,
        "output_tokens": 0,
        "total_tokens": 5,
        "billable_tokens": 5,
        "event_count": 1,
    } in by_account
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
