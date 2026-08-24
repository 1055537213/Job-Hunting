"""add resume export generation keys

Revision ID: 20260825_0010
Revises: 20260823_0009
Create Date: 2026-08-25 00:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "20260825_0010"
down_revision = "20260823_0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add nullable keys used to make Worker resume exports retry-safe."""

    op.add_column("resume_drafts", sa.Column("generation_key", sa.String(length=128)))
    op.create_unique_constraint(
        "uq_resume_drafts_generation_key",
        "resume_drafts",
        ["generation_key"],
    )
    op.add_column("resume_artifacts", sa.Column("generation_key", sa.String(length=128)))
    op.create_unique_constraint(
        "uq_resume_artifacts_generation_key",
        "resume_artifacts",
        ["generation_key"],
    )


def downgrade() -> None:
    """Remove Worker-only generation keys while keeping existing files."""

    op.drop_constraint(
        "uq_resume_artifacts_generation_key",
        "resume_artifacts",
        type_="unique",
    )
    op.drop_column("resume_artifacts", "generation_key")
    op.drop_constraint(
        "uq_resume_drafts_generation_key",
        "resume_drafts",
        type_="unique",
    )
    op.drop_column("resume_drafts", "generation_key")
