from job_hunting_agent.billing_projection import project_account_balances
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


def test_project_account_balances_marks_normal_low_and_suspended_accounts():
    """余额投影应区分可用、低余额和余额耗尽/账号禁用。"""

    projections, summary = project_account_balances(
        [
            account(1, "a@example.com"),
            account(2, "b@example.com"),
            account(3, "c@example.com"),
            account(4, "d@example.com", status="disabled"),
        ],
        [
            {
                "account_id": 1,
                "balance_micro_yuan": 15_000_000,
                "total_recharge_micro_yuan": 100_000_000,
                "total_consumed_micro_yuan": 85_000_000,
                "ledger_entry_count": 12,
            },
            {
                "account_id": 2,
                "balance_micro_yuan": 5_000_000,
                "total_recharge_micro_yuan": 20_000_000,
                "total_consumed_micro_yuan": 15_000_000,
                "ledger_entry_count": 4,
            },
            {
                "account_id": 3,
                "balance_micro_yuan": 0,
                "total_recharge_micro_yuan": 100_000_000,
                "total_consumed_micro_yuan": 100_000_000,
                "ledger_entry_count": 20,
            },
        ],
        price_per_million_tokens_yuan=25,
        starting_balance_yuan=100,
        low_balance_threshold_yuan=10,
    )

    assert summary.configured is True
    assert summary.account_count == 4
    assert summary.healthy_account_count == 1
    assert summary.low_balance_account_count == 1
    assert summary.suspended_account_count == 2
    assert summary.total_balance_micro_yuan == 20_000_000
    assert summary.total_recharge_micro_yuan == 220_000_000
    assert summary.total_consumed_micro_yuan == 200_000_000
    assert summary.total_ledger_entry_count == 36

    assert [projection.state for projection in projections] == [
        "balance",
        "low_balance",
        "suspended",
        "suspended",
    ]
    assert projections[0].state_label == "余额"
    assert projections[1].state_label == "低余额"
    assert projections[2].state_label == "停用"
    assert projections[3].state_label == "停用"


def test_project_account_balances_uses_row_threshold_when_present():
    """账号自己的低余额阈值应覆盖全局默认阈值。"""

    projections, _ = project_account_balances(
        [account(1, "a@example.com")],
        [
            {
                "account_id": 1,
                "balance_micro_yuan": 12_000_000,
                "low_balance_threshold_micro_yuan": 15_000_000,
            }
        ],
        price_per_million_tokens_yuan=25,
        starting_balance_yuan=100,
        low_balance_threshold_yuan=10,
    )

    assert projections[0].state == "low_balance"
    assert projections[0].low_balance_threshold_micro_yuan == 15_000_000
