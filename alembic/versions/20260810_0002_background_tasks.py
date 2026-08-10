"""Add durable background task state for Redis/Celery workers.

Revision ID: 20260810_0002
Revises: 20260807_0001
Create Date: 2026-08-10 00:00:00

The PostgreSQL table is the authoritative task state. Redis only carries a task key.
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision = "20260810_0002"
down_revision = "20260807_0001"
branch_labels = None
depends_on = None


JSON_TYPE = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")
TIMESTAMP_TYPE = sa.DateTime(timezone=True)


def upgrade() -> None:
    """Create the durable background task state table and owner indexes."""

    op.create_table(
        "background_tasks",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("task_key", sa.String(64), nullable=False),
        sa.Column("account_id", sa.Integer, nullable=False),
        sa.Column("candidate_id", sa.Integer),
        sa.Column("session_id", sa.String(128)),
        sa.Column("task_type", sa.String(128), nullable=False),
        sa.Column("status", sa.String(32), nullable=False, server_default=sa.text("'queued'")),
        sa.Column("progress", sa.Integer, nullable=False, server_default=sa.text("0")),
        sa.Column("attempt", sa.Integer, nullable=False, server_default=sa.text("0")),
        sa.Column("max_attempts", sa.Integer, nullable=False, server_default=sa.text("3")),
        sa.Column("idempotency_key", sa.String(128)),
        sa.Column("payload_json", JSON_TYPE, nullable=False, server_default=sa.text("'{}'")),
        sa.Column("result_json", JSON_TYPE, nullable=False, server_default=sa.text("'{}'")),
        sa.Column("error_summary", sa.Text),
        sa.Column("created_at", TIMESTAMP_TYPE, nullable=False),
        sa.Column("started_at", TIMESTAMP_TYPE),
        sa.Column("finished_at", TIMESTAMP_TYPE),
        sa.Column("updated_at", TIMESTAMP_TYPE, nullable=False),
        sa.ForeignKeyConstraint(["account_id"], ["accounts.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["candidate_id"], ["candidate_profiles.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("task_key", name="uq_background_tasks_task_key"),
        sa.UniqueConstraint("account_id", "idempotency_key", name="uq_background_tasks_idempotency"),
        sa.CheckConstraint(
            "status IN ('queued', 'running', 'succeeded', 'failed', 'cancelled')",
            name="background_tasks_status",
        ),
        sa.CheckConstraint("progress >= 0 AND progress <= 100", name="background_tasks_progress_range"),
        sa.CheckConstraint("attempt >= 0", name="background_tasks_attempt_non_negative"),
        sa.CheckConstraint("max_attempts > 0", name="background_tasks_max_attempts_positive"),
    )
    op.create_index(
        "idx_background_tasks_account_status",
        "background_tasks",
        ["account_id", "status", sa.text("updated_at DESC")],
    )
    op.create_index(
        "idx_background_tasks_candidate",
        "background_tasks",
        ["candidate_id", sa.text("updated_at DESC")],
    )


def downgrade() -> None:
    """Drop the task table and its indexes."""

    op.drop_index("idx_background_tasks_candidate", table_name="background_tasks")
    op.drop_index("idx_background_tasks_account_status", table_name="background_tasks")
    op.drop_table("background_tasks")
