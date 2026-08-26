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
    sa.Column("email_verified_at", timestamp_type),
    sa.Column("deleted_at", timestamp_type),
    sa.Column("created_at", timestamp_type, nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
    sa.Column("updated_at", timestamp_type, nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
    sa.CheckConstraint("role IN ('user', 'admin')", name="accounts_role"),
    sa.CheckConstraint("status IN ('active', 'disabled')", name="accounts_status"),
)


# 邮箱验证和密码重置共用一次性令牌表；数据库只保存令牌哈希。
account_action_tokens = sa.Table(
    "account_action_tokens",
    metadata,
    sa.Column("id", sa.Integer, primary_key=True),
    sa.Column(
        "account_id",
        sa.Integer,
        sa.ForeignKey("accounts.id", ondelete="CASCADE"),
        nullable=False,
    ),
    sa.Column("purpose", sa.String(32), nullable=False),
    sa.Column("token_hash", sa.String(64), nullable=False, unique=True),
    sa.Column("expires_at", timestamp_type, nullable=False),
    sa.Column("consumed_at", timestamp_type),
    sa.Column("created_at", timestamp_type, nullable=False),
    sa.Column("requested_ip", sa.String(64)),
    sa.CheckConstraint(
        "purpose IN ('verify_email', 'reset_password')",
        name="account_action_tokens_purpose",
    ),
)
sa.Index(
    "idx_account_action_tokens_account",
    account_action_tokens.c.account_id,
    account_action_tokens.c.purpose,
    account_action_tokens.c.consumed_at,
)
sa.Index("idx_account_action_tokens_expiry", account_action_tokens.c.expires_at)


# Web 只登记事务邮件；Worker 根据此表认领、重试和完成投递。
account_email_outbox = sa.Table(
    "account_email_outbox",
    metadata,
    sa.Column("id", sa.Integer, primary_key=True),
    sa.Column(
        "account_id",
        sa.Integer,
        sa.ForeignKey("accounts.id", ondelete="CASCADE"),
        nullable=False,
    ),
    sa.Column(
        "action_token_id",
        sa.Integer,
        sa.ForeignKey("account_action_tokens.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    ),
    sa.Column("purpose", sa.String(32), nullable=False),
    sa.Column("recipient_email", sa.String(254), nullable=False),
    sa.Column("delivery_key", sa.String(64), nullable=False, unique=True),
    sa.Column("request_source_hash", sa.String(64)),
    sa.Column("status", sa.String(32), nullable=False),
    sa.Column("attempt_count", sa.Integer, nullable=False, server_default=sa.text("0")),
    sa.Column("max_attempts", sa.Integer, nullable=False),
    sa.Column("next_attempt_at", timestamp_type, nullable=False),
    sa.Column("claimed_at", timestamp_type),
    sa.Column("sent_at", timestamp_type),
    sa.Column("last_error_type", sa.String(128)),
    sa.Column("last_error_summary", sa.String(500)),
    sa.Column("created_at", timestamp_type, nullable=False),
    sa.Column("updated_at", timestamp_type, nullable=False),
    sa.CheckConstraint(
        "purpose IN ('verify_email', 'reset_password')",
        name="account_email_outbox_purpose",
    ),
    sa.CheckConstraint(
        "status IN ('pending', 'sending', 'retrying', 'sent', 'failed', 'cancelled')",
        name="account_email_outbox_status",
    ),
    sa.CheckConstraint(
        "attempt_count >= 0 AND max_attempts > 0 AND attempt_count <= max_attempts",
        name="account_email_outbox_attempts",
    ),
)
sa.Index(
    "idx_account_email_outbox_due",
    account_email_outbox.c.status,
    account_email_outbox.c.next_attempt_at,
    account_email_outbox.c.id,
)
sa.Index(
    "idx_account_email_outbox_account",
    account_email_outbox.c.account_id,
    account_email_outbox.c.created_at,
)
sa.Index(
    "idx_account_email_outbox_source",
    account_email_outbox.c.request_source_hash,
    account_email_outbox.c.created_at,
)


# 保存用户同意的协议版本，避免只保留一个随版本更新而失真的布尔值。
account_consents = sa.Table(
    "account_consents",
    metadata,
    sa.Column("id", sa.Integer, primary_key=True),
    sa.Column(
        "account_id",
        sa.Integer,
        sa.ForeignKey("accounts.id", ondelete="CASCADE"),
        nullable=False,
    ),
    sa.Column("document_type", sa.String(32), nullable=False),
    sa.Column("version", sa.String(64), nullable=False),
    sa.Column("accepted_at", timestamp_type, nullable=False),
    sa.Column("ip_address", sa.String(64)),
    sa.Column("user_agent", sa.String(512)),
    sa.CheckConstraint(
        "document_type IN ('terms', 'privacy')",
        name="account_consents_document_type",
    ),
    sa.UniqueConstraint(
        "account_id",
        "document_type",
        "version",
        name="uq_account_consents_account_document_version",
    ),
)
sa.Index("idx_account_consents_account", account_consents.c.account_id, account_consents.c.accepted_at)


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
    # Worker 重试时复用已经保存的草稿，避免同一导出任务产生多个版本。
    sa.Column("generation_key", sa.String(128)),
    sa.UniqueConstraint(
        "candidate_id",
        "job_id",
        "version",
        name="uq_resume_drafts_candidate_job_version",
    ),
    sa.CheckConstraint("version > 0", name="resume_drafts_version_positive"),
    sa.UniqueConstraint("generation_key", name="uq_resume_drafts_generation_key"),
)
sa.Index("idx_resume_drafts_owner", resume_drafts.c.account_id, resume_drafts.c.candidate_id, resume_drafts.c.id)


# 统一知识资产把“文件是什么”与具体业务用途分离；原件版本只追加，不原地覆盖。
knowledge_assets = sa.Table(
    "knowledge_assets",
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
    sa.Column("asset_kind", sa.String(64), nullable=False),
    sa.Column("title", sa.String(512), nullable=False),
    sa.Column("lifecycle_status", sa.String(32), nullable=False, server_default=sa.text("'active'")),
    sa.Column("metadata_json", json_type, nullable=False, server_default=sa.text("'{}'")),
    sa.Column("created_at", timestamp_type, nullable=False),
    sa.Column("updated_at", timestamp_type, nullable=False),
    sa.CheckConstraint(
        "lifecycle_status IN ('active', 'archived')",
        name="knowledge_assets_lifecycle_status",
    ),
)
sa.Index(
    "idx_knowledge_assets_owner",
    knowledge_assets.c.account_id,
    knowledge_assets.c.candidate_id,
    knowledge_assets.c.id,
)


knowledge_asset_versions = sa.Table(
    "knowledge_asset_versions",
    metadata,
    sa.Column("id", sa.Integer, primary_key=True),
    sa.Column(
        "asset_id",
        sa.Integer,
        sa.ForeignKey("knowledge_assets.id", ondelete="CASCADE"),
        nullable=False,
    ),
    sa.Column("version_number", sa.Integer, nullable=False),
    sa.Column("is_current", sa.Boolean, nullable=False, server_default=sa.true()),
    sa.Column("original_filename", sa.String(512), nullable=False),
    sa.Column("storage_key", sa.String(1024), nullable=False),
    sa.Column("media_type", sa.String(128), nullable=False),
    sa.Column("file_size", sa.BigInteger, nullable=False),
    sa.Column("sha256", sa.String(64), nullable=False),
    sa.Column("source_kind", sa.String(32), nullable=False, server_default=sa.text("'upload'")),
    sa.Column("source_url", sa.Text),
    sa.Column("revision_label", sa.String(128)),
    sa.Column("processing_status", sa.String(32), nullable=False, server_default=sa.text("'uploaded'")),
    sa.Column("scan_status", sa.String(32), nullable=False, server_default=sa.text("'pending'")),
    sa.Column("scan_engine", sa.String(64)),
    sa.Column("scan_reason", sa.Text),
    sa.Column("metadata_json", json_type, nullable=False, server_default=sa.text("'{}'")),
    sa.Column("created_at", timestamp_type, nullable=False),
    sa.UniqueConstraint("asset_id", "version_number", name="uq_knowledge_asset_versions_number"),
    sa.UniqueConstraint("asset_id", "sha256", name="uq_knowledge_asset_versions_content"),
    sa.UniqueConstraint("storage_key", name="uq_knowledge_asset_versions_storage_key"),
    sa.CheckConstraint("version_number > 0", name="knowledge_asset_versions_number_positive"),
    sa.CheckConstraint("file_size >= 0", name="knowledge_asset_versions_file_size_non_negative"),
    sa.CheckConstraint(
        "processing_status IN ('uploaded', 'scanning', 'processing', 'ready', 'quarantined', 'failed')",
        name="knowledge_asset_versions_processing_status",
    ),
    sa.CheckConstraint(
        "scan_status IN ('pending', 'clean', 'infected', 'error', 'not_required')",
        name="knowledge_asset_versions_scan_status",
    ),
)
sa.Index(
    "idx_knowledge_asset_versions_asset",
    knowledge_asset_versions.c.asset_id,
    knowledge_asset_versions.c.version_number,
)
sa.Index(
    "uq_knowledge_asset_versions_current",
    knowledge_asset_versions.c.asset_id,
    unique=True,
    postgresql_where=knowledge_asset_versions.c.is_current.is_(True),
)


# 项目整包原件复用统一知识资产；这张表只保存业务处理状态和最终项目卡片关联。
project_archive_imports = sa.Table(
    "project_archive_imports",
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
        "knowledge_asset_id",
        sa.Integer,
        sa.ForeignKey("knowledge_assets.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    ),
    sa.Column(
        "knowledge_asset_version_id",
        sa.Integer,
        sa.ForeignKey("knowledge_asset_versions.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    ),
    sa.Column(
        "project_card_id",
        sa.Integer,
        sa.ForeignKey("project_experience_cards.id", ondelete="SET NULL"),
    ),
    sa.Column("source_type", sa.String(64), nullable=False),
    sa.Column("source_url", sa.Text),
    sa.Column("source_ref", sa.String(255)),
    sa.Column("original_filename", sa.String(512), nullable=False),
    sa.Column("content_fingerprint", sa.String(64), nullable=False),
    sa.Column("status", sa.String(32), nullable=False),
    sa.Column("error_summary", sa.Text),
    sa.Column("created_at", timestamp_type, nullable=False),
    sa.Column("updated_at", timestamp_type, nullable=False),
    sa.UniqueConstraint(
        "candidate_id",
        "content_fingerprint",
        name="uq_project_archive_candidate_content",
    ),
    sa.CheckConstraint(
        "status IN ('uploaded', 'processing', 'ready', 'failed', 'quarantined')",
        name="project_archive_imports_status",
    ),
)
sa.Index(
    "idx_project_archive_imports_owner",
    project_archive_imports.c.account_id,
    project_archive_imports.c.candidate_id,
    project_archive_imports.c.id,
)
sa.Index("idx_project_archive_imports_card", project_archive_imports.c.project_card_id)


# 文件清单保留项目内部路径和解析路由，不把二进制正文塞入 PostgreSQL。
project_archive_files = sa.Table(
    "project_archive_files",
    metadata,
    sa.Column("id", sa.Integer, primary_key=True),
    sa.Column(
        "project_archive_id",
        sa.Integer,
        sa.ForeignKey("project_archive_imports.id", ondelete="CASCADE"),
        nullable=False,
    ),
    sa.Column("relative_path", sa.String(2048), nullable=False),
    sa.Column("file_kind", sa.String(64), nullable=False),
    sa.Column("media_type", sa.String(128), nullable=False),
    sa.Column("file_size", sa.BigInteger, nullable=False),
    sa.Column("compressed_size", sa.BigInteger, nullable=False),
    sa.Column("sha256", sa.String(64)),
    sa.Column("analysis_status", sa.String(32), nullable=False),
    sa.Column("skip_reason", sa.String(128)),
    sa.Column("long_text_id", sa.Integer, sa.ForeignKey("long_texts.id", ondelete="SET NULL")),
    sa.Column("extraction_method", sa.String(64)),
    sa.Column("text_length", sa.Integer, nullable=False, server_default=sa.text("0")),
    sa.Column("metadata_json", json_type, nullable=False, server_default=sa.text("'{}'")),
    sa.UniqueConstraint(
        "project_archive_id",
        "relative_path",
        name="uq_project_archive_files_path",
    ),
    sa.CheckConstraint("file_size >= 0", name="project_archive_files_size_non_negative"),
    sa.CheckConstraint(
        "compressed_size >= 0",
        name="project_archive_files_compressed_size_non_negative",
    ),
    sa.CheckConstraint(
        "analysis_status IN ('analyzed', 'pending_parser', 'skipped', 'unsupported', 'failed')",
        name="project_archive_files_analysis_status",
    ),
    sa.CheckConstraint("text_length >= 0", name="project_archive_files_text_length"),
)


# 浏览器先提交目录清单，后端生成采集计划；只有选中的文件才分批上传。
project_collection_sessions = sa.Table(
    "project_collection_sessions",
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
        "project_card_id",
        sa.Integer,
        sa.ForeignKey("project_experience_cards.id", ondelete="SET NULL"),
    ),
    sa.Column("project_name", sa.String(256), nullable=False),
    sa.Column("source_type", sa.String(64), nullable=False),
    sa.Column("manifest_fingerprint", sa.String(64), nullable=False),
    sa.Column("preserve_originals", sa.Boolean, nullable=False, server_default=sa.false()),
    sa.Column("status", sa.String(32), nullable=False),
    sa.Column("file_count", sa.Integer, nullable=False),
    sa.Column("selected_file_count", sa.Integer, nullable=False),
    sa.Column("uploaded_file_count", sa.Integer, nullable=False, server_default=sa.text("0")),
    sa.Column("total_size", sa.BigInteger, nullable=False),
    sa.Column("selected_size", sa.BigInteger, nullable=False),
    sa.Column("error_summary", sa.Text),
    sa.Column("created_at", timestamp_type, nullable=False),
    sa.Column("updated_at", timestamp_type, nullable=False),
    sa.UniqueConstraint(
        "candidate_id",
        "manifest_fingerprint",
        name="uq_project_collection_candidate_manifest",
    ),
    sa.CheckConstraint("file_count >= 0", name="project_collection_file_count"),
    sa.CheckConstraint("selected_file_count >= 0", name="project_collection_selected_count"),
    sa.CheckConstraint("uploaded_file_count >= 0", name="project_collection_uploaded_count"),
    sa.CheckConstraint("total_size >= 0", name="project_collection_total_size"),
    sa.CheckConstraint("selected_size >= 0", name="project_collection_selected_size"),
    sa.CheckConstraint(
        "status IN ('planned', 'uploading', 'processing', 'ready', 'failed', 'cancelled')",
        name="project_collection_status",
    ),
)
sa.Index(
    "idx_project_collection_owner",
    project_collection_sessions.c.account_id,
    project_collection_sessions.c.candidate_id,
    project_collection_sessions.c.id,
)
sa.Index("idx_project_collection_card", project_collection_sessions.c.project_card_id)


project_collection_files = sa.Table(
    "project_collection_files",
    metadata,
    sa.Column("id", sa.Integer, primary_key=True),
    sa.Column(
        "collection_id",
        sa.Integer,
        sa.ForeignKey("project_collection_sessions.id", ondelete="CASCADE"),
        nullable=False,
    ),
    sa.Column("relative_path", sa.String(2048), nullable=False),
    sa.Column("file_kind", sa.String(64), nullable=False),
    sa.Column("media_type", sa.String(128), nullable=False),
    sa.Column("file_size", sa.BigInteger, nullable=False),
    sa.Column("client_sha256", sa.String(64)),
    sa.Column("server_sha256", sa.String(64)),
    sa.Column("selection_status", sa.String(32), nullable=False),
    sa.Column("selection_reason", sa.String(256), nullable=False),
    sa.Column("extraction_method", sa.String(64)),
    sa.Column("text_length", sa.Integer, nullable=False, server_default=sa.text("0")),
    sa.Column("long_text_id", sa.Integer, sa.ForeignKey("long_texts.id", ondelete="SET NULL")),
    sa.Column("storage_key", sa.String(1024)),
    sa.Column("knowledge_asset_id", sa.Integer, sa.ForeignKey("knowledge_assets.id", ondelete="SET NULL")),
    sa.Column(
        "knowledge_asset_version_id",
        sa.Integer,
        sa.ForeignKey("knowledge_asset_versions.id", ondelete="SET NULL"),
    ),
    sa.Column("metadata_json", json_type, nullable=False, server_default=sa.text("'{}'")),
    sa.Column("created_at", timestamp_type, nullable=False),
    sa.Column("updated_at", timestamp_type, nullable=False),
    sa.UniqueConstraint(
        "collection_id",
        "relative_path",
        name="uq_project_collection_files_path",
    ),
    sa.CheckConstraint("file_size >= 0", name="project_collection_files_size"),
    sa.CheckConstraint("text_length >= 0", name="project_collection_files_text"),
    sa.CheckConstraint(
        "selection_status IN ('selected', 'skipped', 'uploaded', 'analyzed', 'failed')",
        name="project_collection_files_status",
    ),
)
sa.Index(
    "idx_project_collection_files_session",
    project_collection_files.c.collection_id,
    project_collection_files.c.selection_status,
    project_collection_files.c.id,
)
sa.Index(
    "idx_project_archive_files_import",
    project_archive_files.c.project_archive_id,
    project_archive_files.c.id,
)


# 视觉知识项保存安全重编码后的图片/PDF 页定位和图像向量；二进制位于对象存储。
visual_knowledge_items = sa.Table(
    "visual_knowledge_items",
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
        "project_archive_file_id",
        sa.Integer,
        sa.ForeignKey("project_archive_files.id", ondelete="CASCADE"),
    ),
    sa.Column(
        "project_collection_file_id",
        sa.Integer,
        sa.ForeignKey("project_collection_files.id", ondelete="CASCADE"),
    ),
    sa.Column(
        "long_text_id",
        sa.Integer,
        sa.ForeignKey("long_texts.id", ondelete="SET NULL"),
    ),
    sa.Column("source_id", sa.String(128), nullable=False),
    sa.Column("source_label", sa.String(2048), nullable=False),
    sa.Column("page_number", sa.Integer),
    sa.Column("media_type", sa.String(128), nullable=False),
    sa.Column("storage_key", sa.String(1024), nullable=False, unique=True),
    sa.Column("file_size", sa.BigInteger, nullable=False),
    sa.Column("sha256", sa.String(64), nullable=False),
    sa.Column("width", sa.Integer, nullable=False),
    sa.Column("height", sa.Integer, nullable=False),
    sa.Column("metadata_json", json_type, nullable=False, server_default=sa.text("'{}'")),
    sa.Column("embedding", vector_type),
    sa.Column("embedding_model", sa.String(256)),
    sa.Column("embedding_dimensions", sa.Integer),
    sa.Column("index_status", sa.String(32), nullable=False, server_default=sa.text("'pending'")),
    sa.Column("index_error_type", sa.String(128)),
    sa.Column("created_at", timestamp_type, nullable=False),
    sa.Column("updated_at", timestamp_type, nullable=False),
    sa.CheckConstraint(
        "(project_archive_file_id IS NOT NULL AND project_collection_file_id IS NULL) "
        "OR (project_archive_file_id IS NULL AND project_collection_file_id IS NOT NULL)",
        name="visual_knowledge_items_one_source",
    ),
    sa.CheckConstraint("page_number IS NULL OR page_number > 0", name="visual_knowledge_items_page"),
    sa.CheckConstraint("file_size > 0", name="visual_knowledge_items_file_size"),
    sa.CheckConstraint("width > 0 AND height > 0", name="visual_knowledge_items_dimensions"),
    sa.CheckConstraint(
        "embedding_dimensions IS NULL OR embedding_dimensions > 0",
        name="visual_knowledge_items_embedding_dimensions",
    ),
    sa.CheckConstraint(
        "index_status IN ('pending', 'indexed', 'failed')",
        name="visual_knowledge_items_status",
    ),
)
sa.Index(
    "idx_visual_knowledge_items_owner",
    visual_knowledge_items.c.account_id,
    visual_knowledge_items.c.candidate_id,
    visual_knowledge_items.c.id,
)
sa.Index(
    "uq_visual_knowledge_archive_source",
    visual_knowledge_items.c.project_archive_file_id,
    visual_knowledge_items.c.source_id,
    unique=True,
    postgresql_where=visual_knowledge_items.c.project_archive_file_id.is_not(None),
)
sa.Index(
    "uq_visual_knowledge_collection_source",
    visual_knowledge_items.c.project_collection_file_id,
    visual_knowledge_items.c.source_id,
    unique=True,
    postgresql_where=visual_knowledge_items.c.project_collection_file_id.is_not(None),
)


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
    sa.Column("knowledge_asset_id", sa.Integer, sa.ForeignKey("knowledge_assets.id", ondelete="SET NULL")),
    sa.Column(
        "knowledge_asset_version_id",
        sa.Integer,
        sa.ForeignKey("knowledge_asset_versions.id", ondelete="SET NULL"),
    ),
    sa.Column("created_at", timestamp_type, nullable=False),
    sa.Column("content_fingerprint", sa.String(64)),
    # 同一后台任务的 DOCX/PDF 各自使用一个键，重试时不会重复登记文件。
    sa.Column("generation_key", sa.String(128)),
    sa.UniqueConstraint("storage_key", name="uq_resume_artifacts_storage_key"),
    sa.UniqueConstraint("generation_key", name="uq_resume_artifacts_generation_key"),
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
sa.Index(
    "idx_resume_artifacts_knowledge_asset",
    resume_artifacts.c.knowledge_asset_id,
    resume_artifacts.c.knowledge_asset_version_id,
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


# 余额总表保存当前可用余额与累计充值/消费金额；流水表保存每一次变动。
account_balances = sa.Table(
    "account_balances",
    metadata,
    sa.Column(
        "account_id",
        sa.Integer,
        sa.ForeignKey("accounts.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    sa.Column("balance_micro_yuan", sa.BigInteger, nullable=False, server_default=sa.text("0")),
    sa.Column(
        "total_recharge_micro_yuan",
        sa.BigInteger,
        nullable=False,
        server_default=sa.text("0"),
    ),
    sa.Column(
        "total_consumed_micro_yuan",
        sa.BigInteger,
        nullable=False,
        server_default=sa.text("0"),
    ),
    sa.Column(
        "low_balance_threshold_micro_yuan",
        sa.BigInteger,
        nullable=False,
        server_default=sa.text("10000000"),
    ),
    sa.Column("created_at", timestamp_type, nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
    sa.Column("updated_at", timestamp_type, nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
)


recharge_orders = sa.Table(
    "recharge_orders",
    metadata,
    sa.Column("id", sa.Integer, primary_key=True),
    sa.Column("order_number", sa.String(64), nullable=False),
    sa.Column(
        "account_id",
        sa.Integer,
        sa.ForeignKey("accounts.id", ondelete="CASCADE"),
        nullable=False,
    ),
    sa.Column(
        "created_by_account_id",
        sa.Integer,
        sa.ForeignKey("accounts.id", ondelete="SET NULL"),
    ),
    sa.Column("amount_micro_yuan", sa.BigInteger, nullable=False),
    sa.Column("status", sa.String(24), nullable=False, server_default=sa.text("'pending'")),
    sa.Column("payment_provider", sa.String(32), nullable=False),
    sa.Column("provider_order_id", sa.String(160)),
    sa.Column("idempotency_key", sa.String(128), nullable=False),
    sa.Column("description", sa.Text, nullable=False),
    sa.Column("failure_reason", sa.Text),
    sa.Column("details_json", json_type, nullable=False, server_default=sa.text("'{}'")),
    sa.Column("created_at", timestamp_type, nullable=False),
    sa.Column("updated_at", timestamp_type, nullable=False),
    sa.Column("paid_at", timestamp_type),
    sa.Column("cancelled_at", timestamp_type),
    sa.Column("refunded_at", timestamp_type),
    sa.UniqueConstraint("order_number", name="uq_recharge_orders_order_number"),
    sa.UniqueConstraint("account_id", "idempotency_key", name="uq_recharge_orders_account_idempotency"),
    sa.UniqueConstraint(
        "payment_provider",
        "provider_order_id",
        name="uq_recharge_orders_provider_order",
    ),
    sa.CheckConstraint("amount_micro_yuan > 0", name="recharge_orders_amount_positive"),
    sa.CheckConstraint(
        "status IN ('pending', 'paid', 'failed', 'cancelled', 'refunded')",
        name="recharge_orders_status",
    ),
)
sa.Index("idx_recharge_orders_account_time", recharge_orders.c.account_id, recharge_orders.c.created_at.desc())
sa.Index("idx_recharge_orders_status_time", recharge_orders.c.status, recharge_orders.c.created_at.desc())


payment_events = sa.Table(
    "payment_events",
    metadata,
    sa.Column("id", sa.Integer, primary_key=True),
    sa.Column(
        "recharge_order_id",
        sa.Integer,
        sa.ForeignKey("recharge_orders.id", ondelete="CASCADE"),
        nullable=False,
    ),
    sa.Column("payment_provider", sa.String(32), nullable=False),
    sa.Column("provider_event_id", sa.String(160), nullable=False),
    sa.Column("event_type", sa.String(64), nullable=False),
    sa.Column("processing_status", sa.String(24), nullable=False),
    sa.Column("signature_valid", sa.Boolean, nullable=False),
    sa.Column("payload_sha256", sa.String(64), nullable=False),
    sa.Column("error_summary", sa.Text),
    sa.Column("details_json", json_type, nullable=False, server_default=sa.text("'{}'")),
    sa.Column("received_at", timestamp_type, nullable=False),
    sa.Column("processed_at", timestamp_type),
    sa.UniqueConstraint(
        "payment_provider",
        "provider_event_id",
        name="uq_payment_events_provider_event",
    ),
    sa.CheckConstraint(
        "processing_status IN ('received', 'processed', 'ignored', 'failed')",
        name="payment_events_processing_status",
    ),
)
sa.Index("idx_payment_events_order_time", payment_events.c.recharge_order_id, payment_events.c.received_at.desc())


account_balance_ledger = sa.Table(
    "account_balance_ledger",
    metadata,
    sa.Column("id", sa.Integer, primary_key=True),
    sa.Column(
        "account_id",
        sa.Integer,
        sa.ForeignKey("accounts.id", ondelete="CASCADE"),
        nullable=False,
    ),
    sa.Column("entry_kind", sa.String(32), nullable=False),
    sa.Column("amount_micro_yuan", sa.BigInteger, nullable=False),
    sa.Column("balance_before_micro_yuan", sa.BigInteger, nullable=False),
    sa.Column("balance_after_micro_yuan", sa.BigInteger, nullable=False),
    sa.Column("token_count", sa.Integer),
    sa.Column("price_per_million_tokens_yuan", sa.Numeric(12, 6)),
    sa.Column(
        "operator_account_id",
        sa.Integer,
        sa.ForeignKey("accounts.id", ondelete="SET NULL"),
    ),
    sa.Column(
        "recharge_order_id",
        sa.Integer,
        sa.ForeignKey("recharge_orders.id", ondelete="SET NULL"),
    ),
    sa.Column("source_reference", sa.String(160)),
    sa.Column("summary", sa.Text, nullable=False),
    sa.Column("details_json", json_type, nullable=False, server_default=sa.text("'{}'")),
    sa.Column("created_at", timestamp_type, nullable=False),
    sa.UniqueConstraint("source_reference", name="uq_account_balance_ledger_source_reference"),
    sa.CheckConstraint(
        "entry_kind IN ('initial_credit', 'recharge', 'consumption', 'adjustment')",
        name="account_balance_ledger_entry_kind",
    ),
    sa.CheckConstraint("token_count >= 0", name="account_balance_ledger_token_count_non_negative"),
)
sa.Index(
    "idx_account_balance_ledger_account_time",
    account_balance_ledger.c.account_id,
    account_balance_ledger.c.created_at.desc(),
)
sa.Index(
    "idx_account_balance_ledger_recharge_order",
    account_balance_ledger.c.recharge_order_id,
)


# 工具调用审计按账号保留最多五页任务轨迹，不保存 prompt、正文或完整模型上下文。
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
