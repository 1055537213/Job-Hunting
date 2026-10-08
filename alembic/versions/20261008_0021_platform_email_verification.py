"""Add the Java-owned email verification delivery ledger."""

import sqlalchemy as sa
from alembic import op

revision = "20261008_0021"
down_revision = "20260829_0020"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "platform_email_verifications",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column(
            "account_id",
            sa.Integer,
            sa.ForeignKey("accounts.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("recipient_email", sa.String(254), nullable=False),
        sa.Column("delivery_key", sa.String(64), nullable=False, unique=True),
        sa.Column("token_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("request_source_hash", sa.String(64)),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True)),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("attempt_count", sa.Integer, nullable=False),
        sa.Column("max_attempts", sa.Integer, nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("claimed_at", sa.DateTime(timezone=True)),
        sa.Column("claim_key", sa.String(64)),
        sa.Column("sent_at", sa.DateTime(timezone=True)),
        sa.Column("last_error_type", sa.String(128)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('pending', 'sending', 'retrying', 'sent', 'failed', 'cancelled')",
            name="platform_verification_status",
        ),
        sa.CheckConstraint(
            "attempt_count >= 0 AND max_attempts > 0 AND attempt_count <= max_attempts",
            name="platform_verification_attempts",
        ),
    )
    for name, columns in (
        ("due", ["status", "next_attempt_at"]),
        ("account", ["account_id", "created_at"]),
        ("source", ["request_source_hash", "created_at"]),
    ):
        op.create_index(
            f"idx_platform_verification_{name}", "platform_email_verifications", columns
        )


def downgrade() -> None:
    op.drop_table("platform_email_verifications")
