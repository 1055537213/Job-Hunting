"""Persist GitHub project submission receipts and recoverable dispatch intents."""

from alembic import op
import sqlalchemy as sa

revision = "20261026_0029"
down_revision = "20261026_0028"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "project_submissions",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("command_id", sa.String(64), nullable=False, unique=True),
        sa.Column("account_id", sa.Integer, sa.ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False),
        sa.Column("candidate_id", sa.Integer, sa.ForeignKey("candidate_profiles.id", ondelete="CASCADE"), nullable=False),
        sa.Column("key_hash", sa.String(64)),
        sa.Column("payload_hash", sa.String(64), nullable=False),
        sa.Column("task_key", sa.String(64), sa.ForeignKey("background_tasks.task_key", ondelete="CASCADE"), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("attempts", sa.Integer, nullable=False, server_default="0"),
        sa.Column("next_dispatch_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("account_id", "key_hash", name="uq_project_submission_key"),
        sa.CheckConstraint("status IN ('pending','dispatched','superseded')", name="project_submission_status"),
        sa.CheckConstraint("attempts >= 0", name="project_submission_attempts"),
    )
    op.create_index("idx_project_submissions_due", "project_submissions", ["status", "next_dispatch_at", "id"])
    op.create_index("idx_project_submissions_task", "project_submissions", ["task_key"])


def downgrade():
    op.drop_table("project_submissions")
