"""Persist local collection cancellation receipts and attachment cleanup leases."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "20261026_0030"
down_revision = "20261026_0029"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("project_collection_deletions",
        sa.Column("account_id", sa.Integer, sa.ForeignKey("accounts.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("collection_id", sa.Integer, primary_key=True),
        sa.Column("candidate_id", sa.Integer, nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("storage_keys_json", postgresql.JSONB, nullable=False),
        sa.Column("attempts", sa.Integer, nullable=False, server_default="0"),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("lease_token", sa.String(64)),
        sa.Column("lease_until", sa.DateTime(timezone=True)),
        sa.Column("last_error", sa.String(256)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("status IN ('pending', 'completed')", name="project_collection_deletions_status"),
        sa.CheckConstraint("attempts >= 0", name="project_collection_deletions_attempts"))
    op.create_index("idx_project_collection_deletions_due", "project_collection_deletions", ["status", "next_attempt_at"])


def downgrade():
    op.drop_table("project_collection_deletions")
