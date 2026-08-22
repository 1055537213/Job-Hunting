from job_hunting_agent.billing_projection import project_account_billing
from job_hunting_agent.models import AccountRecord


def account(
    account_id: int,
    email: str,
    role: str = "user",
    status: str = "active",
) -> AccountRecord:
    return AccountRecord(
        id=account_id,
        email=email,
        display_name=None,
        role=role,
        status=status,
        created_at="2026-08-22T00:00:00+08:00",
        updated_at="2026-08-22T00:00:00+08:00",
    )


def test_project_account_billing_marks_warning_and_over_quota():
    """账单投影应按配额、预警比例和账号使用量给出状态。"""

    projections, summary = project_account_billing(
        [
            account(1, "a@example.com"),
            account(2, "b@example.com"),
            account(3, "c@example.com"),
        ],
        [
            {
                "account_id": 1,
                "input_tokens": 600,
                "output_tokens": 200,
                "total_tokens": 800,
                "billable_tokens": 800,
                "event_count": 12,
            },
            {
                "account_id": 2,
                "input_tokens": 900,
                "output_tokens": 400,
                "total_tokens": 1300,
                "billable_tokens": 1300,
                "event_count": 20,
            },
        ],
        quota_tokens=1000,
        warning_ratio=0.8,
    )

    assert summary.configured is True
    assert summary.account_count == 3
    assert summary.billable_account_count == 3
    assert summary.unlimited_account_count == 0
    assert summary.warning_account_count == 1
    assert summary.over_quota_account_count == 1
    assert summary.total_billable_tokens == 2100
    assert summary.total_tokens == 2100
    assert summary.total_quota_tokens == 3000
    assert summary.total_remaining_tokens == 900

    assert [projection.state for projection in projections] == [
        "warning",
        "over_quota",
        "healthy",
    ]
    assert projections[0].state_label == "接近配额"
    assert projections[0].remaining_tokens == 200
    assert projections[1].state_label == "已超额"
    assert projections[1].remaining_tokens == 0
    assert projections[1].usage_ratio == 1.3
    assert projections[2].state_label == "正常"
    assert projections[2].remaining_tokens == 1000


def test_project_account_billing_without_quota_is_unlimited():
    """未配置配额时，投影应保持只读展示，不标记告警或超额。"""

    projections, summary = project_account_billing(
        [account(1, "a@example.com")],
        [],
        quota_tokens=None,
        warning_ratio=0.8,
    )

    assert summary.configured is False
    assert summary.account_count == 1
    assert summary.billable_account_count == 0
    assert summary.unlimited_account_count == 1
    assert summary.warning_account_count == 0
    assert summary.over_quota_account_count == 0
    assert summary.total_billable_tokens == 0
    assert summary.total_tokens == 0
    assert summary.total_quota_tokens is None
    assert summary.total_remaining_tokens is None

    projection = projections[0]
    assert projection.state == "unlimited"
    assert projection.state_label == "未配置配额"
    assert projection.remaining_tokens is None
    assert projection.usage_ratio is None
