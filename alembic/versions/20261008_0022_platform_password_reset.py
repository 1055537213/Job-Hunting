"""Generalize the Java email ledger without invalidating existing verification links."""

import sqlalchemy as sa
from alembic import op

revision = "20261008_0022"
down_revision = "20261008_0021"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.rename_table("platform_email_verifications", "platform_account_action_emails")
    op.add_column(
        "platform_account_action_emails",
        sa.Column("purpose", sa.String(32), nullable=False, server_default="verify_email"),
    )
    op.add_column("platform_account_action_emails", sa.Column("credential_hash", sa.String(64)))
    op.create_check_constraint(
        "platform_action_email_purpose", "platform_account_action_emails",
        "purpose IN ('verify_email', 'reset_password')",
    )
    for name, columns in (
        ("due", ["purpose", "status", "next_attempt_at"]),
        ("account", ["account_id", "purpose", "created_at"]),
        ("source", ["request_source_hash", "purpose", "created_at"]),
    ):
        op.drop_index(f"idx_platform_verification_{name}", "platform_account_action_emails")
        op.create_index(f"idx_platform_action_email_{name}", "platform_account_action_emails", columns)


def downgrade() -> None:
    # Reset links cannot be interpreted by the previous verification-only service.
    op.execute("DELETE FROM platform_account_action_emails WHERE purpose = 'reset_password'")
    for name, columns in (
        ("due", ["status", "next_attempt_at"]),
        ("account", ["account_id", "created_at"]),
        ("source", ["request_source_hash", "created_at"]),
    ):
        op.drop_index(f"idx_platform_action_email_{name}", "platform_account_action_emails")
        op.create_index(f"idx_platform_verification_{name}", "platform_account_action_emails", columns)
    op.drop_constraint("platform_action_email_purpose", "platform_account_action_emails", type_="check")
    op.drop_column("platform_account_action_emails", "credential_hash")
    op.drop_column("platform_account_action_emails", "purpose")
    op.rename_table("platform_account_action_emails", "platform_email_verifications")
