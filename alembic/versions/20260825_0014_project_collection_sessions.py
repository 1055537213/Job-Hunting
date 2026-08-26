"""Add manifest-first local project collection and extracted project evidence.

Revision ID: 20260825_0014
Revises: 20260825_0013
Create Date: 2026-08-25 00:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "20260825_0014"
down_revision = "20260825_0013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Create collection sessions and connect extracted evidence to source files."""

    op.add_column(
        "project_archive_files",
        sa.Column("long_text_id", sa.Integer()),
    )
    op.add_column(
        "project_archive_files",
        sa.Column("extraction_method", sa.String(length=64)),
    )
    op.add_column(
        "project_archive_files",
        sa.Column("text_length", sa.Integer(), nullable=False, server_default=sa.text("0")),
    )
    op.create_foreign_key(
        "fk_project_archive_files_long_text",
        "project_archive_files",
        "long_texts",
        ["long_text_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_check_constraint(
        "ck_project_archive_files_text_length",
        "project_archive_files",
        "text_length >= 0",
    )

    op.create_table(
        "project_collection_sessions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("account_id", sa.Integer(), nullable=False),
        sa.Column("candidate_id", sa.Integer(), nullable=False),
        sa.Column("project_card_id", sa.Integer()),
        sa.Column("project_name", sa.String(length=256), nullable=False),
        sa.Column("source_type", sa.String(length=64), nullable=False),
        sa.Column("manifest_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("preserve_originals", sa.Boolean(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("file_count", sa.Integer(), nullable=False),
        sa.Column("selected_file_count", sa.Integer(), nullable=False),
        sa.Column("uploaded_file_count", sa.Integer(), nullable=False),
        sa.Column("total_size", sa.BigInteger(), nullable=False),
        sa.Column("selected_size", sa.BigInteger(), nullable=False),
        sa.Column("error_summary", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("file_count >= 0", name="ck_project_collection_file_count"),
        sa.CheckConstraint(
            "selected_file_count >= 0",
            name="ck_project_collection_selected_count",
        ),
        sa.CheckConstraint(
            "uploaded_file_count >= 0",
            name="ck_project_collection_uploaded_count",
        ),
        sa.CheckConstraint("total_size >= 0", name="ck_project_collection_total_size"),
        sa.CheckConstraint("selected_size >= 0", name="ck_project_collection_selected_size"),
        sa.CheckConstraint(
            "status IN ('planned', 'uploading', 'processing', 'ready', 'failed', 'cancelled')",
            name="ck_project_collection_status",
        ),
        sa.ForeignKeyConstraint(
            ["account_id"],
            ["accounts.id"],
            name="fk_project_collection_account",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["candidate_id"],
            ["candidate_profiles.id"],
            name="fk_project_collection_candidate",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["project_card_id"],
            ["project_experience_cards.id"],
            name="fk_project_collection_card",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_project_collection_sessions"),
        sa.UniqueConstraint("project_card_id", name="uq_project_collection_card"),
        sa.UniqueConstraint(
            "candidate_id",
            "manifest_fingerprint",
            name="uq_project_collection_candidate_manifest",
        ),
    )
    op.create_index(
        "idx_project_collection_owner",
        "project_collection_sessions",
        ["account_id", "candidate_id", "id"],
    )

    op.create_table(
        "project_collection_files",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("collection_id", sa.Integer(), nullable=False),
        sa.Column("relative_path", sa.String(length=2048), nullable=False),
        sa.Column("file_kind", sa.String(length=64), nullable=False),
        sa.Column("media_type", sa.String(length=128), nullable=False),
        sa.Column("file_size", sa.BigInteger(), nullable=False),
        sa.Column("client_sha256", sa.String(length=64)),
        sa.Column("server_sha256", sa.String(length=64)),
        sa.Column("selection_status", sa.String(length=32), nullable=False),
        sa.Column("selection_reason", sa.String(length=256), nullable=False),
        sa.Column("extraction_method", sa.String(length=64)),
        sa.Column("text_length", sa.Integer(), nullable=False),
        sa.Column("long_text_id", sa.Integer()),
        sa.Column("storage_key", sa.String(length=1024)),
        sa.Column("knowledge_asset_id", sa.Integer()),
        sa.Column("knowledge_asset_version_id", sa.Integer()),
        sa.Column(
            "metadata_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("file_size >= 0", name="ck_project_collection_files_size"),
        sa.CheckConstraint("text_length >= 0", name="ck_project_collection_files_text"),
        sa.CheckConstraint(
            "selection_status IN ('selected', 'skipped', 'uploaded', 'analyzed', 'failed')",
            name="ck_project_collection_files_status",
        ),
        sa.ForeignKeyConstraint(
            ["collection_id"],
            ["project_collection_sessions.id"],
            name="fk_project_collection_files_session",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["long_text_id"],
            ["long_texts.id"],
            name="fk_project_collection_files_long_text",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["knowledge_asset_id"],
            ["knowledge_assets.id"],
            name="fk_project_collection_files_asset",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["knowledge_asset_version_id"],
            ["knowledge_asset_versions.id"],
            name="fk_project_collection_files_asset_version",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_project_collection_files"),
        sa.UniqueConstraint(
            "collection_id",
            "relative_path",
            name="uq_project_collection_files_path",
        ),
    )
    op.create_index(
        "idx_project_collection_files_session",
        "project_collection_files",
        ["collection_id", "selection_status", "id"],
    )


def downgrade() -> None:
    """Remove manifest-first collection state and archive evidence links."""

    op.drop_index("idx_project_collection_files_session", table_name="project_collection_files")
    op.drop_table("project_collection_files")
    op.drop_index("idx_project_collection_owner", table_name="project_collection_sessions")
    op.drop_table("project_collection_sessions")
    op.drop_constraint(
        "ck_project_archive_files_text_length",
        "project_archive_files",
        type_="check",
    )
    op.drop_constraint(
        "fk_project_archive_files_long_text",
        "project_archive_files",
        type_="foreignkey",
    )
    op.drop_column("project_archive_files", "text_length")
    op.drop_column("project_archive_files", "extraction_method")
    op.drop_column("project_archive_files", "long_text_id")
