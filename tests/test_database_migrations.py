"""PostgreSQL schema 和 Alembic 迁移回归测试。"""

from __future__ import annotations

import pytest
import sqlalchemy as sa

from alembic import command
from job_hunting_agent.config import (
    load_database_settings,
    require_postgresql_database_url,
)
from job_hunting_agent.database_migrations import (
    build_alembic_config,
    current_database_revision,
    downgrade_database,
    latest_database_revision,
    upgrade_database,
)


def test_database_settings_normalize_postgresql_driver_and_mask_password(tmp_path):
    """生产数据库 URL 应统一走 psycopg 驱动，展示摘要不能泄露密码。"""

    env_file = tmp_path / ".env"
    env_file.write_text(
        "JOB_AGENT_DATABASE_URL=postgresql://job_agent:secret@localhost:5432/job_agent\n",
        encoding="utf-8",
    )

    settings = load_database_settings(env_file, environ={})

    assert settings.configured is True
    assert settings.url == "postgresql+psycopg://job_agent:secret@localhost:5432/job_agent"
    assert settings.masked_url == "postgresql+psycopg://job_agent:***@localhost:5432/job_agent"


def test_runtime_database_requirement_rejects_missing_and_non_postgresql_urls(tmp_path):
    """网页和迁移入口拒绝缺失或非 PostgreSQL 数据库配置。"""

    with pytest.raises(ValueError, match="缺少 JOB_AGENT_DATABASE_URL"):
        require_postgresql_database_url(load_database_settings(tmp_path / "missing.env", environ={}))

    invalid_env = tmp_path / "invalid.env"
    invalid_env.write_text("JOB_AGENT_DATABASE_URL=mysql+pymysql://user@localhost/job_agent\n", encoding="utf-8")
    with pytest.raises(ValueError, match="数据库 URL 只支持"):
        load_database_settings(invalid_env, environ={})


def test_upgrade_database_creates_versioned_postgresql_schema(temporary_database_url):
    """迁移链路可在独立 PostgreSQL schema 上创建完整生产表。"""

    upgrade_database(temporary_database_url)
    engine = sa.create_engine(temporary_database_url)
    try:
        with engine.connect() as connection:
            tables = {
                row[0]
                for row in connection.execute(
                    sa.text(
                        "SELECT table_name FROM information_schema.tables "
                        "WHERE table_schema = current_schema()"
                    )
                )
            }
            version = connection.execute(sa.text("SELECT version_num FROM alembic_version")).scalar_one()
            candidate_columns = {
                row[0]
                for row in connection.execute(
                    sa.text(
                        "SELECT column_name FROM information_schema.columns "
                        "WHERE table_schema = current_schema() AND table_name = 'candidate_profiles'"
                    )
                )
            }
            job_columns = {
                row[0]
                for row in connection.execute(
                    sa.text(
                        "SELECT column_name FROM information_schema.columns "
                        "WHERE table_schema = current_schema() AND table_name = 'jobs'"
                    )
                )
            }
    finally:
        engine.dispose()

    assert {
        "accounts",
        "auth_sessions",
        "candidate_profiles",
        "chat_sessions",
        "chat_messages",
        "jobs",
        "long_texts",
        "project_experience_cards",
        "rag_chunks",
        "resume_artifacts",
        "resume_drafts",
        "usage_events",
        "background_tasks",
    }.issubset(tables)
    assert version == latest_database_revision()
    assert "content_fingerprint" in candidate_columns
    assert {"content_fingerprint", "import_method", "captured_at"}.issubset(job_columns)


def test_upgrade_repairs_legacy_0003_without_job_import_provenance(temporary_database_url):
    """版本已到 0003 但缺少来源列的旧库，应被后续迁移安全修复。"""

    upgrade_database(temporary_database_url, "20260810_0002")
    engine = sa.create_engine(temporary_database_url)
    try:
        with engine.begin() as connection:
            # 模拟历史上已经写入内容去重列、但尚未写入来源追溯列的已发布 0003 状态。
            connection.execute(sa.text("ALTER TABLE jobs ADD COLUMN content_fingerprint VARCHAR(64)"))
        command.stamp(build_alembic_config(temporary_database_url), "20260814_0003")
        upgrade_database(temporary_database_url)
        with engine.connect() as connection:
            job_columns = {
                row[0]
                for row in connection.execute(
                    sa.text(
                        "SELECT column_name FROM information_schema.columns "
                        "WHERE table_schema = current_schema() AND table_name = 'jobs'"
                    )
                )
            }
            version = connection.execute(sa.text("SELECT version_num FROM alembic_version")).scalar_one()
    finally:
        engine.dispose()

    assert {"content_fingerprint", "import_method", "captured_at"}.issubset(job_columns)
    assert version == latest_database_revision()


def test_downgrade_database_returns_an_empty_revision_chain(temporary_database_url):
    """初始 revision 必须可回退，供部署演练和失败恢复使用。"""

    upgrade_database(temporary_database_url)
    revision = downgrade_database(temporary_database_url, "base")

    assert revision is None
    assert current_database_revision(temporary_database_url) is None
    engine = sa.create_engine(temporary_database_url)
    try:
        with engine.connect() as connection:
            tables = {
                row[0]
                for row in connection.execute(
                    sa.text(
                        "SELECT table_name FROM information_schema.tables "
                        "WHERE table_schema = current_schema()"
                    )
                )
            }
    finally:
        engine.dispose()
    assert "accounts" not in tables
    assert "rag_chunks" not in tables
