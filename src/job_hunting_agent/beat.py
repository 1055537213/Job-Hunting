"""Celery Beat 启动入口。"""

from __future__ import annotations

import argparse
from pathlib import Path

from .task_queue import TaskQueueError
from .worker import create_worker_app


def main(argv: list[str] | None = None) -> None:
    """启动独立调度器，按计划投递维护任务。"""

    parser = argparse.ArgumentParser(prog="job-agent-beat")
    parser.add_argument("--env-file", default=".env")
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args(argv)

    try:
        celery_app = create_worker_app(Path(args.env_file))
    except (TaskQueueError, ValueError) as error:
        raise SystemExit(str(error)) from error

    celery_app.start(
        [
            "beat",
            f"--loglevel={args.log_level}",
        ]
    )


if __name__ == "__main__":
    main()
