"""Celery Worker 启动入口。

Web 进程只负责鉴权、登记任务和投递 task_key；本模块由独立容器启动，消费 Redis
队列并调用 ``background_tasks`` 执行器。这样 Worker 可以单独扩容和重启。
"""

from __future__ import annotations

import argparse
from pathlib import Path

from .background_tasks import register_background_tasks
from .config import DEFAULT_ENV_PATH, load_task_queue_settings
from .task_queue import TaskQueueError, build_celery_app, maintenance_queue_name


def create_worker_app(env_path: str | Path = DEFAULT_ENV_PATH):
    """加载 Worker 配置、构造 Celery 应用并注册任务。"""

    settings = load_task_queue_settings(env_path)
    if not settings.enabled:
        raise TaskQueueError(
            "Worker 未启用；请设置 JOB_AGENT_TASK_QUEUE_ENABLED=true 并配置 Redis URL。"
        )
    celery_app = build_celery_app(settings)
    register_background_tasks(celery_app, env_path=env_path)
    return celery_app


def main(argv: list[str] | None = None) -> None:
    """启动 Celery Worker，参数只控制进程级日志和并发。"""

    parser = argparse.ArgumentParser(prog="job-agent-worker")
    parser.add_argument("--env-file", default=".env")
    parser.add_argument("--log-level", default="INFO")
    parser.add_argument("--concurrency", type=int, default=1)
    parser.add_argument(
        "--queue",
        default=None,
        help="只消费指定队列；默认同时消费业务队列和 Beat 维护队列。",
    )
    args = parser.parse_args(argv)
    if args.concurrency <= 0:
        raise SystemExit("--concurrency 必须大于 0。")
    try:
        settings = load_task_queue_settings(args.env_file)
        queue_name = (
            args.queue
            or f"{settings.queue_name},{maintenance_queue_name(settings.queue_name)}"
        ).strip()
        if not queue_name:
            raise ValueError("--queue 不能为空。")
        celery_app = create_worker_app(args.env_file)
    except (TaskQueueError, ValueError) as error:
        raise SystemExit(str(error)) from error

    # 使用显式队列和单进程默认值，避免 OCR/Embedding 任务在本地开发时抢占过多内存。
    celery_app.worker_main(
        [
            "worker",
            f"--loglevel={args.log_level}",
            f"--concurrency={args.concurrency}",
            f"--queues={queue_name}",
        ]
    )


if __name__ == "__main__":
    main()
