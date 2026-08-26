"""Add email verification, action tokens, and consent records.

Revision ID: 20260826_0017
Revises: 20260826_0016
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "20260826_0017"
down_revision = "20260826_0016"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add account lifecycle state and one-time action records."""

    op.add_column("accounts", sa.Column("email_verified_at", sa.DateTime(timezone=True)))
    op.add_column("accounts", sa.Column("deleted_at", sa.DateTime(timezone=True)))
    op.execute("UPDATE accounts SET email_verified_at = created_at")

    op.create_table(
        "account_action_tokens",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("account_id", sa.Integer(), nullable=False),
        sa.Column("purpose", sa.String(length=32), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("requested_ip", sa.String(length=64)),
        sa.CheckConstraint(
            "purpose IN ('verify_email', 'reset_password')",
            name=op.f("ck_account_action_tokens_account_action_tokens_purpose"),
        ),
        sa.ForeignKeyConstraint(
            ["account_id"],
            ["accounts.id"],
            name=op.f("fk_account_action_tokens_account_id_accounts"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_account_action_tokens")),
        sa.UniqueConstraint("token_hash", name=op.f("uq_account_action_tokens_token_hash")),
    )
    op.create_index(
        "idx_account_action_tokens_account",
        "account_action_tokens",
        ["account_id", "purpose", "consumed_at"],
    )
    op.create_index(
        "idx_account_action_tokens_expiry",
        "account_action_tokens",
        ["expires_at"],
    )

    op.create_table(
        "account_consents",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("account_id", sa.Integer(), nullable=False),
        sa.Column("document_type", sa.String(length=32), nullable=False),
        sa.Column("version", sa.String(length=64), nullable=False),
        sa.Column("accepted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ip_address", sa.String(length=64)),
        sa.Column("user_agent", sa.String(length=512)),
        sa.CheckConstraint(
            "document_type IN ('terms', 'privacy')",
            name=op.f("ck_account_consents_account_consents_document_type"),
        ),
        sa.ForeignKeyConstraint(
            ["account_id"],
            ["accounts.id"],
            name=op.f("fk_account_consents_account_id_accounts"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_account_consents")),
        sa.UniqueConstraint(
            "account_id",
            "document_type",
            "version",
            name="uq_account_consents_account_document_version",
        ),
    )
    op.create_index(
        "idx_account_consents_account",
        "account_consents",
        ["account_id", "accepted_at"],
    )


def downgrade() -> None:
    """Remove account lifecycle records and state."""

    op.drop_index("idx_account_consents_account", table_name="account_consents")
    op.drop_table("account_consents")
    op.drop_index("idx_account_action_tokens_expiry", table_name="account_action_tokens")
    op.drop_index("idx_account_action_tokens_account", table_name="account_action_tokens")
    op.drop_table("account_action_tokens")
    op.drop_column("accounts", "deleted_at")
    op.drop_column("accounts", "email_verified_at")
