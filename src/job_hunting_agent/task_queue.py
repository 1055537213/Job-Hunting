"""Redis/Celery 后台任务边界。

业务层只依赖本模块暴露的最小队列接口，不直接拼接 Redis 命令或 Celery 参数。
任务的权威状态仍由 PostgreSQL ``background_tasks`` 表保存；队列消息只包含安全的
``task_key``，避免候选人正文进入 Redis。
"""

from __future__ import annotations

from typing import Any, Protocol

from .config import TaskQueueSettings

BACKGROUND_TASK_NAME = "job_hunting_agent.background_tasks.execute_background_task"
MAINTENANCE_QUEUE_SUFFIX = "_maintenance"
# Token 和工具调用记录均按账号保留固定分页窗口，Beat 每天触发一次兜底裁剪。
OPERATIONAL_LEDGER_RETENTION_TASK_NAME = (
    "job_hunting_agent.background_tasks.prune_operational_ledgers"
)
# 定期回收 Worker 崩溃后停留在 running 的失联任务，并重新投递安全的 task_key。
STALE_BACKGROUND_TASK_RECOVERY_TASK_NAME = (
    "job_hunting_agent.background_tasks.recover_stale_background_tasks"
)


def maintenance_queue_name(queue_name: str) -> str:
    """返回与业务队列隔离的 Beat 维护队列名称。"""

    return f"{queue_name}{MAINTENANCE_QUEUE_SUFFIX}"
# RAG 增量索引使用独立任务类型，Web、应用门面和 Worker 共用这个稳定标识。
RAG_INDEX_TASK_TYPE = "rag_index"
# 图片和 PDF 视觉页使用独立任务，失败不会回滚已经保存的项目证据。
VISUAL_INDEX_TASK_TYPE = "visual_index"
# 扫描 PDF OCR 先完成正文提取，再由 Worker 创建独立的 RAG 增量索引任务。
RESUME_OCR_TASK_TYPE = "resume_ocr"
# 公开 GitHub 仓库分析会下载受限归档并生成待确认项目经历卡片。
GITHUB_PROJECT_ANALYSIS_TASK_TYPE = "github_project_analysis"
# 用户上传的项目 ZIP 只通过数据库资源 ID 交给 Worker 扫描文件清单和生成项目卡片。
PROJECT_ARCHIVE_ANALYSIS_TASK_TYPE = "project_archive_analysis"
# 定制简历的模型改写和 DOCX/PDF 导出在 Worker 中执行。
RESUME_EXPORT_TASK_TYPE = "resume_export"


class TaskQueueError(RuntimeError):
    """任务队列未配置、不可访问或投递失败。"""


class BackgroundTaskQueue(Protocol):
    """业务层需要的最小队列协议。"""

    def health_check(self) -> None:
        """确认 broker 可连接。"""

    def enqueue(self, task_key: str) -> None:
        """把数据库中的任务键投递给 Worker。"""


def build_celery_app(settings: TaskQueueSettings) -> Any:
    """根据类型化配置构造 Celery 应用，不在业务模块暴露供应商 SDK。"""

    if not settings.enabled or not settings.redis_url:
        raise TaskQueueError("后台任务队列未启用。")
    try:
        from celery import Celery
        from celery.schedules import crontab
    except ModuleNotFoundError as error:
        raise TaskQueueError(
            "后台任务队列需要 celery[redis]，请先安装项目依赖。"
        ) from error

    app = Celery("job_hunting_agent", broker=settings.redis_url)
    # 任务结果以 PostgreSQL 状态表为准，Redis 不保存第二份可能过期的业务结果。
    app.conf.update(
        task_default_queue=settings.queue_name,
        task_serializer="json",
        accept_content=["json"],
        result_serializer="json",
        task_ignore_result=True,
        task_track_started=False,
        task_acks_late=True,
        task_reject_on_worker_lost=True,
        worker_prefetch_multiplier=1,
        broker_connection_retry_on_startup=True,
        timezone="Asia/Shanghai",
        enable_utc=True,
        task_time_limit=settings.task_time_limit_seconds,
        task_soft_time_limit=settings.task_soft_time_limit_seconds,
        beat_schedule={
            "prune-operational-ledgers-daily": {
                "task": OPERATIONAL_LEDGER_RETENTION_TASK_NAME,
                "schedule": crontab(hour=0, minute=0),
                "options": {"queue": maintenance_queue_name(settings.queue_name)},
            },
            "recover-stale-background-tasks": {
                "task": STALE_BACKGROUND_TASK_RECOVERY_TASK_NAME,
                "schedule": 60.0,
                "options": {"queue": maintenance_queue_name(settings.queue_name)},
            },
        },
    )
    return app


class CeleryTaskQueue:
    """使用 Redis broker 投递任务的 Celery 适配器。"""

    def __init__(self, settings: TaskQueueSettings, celery_app: Any | None = None):
        """保存配置并允许测试注入假的 Celery 应用。"""

        self.settings = settings
        self.app = celery_app or build_celery_app(settings)

    def health_check(self) -> None:
        """通过 redis-py 的 PING 验证 broker，避免只检查 URL 格式。"""

        if not self.settings.redis_url:
            raise TaskQueueError("后台任务队列缺少 Redis URL。")
        try:
            import redis

            client = redis.Redis.from_url(
                self.settings.redis_url,
                socket_connect_timeout=3,
                socket_timeout=3,
                decode_responses=True,
            )
            client.ping()
        except Exception as error:
            raise TaskQueueError("Redis 任务队列不可用。") from error
        finally:
            close = locals().get("client")
            if close is not None:
                close.close()

    def enqueue(self, task_key: str) -> None:
        """以数据库任务键作为 Celery task_id 投递消息。"""

        if not task_key.strip():
            raise TaskQueueError("后台任务键不能为空。")
        try:
            self.app.send_task(
                BACKGROUND_TASK_NAME,
                args=[task_key],
                kwargs={},
                task_id=task_key,
                queue=self.settings.queue_name,
            )
        except Exception as error:
            raise TaskQueueError("后台任务投递失败。") from error
