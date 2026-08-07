"""生产数据库 schema 和 Alembic 迁移的回归测试。"""

from __future__ import annotations

import json
import sqlite3

from job_hunting_agent.cli import main
from job_hunting_agent.config import load_database_settings
from job_hunting_agent.database_migrations import (
    current_database_revision,
    downgrade_database,
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


def test_upgrade_database_creates_versioned_schema_on_empty_sqlite_file(tmp_path):
    """迁移链路可在空数据库上升级，供持续集成验证版本完整性。"""

    database_path = tmp_path / "migrated.db"
    database_url = f"sqlite+pysqlite:///{database_path.as_posix()}"

    upgrade_database(database_url)

    with sqlite3.connect(database_path) as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        version = connection.execute(
            "SELECT version_num FROM alembic_version"
        ).fetchone()[0]

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
    }.issubset(tables)
    assert version == "20260807_0001"


def test_downgrade_database_returns_an_empty_revision_chain(tmp_path):
    """初始 revision 必须可回退，供部署演练和失败恢复使用。"""

    database_path = tmp_path / "downgraded.db"
    database_url = f"sqlite+pysqlite:///{database_path.as_posix()}"
    upgrade_database(database_url)

    revision = downgrade_database(database_url, "base")

    assert revision is None
    assert current_database_revision(database_url) is None
    with sqlite3.connect(database_path) as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
    assert "accounts" not in tables
    assert "rag_chunks" not in tables


def test_cli_database_config_masks_database_password(tmp_path, capsys):
    """数据库配置命令只展示脱敏 URL，不能把密码输出到终端。"""

    env_file = tmp_path / ".env"
    env_file.write_text(
        "JOB_AGENT_DATABASE_URL=postgresql://job_agent:secret@localhost:5432/job_agent\n",
        encoding="utf-8",
    )

    main(["--env-file", str(env_file), "database-config"])

    output_text = capsys.readouterr().out
    output = json.loads(output_text)
    assert output["configured"] is True
    assert output["url"] == "postgresql+psycopg://job_agent:***@localhost:5432/job_agent"
    assert "secret" not in output_text


def test_cli_config_command_does_not_create_a_default_sqlite_file(tmp_path, monkeypatch, capsys):
    """查询模型配置是只读操作，不能顺带初始化旧 SQLite 测试库。"""

    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            [
                "JOB_AGENT_LLM_PROVIDER=test-provider",
                "JOB_AGENT_LLM_MODEL=test-model",
                "JOB_AGENT_LLM_API_KEY=test-key",
                "JOB_AGENT_LLM_BASE_URL=https://example.test/v1",
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)

    main(["--env-file", str(env_file), "llm-config"])

    assert json.loads(capsys.readouterr().out)["provider"] == "test-provider"
    assert not (tmp_path / "data" / "job_agent.db").exists()
