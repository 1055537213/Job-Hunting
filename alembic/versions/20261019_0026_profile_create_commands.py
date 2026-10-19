"""Python-owned profile creation receipts; no sessions or profile text retained."""

import sqlalchemy as sa
from alembic import op

revision = "20261019_0026"
down_revision = "20261018_0025"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "profile_create_commands",
        sa.Column("account_id", sa.Integer, sa.ForeignKey("accounts.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("key_hash", sa.String(64), primary_key=True),
        sa.Column("payload_hash", sa.String(64), nullable=False),
        sa.Column("candidate_id", sa.Integer, sa.ForeignKey("candidate_profiles.id", ondelete="SET NULL")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("idx_profile_create_candidate", "profile_create_commands", ["candidate_id"])


def downgrade():
    op.drop_table("profile_create_commands")
