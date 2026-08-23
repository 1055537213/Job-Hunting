"""payment accounting foundation

Revision ID: 20260823_0009
Revises: 20260822_0008
Create Date: 2026-08-23 10:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

revision = "20260823_0009"
down_revision = "20260822_0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add immutable recharge orders, payment events and ledger provenance."""

    op.create_table(
        "recharge_orders",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("order_number", sa.String(length=64), nullable=False),
        sa.Column("account_id", sa.Integer(), nullable=False),
        sa.Column("created_by_account_id", sa.Integer(), nullable=True),
        sa.Column("amount_micro_yuan", sa.BigInteger(), nullable=False),
        sa.Column("status", sa.String(length=24), server_default="pending", nullable=False),
        sa.Column("payment_provider", sa.String(length=32), nullable=False),
        sa.Column("provider_order_id", sa.String(length=160), nullable=True),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("failure_reason", sa.Text(), nullable=True),
        sa.Column("details_json", JSONB(), server_default=sa.text("'{}'"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("paid_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("refunded_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("amount_micro_yuan > 0", name="recharge_orders_amount_positive"),
        sa.CheckConstraint(
            "status IN ('pending', 'paid', 'failed', 'cancelled', 'refunded')",
            name="recharge_orders_status",
        ),
        sa.ForeignKeyConstraint(["account_id"], ["accounts.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["created_by_account_id"],
            ["accounts.id"],
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("order_number", name="uq_recharge_orders_order_number"),
        sa.UniqueConstraint(
            "account_id",
            "idempotency_key",
            name="uq_recharge_orders_account_idempotency",
        ),
        sa.UniqueConstraint(
            "payment_provider",
            "provider_order_id",
            name="uq_recharge_orders_provider_order",
        ),
    )
    op.create_index(
        "idx_recharge_orders_account_time",
        "recharge_orders",
        ["account_id", sa.text("created_at DESC")],
    )
    op.create_index(
        "idx_recharge_orders_status_time",
        "recharge_orders",
        ["status", sa.text("created_at DESC")],
    )

    op.create_table(
        "payment_events",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("recharge_order_id", sa.Integer(), nullable=False),
        sa.Column("payment_provider", sa.String(length=32), nullable=False),
        sa.Column("provider_event_id", sa.String(length=160), nullable=False),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("processing_status", sa.String(length=24), nullable=False),
        sa.Column("signature_valid", sa.Boolean(), nullable=False),
        sa.Column("payload_sha256", sa.String(length=64), nullable=False),
        sa.Column("error_summary", sa.Text(), nullable=True),
        sa.Column("details_json", JSONB(), server_default=sa.text("'{}'"), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "processing_status IN ('received', 'processed', 'ignored', 'failed')",
            name="payment_events_processing_status",
        ),
        sa.ForeignKeyConstraint(
            ["recharge_order_id"],
            ["recharge_orders.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "payment_provider",
            "provider_event_id",
            name="uq_payment_events_provider_event",
        ),
    )
    op.create_index(
        "idx_payment_events_order_time",
        "payment_events",
        ["recharge_order_id", sa.text("received_at DESC")],
    )

    op.add_column("account_balance_ledger", sa.Column("operator_account_id", sa.Integer(), nullable=True))
    op.add_column("account_balance_ledger", sa.Column("recharge_order_id", sa.Integer(), nullable=True))
    op.create_foreign_key(
        "fk_account_balance_ledger_operator_account_id_accounts",
        "account_balance_ledger",
        "accounts",
        ["operator_account_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_foreign_key(
        "fk_account_balance_ledger_recharge_order_id_recharge_orders",
        "account_balance_ledger",
        "recharge_orders",
        ["recharge_order_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "idx_account_balance_ledger_recharge_order",
        "account_balance_ledger",
        ["recharge_order_id"],
    )


def downgrade() -> None:
    """Remove payment accounting tables and ledger provenance."""

    op.drop_index("idx_account_balance_ledger_recharge_order", table_name="account_balance_ledger")
    op.drop_constraint(
        "fk_account_balance_ledger_recharge_order_id_recharge_orders",
        "account_balance_ledger",
        type_="foreignkey",
    )
    op.drop_constraint(
        "fk_account_balance_ledger_operator_account_id_accounts",
        "account_balance_ledger",
        type_="foreignkey",
    )
    op.drop_column("account_balance_ledger", "recharge_order_id")
    op.drop_column("account_balance_ledger", "operator_account_id")
    op.drop_index("idx_payment_events_order_time", table_name="payment_events")
    op.drop_table("payment_events")
    op.drop_index("idx_recharge_orders_status_time", table_name="recharge_orders")
    op.drop_index("idx_recharge_orders_account_time", table_name="recharge_orders")
    op.drop_table("recharge_orders")
