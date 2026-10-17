"""Persist administrator task intents and queue dispatch acknowledgements."""

import sqlalchemy as sa
from alembic import op

revision = "20261017_0024"
down_revision = "20261011_0023"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "task_commands",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("command_id", sa.String(64), nullable=False, unique=True),
        sa.Column("actor_account_id", sa.Integer, sa.ForeignKey("accounts.id", ondelete="SET NULL")),
        sa.Column("operation", sa.String(16), nullable=False),
        sa.Column("task_key", sa.String(64), sa.ForeignKey("background_tasks.task_key", ondelete="CASCADE"), nullable=False),
        sa.Column("request_id", sa.String(128), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default=sa.text("'pending'")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("operation IN ('probe','retry')", name="task_command_operation"),
        sa.CheckConstraint("status IN ('pending','dispatched','superseded')", name="task_command_status"),
    )
    op.create_index("idx_task_commands_pending", "task_commands", ["status", "id"])
    op.create_index("idx_task_commands_task", "task_commands", ["task_key", "id"])


def downgrade():
    op.drop_table("task_commands")
