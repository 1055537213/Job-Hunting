"""PostgreSQL schema 和 Alembic 迁移回归测试。"""

from __future__ import annotations

from datetime import UTC, datetime

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
        inspector = sa.inspect(engine)
        archive_unique_constraints = {
            item["name"] for item in inspector.get_unique_constraints("project_archive_imports")
        }
        collection_unique_constraints = {
            item["name"] for item in inspector.get_unique_constraints("project_collection_sessions")
        }
        archive_indexes = {item["name"] for item in inspector.get_indexes("project_archive_imports")}
        collection_indexes = {
            item["name"] for item in inspector.get_indexes("project_collection_sessions")
        }
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
            vector_extension_schema = connection.execute(
                sa.text(
                    "SELECT n.nspname FROM pg_extension AS e "
                    "JOIN pg_namespace AS n ON n.oid = e.extnamespace "
                    "WHERE e.extname = 'vector'"
                )
            ).scalar_one()
            trigram_extension_schema = connection.execute(
                sa.text(
                    "SELECT n.nspname FROM pg_extension AS e "
                    "JOIN pg_namespace AS n ON n.oid = e.extnamespace "
                    "WHERE e.extname = 'pg_trgm'"
                )
            ).scalar_one()
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
            balance_ledger_columns = {
                row[0]
                for row in connection.execute(
                    sa.text(
                        "SELECT column_name FROM information_schema.columns "
                        "WHERE table_schema = current_schema() "
                        "AND table_name = 'account_balance_ledger'"
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
        "visual_knowledge_items",
        "rag_chunks",
        "resume_artifacts",
        "resume_drafts",
        "knowledge_assets",
        "knowledge_asset_versions",
        "usage_events",
        "tool_call_traces",
        "background_tasks",
        "account_balances",
        "account_balance_ledger",
        "recharge_orders",
        "payment_events",
        "admin_audit_events",
    }.issubset(tables)
    assert version == latest_database_revision()
    assert vector_extension_schema == "public"
    assert trigram_extension_schema == "public"
    assert "content_fingerprint" in candidate_columns
    assert {"content_fingerprint", "import_method", "captured_at"}.issubset(job_columns)
    assert {"operator_account_id", "recharge_order_id"}.issubset(balance_ledger_columns)
    assert "uq_project_archive_imports_card" not in archive_unique_constraints
    assert "uq_project_collection_card" not in collection_unique_constraints
    assert "idx_project_archive_imports_card" in archive_indexes
    assert "idx_project_collection_card" in collection_indexes


def test_knowledge_asset_migration_backfills_existing_source_resumes(temporary_database_url):
    """升级统一资产表时，历史简历原件应原地关联，不能移动或复制对象。"""

    upgrade_database(temporary_database_url, "20260825_0011")
    engine = sa.create_engine(temporary_database_url)
    now = datetime.now(UTC)
    try:
        with engine.begin() as connection:
            account_id = connection.execute(
                sa.text(
                    """
                    INSERT INTO accounts (
                        email, password_hash, display_name, role, status,
                        must_change_password, created_at, updated_at
                    ) VALUES (
                        'legacy-resume@example.com', 'test-hash', NULL, 'user', 'active',
                        FALSE, :created_at, :updated_at
                    ) RETURNING id
                    """
                ),
                {"created_at": now, "updated_at": now},
            ).scalar_one()
            candidate_id = connection.execute(
                sa.text(
                    """
                    INSERT INTO candidate_profiles (
                        account_id, name, status, education, experience_years,
                        salary_floor_k, expected_salary_k, skills_json,
                        preferred_cities_json, acceptable_cities_json,
                        preference_weights_json, target_directions_json, unacceptable_json
                    ) VALUES (
                        :account_id, '历史候选人', '在职', '本科', 2,
                        NULL, NULL, CAST('{}' AS JSONB), CAST('[]' AS JSONB),
                        CAST('[]' AS JSONB), CAST('{}' AS JSONB),
                        CAST('[]' AS JSONB), CAST('[]' AS JSONB)
                    ) RETURNING id
                    """
                ),
                {"account_id": account_id},
            ).scalar_one()
            artifact_id = connection.execute(
                sa.text(
                    """
                    INSERT INTO resume_artifacts (
                        account_id, candidate_id, version, artifact_type,
                        original_filename, download_filename, storage_key, media_type,
                        file_size, sha256, extraction_method, extracted_text, text_length,
                        page_count, status, created_at, scan_status, scan_engine
                    ) VALUES (
                        :account_id, :candidate_id, 1, 'source',
                        'legacy.pdf', 'legacy.pdf', 'resumes/legacy.pdf', 'application/pdf',
                        1024, :sha256, 'pdf_text', '历史简历正文', 6,
                        1, 'ready', :created_at, 'clean', 'clamav'
                    ) RETURNING id
                    """
                ),
                {
                    "account_id": account_id,
                    "candidate_id": candidate_id,
                    "sha256": "f" * 64,
                    "created_at": now,
                },
            ).scalar_one()

        upgrade_database(temporary_database_url)
        with engine.connect() as connection:
            linked = connection.execute(
                sa.text(
                    """
                    SELECT artifact.knowledge_asset_id, artifact.knowledge_asset_version_id,
                           asset.asset_kind, version.version_number, version.storage_key,
                           version.processing_status, version.scan_status
                    FROM resume_artifacts AS artifact
                    JOIN knowledge_assets AS asset ON asset.id = artifact.knowledge_asset_id
                    JOIN knowledge_asset_versions AS version
                      ON version.id = artifact.knowledge_asset_version_id
                    WHERE artifact.id = :artifact_id
                    """
                ),
                {"artifact_id": artifact_id},
            ).mappings().one()
    finally:
        engine.dispose()

    assert linked["knowledge_asset_id"] is not None
    assert linked["knowledge_asset_version_id"] is not None
    assert linked["asset_kind"] == "resume"
    assert linked["version_number"] == 1
    assert linked["storage_key"] == "resumes/legacy.pdf"
    assert linked["processing_status"] == "ready"
    assert linked["scan_status"] == "clean"


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


def test_zero_starting_balance_migration_preserves_user_recharge(temporary_database_url):
    """取消历史初始化赠送额时，用户实际充值必须保留并可审计。"""

    upgrade_database(temporary_database_url, "20260820_0006")
    engine = sa.create_engine(temporary_database_url)
    now = datetime.now(UTC)
    try:
        with engine.begin() as connection:
            account_id = connection.execute(
                sa.text(
                    """
                    INSERT INTO accounts (
                        email, password_hash, display_name, role, status,
                        must_change_password, created_at, updated_at
                    ) VALUES (
                        :email, :password_hash, NULL, 'user', 'active',
                        FALSE, :created_at, :updated_at
                    )
                    RETURNING id
                    """
                ),
                {
                    "email": "migration-zero-balance@example.com",
                    "password_hash": "test-hash",
                    "created_at": now,
                    "updated_at": now,
                },
            ).scalar_one()

        upgrade_database(temporary_database_url, "20260822_0007")
        with engine.begin() as connection:
            connection.execute(
                sa.text(
                    """
                    UPDATE account_balances
                    SET balance_micro_yuan = 120000000,
                        total_recharge_micro_yuan = 120000000,
                        updated_at = :updated_at
                    WHERE account_id = :account_id
                    """
                ),
                {"account_id": account_id, "updated_at": now},
            )
            connection.execute(
                sa.text(
                    """
                    INSERT INTO account_balance_ledger (
                        account_id, entry_kind, amount_micro_yuan,
                        balance_before_micro_yuan, balance_after_micro_yuan,
                        token_count, price_per_million_tokens_yuan,
                        source_reference, summary, details_json, created_at
                    ) VALUES (
                        :account_id, 'recharge', 20000000,
                        100000000, 120000000,
                        NULL, NULL,
                        :source_reference, '测试充值', CAST('{}' AS JSONB), :created_at
                    )
                    """
                ),
                {
                    "account_id": account_id,
                    "source_reference": f"migration-test-recharge:{account_id}",
                    "created_at": now,
                },
            )

        upgrade_database(temporary_database_url)
        with engine.connect() as connection:
            balance = connection.execute(
                sa.text(
                    """
                    SELECT balance_micro_yuan, total_recharge_micro_yuan
                    FROM account_balances
                    WHERE account_id = :account_id
                    """
                ),
                {"account_id": account_id},
            ).mappings().one()
            ledger = connection.execute(
                sa.text(
                    """
                    SELECT entry_kind, amount_micro_yuan
                    FROM account_balance_ledger
                    WHERE account_id = :account_id
                    ORDER BY id
                    """
                ),
                {"account_id": account_id},
            ).mappings().all()
            version = connection.execute(sa.text("SELECT version_num FROM alembic_version")).scalar_one()
    finally:
        engine.dispose()

        assert version == latest_database_revision()
    assert balance["balance_micro_yuan"] == 20_000_000
    assert balance["total_recharge_micro_yuan"] == 20_000_000
    assert [(row["entry_kind"], row["amount_micro_yuan"]) for row in ledger] == [
        ("initial_credit", 100_000_000),
        ("recharge", 20_000_000),
        ("adjustment", -100_000_000),
    ]


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
