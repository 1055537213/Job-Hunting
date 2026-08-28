"""Add a trigram index for exact numeric and identifier RAG fallback search.

Revision ID: 20260828_0019
Revises: 20260826_0018
"""

from __future__ import annotations

from alembic import op

revision = "20260828_0019"
down_revision = "20260826_0018"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Allow bounded ILIKE fallback queries to use a PostgreSQL trigram index."""

    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm WITH SCHEMA public")
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_rag_chunks_content_trgm "
        "ON rag_chunks USING gin (content gin_trgm_ops)"
    )


def downgrade() -> None:
    """Remove the exact-match acceleration index."""

    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute("DROP INDEX IF EXISTS idx_rag_chunks_content_trgm")
