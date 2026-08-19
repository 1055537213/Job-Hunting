"""add tool call audit traces

Revision ID: 20260819_0005
Revises: 20260816_0004
Create Date: 2026-08-19 00:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "20260819_0005"
down_revision = "20260816_0004"
branch_labels = None
depends_on = None


JSON_TYPE = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")
TIMESTAMP_TYPE = sa.DateTime(timezone=True)


def upgrade() -> None:
    """Create the short-retention admin audit table for real tool calls."""

    op.create_table(
        "tool_call_traces",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("account_id", sa.Integer, nullable=False),
        sa.Column("candidate_id", sa.Integer),
        sa.Column("session_id", sa.String(128)),
        sa.Column("root_request_id", sa.String(128), nullable=False),
        sa.Column("title", sa.String(256), nullable=False),
        sa.Column("status", sa.String(32), nullable=False, server_default=sa.text("'running'")),
        sa.Column("source", sa.String(32), nullable=False, server_default=sa.text("'chat'")),
        sa.Column("step_count", sa.Integer, nullable=False, server_default=sa.text("0")),
        sa.Column("attempt_count", sa.Integer, nullable=False, server_default=sa.text("0")),
        sa.Column("last_step_name", sa.String(128)),
        sa.Column("last_error_summary", sa.Text),
        sa.Column("trace_json", JSON_TYPE, nullable=False, server_default=sa.text("'{}'")),
        sa.Column("created_at", TIMESTAMP_TYPE, nullable=False),
        sa.Column("started_at", TIMESTAMP_TYPE),
        sa.Column("finished_at", TIMESTAMP_TYPE),
        sa.Column("updated_at", TIMESTAMP_TYPE, nullable=False),
        sa.ForeignKeyConstraint(["account_id"], ["accounts.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["candidate_id"], ["candidate_profiles.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("root_request_id", name="uq_tool_call_traces_root_request_id"),
        sa.CheckConstraint(
            "status IN ('running', 'waiting_confirmation', 'completed', 'failed', 'cancelled')",
            name="tool_call_traces_status",
        ),
        sa.CheckConstraint("step_count >= 0", name="tool_call_traces_step_count_non_negative"),
        sa.CheckConstraint("attempt_count >= 0", name="tool_call_traces_attempt_count_non_negative"),
    )
    op.create_index(
        "idx_tool_call_traces_account_time",
        "tool_call_traces",
        ["account_id", "created_at"],
    )
    op.create_index(
        "idx_tool_call_traces_account_update",
        "tool_call_traces",
        ["account_id", sa.text("updated_at DESC")],
    )
    op.create_index("idx_tool_call_traces_request", "tool_call_traces", ["root_request_id"])


def downgrade() -> None:
    """Drop the tool-call audit table and indexes."""

    op.drop_index("idx_tool_call_traces_request", table_name="tool_call_traces")
    op.drop_index("idx_tool_call_traces_account_update", table_name="tool_call_traces")
    op.drop_index("idx_tool_call_traces_account_time", table_name="tool_call_traces")
    op.drop_table("tool_call_traces")
