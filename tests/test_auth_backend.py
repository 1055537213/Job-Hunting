"""认证、账号归属和 Token 用量流水的聚焦测试。"""

from datetime import UTC, datetime, timedelta

import pytest

from job_hunting_agent.auth import (
    AccountAlreadyExistsError,
    AuthService,
    PermissionDeniedError,
    SessionInvalidError,
    session_token_hash,
)
from job_hunting_agent.models import CandidateProfileInput, UsageEventRecord
from job_hunting_agent.sqlalchemy_store import SQLAlchemyStore


def test_register_login_and_logout_all_use_server_side_session(database_url):
    """登录只返回原始令牌，数据库保存令牌摘要，退出所有设备会立即失效。"""

    store = SQLAlchemyStore(database_url)
    store.initialize()
    auth = AuthService(store)

    account = auth.register("Demo@Example.com", "password-123", "Demo")
    with pytest.raises(AccountAlreadyExistsError):
        auth.register("demo@example.com", "password-123")

    admin = auth.create_admin("admin@example.com", "password-123")
    assert auth.require_admin(admin).role == "admin"
    with pytest.raises(PermissionDeniedError):
        auth.require_admin(account)

    login = auth.login("demo@example.com", "password-123")
    assert login.account.id == account.id
    assert len(login.session_token) >= 48
    saved = store.get_auth_session_by_token_hash(session_token_hash(login.session_token))
    assert saved is not None
    assert saved.token_hash != login.session_token
    assert auth.current_account(login.session_token).id == account.id

    auth.logout_all(account.id)
    with pytest.raises(SessionInvalidError):
        auth.current_account(login.session_token)


def test_session_has_idle_and_absolute_expiry(database_url):
    """闲置 Session 会滑动，但绝对过期时间不会被延长。"""

    store = SQLAlchemyStore(database_url)
    store.initialize()
    current = [datetime(2026, 1, 1, tzinfo=UTC)]
    auth = AuthService(store, idle_days=7, max_days=30, clock=lambda: current[0])
    account = auth.register("time@example.com", "password-123")
    login = auth.login("time@example.com", "password-123")
    original = store.get_auth_session(login.session.id)

    current[0] += timedelta(days=6)
    auth.current_account(login.session_token)
    touched = store.get_auth_session(login.session.id)
    assert touched.expires_at != original.expires_at
    assert touched.absolute_expires_at == original.absolute_expires_at

    current[0] = datetime(2026, 2, 1, tzinfo=UTC)
    with pytest.raises(SessionInvalidError):
        auth.current_account(login.session_token)
    assert store.get_auth_session(login.session.id).revoked_at is not None
    assert account.id == login.account.id


def test_account_filters_profiles_jobs_and_usage_events(database_url):
    """同一数据库内不同账号只能按 account_id 读取自己的资源和用量。"""

    store = SQLAlchemyStore(database_url)
    store.initialize()
    auth = AuthService(store)
    account_a = auth.register("a@example.com", "password-123")
    account_b = auth.register("b@example.com", "password-123")

    profile = CandidateProfileInput(
        name="档案 A",
        status="待补充",
        education="本科",
        experience_years=0,
        skills={},
        preferred_cities=[],
        salary_floor_k=None,
        expected_salary_k=None,
        target_directions=[],
        unacceptable=[],
    )
    profile_a = store.save_candidate_profile(profile, account_id=account_a.id)
    assert [item.id for item in store.list_candidate_profiles(account_a.id)] == [profile_a]
    assert store.list_candidate_profiles(account_b.id) == []
    with pytest.raises(KeyError):
        store.get_candidate_profile(profile_a, account_id=account_b.id)

    session = store.create_chat_session(
        "profile-a-job-chat",
        account_a.id,
        profile_a,
        "岗位定向对话",
    )
    assert store.list_chat_sessions(account_a.id)[0].id == session.id
    assert store.list_chat_sessions(account_b.id) == []
    with pytest.raises(KeyError):
        store.create_chat_session(
            "cross-account-chat",
            account_b.id,
            profile_a,
            "不应创建",
        )

    event = UsageEventRecord(
        id=0,
        account_id=account_a.id,
        candidate_id=profile_a,
        session_id="session-a",
        root_request_id="turn-a",
        call_id="call-a",
        provider="deepseek",
        model="deepseek-chat",
        operation="agent_reply",
        input_tokens=10,
        output_tokens=4,
        total_tokens=14,
        usage_source="provider",
        status="succeeded",
        attempt=1,
        provider_request_id="provider-a",
        raw_usage={"prompt_tokens": 10},
        created_at="2026-01-01T00:00:00+00:00",
        billable=True,
        pricing_version="v1",
    )
    stored = store.record_usage_event(event)
    assert stored.total_tokens == 14
    assert store.record_usage_event(event).id == stored.id
    assert store.summarize_usage(account_a.id)["billable_tokens"] == 14
    assert store.summarize_usage(account_b.id)["event_count"] == 0
