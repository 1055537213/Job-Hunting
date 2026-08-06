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
    assert ".env" in dockerignore
    assert "data/" in dockerignore
    assert "tests/" in dockerignore


def test_compose_mounts_env_read_only_and_data_persistently():
    """Compose 只读挂载配置，并把运行数据留在宿主机。"""

    compose = (ROOT / "compose.yaml").read_text(encoding="utf-8")

    assert "./.env:/app/.env:ro" in compose
    assert "./data:/app/data" in compose
    assert "8000:8000" in compose
    assert "JOB_AGENT_DOCKER_BASE_IMAGE" in compose
    assert "/api/health" in compose
    # 不把整个 .env 作为 env_file 注入，避免 compose config 展开 API Key。
    assert "env_file:" not in compose


def test_docker_learning_document_explains_stack_and_boundaries():
    """面向初学者的文档必须说明技术作用、选型理由和当前边界。"""

    document = (ROOT / "docs" / "learning" / "docker-environment.md").read_text(encoding="utf-8")

    for phrase in ("Docker", "Docker Compose", "为什么现在选用", "SQLite", "PostgreSQL"):
        assert phrase in document
