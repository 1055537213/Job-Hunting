"""Add project archive imports and routed file manifests.

Revision ID: 20260825_0013
Revises: 20260825_0012
Create Date: 2026-08-25 00:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "20260825_0013"
down_revision = "20260825_0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Create project-package ownership, lifecycle, and file-manifest tables."""

    op.create_table(
        "project_archive_imports",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("account_id", sa.Integer(), nullable=False),
        sa.Column("candidate_id", sa.Integer(), nullable=False),
        sa.Column("knowledge_asset_id", sa.Integer(), nullable=False),
        sa.Column("knowledge_asset_version_id", sa.Integer(), nullable=False),
        sa.Column("project_card_id", sa.Integer()),
        sa.Column("source_type", sa.String(length=64), nullable=False),
        sa.Column("source_url", sa.Text()),
        sa.Column("source_ref", sa.String(length=255)),
        sa.Column("original_filename", sa.String(length=512), nullable=False),
        sa.Column("content_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("error_summary", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('uploaded', 'processing', 'ready', 'failed', 'quarantined')",
            name="ck_project_archive_imports_status",
        ),
        sa.ForeignKeyConstraint(
            ["account_id"],
            ["accounts.id"],
            name="fk_project_archive_imports_account",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["candidate_id"],
            ["candidate_profiles.id"],
            name="fk_project_archive_imports_candidate",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["knowledge_asset_id"],
            ["knowledge_assets.id"],
            name="fk_project_archive_imports_asset",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["knowledge_asset_version_id"],
            ["knowledge_asset_versions.id"],
            name="fk_project_archive_imports_asset_version",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["project_card_id"],
            ["project_experience_cards.id"],
            name="fk_project_archive_imports_card",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_project_archive_imports"),
        sa.UniqueConstraint("knowledge_asset_id", name="uq_project_archive_imports_asset"),
        sa.UniqueConstraint(
            "knowledge_asset_version_id",
            name="uq_project_archive_imports_asset_version",
        ),
        sa.UniqueConstraint("project_card_id", name="uq_project_archive_imports_card"),
        sa.UniqueConstraint(
            "candidate_id",
            "content_fingerprint",
            name="uq_project_archive_candidate_content",
        ),
    )
    op.create_index(
        "idx_project_archive_imports_owner",
        "project_archive_imports",
        ["account_id", "candidate_id", "id"],
    )

    op.create_table(
        "project_archive_files",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("project_archive_id", sa.Integer(), nullable=False),
        sa.Column("relative_path", sa.String(length=2048), nullable=False),
        sa.Column("file_kind", sa.String(length=64), nullable=False),
        sa.Column("media_type", sa.String(length=128), nullable=False),
        sa.Column("file_size", sa.BigInteger(), nullable=False),
        sa.Column("compressed_size", sa.BigInteger(), nullable=False),
        sa.Column("sha256", sa.String(length=64)),
        sa.Column("analysis_status", sa.String(length=32), nullable=False),
        sa.Column("skip_reason", sa.String(length=128)),
        sa.Column(
            "metadata_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.CheckConstraint("compressed_size >= 0", name="ck_project_archive_files_compressed_size"),
        sa.CheckConstraint("file_size >= 0", name="ck_project_archive_files_size"),
        sa.CheckConstraint(
            "analysis_status IN ('analyzed', 'pending_parser', 'skipped', 'unsupported', 'failed')",
            name="ck_project_archive_files_status",
        ),
        sa.ForeignKeyConstraint(
            ["project_archive_id"],
            ["project_archive_imports.id"],
            name="fk_project_archive_files_import",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_project_archive_files"),
        sa.UniqueConstraint(
            "project_archive_id",
            "relative_path",
            name="uq_project_archive_files_path",
        ),
    )
    op.create_index(
        "idx_project_archive_files_import",
        "project_archive_files",
        ["project_archive_id", "id"],
    )


def downgrade() -> None:
    """Remove project-package adapters while preserving generic assets."""

    op.drop_index("idx_project_archive_files_import", table_name="project_archive_files")
    op.drop_table("project_archive_files")
    op.drop_index("idx_project_archive_imports_owner", table_name="project_archive_imports")
    op.drop_table("project_archive_imports")
