"""Add halfvec HNSW indexes for the production 2560-dimensional RAG space.

Revision ID: 20260829_0020
Revises: 20260828_0019
"""

from __future__ import annotations

from alembic import op

revision = "20260829_0020"
down_revision = "20260828_0019"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Index full-precision stored vectors through halfvec ANN expressions."""

    if op.get_bind().dialect.name != "postgresql":
        return
    with op.get_context().autocommit_block():
        op.execute(
            "CREATE INDEX CONCURRENTLY IF NOT EXISTS "
            "idx_rag_chunks_embedding_halfvec_hnsw_2560 "
            "ON rag_chunks USING hnsw "
            "((embedding::halfvec(2560)) halfvec_cosine_ops) "
            "WITH (m = 32, ef_construction = 128) "
            "WHERE embedding IS NOT NULL AND embedding_dimensions = 2560"
        )
        op.execute(
            "CREATE INDEX CONCURRENTLY IF NOT EXISTS "
            "idx_visual_knowledge_embedding_halfvec_hnsw_2560 "
            "ON visual_knowledge_items USING hnsw "
            "((embedding::halfvec(2560)) halfvec_cosine_ops) "
            "WITH (m = 32, ef_construction = 128) "
            "WHERE embedding IS NOT NULL "
            "AND embedding_dimensions = 2560 "
            "AND index_status = 'indexed'"
        )


def downgrade() -> None:
    """Remove the production-space ANN indexes without touching stored vectors."""

    if op.get_bind().dialect.name != "postgresql":
        return
    with op.get_context().autocommit_block():
        op.execute(
            "DROP INDEX CONCURRENTLY IF EXISTS "
            "idx_visual_knowledge_embedding_halfvec_hnsw_2560"
        )
        op.execute(
            "DROP INDEX CONCURRENTLY IF EXISTS "
            "idx_rag_chunks_embedding_halfvec_hnsw_2560"
        )
