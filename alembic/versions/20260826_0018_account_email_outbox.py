"""Add durable account email outbox.

Revision ID: 20260826_0018
Revises: 20260826_0017
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "20260826_0018"
down_revision = "20260826_0017"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Create the transactional email delivery ledger."""

    op.create_table(
        "account_email_outbox",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("account_id", sa.Integer(), nullable=False),
        sa.Column("action_token_id", sa.Integer(), nullable=False),
        sa.Column("purpose", sa.String(length=32), nullable=False),
        sa.Column("recipient_email", sa.String(length=254), nullable=False),
        sa.Column("delivery_key", sa.String(length=64), nullable=False),
        sa.Column("request_source_hash", sa.String(length=64)),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("attempt_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("max_attempts", sa.Integer(), nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("claimed_at", sa.DateTime(timezone=True)),
        sa.Column("sent_at", sa.DateTime(timezone=True)),
        sa.Column("last_error_type", sa.String(length=128)),
        sa.Column("last_error_summary", sa.String(length=500)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "purpose IN ('verify_email', 'reset_password')",
            name=op.f("ck_account_email_outbox_account_email_outbox_purpose"),
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'sending', 'retrying', 'sent', 'failed', 'cancelled')",
            name=op.f("ck_account_email_outbox_account_email_outbox_status"),
        ),
        sa.CheckConstraint(
            "attempt_count >= 0 AND max_attempts > 0 AND attempt_count <= max_attempts",
            name=op.f("ck_account_email_outbox_account_email_outbox_attempts"),
        ),
        sa.ForeignKeyConstraint(
            ["account_id"],
            ["accounts.id"],
            name=op.f("fk_account_email_outbox_account_id_accounts"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["action_token_id"],
            ["account_action_tokens.id"],
            name=op.f("fk_account_email_outbox_action_token_id_account_action_tokens"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_account_email_outbox")),
        sa.UniqueConstraint(
            "action_token_id",
            name=op.f("uq_account_email_outbox_action_token_id"),
        ),
        sa.UniqueConstraint(
            "delivery_key",
            name=op.f("uq_account_email_outbox_delivery_key"),
        ),
    )
    op.create_index(
        "idx_account_email_outbox_due",
        "account_email_outbox",
        ["status", "next_attempt_at", "id"],
    )
    op.create_index(
        "idx_account_email_outbox_account",
        "account_email_outbox",
        ["account_id", "created_at"],
    )
    op.create_index(
        "idx_account_email_outbox_source",
        "account_email_outbox",
        ["request_source_hash", "created_at"],
    )


def downgrade() -> None:
    """Remove the transactional email delivery ledger."""

    op.drop_index("idx_account_email_outbox_source", table_name="account_email_outbox")
    op.drop_index("idx_account_email_outbox_account", table_name="account_email_outbox")
    op.drop_index("idx_account_email_outbox_due", table_name="account_email_outbox")
    op.drop_table("account_email_outbox")
