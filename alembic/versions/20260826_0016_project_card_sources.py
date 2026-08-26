"""Allow one project card to retain multiple source revisions.

Revision ID: 20260826_0016
Revises: 20260825_0015
"""

from __future__ import annotations

from alembic import op

revision = "20260826_0016"
down_revision = "20260825_0015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Replace one-to-one card links with indexed one-to-many source links."""

    op.drop_constraint(
        "uq_project_archive_imports_card",
        "project_archive_imports",
        type_="unique",
    )
    op.drop_constraint(
        "uq_project_collection_card",
        "project_collection_sessions",
        type_="unique",
    )
    op.create_index(
        "idx_project_archive_imports_card",
        "project_archive_imports",
        ["project_card_id"],
    )
    op.create_index(
        "idx_project_collection_card",
        "project_collection_sessions",
        ["project_card_id"],
    )


def downgrade() -> None:
    """Restore the historical one-source-per-card constraint."""

    op.drop_index("idx_project_collection_card", table_name="project_collection_sessions")
    op.drop_index("idx_project_archive_imports_card", table_name="project_archive_imports")
    op.create_unique_constraint(
        "uq_project_collection_card",
        "project_collection_sessions",
        ["project_card_id"],
    )
    op.create_unique_constraint(
        "uq_project_archive_imports_card",
        "project_archive_imports",
        ["project_card_id"],
    )
