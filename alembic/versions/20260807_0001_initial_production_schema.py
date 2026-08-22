"""Create the initial production schema.

Revision ID: 20260807_0001
Revises: None
Create Date: 2026-08-07 00:00:00

This revision is intentionally explicit instead of calling metadata.create_all().
Future metadata changes must be represented by new revisions rather than changing
the meaning of this historical migration.
"""

from __future__ import annotations

import sqlalchemy as sa
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision = "20260807_0001"
down_revision = None
branch_labels = None
depends_on = None


# PostgreSQL 使用 JSONB 和 pgvector 类型；这条历史 migration 只面向生产 PostgreSQL。
JSON_TYPE = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")
VECTOR_TYPE = sa.JSON().with_variant(Vector(), "postgresql")
TIMESTAMP_TYPE = sa.DateTime(timezone=True)


def upgrade() -> None:
    """Create the frozen baseline schema and enable pgvector when available."""

    if op.get_bind().dialect.name == "postgresql":
        # 测试和租户迁移会覆盖 search_path；扩展必须固定在共享 public schema，
        # 否则 VECTOR 类型会被装进临时 schema，其他迁移连接将无法解析。
        op.execute("CREATE EXTENSION IF NOT EXISTS vector WITH SCHEMA public")
        op.execute("ALTER EXTENSION vector SET SCHEMA public")

    op.create_table(
        "accounts",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("email", sa.String(254), nullable=False),
        sa.Column("password_hash", sa.Text, nullable=False),
        sa.Column("display_name", sa.String(128)),
        sa.Column("role", sa.String(32), nullable=False, server_default=sa.text("'user'")),
        sa.Column("status", sa.String(32), nullable=False, server_default=sa.text("'active'")),
        sa.Column("must_change_password", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("created_at", TIMESTAMP_TYPE, nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("updated_at", TIMESTAMP_TYPE, nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.UniqueConstraint("email", name="uq_accounts_email"),
        sa.CheckConstraint("role IN ('user', 'admin')", name="ck_accounts_role"),
        sa.CheckConstraint("status IN ('active', 'disabled')", name="ck_accounts_status"),
    )

    op.create_table(
        "candidate_profiles",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("account_id", sa.Integer, nullable=False),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("status", sa.String(64), nullable=False),
        sa.Column("education", sa.String(64), nullable=False),
        sa.Column("experience_years", sa.Float, nullable=False),
        sa.Column("salary_floor_k", sa.Integer),
        sa.Column("expected_salary_k", sa.Integer),
        sa.Column("skills_json", JSON_TYPE, nullable=False),
        sa.Column("preferred_cities_json", JSON_TYPE, nullable=False),
        sa.Column("acceptable_cities_json", JSON_TYPE, nullable=False, server_default=sa.text("'[]'")),
        sa.Column("preference_weights_json", JSON_TYPE, nullable=False, server_default=sa.text("'{}'")),
        sa.Column("target_directions_json", JSON_TYPE, nullable=False),
        sa.Column("unacceptable_json", JSON_TYPE, nullable=False),
        sa.ForeignKeyConstraint(["account_id"], ["accounts.id"], ondelete="CASCADE"),
        sa.CheckConstraint("experience_years >= 0", name="ck_candidate_profiles_experience_non_negative"),
        sa.CheckConstraint(
            "salary_floor_k IS NULL OR salary_floor_k >= 0",
            name="ck_candidate_profiles_salary_floor_non_negative",
        ),
        sa.CheckConstraint(
            "expected_salary_k IS NULL OR expected_salary_k >= 0",
            name="ck_candidate_profiles_expected_salary_non_negative",
        ),
    )
    op.create_index("idx_candidate_profiles_account", "candidate_profiles", ["account_id", "id"])

    op.create_table(
        "jobs",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("account_id", sa.Integer, nullable=False),
        sa.Column("raw_text", sa.Text, nullable=False),
        sa.Column("source_url", sa.Text),
        sa.Column("title", sa.String(256), nullable=False),
        sa.Column("city", sa.String(128)),
        sa.Column("salary_min_k", sa.Integer),
        sa.Column("salary_max_k", sa.Integer),
        sa.Column("salary_months", sa.Integer),
        sa.Column("salary_unit", sa.String(32), nullable=False),
        sa.Column("experience_min_years", sa.Float),
        sa.Column("experience_max_years", sa.Float),
        sa.Column("experience_label", sa.String(64)),
        sa.Column("education", sa.String(64)),
        sa.Column("company_name", sa.String(256)),
        sa.Column("industry", sa.String(128)),
        sa.Column("company_size", sa.String(64)),
        sa.Column("skills_json", JSON_TYPE, nullable=False),
        sa.Column("skill_requirements_json", JSON_TYPE, nullable=False, server_default=sa.text("'[]'")),
        sa.Column("description_text", sa.Text, nullable=False),
        sa.Column("field_confidence_json", JSON_TYPE, nullable=False),
        sa.Column("uncertainty_notes_json", JSON_TYPE, nullable=False),
        sa.ForeignKeyConstraint(["account_id"], ["accounts.id"], ondelete="CASCADE"),
        sa.CheckConstraint(
            "salary_min_k IS NULL OR salary_min_k >= 0",
            name="ck_jobs_salary_min_non_negative",
        ),
        sa.CheckConstraint(
            "salary_max_k IS NULL OR salary_max_k >= 0",
            name="ck_jobs_salary_max_non_negative",
        ),
        sa.CheckConstraint(
            "experience_min_years IS NULL OR experience_min_years >= 0",
            name="ck_jobs_experience_min_non_negative",
        ),
        sa.CheckConstraint(
            "experience_max_years IS NULL OR experience_max_years >= 0",
            name="ck_jobs_experience_max_non_negative",
        ),
    )
    op.create_index("idx_jobs_account", "jobs", ["account_id", "id"])

    op.create_table(
        "long_texts",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("account_id", sa.Integer, nullable=False),
        sa.Column("candidate_id", sa.Integer),
        sa.Column("entity_type", sa.String(64), nullable=False),
        sa.Column("entity_id", sa.Integer, nullable=False),
        sa.Column("source_label", sa.String(256), nullable=False),
        sa.Column("text", sa.Text, nullable=False),
        sa.ForeignKeyConstraint(["account_id"], ["accounts.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["candidate_id"], ["candidate_profiles.id"], ondelete="CASCADE"),
    )
    op.create_index("idx_long_texts_account", "long_texts", ["account_id", "id"])

    op.create_table(
        "auth_sessions",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("account_id", sa.Integer, nullable=False),
        sa.Column("token_hash", sa.String(64), nullable=False),
        sa.Column("created_at", TIMESTAMP_TYPE, nullable=False),
        sa.Column("last_seen_at", TIMESTAMP_TYPE, nullable=False),
        sa.Column("expires_at", TIMESTAMP_TYPE, nullable=False),
        sa.Column("absolute_expires_at", TIMESTAMP_TYPE, nullable=False),
        sa.Column("revoked_at", TIMESTAMP_TYPE),
        sa.Column("user_agent", sa.Text),
        sa.Column("ip_address", sa.String(64)),
        sa.ForeignKeyConstraint(["account_id"], ["accounts.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("token_hash", name="uq_auth_sessions_token_hash"),
    )
    op.create_index("idx_auth_sessions_account", "auth_sessions", ["account_id", "revoked_at"])
    op.create_index("idx_auth_sessions_expiry", "auth_sessions", ["expires_at", "absolute_expires_at"])

    op.create_table(
        "chat_sessions",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("session_id", sa.String(128), nullable=False),
        sa.Column("account_id", sa.Integer, nullable=False),
        sa.Column("candidate_id", sa.Integer, nullable=False),
        sa.Column("job_id", sa.Integer),
        sa.Column("title", sa.String(256), nullable=False),
        sa.Column("status", sa.String(32), nullable=False, server_default=sa.text("'active'")),
        sa.Column("created_at", TIMESTAMP_TYPE, nullable=False),
        sa.Column("updated_at", TIMESTAMP_TYPE, nullable=False),
        sa.Column("archived_at", TIMESTAMP_TYPE),
        sa.ForeignKeyConstraint(["account_id"], ["accounts.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["candidate_id"], ["candidate_profiles.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["job_id"], ["jobs.id"], ondelete="SET NULL"),
        sa.UniqueConstraint("session_id", name="uq_chat_sessions_session_id"),
        sa.CheckConstraint("status IN ('active', 'archived')", name="ck_chat_sessions_status"),
    )
    op.create_index("idx_chat_sessions_account", "chat_sessions", ["account_id", sa.text("updated_at DESC")])
    op.create_index("idx_chat_sessions_candidate", "chat_sessions", ["candidate_id", sa.text("updated_at DESC")])

    op.create_table(
        "chat_messages",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("account_id", sa.Integer, nullable=False),
        sa.Column("candidate_id", sa.Integer, nullable=False),
        sa.Column("session_id", sa.String(128), nullable=False),
        sa.Column("role", sa.String(32), nullable=False),
        sa.Column("content", sa.Text, nullable=False),
        sa.Column("metadata_json", JSON_TYPE, nullable=False, server_default=sa.text("'{}'")),
        sa.Column("created_at", TIMESTAMP_TYPE, nullable=False),
        sa.ForeignKeyConstraint(["account_id"], ["accounts.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["candidate_id"], ["candidate_profiles.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["session_id"], ["chat_sessions.session_id"], ondelete="CASCADE"),
    )
    op.create_index(
        "idx_chat_messages_account",
        "chat_messages",
        ["account_id", "candidate_id", "session_id", "id"],
    )

    op.create_table(
        "project_experience_cards",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("account_id", sa.Integer, nullable=False),
        sa.Column("candidate_id", sa.Integer, nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("project_name", sa.String(256), nullable=False),
        sa.Column("card_json", JSON_TYPE, nullable=False),
        sa.Column("confirmed_summary", sa.Text),
        sa.Column("created_at", TIMESTAMP_TYPE, nullable=False),
        sa.Column("confirmed_at", TIMESTAMP_TYPE),
        sa.ForeignKeyConstraint(["account_id"], ["accounts.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["candidate_id"], ["candidate_profiles.id"], ondelete="CASCADE"),
        sa.CheckConstraint(
            "status IN ('待确认', '已确认', '已拒绝')",
            name="ck_project_experience_cards_status",
        ),
    )
    op.create_index(
        "idx_project_experience_cards_owner",
        "project_experience_cards",
        ["account_id", "candidate_id", "id"],
    )

    op.create_table(
        "resume_drafts",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("account_id", sa.Integer, nullable=False),
        sa.Column("candidate_id", sa.Integer, nullable=False),
        sa.Column("job_id", sa.Integer, nullable=False),
        sa.Column("version", sa.Integer, nullable=False),
        sa.Column("status", sa.String(64), nullable=False),
        sa.Column("draft_json", JSON_TYPE, nullable=False),
        sa.Column("created_at", TIMESTAMP_TYPE, nullable=False),
        sa.ForeignKeyConstraint(["account_id"], ["accounts.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["candidate_id"], ["candidate_profiles.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["job_id"], ["jobs.id"], ondelete="CASCADE"),
        sa.UniqueConstraint(
            "candidate_id",
            "job_id",
            "version",
            name="uq_resume_drafts_candidate_job_version",
        ),
        sa.CheckConstraint("version > 0", name="ck_resume_drafts_version_positive"),
    )
    op.create_index("idx_resume_drafts_owner", "resume_drafts", ["account_id", "candidate_id", "id"])

    op.create_table(
        "resume_artifacts",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("account_id", sa.Integer, nullable=False),
        sa.Column("candidate_id", sa.Integer, nullable=False),
        sa.Column("job_id", sa.Integer),
        sa.Column("draft_id", sa.Integer),
        sa.Column("parent_artifact_id", sa.Integer),
        sa.Column("version", sa.Integer, nullable=False),
        sa.Column("artifact_type", sa.String(32), nullable=False),
        sa.Column("original_filename", sa.String(512), nullable=False),
        sa.Column("download_filename", sa.String(512), nullable=False),
        sa.Column("storage_key", sa.String(1024), nullable=False),
        sa.Column("media_type", sa.String(128), nullable=False),
        sa.Column("file_size", sa.BigInteger, nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("extraction_method", sa.String(64), nullable=False),
        sa.Column("extracted_text", sa.Text, nullable=False),
        sa.Column("text_length", sa.Integer, nullable=False),
        sa.Column("page_count", sa.Integer),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("long_text_id", sa.Integer),
        sa.Column("created_at", TIMESTAMP_TYPE, nullable=False),
        sa.ForeignKeyConstraint(["account_id"], ["accounts.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["candidate_id"], ["candidate_profiles.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["job_id"], ["jobs.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["draft_id"], ["resume_drafts.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["parent_artifact_id"], ["resume_artifacts.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["long_text_id"], ["long_texts.id"], ondelete="SET NULL"),
        sa.UniqueConstraint("storage_key", name="uq_resume_artifacts_storage_key"),
        sa.CheckConstraint("version > 0", name="ck_resume_artifacts_version_positive"),
        sa.CheckConstraint("file_size >= 0", name="ck_resume_artifacts_file_size_non_negative"),
        sa.CheckConstraint("text_length >= 0", name="ck_resume_artifacts_text_length_non_negative"),
        sa.CheckConstraint("artifact_type IN ('source', 'tailored')", name="ck_resume_artifacts_type"),
    )
    op.create_index(
        "idx_resume_artifacts_owner",
        "resume_artifacts",
        ["account_id", "candidate_id", "id"],
    )
    op.create_index(
        "idx_resume_artifacts_parent",
        "resume_artifacts",
        ["parent_artifact_id", "draft_id"],
    )

    op.create_table(
        "usage_events",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("account_id", sa.Integer, nullable=False),
        sa.Column("candidate_id", sa.Integer),
        sa.Column("session_id", sa.String(128)),
        sa.Column("root_request_id", sa.String(128)),
        sa.Column("call_id", sa.String(160), nullable=False),
        sa.Column("provider", sa.String(128), nullable=False),
        sa.Column("model", sa.String(256), nullable=False),
        sa.Column("operation", sa.String(128), nullable=False),
        sa.Column("input_tokens", sa.Integer, nullable=False, server_default=sa.text("0")),
        sa.Column("output_tokens", sa.Integer, nullable=False, server_default=sa.text("0")),
        sa.Column("total_tokens", sa.Integer, nullable=False, server_default=sa.text("0")),
        sa.Column("usage_source", sa.String(32), nullable=False),
        sa.Column("status", sa.String(32), nullable=False, server_default=sa.text("'succeeded'")),
        sa.Column("attempt", sa.Integer, nullable=False, server_default=sa.text("1")),
        sa.Column("provider_request_id", sa.String(256)),
        sa.Column("raw_usage_json", JSON_TYPE, nullable=False, server_default=sa.text("'{}'")),
        sa.Column("created_at", TIMESTAMP_TYPE, nullable=False),
        sa.Column("billable", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("pricing_version", sa.String(128)),
        sa.ForeignKeyConstraint(["account_id"], ["accounts.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("call_id", name="uq_usage_events_call_id"),
        sa.CheckConstraint("input_tokens >= 0", name="ck_usage_events_input_tokens_non_negative"),
        sa.CheckConstraint("output_tokens >= 0", name="ck_usage_events_output_tokens_non_negative"),
        sa.CheckConstraint("total_tokens >= 0", name="ck_usage_events_total_tokens_non_negative"),
        sa.CheckConstraint("attempt > 0", name="ck_usage_events_attempt_positive"),
    )
    op.create_index("idx_usage_events_account_time", "usage_events", ["account_id", "created_at"])
    op.create_index("idx_usage_events_session", "usage_events", ["session_id", "created_at"])
    op.create_index("idx_usage_events_request", "usage_events", ["root_request_id", "created_at"])

    op.create_table(
        "rag_chunks",
        sa.Column("id", sa.String(128), primary_key=True),
        sa.Column("account_id", sa.Integer, nullable=False),
        sa.Column("candidate_id", sa.Integer),
        sa.Column("long_text_id", sa.Integer, nullable=False),
        sa.Column("entity_type", sa.String(64), nullable=False),
        sa.Column("entity_id", sa.Integer, nullable=False),
        sa.Column("source_label", sa.String(256), nullable=False),
        sa.Column("chunk_index", sa.Integer, nullable=False),
        sa.Column("content", sa.Text, nullable=False),
        sa.Column("content_sha256", sa.String(64), nullable=False),
        sa.Column("metadata_json", JSON_TYPE, nullable=False, server_default=sa.text("'{}'")),
        sa.Column("embedding", VECTOR_TYPE),
        sa.Column("embedding_model", sa.String(256), nullable=False),
        sa.Column("embedding_dimensions", sa.Integer, nullable=False),
        sa.Column("created_at", TIMESTAMP_TYPE, nullable=False),
        sa.Column("updated_at", TIMESTAMP_TYPE, nullable=False),
        sa.ForeignKeyConstraint(["account_id"], ["accounts.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["candidate_id"], ["candidate_profiles.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["long_text_id"], ["long_texts.id"], ondelete="CASCADE"),
        sa.CheckConstraint("chunk_index >= 0", name="ck_rag_chunks_index_non_negative"),
        sa.CheckConstraint("embedding_dimensions > 0", name="ck_rag_chunks_dimensions_positive"),
    )
    op.create_index("idx_rag_chunks_account_long_text", "rag_chunks", ["account_id", "long_text_id"])
    op.create_index("idx_rag_chunks_account_candidate", "rag_chunks", ["account_id", "candidate_id"])


def downgrade() -> None:
    """Drop this revision in dependency-safe reverse order."""

    op.drop_index("idx_rag_chunks_account_candidate", table_name="rag_chunks")
    op.drop_index("idx_rag_chunks_account_long_text", table_name="rag_chunks")
    op.drop_table("rag_chunks")

    op.drop_index("idx_usage_events_request", table_name="usage_events")
    op.drop_index("idx_usage_events_session", table_name="usage_events")
    op.drop_index("idx_usage_events_account_time", table_name="usage_events")
    op.drop_table("usage_events")

    op.drop_index("idx_resume_artifacts_parent", table_name="resume_artifacts")
    op.drop_index("idx_resume_artifacts_owner", table_name="resume_artifacts")
    op.drop_table("resume_artifacts")

    op.drop_index("idx_resume_drafts_owner", table_name="resume_drafts")
    op.drop_table("resume_drafts")

    op.drop_index("idx_project_experience_cards_owner", table_name="project_experience_cards")
    op.drop_table("project_experience_cards")

    op.drop_index("idx_chat_messages_account", table_name="chat_messages")
    op.drop_table("chat_messages")

    op.drop_index("idx_chat_sessions_candidate", table_name="chat_sessions")
    op.drop_index("idx_chat_sessions_account", table_name="chat_sessions")
    op.drop_table("chat_sessions")

    op.drop_index("idx_auth_sessions_expiry", table_name="auth_sessions")
    op.drop_index("idx_auth_sessions_account", table_name="auth_sessions")
    op.drop_table("auth_sessions")

    op.drop_index("idx_long_texts_account", table_name="long_texts")
    op.drop_table("long_texts")

    op.drop_index("idx_jobs_account", table_name="jobs")
    op.drop_table("jobs")

    op.drop_index("idx_candidate_profiles_account", table_name="candidate_profiles")
    op.drop_table("candidate_profiles")

    op.drop_table("accounts")
