"""add account balance ledger

Revision ID: 20260822_0007
Revises: 20260820_0006
Create Date: 2026-08-22 00:00:00.000000
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "20260822_0007"
down_revision = "20260820_0006"
branch_labels = None
depends_on = None


JSON_TYPE = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")
TIMESTAMP_TYPE = sa.DateTime(timezone=True)
INITIAL_BALANCE_YUAN = 100.0
LOW_BALANCE_THRESHOLD_YUAN = 10.0
PRICE_PER_MILLION_TOKENS_YUAN = 25.0
INITIAL_BALANCE_MICRO_YUAN = round(INITIAL_BALANCE_YUAN * 1_000_000)
LOW_BALANCE_THRESHOLD_MICRO_YUAN = round(LOW_BALANCE_THRESHOLD_YUAN * 1_000_000)


def upgrade() -> None:
    """Create the balance tables and backfill existing usage history."""

    op.create_table(
        "account_balances",
        sa.Column("account_id", sa.Integer, nullable=False),
        sa.Column("balance_micro_yuan", sa.BigInteger, nullable=False, server_default=sa.text("0")),
        sa.Column(
            "total_recharge_micro_yuan",
            sa.BigInteger,
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "total_consumed_micro_yuan",
            sa.BigInteger,
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "low_balance_threshold_micro_yuan",
            sa.BigInteger,
            nullable=False,
            server_default=sa.text("10000000"),
        ),
        sa.Column("created_at", TIMESTAMP_TYPE, nullable=False),
        sa.Column("updated_at", TIMESTAMP_TYPE, nullable=False),
        sa.ForeignKeyConstraint(["account_id"], ["accounts.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("account_id"),
    )
    op.create_table(
        "account_balance_ledger",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("account_id", sa.Integer, nullable=False),
        sa.Column("entry_kind", sa.String(32), nullable=False),
        sa.Column("amount_micro_yuan", sa.BigInteger, nullable=False),
        sa.Column("balance_before_micro_yuan", sa.BigInteger, nullable=False),
        sa.Column("balance_after_micro_yuan", sa.BigInteger, nullable=False),
        sa.Column("token_count", sa.Integer),
        sa.Column("price_per_million_tokens_yuan", sa.Numeric(12, 6)),
        sa.Column("source_reference", sa.String(160)),
        sa.Column("summary", sa.Text, nullable=False),
        sa.Column("details_json", JSON_TYPE, nullable=False, server_default=sa.text("'{}'")),
        sa.Column("created_at", TIMESTAMP_TYPE, nullable=False),
        sa.ForeignKeyConstraint(["account_id"], ["accounts.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("source_reference", name="uq_account_balance_ledger_source_reference"),
        sa.CheckConstraint(
            "entry_kind IN ('initial_credit', 'recharge', 'consumption', 'adjustment')",
            name="account_balance_ledger_entry_kind",
        ),
        sa.CheckConstraint("token_count >= 0", name="account_balance_ledger_token_count_non_negative"),
    )
    op.create_index(
        "idx_account_balance_ledger_account_time",
        "account_balance_ledger",
        ["account_id", sa.text("created_at DESC")],
    )

    connection = op.get_bind()
    accounts = connection.execute(
        sa.text("SELECT id, created_at FROM accounts ORDER BY id")
    ).mappings().all()
    for account in accounts:
        account_id = int(account["id"])
        created_at = account["created_at"] or datetime.now(UTC)
        balance = INITIAL_BALANCE_MICRO_YUAN
        recharge_total = INITIAL_BALANCE_MICRO_YUAN
        consumed_total = 0
        connection.execute(
            sa.text(
                """
                INSERT INTO account_balances (
                    account_id, balance_micro_yuan, total_recharge_micro_yuan,
                    total_consumed_micro_yuan, low_balance_threshold_micro_yuan,
                    created_at, updated_at
                ) VALUES (
                    :account_id, :balance_micro_yuan, :total_recharge_micro_yuan,
                    :total_consumed_micro_yuan, :low_balance_threshold_micro_yuan,
                    :created_at, :updated_at
                )
                ON CONFLICT (account_id) DO NOTHING
                """
            ),
            {
                "account_id": account_id,
                "balance_micro_yuan": balance,
                "total_recharge_micro_yuan": recharge_total,
                "total_consumed_micro_yuan": consumed_total,
                "low_balance_threshold_micro_yuan": LOW_BALANCE_THRESHOLD_MICRO_YUAN,
                "created_at": created_at,
                "updated_at": created_at,
            },
        )
        connection.execute(
            sa.text(
                """
                INSERT INTO account_balance_ledger (
                    account_id, entry_kind, amount_micro_yuan, balance_before_micro_yuan,
                    balance_after_micro_yuan, token_count, price_per_million_tokens_yuan,
                    source_reference, summary, details_json, created_at
                ) VALUES (
                    :account_id, 'initial_credit', :amount_micro_yuan, 0, :balance_after_micro_yuan,
                    NULL, NULL, :source_reference, :summary, :details_json, :created_at
                )
                ON CONFLICT (source_reference) DO NOTHING
                """
            ),
            {
                "account_id": account_id,
                "amount_micro_yuan": INITIAL_BALANCE_MICRO_YUAN,
                "balance_after_micro_yuan": INITIAL_BALANCE_MICRO_YUAN,
                "source_reference": f"initial-credit:{account_id}",
                "summary": "系统初始化余额",
                "details_json": json.dumps({}, ensure_ascii=False),
                "created_at": created_at,
            },
        )
        usage_rows = connection.execute(
            sa.text(
                """
                SELECT id, call_id, operation, total_tokens, created_at
                FROM usage_events
                WHERE account_id = :account_id
                  AND billable = TRUE
                  AND status = 'succeeded'
                ORDER BY created_at, id
                """
            ),
            {"account_id": account_id},
        ).mappings().all()
        for usage in usage_rows:
            total_tokens = int(usage["total_tokens"] or 0)
            if total_tokens <= 0:
                continue
            call_id = str(usage["call_id"])
            cost_micro_yuan = round(total_tokens * PRICE_PER_MILLION_TOKENS_YUAN)
            balance_before = balance
            balance_after = balance_before - cost_micro_yuan
            balance = balance_after
            consumed_total += cost_micro_yuan
            connection.execute(
                sa.text(
                    """
                    INSERT INTO account_balance_ledger (
                        account_id, entry_kind, amount_micro_yuan, balance_before_micro_yuan,
                        balance_after_micro_yuan, token_count, price_per_million_tokens_yuan,
                        source_reference, summary, details_json, created_at
                    ) VALUES (
                        :account_id, 'consumption', :amount_micro_yuan, :balance_before_micro_yuan,
                        :balance_after_micro_yuan, :token_count, :price_per_million_tokens_yuan,
                        :source_reference, :summary, :details_json, :created_at
                    )
                    ON CONFLICT (source_reference) DO NOTHING
                    """
                ),
                {
                    "account_id": account_id,
                    "amount_micro_yuan": -cost_micro_yuan,
                    "balance_before_micro_yuan": balance_before,
                    "balance_after_micro_yuan": balance_after,
                    "token_count": total_tokens,
                    "price_per_million_tokens_yuan": PRICE_PER_MILLION_TOKENS_YUAN,
                    "source_reference": call_id,
                    "summary": f"{usage['operation']} 扣费",
                    "details_json": json.dumps(
                        {
                            "call_id": call_id,
                            "operation": usage["operation"],
                            "total_tokens": total_tokens,
                            "price_per_million_tokens_yuan": PRICE_PER_MILLION_TOKENS_YUAN,
                            "consumption_micro_yuan": cost_micro_yuan,
                        },
                        ensure_ascii=False,
                    ),
                    "created_at": usage["created_at"] or created_at,
                },
            )
        connection.execute(
            sa.text(
                """
                UPDATE account_balances
                SET balance_micro_yuan = :balance_micro_yuan,
                    total_recharge_micro_yuan = :total_recharge_micro_yuan,
                    total_consumed_micro_yuan = :total_consumed_micro_yuan,
                    low_balance_threshold_micro_yuan = :low_balance_threshold_micro_yuan,
                    updated_at = :updated_at
                WHERE account_id = :account_id
                """
            ),
            {
                "account_id": account_id,
                "balance_micro_yuan": balance,
                "total_recharge_micro_yuan": recharge_total,
                "total_consumed_micro_yuan": consumed_total,
                "low_balance_threshold_micro_yuan": LOW_BALANCE_THRESHOLD_MICRO_YUAN,
                "updated_at": datetime.now(UTC),
            },
        )


def downgrade() -> None:
    """Drop the balance ledger tables."""

    op.drop_index("idx_account_balance_ledger_account_time", table_name="account_balance_ledger")
    op.drop_table("account_balance_ledger")
    op.drop_table("account_balances")
