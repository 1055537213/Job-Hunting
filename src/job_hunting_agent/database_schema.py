"""面向 PostgreSQL 的 SQLAlchemy 数据库 schema。

生产 Web 与配置了 ``JOB_AGENT_DATABASE_URL`` 的迁移任务通过 ``SQLAlchemyStore`` 使用这组
表；Alembic 负责创建和升级它们。当前运行时和自动化测试统一使用 PostgreSQL。

本模块描述当前目标结构，供迁移校验和后续 revision 参考；已发布的历史 DDL 必须保留在
``alembic/versions`` 中，不能通过修改此处倒改历史数据库。
"""

from __future__ import annotations

import sqlalchemy as sa
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects import postgresql

# 统一约束命名，方便 Alembic 在升级和回退时稳定定位数据库对象。
NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_name)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

metadata = sa.MetaData(naming_convention=NAMING_CONVENTION)

# PostgreSQL 使用 JSONB，便于保存结构化字段并支持后续索引。
json_type = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")
# pgvector 向量类型只在 PostgreSQL 目标 schema 中启用。
vector_type = sa.JSON().with_variant(Vector(), "postgresql")
timestamp_type = sa.DateTime(timezone=True)


accounts = sa.Table(
    "accounts",
    metadata,
    sa.Column("id", sa.Integer, primary_key=True),
    sa.Column("email", sa.String(254), nullable=False, unique=True),
    sa.Column("password_hash", sa.Text, nullable=False),
    sa.Column("display_name", sa.String(128)),
    sa.Column("role", sa.String(32), nullable=False, server_default=sa.text("'user'")),
    sa.Column("status", sa.String(32), nullable=False, server_default=sa.text("'active'")),
    sa.Column("must_change_password", sa.Boolean, nullable=False, server_default=sa.false()),
    sa.Column("created_at", timestamp_type, nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
    sa.Column("updated_at", timestamp_type, nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
    sa.CheckConstraint("role IN ('user', 'admin')", name="accounts_role"),
    sa.CheckConstraint("status IN ('active', 'disabled')", name="accounts_status"),
)


candidate_profiles = sa.Table(
    "candidate_profiles",
    metadata,
    sa.Column("id", sa.Integer, primary_key=True),
    sa.Column(
        "account_id",
        sa.Integer,
        sa.ForeignKey("accounts.id", ondelete="CASCADE"),
        nullable=False,
    ),
    sa.Column("name", sa.String(128), nullable=False),
    sa.Column("status", sa.String(64), nullable=False),
    sa.Column("education", sa.String(64), nullable=False),
    sa.Column("experience_years", sa.Float, nullable=False),
    sa.Column("salary_floor_k", sa.Integer),
    sa.Column("expected_salary_k", sa.Integer),
    sa.Column("skills_json", json_type, nullable=False),
    sa.Column("preferred_cities_json", json_type, nullable=False),
    sa.Column("acceptable_cities_json", json_type, nullable=False, server_default=sa.text("'[]'")),
    sa.Column("preference_weights_json", json_type, nullable=False, server_default=sa.text("'{}'")),
    sa.Column("target_directions_json", json_type, nullable=False),
    sa.Column("unacceptable_json", json_type, nullable=False),
    sa.Column("content_fingerprint", sa.String(64)),
    sa.UniqueConstraint(
        "account_id",
        "content_fingerprint",
        name="uq_candidate_profiles_account_content_fingerprint",
    ),
)
sa.Index("idx_candidate_profiles_account", candidate_profiles.c.account_id, candidate_profiles.c.id)


jobs = sa.Table(
    "jobs",
    metadata,
    sa.Column("id", sa.Integer, primary_key=True),
    sa.Column(
        "account_id",
        sa.Integer,
        sa.ForeignKey("accounts.id", ondelete="CASCADE"),
        nullable=False,
    ),
    sa.Column("raw_text", sa.Text, nullable=False),
    sa.Column("source_url", sa.Text),
    sa.Column("import_method", sa.String(32), nullable=False, server_default=sa.text("'text'")),
    sa.Column("captured_at", timestamp_type),
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
    sa.Column("skills_json", json_type, nullable=False),
    sa.Column("skill_requirements_json", json_type, nullable=False, server_default=sa.text("'[]'")),
    sa.Column("description_text", sa.Text, nullable=False),
    sa.Column("field_confidence_json", json_type, nullable=False),
    sa.Column("uncertainty_notes_json", json_type, nullable=False),
    sa.Column("content_fingerprint", sa.String(64)),
    sa.UniqueConstraint(
        "account_id",
        "content_fingerprint",
        name="uq_jobs_account_content_fingerprint",
    ),
)
sa.Index("idx_jobs_account", jobs.c.account_id, jobs.c.id)


long_texts = sa.Table(
    "long_texts",
    metadata,
    sa.Column("id", sa.Integer, primary_key=True),
    sa.Column(
        "account_id",
        sa.Integer,
        sa.ForeignKey("accounts.id", ondelete="CASCADE"),
        nullable=False,
    ),
    sa.Column(
        "candidate_id",
        sa.Integer,
        sa.ForeignKey("candidate_profiles.id", ondelete="CASCADE"),
    ),
    sa.Column("entity_type", sa.String(64), nullable=False),
    sa.Column("entity_id", sa.Integer, nullable=False),
    sa.Column("source_label", sa.String(256), nullable=False),
    sa.Column("text", sa.Text, nullable=False),
)
sa.Index("idx_long_texts_account", long_texts.c.account_id, long_texts.c.id)


# 登录凭证只保存随机 Cookie 的哈希；原始 Cookie 永远不会写进数据库。
auth_sessions = sa.Table(
    "auth_sessions",
    metadata,
    sa.Column("id", sa.Integer, primary_key=True),
    sa.Column(
        "account_id",
        sa.Integer,
        sa.ForeignKey("accounts.id", ondelete="CASCADE"),
        nullable=False,
    ),
    sa.Column("token_hash", sa.String(64), nullable=False),
    sa.Column("created_at", timestamp_type, nullable=False),
    sa.Column("last_seen_at", timestamp_type, nullable=False),
    sa.Column("expires_at", timestamp_type, nullable=False),
    sa.Column("absolute_expires_at", timestamp_type, nullable=False),
    sa.Column("revoked_at", timestamp_type),
    sa.Column("user_agent", sa.Text),
    sa.Column("ip_address", sa.String(64)),
    sa.UniqueConstraint("token_hash", name="uq_auth_sessions_token_hash"),
)
sa.Index("idx_auth_sessions_account", auth_sessions.c.account_id, auth_sessions.c.revoked_at)
sa.Index("idx_auth_sessions_expiry", auth_sessions.c.expires_at, auth_sessions.c.absolute_expires_at)


# 一个候选人档案可以创建多段独立对话，且每段对话可选地围绕一个职位展开。
chat_sessions = sa.Table(
    "chat_sessions",
    metadata,
    sa.Column("id", sa.Integer, primary_key=True),
    sa.Column("session_id", sa.String(128), nullable=False),
    sa.Column(
        "account_id",
        sa.Integer,
        sa.ForeignKey("accounts.id", ondelete="CASCADE"),
        nullable=False,
    ),
    sa.Column(
        "candidate_id",
        sa.Integer,
        sa.ForeignKey("candidate_profiles.id", ondelete="CASCADE"),
        nullable=False,
    ),
    sa.Column(
        "job_id",
        sa.Integer,
        sa.ForeignKey("jobs.id", ondelete="SET NULL"),
    ),
    sa.Column("title", sa.String(256), nullable=False),
    sa.Column("status", sa.String(32), nullable=False, server_default=sa.text("'active'")),
    sa.Column("created_at", timestamp_type, nullable=False),
    sa.Column("updated_at", timestamp_type, nullable=False),
    sa.Column("archived_at", timestamp_type),
    sa.UniqueConstraint("session_id", name="uq_chat_sessions_session_id"),
    sa.CheckConstraint("status IN ('active', 'archived')", name="chat_sessions_status"),
)
sa.Index("idx_chat_sessions_account", chat_sessions.c.account_id, chat_sessions.c.updated_at.desc())
sa.Index("idx_chat_sessions_candidate", chat_sessions.c.candidate_id, chat_sessions.c.updated_at.desc())


# 对话消息是 UI 历史，不会直接成为候选人档案事实；metadata 保存 SSE/Agent 的摘要。
chat_messages = sa.Table(
    "chat_messages",
    metadata,
    sa.Column("id", sa.Integer, primary_key=True),
    sa.Column(
        "account_id",
        sa.Integer,
        sa.ForeignKey("accounts.id", ondelete="CASCADE"),
        nullable=False,
    ),
    sa.Column(
        "candidate_id",
        sa.Integer,
        sa.ForeignKey("candidate_profiles.id", ondelete="CASCADE"),
        nullable=False,
    ),
    sa.Column(
        "session_id",
        sa.String(128),
        sa.ForeignKey("chat_sessions.session_id", ondelete="CASCADE"),
        nullable=False,
    ),
    sa.Column("role", sa.String(32), nullable=False),
    sa.Column("content", sa.Text, nullable=False),
    sa.Column("metadata_json", json_type, nullable=False, server_default=sa.text("'{}'")),
    sa.Column("created_at", timestamp_type, nullable=False),
)
sa.Index(
    "idx_chat_messages_account",
    chat_messages.c.account_id,
    chat_messages.c.candidate_id,
    chat_messages.c.session_id,
    chat_messages.c.id,
)


# 自动项目分析只能写入待确认卡片，确认摘要也与结构化候选人事实保持分离。
project_experience_cards = sa.Table(
    "project_experience_cards",
    metadata,
    sa.Column("id", sa.Integer, primary_key=True),
    sa.Column(
        "account_id",
        sa.Integer,
        sa.ForeignKey("accounts.id", ondelete="CASCADE"),
        nullable=False,
    ),
    sa.Column(
        "candidate_id",
        sa.Integer,
        sa.ForeignKey("candidate_profiles.id", ondelete="CASCADE"),
        nullable=False,
    ),
    sa.Column("status", sa.String(32), nullable=False),
    sa.Column("project_name", sa.String(256), nullable=False),
    sa.Column("card_json", json_type, nullable=False),
    sa.Column("confirmed_summary", sa.Text),
    sa.Column("created_at", timestamp_type, nullable=False),
    sa.Column("confirmed_at", timestamp_type),
    sa.Column("content_fingerprint", sa.String(64)),
    sa.UniqueConstraint(
        "candidate_id",
        "content_fingerprint",
        name="uq_project_cards_candidate_content_fingerprint",
    ),
    sa.CheckConstraint(
        "status IN ('待确认', '已确认', '已拒绝')",
        name="project_experience_cards_status",
    ),
)
sa.Index(
    "idx_project_experience_cards_owner",
    project_experience_cards.c.account_id,
    project_experience_cards.c.candidate_id,
    project_experience_cards.c.id,
)


# 同一职位可以生成多份草稿版本，草稿不能反向覆盖候选人档案。
resume_drafts = sa.Table(
    "resume_drafts",
    metadata,
    sa.Column("id", sa.Integer, primary_key=True),
    sa.Column(
        "account_id",
        sa.Integer,
        sa.ForeignKey("accounts.id", ondelete="CASCADE"),
        nullable=False,
    ),
    sa.Column(
        "candidate_id",
        sa.Integer,
        sa.ForeignKey("candidate_profiles.id", ondelete="CASCADE"),
        nullable=False,
    ),
    sa.Column(
        "job_id",
        sa.Integer,
        sa.ForeignKey("jobs.id", ondelete="CASCADE"),
        nullable=False,
    ),
    sa.Column("version", sa.Integer, nullable=False),
    sa.Column("status", sa.String(64), nullable=False),
    sa.Column("draft_json", json_type, nullable=False),
    sa.Column("created_at", timestamp_type, nullable=False),
    sa.UniqueConstraint(
        "candidate_id",
        "job_id",
        "version",
        name="uq_resume_drafts_candidate_job_version",
    ),
    sa.CheckConstraint("version > 0", name="resume_drafts_version_positive"),
)
sa.Index("idx_resume_drafts_owner", resume_drafts.c.account_id, resume_drafts.c.candidate_id, resume_drafts.c.id)


# 二进制简历文件放在文件存储中；这张表只保留可鉴权、可校验的元数据和文本提取结果。
resume_artifacts = sa.Table(
    "resume_artifacts",
    metadata,
    sa.Column("id", sa.Integer, primary_key=True),
    sa.Column(
        "account_id",
        sa.Integer,
        sa.ForeignKey("accounts.id", ondelete="CASCADE"),
        nullable=False,
    ),
    sa.Column(
        "candidate_id",
        sa.Integer,
        sa.ForeignKey("candidate_profiles.id", ondelete="CASCADE"),
        nullable=False,
    ),
    sa.Column("job_id", sa.Integer, sa.ForeignKey("jobs.id", ondelete="SET NULL")),
    sa.Column("draft_id", sa.Integer, sa.ForeignKey("resume_drafts.id", ondelete="SET NULL")),
    sa.Column(
        "parent_artifact_id",
        sa.Integer,
        sa.ForeignKey("resume_artifacts.id", ondelete="SET NULL"),
    ),
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
    sa.Column("long_text_id", sa.Integer, sa.ForeignKey("long_texts.id", ondelete="SET NULL")),
    sa.Column("created_at", timestamp_type, nullable=False),
    sa.Column("content_fingerprint", sa.String(64)),
    sa.UniqueConstraint("storage_key", name="uq_resume_artifacts_storage_key"),
    sa.UniqueConstraint(
        "candidate_id",
        "content_fingerprint",
        name="uq_resume_artifacts_candidate_content_fingerprint",
    ),
    sa.CheckConstraint("version > 0", name="resume_artifacts_version_positive"),
    sa.CheckConstraint("file_size >= 0", name="resume_artifacts_file_size_non_negative"),
    sa.CheckConstraint("text_length >= 0", name="resume_artifacts_text_length_non_negative"),
    sa.CheckConstraint("artifact_type IN ('source', 'tailored')", name="resume_artifacts_type"),
)
sa.Index(
    "idx_resume_artifacts_owner",
    resume_artifacts.c.account_id,
    resume_artifacts.c.candidate_id,
    resume_artifacts.c.id,
)
sa.Index(
    "idx_resume_artifacts_parent",
    resume_artifacts.c.parent_artifact_id,
    resume_artifacts.c.draft_id,
)


# 用量流水是追加式账本：删除会话或候选人不能破坏已记录的调用归因。
usage_events = sa.Table(
    "usage_events",
    metadata,
    sa.Column("id", sa.Integer, primary_key=True),
    sa.Column(
        "account_id",
        sa.Integer,
        sa.ForeignKey("accounts.id", ondelete="CASCADE"),
        nullable=False,
    ),
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
    sa.Column("raw_usage_json", json_type, nullable=False, server_default=sa.text("'{}'")),
    sa.Column("created_at", timestamp_type, nullable=False),
    sa.Column("billable", sa.Boolean, nullable=False, server_default=sa.false()),
    sa.Column("pricing_version", sa.String(128)),
    sa.UniqueConstraint("call_id", name="uq_usage_events_call_id"),
    sa.CheckConstraint("input_tokens >= 0", name="usage_events_input_tokens_non_negative"),
    sa.CheckConstraint("output_tokens >= 0", name="usage_events_output_tokens_non_negative"),
    sa.CheckConstraint("total_tokens >= 0", name="usage_events_total_tokens_non_negative"),
    sa.CheckConstraint("attempt > 0", name="usage_events_attempt_positive"),
)
sa.Index("idx_usage_events_account_time", usage_events.c.account_id, usage_events.c.created_at)
sa.Index("idx_usage_events_session", usage_events.c.session_id, usage_events.c.created_at)
sa.Index("idx_usage_events_request", usage_events.c.root_request_id, usage_events.c.created_at)


# 工具调用审计只保留最近两天的任务轨迹，不保存 prompt、正文或完整模型上下文。
tool_call_traces = sa.Table(
    "tool_call_traces",
    metadata,
    sa.Column("id", sa.Integer, primary_key=True),
    sa.Column(
        "account_id",
        sa.Integer,
        sa.ForeignKey("accounts.id", ondelete="CASCADE"),
        nullable=False,
    ),
    sa.Column(
        "candidate_id",
        sa.Integer,
        sa.ForeignKey("candidate_profiles.id", ondelete="CASCADE"),
    ),
    sa.Column("session_id", sa.String(128)),
    sa.Column("root_request_id", sa.String(128), nullable=False),
    sa.Column("title", sa.String(256), nullable=False),
    sa.Column("status", sa.String(32), nullable=False, server_default=sa.text("'running'")),
    sa.Column("source", sa.String(32), nullable=False, server_default=sa.text("'chat'")),
    sa.Column("step_count", sa.Integer, nullable=False, server_default=sa.text("0")),
    sa.Column("attempt_count", sa.Integer, nullable=False, server_default=sa.text("0")),
    sa.Column("last_step_name", sa.String(128)),
    sa.Column("last_error_summary", sa.Text),
    sa.Column("trace_json", json_type, nullable=False, server_default=sa.text("'{}'")),
    sa.Column("created_at", timestamp_type, nullable=False),
    sa.Column("started_at", timestamp_type),
    sa.Column("finished_at", timestamp_type),
    sa.Column("updated_at", timestamp_type, nullable=False),
    sa.UniqueConstraint("root_request_id", name="uq_tool_call_traces_root_request_id"),
    sa.CheckConstraint(
        "status IN ('running', 'waiting_confirmation', 'completed', 'failed', 'cancelled')",
        name="tool_call_traces_status",
    ),
    sa.CheckConstraint("step_count >= 0", name="tool_call_traces_step_count_non_negative"),
    sa.CheckConstraint("attempt_count >= 0", name="tool_call_traces_attempt_count_non_negative"),
)
sa.Index("idx_tool_call_traces_account_time", tool_call_traces.c.account_id, tool_call_traces.c.created_at)
sa.Index("idx_tool_call_traces_account_update", tool_call_traces.c.account_id, tool_call_traces.c.updated_at.desc())
sa.Index("idx_tool_call_traces_request", tool_call_traces.c.root_request_id)


# 管理员审计日志是追加式流水，只保存动作、资源 ID 和低敏摘要，不保存正文或密钥。
admin_audit_events = sa.Table(
    "admin_audit_events",
    metadata,
    sa.Column("id", sa.Integer, primary_key=True),
    sa.Column(
        "actor_account_id",
        sa.Integer,
        sa.ForeignKey("accounts.id", ondelete="SET NULL"),
    ),
    sa.Column(
        "target_account_id",
        sa.Integer,
        sa.ForeignKey("accounts.id", ondelete="SET NULL"),
    ),
    sa.Column("action", sa.String(96), nullable=False),
    sa.Column("target_type", sa.String(64), nullable=False),
    sa.Column("target_id", sa.String(160)),
    sa.Column("outcome", sa.String(32), nullable=False, server_default=sa.text("'succeeded'")),
    sa.Column("summary", sa.Text, nullable=False),
    sa.Column("details_json", json_type, nullable=False, server_default=sa.text("'{}'")),
    sa.Column("request_id", sa.String(128)),
    sa.Column("created_at", timestamp_type, nullable=False),
    sa.CheckConstraint(
        "outcome IN ('succeeded', 'blocked', 'failed')",
        name="admin_audit_events_outcome",
    ),
)
sa.Index(
    "idx_admin_audit_events_actor_time",
    admin_audit_events.c.actor_account_id,
    admin_audit_events.c.created_at.desc(),
)
sa.Index("idx_admin_audit_events_action_time", admin_audit_events.c.action, admin_audit_events.c.created_at.desc())
sa.Index("idx_admin_audit_events_created", admin_audit_events.c.created_at.desc())


# 后台任务的状态以 PostgreSQL 为准；Redis/Celery 只传递 task_key，不保存业务事实。
background_tasks = sa.Table(
    "background_tasks",
    metadata,
    sa.Column("id", sa.Integer, primary_key=True),
    sa.Column("task_key", sa.String(64), nullable=False),
    sa.Column(
        "account_id",
        sa.Integer,
        sa.ForeignKey("accounts.id", ondelete="CASCADE"),
        nullable=False,
    ),
    sa.Column(
        "candidate_id",
        sa.Integer,
        sa.ForeignKey("candidate_profiles.id", ondelete="CASCADE"),
    ),
    sa.Column("session_id", sa.String(128)),
    sa.Column("task_type", sa.String(128), nullable=False),
    sa.Column("status", sa.String(32), nullable=False, server_default=sa.text("'queued'")),
    sa.Column("progress", sa.Integer, nullable=False, server_default=sa.text("0")),
    sa.Column("attempt", sa.Integer, nullable=False, server_default=sa.text("0")),
    sa.Column("max_attempts", sa.Integer, nullable=False, server_default=sa.text("3")),
    sa.Column("idempotency_key", sa.String(128)),
    sa.Column("payload_json", json_type, nullable=False, server_default=sa.text("'{}'")),
    sa.Column("result_json", json_type, nullable=False, server_default=sa.text("'{}'")),
    sa.Column("error_summary", sa.Text),
    sa.Column("created_at", timestamp_type, nullable=False),
    sa.Column("started_at", timestamp_type),
    sa.Column("finished_at", timestamp_type),
    sa.Column("updated_at", timestamp_type, nullable=False),
    sa.UniqueConstraint("task_key", name="uq_background_tasks_task_key"),
    sa.UniqueConstraint("account_id", "idempotency_key", name="uq_background_tasks_idempotency"),
    sa.CheckConstraint(
        "status IN ('queued', 'running', 'succeeded', 'failed', 'cancelled')",
        name="background_tasks_status",
    ),
    sa.CheckConstraint("progress >= 0 AND progress <= 100", name="background_tasks_progress_range"),
    sa.CheckConstraint("attempt >= 0", name="background_tasks_attempt_non_negative"),
    sa.CheckConstraint("max_attempts > 0", name="background_tasks_max_attempts_positive"),
)
sa.Index(
    "idx_background_tasks_account_status",
    background_tasks.c.account_id,
    background_tasks.c.status,
    background_tasks.c.updated_at.desc(),
)
sa.Index("idx_background_tasks_candidate", background_tasks.c.candidate_id, background_tasks.c.updated_at.desc())


# RAG 分块是 long_texts 的派生索引。维度尚未锁定，因此本阶段不创建 HNSW/IVFFlat 索引。
rag_chunks = sa.Table(
    "rag_chunks",
    metadata,
    sa.Column("id", sa.String(128), primary_key=True),
    sa.Column(
        "account_id",
        sa.Integer,
        sa.ForeignKey("accounts.id", ondelete="CASCADE"),
        nullable=False,
    ),
    sa.Column(
        "candidate_id",
        sa.Integer,
        sa.ForeignKey("candidate_profiles.id", ondelete="CASCADE"),
    ),
    sa.Column(
        "long_text_id",
        sa.Integer,
        sa.ForeignKey("long_texts.id", ondelete="CASCADE"),
        nullable=False,
    ),
    sa.Column("entity_type", sa.String(64), nullable=False),
    sa.Column("entity_id", sa.Integer, nullable=False),
    sa.Column("source_label", sa.String(256), nullable=False),
    sa.Column("chunk_index", sa.Integer, nullable=False),
    sa.Column("content", sa.Text, nullable=False),
    sa.Column("content_sha256", sa.String(64), nullable=False),
    sa.Column("metadata_json", json_type, nullable=False, server_default=sa.text("'{}'")),
    sa.Column("embedding", vector_type),
    sa.Column("embedding_model", sa.String(256), nullable=False),
    sa.Column("embedding_dimensions", sa.Integer, nullable=False),
    sa.Column("created_at", timestamp_type, nullable=False),
    sa.Column("updated_at", timestamp_type, nullable=False),
    sa.CheckConstraint("chunk_index >= 0", name="rag_chunks_index_non_negative"),
    sa.CheckConstraint("embedding_dimensions > 0", name="rag_chunks_dimensions_positive"),
)
sa.Index("idx_rag_chunks_account_long_text", rag_chunks.c.account_id, rag_chunks.c.long_text_id)
sa.Index("idx_rag_chunks_account_candidate", rag_chunks.c.account_id, rag_chunks.c.candidate_id)
