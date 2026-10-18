"""Persist Java-owned deletion intent atomically with account admission."""

import sqlalchemy as sa
from alembic import op

revision = "20261018_0025"
down_revision = "20261017_0024"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "platform_account_deletion_requests",
        sa.Column("account_id", sa.Integer, sa.ForeignKey("accounts.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("request_key", sa.String(64), nullable=False, unique=True),
        sa.Column("task_key", sa.String(64), nullable=False, unique=True),
        sa.Column("request_id", sa.String(128)),
        sa.Column("next_dispatch_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
    )
    op.create_index("idx_platform_deletion_due", "platform_account_deletion_requests", ["completed_at", "next_dispatch_at"])
    # Only an explicit historical Java admission audit is authorization to resume cleanup.
    # Administratively disabled accounts without that audit must never be auto-deleted.
    op.execute("""
        INSERT INTO platform_account_deletion_requests(account_id,request_key,task_key,created_at)
        SELECT a.id, md5('deletion-request:' || a.id::text || ':' || e.id::text),
               COALESCE((SELECT t.task_key FROM background_tasks t
                         WHERE t.account_id=a.id AND t.task_type='account_deletion'
                         ORDER BY t.id DESC LIMIT 1),
                        md5('deletion-task:' || a.id::text || ':' || e.id::text)), e.created_at
        FROM accounts a JOIN LATERAL (
            SELECT id,created_at FROM admin_audit_events WHERE target_account_id=a.id
              AND action='account.deletion_requested' ORDER BY id DESC LIMIT 1
        ) e ON TRUE
        WHERE a.status='disabled'
          AND a.deleted_at IS NULL AND a.role='user'
          AND NOT EXISTS(SELECT 1 FROM admin_audit_events later WHERE later.target_account_id=a.id
                         AND later.id>e.id AND later.action='account.status_updated')
    """)


def downgrade():
    op.drop_table("platform_account_deletion_requests")
