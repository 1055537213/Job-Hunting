"""Persist profile deletion tombstones and resumable object manifests."""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from alembic import op

revision = "20261019_0027"
down_revision = "20261019_0026"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "profile_deletions",
        sa.Column("account_id", sa.Integer, sa.ForeignKey("accounts.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("candidate_id", sa.Integer, primary_key=True),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("storage_keys_json", postgresql.JSONB, nullable=False),
        sa.Column("attempts", sa.Integer, nullable=False, server_default="0"),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("lease_token", sa.String(64)),
        sa.Column("lease_until", sa.DateTime(timezone=True)),
        sa.Column("last_error", sa.String(256)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("status IN ('pending', 'completed')", name="profile_deletions_status"),
        sa.CheckConstraint("attempts >= 0", name="profile_deletions_attempts"),
    )
    op.create_index("idx_profile_deletions_due", "profile_deletions", ["status", "next_attempt_at"])


def downgrade():
    op.drop_table("profile_deletions")
