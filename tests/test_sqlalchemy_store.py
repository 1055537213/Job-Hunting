"""SQLAlchemy 生产仓储适配层的回归测试。"""

from __future__ import annotations

from job_hunting_agent.app import JobHuntingApp
from job_hunting_agent.models import CandidateProfileInput, UsageEventRecord
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
