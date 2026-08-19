"""add admin action audit events

Revision ID: 20260820_0006
Revises: 20260819_0005
Create Date: 2026-08-20 00:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "20260820_0006"
down_revision = "20260819_0005"
branch_labels = None
depends_on = None


JSON_TYPE = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")
TIMESTAMP_TYPE = sa.DateTime(timezone=True)


def upgrade() -> None:
    """Create an append-only low-sensitivity admin action audit ledger."""

    op.create_table(
        "admin_audit_events",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("actor_account_id", sa.Integer),
        sa.Column("target_account_id", sa.Integer),
        sa.Column("action", sa.String(96), nullable=False),
        sa.Column("target_type", sa.String(64), nullable=False),
        sa.Column("target_id", sa.String(160)),
        sa.Column("outcome", sa.String(32), nullable=False, server_default=sa.text("'succeeded'")),
        sa.Column("summary", sa.Text, nullable=False),
        sa.Column("details_json", JSON_TYPE, nullable=False, server_default=sa.text("'{}'")),
        sa.Column("request_id", sa.String(128)),
        sa.Column("created_at", TIMESTAMP_TYPE, nullable=False),
        sa.ForeignKeyConstraint(["actor_account_id"], ["accounts.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["target_account_id"], ["accounts.id"], ondelete="SET NULL"),
        sa.CheckConstraint(
            "outcome IN ('succeeded', 'blocked', 'failed')",
            name="admin_audit_events_outcome",
        ),
    )
    op.create_index(
        "idx_admin_audit_events_actor_time",
        "admin_audit_events",
        ["actor_account_id", sa.text("created_at DESC")],
    )
    op.create_index(
        "idx_admin_audit_events_action_time",
        "admin_audit_events",
        ["action", sa.text("created_at DESC")],
    )
    op.create_index(
        "idx_admin_audit_events_created",
        "admin_audit_events",
        [sa.text("created_at DESC")],
    )


def downgrade() -> None:
    """Drop the admin action audit ledger."""

    op.drop_index("idx_admin_audit_events_created", table_name="admin_audit_events")
    op.drop_index("idx_admin_audit_events_action_time", table_name="admin_audit_events")
    op.drop_index("idx_admin_audit_events_actor_time", table_name="admin_audit_events")
    op.drop_table("admin_audit_events")
