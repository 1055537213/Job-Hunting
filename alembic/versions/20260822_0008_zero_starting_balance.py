"""zero default starting balance

Revision ID: 20260822_0008
Revises: 20260822_0007
Create Date: 2026-08-22 22:00:00.000000
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import sqlalchemy as sa

from alembic import op

revision = "20260822_0008"
down_revision = "20260822_0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Remove the historical system credit while preserving real recharges."""

    connection = op.get_bind()
    rows = connection.execute(
        sa.text(
            """
            SELECT
                b.account_id,
                b.balance_micro_yuan,
                b.total_recharge_micro_yuan,
                SUM(l.amount_micro_yuan) AS initial_credit_micro_yuan
            FROM account_balances AS b
            JOIN account_balance_ledger AS l ON l.account_id = b.account_id
            WHERE l.entry_kind = 'initial_credit'
              AND l.amount_micro_yuan > 0
            GROUP BY b.account_id, b.balance_micro_yuan, b.total_recharge_micro_yuan
            ORDER BY b.account_id
            """
        )
    ).mappings().all()
    now = datetime.now(UTC)
    for row in rows:
        account_id = int(row["account_id"])
        initial_credit = int(row["initial_credit_micro_yuan"] or 0)
        if initial_credit <= 0:
            continue
        balance_before = int(row["balance_micro_yuan"])
        balance_after = balance_before - initial_credit
        recharge_after = max(0, int(row["total_recharge_micro_yuan"]) - initial_credit)
        source_reference = f"zero-starting-balance:{account_id}"
        connection.execute(
            sa.text(
                """
                INSERT INTO account_balance_ledger (
                    account_id, entry_kind, amount_micro_yuan,
                    balance_before_micro_yuan, balance_after_micro_yuan,
                    token_count, price_per_million_tokens_yuan,
                    source_reference, summary, details_json, created_at
                ) VALUES (
                    :account_id, 'adjustment', :amount_micro_yuan,
                    :balance_before_micro_yuan, :balance_after_micro_yuan,
                    NULL, NULL,
                    :source_reference, :summary, :details_json, :created_at
                )
                ON CONFLICT (source_reference) DO NOTHING
                """
            ),
            {
                "account_id": account_id,
                "amount_micro_yuan": -initial_credit,
                "balance_before_micro_yuan": balance_before,
                "balance_after_micro_yuan": balance_after,
                "source_reference": source_reference,
                "summary": "取消系统初始化余额",
                "details_json": json.dumps(
                    {
                        "reason": "starting_balance_policy_changed_to_zero",
                        "removed_initial_credit_micro_yuan": initial_credit,
                    },
                    ensure_ascii=False,
                ),
                "created_at": now,
            },
        )
        connection.execute(
            sa.text(
                """
                UPDATE account_balances
                SET balance_micro_yuan = :balance_micro_yuan,
                    total_recharge_micro_yuan = :total_recharge_micro_yuan,
                    updated_at = :updated_at
                WHERE account_id = :account_id
                """
            ),
            {
                "account_id": account_id,
                "balance_micro_yuan": balance_after,
                "total_recharge_micro_yuan": recharge_after,
                "updated_at": now,
            },
        )


def downgrade() -> None:
    """Restore credits removed by this policy migration when possible."""

    connection = op.get_bind()
    rows = connection.execute(
        sa.text(
            """
            SELECT account_id, amount_micro_yuan
            FROM account_balance_ledger
            WHERE entry_kind = 'adjustment'
              AND source_reference LIKE 'zero-starting-balance:%'
            ORDER BY account_id
            """
        )
    ).mappings().all()
    now = datetime.now(UTC)
    for row in rows:
        account_id = int(row["account_id"])
        restored_credit = max(0, -int(row["amount_micro_yuan"]))
        connection.execute(
            sa.text(
                """
                UPDATE account_balances
                SET balance_micro_yuan = balance_micro_yuan + :restored_credit,
                    total_recharge_micro_yuan = total_recharge_micro_yuan + :restored_credit,
                    updated_at = :updated_at
                WHERE account_id = :account_id
                """
            ),
            {
                "account_id": account_id,
                "restored_credit": restored_credit,
                "updated_at": now,
            },
        )
    connection.execute(
        sa.text(
            """
            DELETE FROM account_balance_ledger
            WHERE entry_kind = 'adjustment'
              AND source_reference LIKE 'zero-starting-balance:%'
            """
        )
    )
