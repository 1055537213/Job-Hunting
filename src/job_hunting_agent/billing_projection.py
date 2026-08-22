"""账户级账单投影。

这层只做读取已有 usage ledger 后的投影，不修改账本本身。
它把“每账号配额、预警比例、当前可计费 Token、状态标签”整理成前端和管理
员都能直接消费的结构。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from math import ceil

from .models import AccountRecord


@dataclass(frozen=True)
class AccountBillingProjection:
    """单个账号的账单投影。"""

    account_id: int
    email: str
    role: str
    status: str
    billable_tokens: int
    total_tokens: int
    quota_tokens: int | None
    warning_ratio: float
    warning_threshold_tokens: int | None
    remaining_tokens: int | None
    usage_ratio: float | None
    state: str
    state_label: str


@dataclass(frozen=True)
class BillingSummary:
    """管理端账单总览。"""

    configured: bool
    quota_tokens: int | None
    warning_ratio: float
    account_count: int
    billable_account_count: int
    unlimited_account_count: int
    warning_account_count: int
    over_quota_account_count: int
    total_billable_tokens: int
    total_tokens: int
    total_quota_tokens: int | None
    total_remaining_tokens: int | None


def project_account_billing(
    accounts: Sequence[AccountRecord],
    usage_by_account: Sequence[Mapping[str, object]],
    *,
    quota_tokens: int | None,
    warning_ratio: float,
) -> tuple[list[AccountBillingProjection], BillingSummary]:
    """把 usage ledger 投影成账户级账单状态。"""

    usage_map = {
        int(row["account_id"]): row
        for row in usage_by_account
        if row.get("account_id") is not None
    }
    projections: list[AccountBillingProjection] = []
    total_billable_tokens = 0
    total_tokens = 0
    warning_account_count = 0
    over_quota_account_count = 0
    unlimited_account_count = 0

    for account in accounts:
        usage_row = usage_map.get(account.id, {})
        billable_tokens = int(usage_row.get("billable_tokens", 0) or 0)
        account_total_tokens = int(usage_row.get("total_tokens", 0) or 0)
        total_billable_tokens += billable_tokens
        total_tokens += account_total_tokens

        if quota_tokens is None:
            state = "unlimited"
            state_label = "未配置配额"
            remaining_tokens = None
            usage_ratio = None
            warning_threshold_tokens = None
            unlimited_account_count += 1
        else:
            warning_threshold_tokens = max(1, ceil(quota_tokens * warning_ratio))
            usage_ratio = billable_tokens / quota_tokens if quota_tokens > 0 else None
            remaining_tokens = max(quota_tokens - billable_tokens, 0)
            if billable_tokens >= quota_tokens:
                state = "over_quota"
                state_label = "已超额"
                over_quota_account_count += 1
            elif billable_tokens >= warning_threshold_tokens:
                state = "warning"
                state_label = "接近配额"
                warning_account_count += 1
            else:
                state = "healthy"
                state_label = "正常"

        projections.append(
            AccountBillingProjection(
                account_id=account.id,
                email=account.email,
                role=account.role,
                status=account.status,
                billable_tokens=billable_tokens,
                total_tokens=account_total_tokens,
                quota_tokens=quota_tokens,
                warning_ratio=warning_ratio,
                warning_threshold_tokens=warning_threshold_tokens,
                remaining_tokens=remaining_tokens,
                usage_ratio=usage_ratio,
                state=state,
                state_label=state_label,
            )
        )

    if quota_tokens is None:
        total_quota_tokens = None
        total_remaining_tokens = None
    else:
        total_quota_tokens = quota_tokens * len(accounts)
        total_remaining_tokens = max(total_quota_tokens - total_billable_tokens, 0)

    summary = BillingSummary(
        configured=quota_tokens is not None,
        quota_tokens=quota_tokens,
        warning_ratio=warning_ratio,
        account_count=len(accounts),
        billable_account_count=len(accounts) - unlimited_account_count,
        unlimited_account_count=unlimited_account_count,
        warning_account_count=warning_account_count,
        over_quota_account_count=over_quota_account_count,
        total_billable_tokens=total_billable_tokens,
        total_tokens=total_tokens,
        total_quota_tokens=total_quota_tokens,
        total_remaining_tokens=total_remaining_tokens,
    )
    return projections, summary


def billing_projection_to_dict(projection: AccountBillingProjection) -> dict[str, object]:
    """导出给 API/前端使用的投影字典。"""

    return asdict(projection)


def billing_summary_to_dict(summary: BillingSummary) -> dict[str, object]:
    """导出给 API/前端使用的总览字典。"""

    return asdict(summary)
