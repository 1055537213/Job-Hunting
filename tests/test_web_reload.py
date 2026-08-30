"""Web 开发热更新启动方式的回归测试。"""

from __future__ import annotations

import os
import sys
from types import SimpleNamespace

from job_hunting_agent import web


def test_reloadable_web_app_reads_runtime_paths_from_reloader_environment(monkeypatch, tmp_path):
    """Uvicorn 重载子进程应重建使用同一 PostgreSQL 配置的认证 Web 应用。"""

    received: dict[str, object] = {}
    sentinel = object()

    def fake_create_web_app(**kwargs):
        received.update(kwargs)
        return sentinel

    monkeypatch.setattr(web, "create_web_app", fake_create_web_app)
    monkeypatch.setenv(web.WEB_RELOAD_ENV_FILE_ENV, str(tmp_path / ".env"))
    monkeypatch.setenv(web.WEB_RELOAD_RESUME_DIR_ENV, str(tmp_path / "resumes"))
    monkeypatch.setenv(
        "JOB_AGENT_DATABASE_URL",
        "postgresql+psycopg://job_agent@postgres:5432/job_agent",
    )

    assert web.create_reloadable_web_app() is sentinel
    assert received == {
        "env_file": str(tmp_path / ".env"),
        "resume_dir": str(tmp_path / "resumes"),
        "database_url": "postgresql+psycopg://job_agent@postgres:5432/job_agent",
    }


def test_web_reload_uses_an_importable_factory_and_watches_requested_directory(monkeypatch, tmp_path):
    """重载模式不能传递已创建的应用对象，否则 Uvicorn 无法重新导入新源码。"""

    calls: list[tuple[object, dict[str, object]]] = []

    def fake_run(app, **kwargs):
        calls.append((app, kwargs))

    monkeypatch.setitem(sys.modules, "uvicorn", SimpleNamespace(run=fake_run))
    monkeypatch.setenv(
        "JOB_AGENT_DATABASE_URL",
        "postgresql+psycopg://job_agent@postgres:5432/job_agent",
    )
    reload_env_keys = (
        web.WEB_RELOAD_ENV_FILE_ENV,
        web.WEB_RELOAD_RESUME_DIR_ENV,
    )
    original_environment = {key: os.environ.get(key) for key in reload_env_keys}

    try:
        for key in reload_env_keys:
            os.environ.pop(key, None)

        watch_dir = tmp_path / "source"
        web.main(
            [
                "--env-file",
                str(tmp_path / ".env"),
                "--resume-dir",
                str(tmp_path / "resumes"),
                "--host",
                "0.0.0.0",
                "--port",
                "8123",
                "--reload",
                "--reload-dir",
                str(watch_dir),
            ]
        )

        assert calls == [
            (
                "job_hunting_agent.web:create_reloadable_web_app",
                {
                    "factory": True,
                    "host": "0.0.0.0",
                    "port": 8123,
                    "reload": True,
                    "reload_dirs": [str(watch_dir)],
                    "log_config": None,
                    "access_log": False,
                },
            )
        ]
        assert os.environ[web.WEB_RELOAD_ENV_FILE_ENV] == str(tmp_path / ".env")
        assert os.environ[web.WEB_RELOAD_RESUME_DIR_ENV] == str(tmp_path / "resumes")
    finally:
        # 生产入口会为重载子进程保留变量；测试必须自行恢复，避免污染后续用例。
        for key, value in original_environment.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
