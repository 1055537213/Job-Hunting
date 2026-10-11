"""Add transactional email-change targets to account action ledgers."""

import sqlalchemy as sa
from alembic import op


revision = "20261011_0023"
down_revision = "20261008_0022"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("account_action_tokens", sa.Column("target_email", sa.String(254)))
    op.drop_constraint(
        op.f("ck_account_action_tokens_account_action_tokens_purpose"),
        "account_action_tokens",
        type_="check",
    )
    op.create_check_constraint(
        op.f("ck_account_action_tokens_account_action_tokens_purpose"),
        "account_action_tokens",
        "purpose IN ('verify_email', 'reset_password', 'change_email')",
    )

    op.add_column(
        "account_email_outbox",
        sa.Column("target_email", sa.String(254)),
    )
    op.drop_constraint(
        op.f("ck_account_email_outbox_account_email_outbox_purpose"),
        "account_email_outbox",
        type_="check",
    )
    op.create_check_constraint(
        op.f("ck_account_email_outbox_account_email_outbox_purpose"),
        "account_email_outbox",
        "purpose IN ('verify_email', 'reset_password', 'change_email')",
    )

    op.add_column(
        "platform_account_action_emails",
        sa.Column("target_email", sa.String(254)),
    )
    op.drop_constraint(
        op.f("ck_platform_account_action_emails_platform_action_email_purpose"),
        "platform_account_action_emails",
        type_="check",
    )
    op.create_check_constraint(
        op.f("ck_platform_account_action_emails_platform_action_email_purpose"),
        "platform_account_action_emails",
        "purpose IN ('verify_email', 'reset_password', 'change_email')",
    )


def downgrade() -> None:
    op.execute(
        "DELETE FROM account_action_tokens WHERE purpose = 'change_email'"
    )
    op.execute(
        "DELETE FROM account_email_outbox WHERE purpose = 'change_email'"
    )
    op.execute(
        "DELETE FROM platform_account_action_emails WHERE purpose = 'change_email'"
    )

    for table, constraint, expression in (
        (
            "platform_account_action_emails",
            op.f("ck_platform_account_action_emails_platform_action_email_purpose"),
            "purpose IN ('verify_email', 'reset_password')",
        ),
        (
            "account_email_outbox",
            op.f("ck_account_email_outbox_account_email_outbox_purpose"),
            "purpose IN ('verify_email', 'reset_password')",
        ),
        (
            "account_action_tokens",
            op.f("ck_account_action_tokens_account_action_tokens_purpose"),
            "purpose IN ('verify_email', 'reset_password')",
        ),
    ):
        op.drop_constraint(constraint, table, type_="check")
        op.create_check_constraint(constraint, table, expression)

    op.drop_column("platform_account_action_emails", "target_email")
    op.drop_column("account_email_outbox", "target_email")
    op.drop_column("account_action_tokens", "target_email")
