"""Add unified knowledge assets and immutable file versions.

Revision ID: 20260825_0012
Revises: 20260825_0011
Create Date: 2026-08-25 00:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "20260825_0012"
down_revision = "20260825_0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Create the generic asset catalog and link existing source resumes."""

    op.create_table(
        "knowledge_assets",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("account_id", sa.Integer(), nullable=False),
        sa.Column("candidate_id", sa.Integer()),
        sa.Column("asset_kind", sa.String(length=64), nullable=False),
        sa.Column("title", sa.String(length=512), nullable=False),
        sa.Column(
            "lifecycle_status",
            sa.String(length=32),
            nullable=False,
            server_default=sa.text("'active'"),
        ),
        sa.Column(
            "metadata_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "lifecycle_status IN ('active', 'archived')",
            name="ck_knowledge_assets_lifecycle",
        ),
        sa.ForeignKeyConstraint(
            ["account_id"],
            ["accounts.id"],
            name="fk_knowledge_assets_account",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["candidate_id"],
            ["candidate_profiles.id"],
            name="fk_knowledge_assets_candidate",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_knowledge_assets"),
    )
    op.create_index(
        "idx_knowledge_assets_owner",
        "knowledge_assets",
        ["account_id", "candidate_id", "id"],
    )

    op.create_table(
        "knowledge_asset_versions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("asset_id", sa.Integer(), nullable=False),
        sa.Column("version_number", sa.Integer(), nullable=False),
        sa.Column("is_current", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("original_filename", sa.String(length=512), nullable=False),
        sa.Column("storage_key", sa.String(length=1024), nullable=False),
        sa.Column("media_type", sa.String(length=128), nullable=False),
        sa.Column("file_size", sa.BigInteger(), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("source_kind", sa.String(length=32), nullable=False, server_default=sa.text("'upload'")),
        sa.Column("source_url", sa.Text()),
        sa.Column("revision_label", sa.String(length=128)),
        sa.Column(
            "processing_status",
            sa.String(length=32),
            nullable=False,
            server_default=sa.text("'uploaded'"),
        ),
        sa.Column(
            "scan_status",
            sa.String(length=32),
            nullable=False,
            server_default=sa.text("'pending'"),
        ),
        sa.Column("scan_engine", sa.String(length=64)),
        sa.Column("scan_reason", sa.Text()),
        sa.Column(
            "metadata_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "file_size >= 0",
            name="ck_kav_file_size_non_negative",
        ),
        sa.CheckConstraint(
            "processing_status IN ('uploaded', 'scanning', 'processing', 'ready', 'quarantined', 'failed')",
            name="ck_kav_processing_status",
        ),
        sa.CheckConstraint(
            "scan_status IN ('pending', 'clean', 'infected', 'error', 'not_required')",
            name="ck_kav_scan_status",
        ),
        sa.CheckConstraint(
            "version_number > 0",
            name="ck_kav_version_positive",
        ),
        sa.ForeignKeyConstraint(
            ["asset_id"],
            ["knowledge_assets.id"],
            name="fk_kav_asset",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_knowledge_asset_versions"),
        sa.UniqueConstraint(
            "asset_id",
            "sha256",
            name="uq_knowledge_asset_versions_content",
        ),
        sa.UniqueConstraint(
            "asset_id",
            "version_number",
            name="uq_knowledge_asset_versions_number",
        ),
        sa.UniqueConstraint(
            "storage_key",
            name="uq_knowledge_asset_versions_storage_key",
        ),
    )
    op.create_index(
        "idx_knowledge_asset_versions_asset",
        "knowledge_asset_versions",
        ["asset_id", "version_number"],
    )
    op.create_index(
        "uq_knowledge_asset_versions_current",
        "knowledge_asset_versions",
        ["asset_id"],
        unique=True,
        postgresql_where=sa.text("is_current IS true"),
    )

    op.add_column("resume_artifacts", sa.Column("knowledge_asset_id", sa.Integer()))
    op.add_column("resume_artifacts", sa.Column("knowledge_asset_version_id", sa.Integer()))
    op.create_foreign_key(
        "fk_resume_artifacts_asset",
        "resume_artifacts",
        "knowledge_assets",
        ["knowledge_asset_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_foreign_key(
        "fk_resume_artifacts_asset_version",
        "resume_artifacts",
        "knowledge_asset_versions",
        ["knowledge_asset_version_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "idx_resume_artifacts_knowledge_asset",
        "resume_artifacts",
        ["knowledge_asset_id", "knowledge_asset_version_id"],
    )

    # Existing source resumes become one-version assets without rewriting or moving their objects.
    op.execute(
        """
        DO $$
        DECLARE
            artifact RECORD;
            new_asset_id INTEGER;
            new_version_id INTEGER;
        BEGIN
            FOR artifact IN
                SELECT * FROM resume_artifacts
                WHERE artifact_type = 'source' AND knowledge_asset_id IS NULL
                ORDER BY id
            LOOP
                INSERT INTO knowledge_assets (
                    account_id, candidate_id, asset_kind, title, lifecycle_status,
                    metadata_json, created_at, updated_at
                ) VALUES (
                    artifact.account_id, artifact.candidate_id, 'resume', artifact.original_filename,
                    'active', jsonb_build_object('migrated_from', 'resume_artifacts'),
                    artifact.created_at, artifact.created_at
                ) RETURNING id INTO new_asset_id;

                INSERT INTO knowledge_asset_versions (
                    asset_id, version_number, is_current, original_filename, storage_key,
                    media_type, file_size, sha256, source_kind, processing_status,
                    scan_status, scan_engine, scan_reason, metadata_json, created_at
                ) VALUES (
                    new_asset_id, 1, true, artifact.original_filename, artifact.storage_key,
                    artifact.media_type, artifact.file_size, artifact.sha256, 'migration',
                    artifact.status, COALESCE(artifact.scan_status, 'clean'), artifact.scan_engine,
                    artifact.scan_reason, jsonb_build_object('resume_artifact_id', artifact.id),
                    artifact.created_at
                ) RETURNING id INTO new_version_id;

                UPDATE resume_artifacts
                SET knowledge_asset_id = new_asset_id,
                    knowledge_asset_version_id = new_version_id
                WHERE id = artifact.id;
            END LOOP;
        END $$;
        """
    )


def downgrade() -> None:
    """Remove generic asset metadata while leaving resume files untouched."""

    op.drop_index("idx_resume_artifacts_knowledge_asset", table_name="resume_artifacts")
    op.drop_constraint(
        "fk_resume_artifacts_asset_version",
        "resume_artifacts",
        type_="foreignkey",
    )
    op.drop_constraint(
        "fk_resume_artifacts_asset",
        "resume_artifacts",
        type_="foreignkey",
    )
    op.drop_column("resume_artifacts", "knowledge_asset_version_id")
    op.drop_column("resume_artifacts", "knowledge_asset_id")
    op.drop_index("uq_knowledge_asset_versions_current", table_name="knowledge_asset_versions")
    op.drop_index("idx_knowledge_asset_versions_asset", table_name="knowledge_asset_versions")
    op.drop_table("knowledge_asset_versions")
    op.drop_index("idx_knowledge_assets_owner", table_name="knowledge_assets")
    op.drop_table("knowledge_assets")
