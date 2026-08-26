"""Add multimodal visual knowledge assets and image embeddings.

Revision ID: 20260825_0015
Revises: 20260825_0014
Create Date: 2026-08-25 00:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "20260825_0015"
down_revision = "20260825_0014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Create source-linked visual derivatives and their pgvector index state."""

    op.create_table(
        "visual_knowledge_items",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("account_id", sa.Integer(), nullable=False),
        sa.Column("candidate_id", sa.Integer(), nullable=False),
        sa.Column("project_archive_file_id", sa.Integer()),
        sa.Column("project_collection_file_id", sa.Integer()),
        sa.Column("long_text_id", sa.Integer()),
        sa.Column("source_id", sa.String(length=128), nullable=False),
        sa.Column("source_label", sa.String(length=2048), nullable=False),
        sa.Column("page_number", sa.Integer()),
        sa.Column("media_type", sa.String(length=128), nullable=False),
        sa.Column("storage_key", sa.String(length=1024), nullable=False),
        sa.Column("file_size", sa.BigInteger(), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("width", sa.Integer(), nullable=False),
        sa.Column("height", sa.Integer(), nullable=False),
        sa.Column(
            "metadata_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("embedding", Vector()),
        sa.Column("embedding_model", sa.String(length=256)),
        sa.Column("embedding_dimensions", sa.Integer()),
        sa.Column("index_status", sa.String(length=32), nullable=False),
        sa.Column("index_error_type", sa.String(length=128)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("page_number IS NULL OR page_number > 0", name="ck_visual_knowledge_items_page"),
        sa.CheckConstraint("file_size > 0", name="ck_visual_knowledge_items_file_size"),
        sa.CheckConstraint(
            "width > 0 AND height > 0",
            name="ck_visual_knowledge_items_dimensions",
        ),
        sa.CheckConstraint(
            "embedding_dimensions IS NULL OR embedding_dimensions > 0",
            name="ck_visual_knowledge_items_embedding_dimensions",
        ),
        sa.CheckConstraint(
            "(project_archive_file_id IS NOT NULL AND project_collection_file_id IS NULL) "
            "OR (project_archive_file_id IS NULL AND project_collection_file_id IS NOT NULL)",
            name="ck_visual_knowledge_items_one_source",
        ),
        sa.CheckConstraint(
            "index_status IN ('pending', 'indexed', 'failed')",
            name="ck_visual_knowledge_items_status",
        ),
        sa.ForeignKeyConstraint(
            ["account_id"],
            ["accounts.id"],
            name="fk_visual_knowledge_items_account",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["candidate_id"],
            ["candidate_profiles.id"],
            name="fk_visual_knowledge_items_candidate",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["project_archive_file_id"],
            ["project_archive_files.id"],
            name="fk_visual_knowledge_items_archive_file",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["project_collection_file_id"],
            ["project_collection_files.id"],
            name="fk_visual_knowledge_items_collection_file",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["long_text_id"],
            ["long_texts.id"],
            name="fk_visual_knowledge_items_long_text",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_visual_knowledge_items"),
        sa.UniqueConstraint("storage_key", name="uq_visual_knowledge_items_storage_key"),
    )
    op.create_index(
        "idx_visual_knowledge_items_owner",
        "visual_knowledge_items",
        ["account_id", "candidate_id", "id"],
    )
    op.create_index(
        "uq_visual_knowledge_archive_source",
        "visual_knowledge_items",
        ["project_archive_file_id", "source_id"],
        unique=True,
        postgresql_where=sa.text("project_archive_file_id IS NOT NULL"),
    )
    op.create_index(
        "uq_visual_knowledge_collection_source",
        "visual_knowledge_items",
        ["project_collection_file_id", "source_id"],
        unique=True,
        postgresql_where=sa.text("project_collection_file_id IS NOT NULL"),
    )


def downgrade() -> None:
    """Remove visual derivatives and their vector metadata."""

    op.drop_index(
        "uq_visual_knowledge_collection_source",
        table_name="visual_knowledge_items",
    )
    op.drop_index(
        "uq_visual_knowledge_archive_source",
        table_name="visual_knowledge_items",
    )
    op.drop_index("idx_visual_knowledge_items_owner", table_name="visual_knowledge_items")
    op.drop_table("visual_knowledge_items")
