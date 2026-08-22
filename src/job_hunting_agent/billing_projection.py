"""账号级余额投影。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass

from .models import AccountRecord


@dataclass(frozen=True)
class AccountBalanceProjection:
    """单个账号的余额投影。"""

    account_id: int
    email: str
    role: str
    status: str
    balance_micro_yuan: int
    total_recharge_micro_yuan: int
    total_consumed_micro_yuan: int
    ledger_entry_count: int
    low_balance_threshold_micro_yuan: int
    state: str
    state_label: str


@dataclass(frozen=True)
class BalanceSummary:
    """管理端余额总览。"""

    configured: bool
    price_per_million_tokens_yuan: float
    starting_balance_yuan: float
    low_balance_threshold_yuan: float
    account_count: int
    healthy_account_count: int
    low_balance_account_count: int
    suspended_account_count: int
    total_balance_micro_yuan: int
    total_recharge_micro_yuan: int
    total_consumed_micro_yuan: int
    total_ledger_entry_count: int


def project_account_balances(
    accounts: Sequence[AccountRecord],
    balance_by_account: Sequence[Mapping[str, object]],
    *,
    price_per_million_tokens_yuan: float,
    starting_balance_yuan: float,
    low_balance_threshold_yuan: float,
) -> tuple[list[AccountBalanceProjection], BalanceSummary]:
    """把余额流水与账号列表投影成前端需要的结构。"""

    balance_map = {
        int(row["account_id"]): row
        for row in balance_by_account
        if row.get("account_id") is not None
    }
    projections: list[AccountBalanceProjection] = []
    healthy_account_count = 0
    low_balance_account_count = 0
    suspended_account_count = 0
    total_balance_micro_yuan = 0
    total_recharge_micro_yuan = 0
    total_consumed_micro_yuan = 0
    total_ledger_entry_count = 0
    threshold_micro_yuan = round(low_balance_threshold_yuan * 1_000_000)

    for account in accounts:
        row = balance_map.get(account.id, {})
        balance_micro_yuan = int(row.get("balance_micro_yuan", 0) or 0)
        recharge_micro_yuan = int(row.get("total_recharge_micro_yuan", 0) or 0)
        consumed_micro_yuan = int(row.get("total_consumed_micro_yuan", 0) or 0)
        ledger_entry_count = int(row.get("ledger_entry_count", 0) or 0)
        row_threshold_micro_yuan = int(
            row.get("low_balance_threshold_micro_yuan", threshold_micro_yuan) or threshold_micro_yuan
        )
        state = _balance_state(balance_micro_yuan, row_threshold_micro_yuan, account.status)
        state_label = _balance_state_label(state)
        total_balance_micro_yuan += balance_micro_yuan
        total_recharge_micro_yuan += recharge_micro_yuan
        total_consumed_micro_yuan += consumed_micro_yuan
        total_ledger_entry_count += ledger_entry_count
        if state == "balance":
            healthy_account_count += 1
        elif state == "low_balance":
            low_balance_account_count += 1
        else:
            suspended_account_count += 1

        projections.append(
            AccountBalanceProjection(
                account_id=account.id,
                email=account.email,
                role=account.role,
                status=account.status,
                balance_micro_yuan=balance_micro_yuan,
                total_recharge_micro_yuan=recharge_micro_yuan,
                total_consumed_micro_yuan=consumed_micro_yuan,
                ledger_entry_count=ledger_entry_count,
                low_balance_threshold_micro_yuan=row_threshold_micro_yuan,
                state=state,
                state_label=state_label,
            )
        )

    summary = BalanceSummary(
        configured=True,
        price_per_million_tokens_yuan=price_per_million_tokens_yuan,
        starting_balance_yuan=starting_balance_yuan,
        low_balance_threshold_yuan=low_balance_threshold_yuan,
        account_count=len(accounts),
        healthy_account_count=healthy_account_count,
        low_balance_account_count=low_balance_account_count,
        suspended_account_count=suspended_account_count,
        total_balance_micro_yuan=total_balance_micro_yuan,
        total_recharge_micro_yuan=total_recharge_micro_yuan,
        total_consumed_micro_yuan=total_consumed_micro_yuan,
        total_ledger_entry_count=total_ledger_entry_count,
    )
    return projections, summary


def balance_projection_to_dict(projection: AccountBalanceProjection) -> dict[str, object]:
    """导出给 API/前端使用的余额投影字典。"""

    return asdict(projection)


def balance_summary_to_dict(summary: BalanceSummary) -> dict[str, object]:
    """导出给 API/前端使用的余额总览字典。"""

    return asdict(summary)


def _balance_state(balance_micro_yuan: int, low_balance_threshold_micro_yuan: int, account_status: str) -> str:
    if account_status != "active" or balance_micro_yuan <= 0:
        return "suspended"
    if balance_micro_yuan <= low_balance_threshold_micro_yuan:
        return "low_balance"
    return "balance"


def _balance_state_label(state: str) -> str:
    return {
        "balance": "余额",
        "low_balance": "低余额",
        "suspended": "停用",
    }[state]
