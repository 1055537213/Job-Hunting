"""SQLAlchemy 生产仓储适配层的回归测试。"""

from __future__ import annotations

from job_hunting_agent.app import JobHuntingApp
from job_hunting_agent.models import (
    AdminAuditEventRecord,
    CandidateProfileInput,
    ToolCallTraceRecord,
    UsageEventRecord,
)
from job_hunting_agent.sqlalchemy_store import SQLAlchemyStore


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
            created_at="2026-08-18T16:01:00+00:00",
            started_at="2026-08-18T16:01:00+00:00",
            finished_at=None,
            updated_at="2026-08-18T16:01:00+00:00",
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
            created_at="2026-08-18T16:01:00+00:00",
            started_at="2026-08-18T16:01:00+00:00",
            finished_at="2026-08-18T16:02:00+00:00",
            updated_at="2026-08-18T16:02:00+00:00",
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
            created_at="2026-08-18T16:03:00+00:00",
            started_at="2026-08-18T16:03:00+00:00",
            finished_at="2026-08-18T16:04:00+00:00",
            updated_at="2026-08-18T16:04:00+00:00",
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
            created_at="2026-08-17T15:59:59+00:00",
            started_at="2026-08-17T15:59:59+00:00",
            finished_at="2026-08-17T16:00:10+00:00",
            updated_at="2026-08-17T16:00:10+00:00",
        )
    )

    assert updated.id == first.id
    assert store.get_tool_call_trace("root-1").status == "completed"
    assert store.count_tool_call_traces(account.id) == 2
    assert [trace.root_request_id for trace in store.list_tool_call_traces(account.id)] == ["root-1", "root-old"]
    summary = store.summarize_tool_call_traces_by_account()
    assert {"account_id": account.id, "trace_count": 2, "failed_trace_count": 0} in summary
    assert {"account_id": other.id, "trace_count": 1, "failed_trace_count": 1} in summary

    deleted = store.delete_tool_call_traces_before("2026-08-17T16:00:00+00:00")

    assert deleted == 1
    assert store.count_tool_call_traces(account.id) == 1
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
