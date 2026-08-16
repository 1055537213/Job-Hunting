"""Docker 本地开发环境的静态回归测试。"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_docker_files_keep_runtime_data_and_secrets_out_of_image():
    """Dockerfile/ignore 规则必须保留数据卷，并排除密钥和运行时数据。"""

    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    dockerignore = (ROOT / ".dockerignore").read_text(encoding="utf-8")

    assert "ARG BASE_IMAGE=python:3.12-slim" in dockerfile
    assert "FROM ${BASE_IMAGE}" in dockerfile
    assert "USER appuser" in dockerfile
    assert "--host" in dockerfile
    assert "0.0.0.0" in dockerfile
    assert "--db" not in dockerfile
    assert "--rag-dir" not in dockerfile
    assert ".env" in dockerignore
    assert "data/" in dockerignore
    assert "tests/" in dockerignore


def test_compose_mounts_env_read_only_and_starts_postgres_before_web():
    """Compose 只读挂载配置，并在 Web 前完成 PostgreSQL 与 Alembic 迁移。"""

    compose = (ROOT / "compose.yaml").read_text(encoding="utf-8")

    assert "./.env:/app/.env:ro" in compose
    assert "./data:/app/data" not in compose
    assert "8000:8000" in compose
    assert "JOB_AGENT_DOCKER_BASE_IMAGE" in compose
    assert "pgvector/pgvector:pg16" in compose
    assert "JOB_AGENT_DATABASE_URL" in compose
    assert '["alembic", "upgrade", "head"]' in compose
    # 仅禁止已删除的旧 CLI 启动命令；对象存储 bucket 可以包含项目名称。
    assert '["job-agent"' not in compose
    assert "service_completed_successfully" in compose
    assert "postgres_data" in compose
    assert "minio_data" in compose
    assert "minio/minio" in compose
    assert "redis_data" in compose
    assert "redis:" in compose
    assert "JOB_AGENT_REDIS_URL" in compose
    assert "worker:" in compose
    assert "job-agent-worker" in compose
    assert "JOB_AGENT_OBJECT_STORAGE_BACKEND" in compose
    assert "http://minio:9000" in compose
    assert "JOB_AGENT_OBJECT_STORAGE_AUTO_CREATE_BUCKET" in compose
    assert "/api/health" in compose
    # 不把整个 .env 作为 env_file 注入，避免 compose config 展开 API Key。
    assert "env_file:" not in compose


def test_development_compose_mounts_source_and_enables_web_reload():
    """开发覆盖配置应只为本地编辑增加源码挂载和自动重载。"""

    compose = (ROOT / "compose.yaml").read_text(encoding="utf-8")
    development_compose = (ROOT / "compose.dev.yaml").read_text(encoding="utf-8")

    assert "./src:/app/src:ro" not in compose
    assert "./src:/app/src:ro" in development_compose
    assert "--reload" in development_compose
    assert "--reload-dir" in development_compose
    assert "/app/src" in development_compose
    assert "--db" not in development_compose
    assert "--rag-dir" not in development_compose


def test_docker_learning_document_explains_stack_and_boundaries():
    """面向初学者的文档必须说明技术作用、选型理由和当前边界。"""

    document = (ROOT / "docs" / "learning" / "docker-environment.md").read_text(encoding="utf-8")

    for phrase in ("Docker", "Docker Compose", "为什么现在选用", "PostgreSQL", "pgvector", "热更新"):
        assert phrase in document
