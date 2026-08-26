"""Add persistent upload security scan state."""

from alembic import op
import sqlalchemy as sa


revision = "20260825_0011"
down_revision = "20260825_0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "resume_artifacts",
        sa.Column(
            "scan_status",
            sa.String(length=32),
            nullable=False,
            server_default=sa.text("'clean'"),
        ),
    )
    op.add_column("resume_artifacts", sa.Column("scan_engine", sa.String(length=64)))
    op.add_column("resume_artifacts", sa.Column("scan_reason", sa.Text))


def downgrade() -> None:
    op.drop_column("resume_artifacts", "scan_reason")
    op.drop_column("resume_artifacts", "scan_engine")
    op.drop_column("resume_artifacts", "scan_status")
