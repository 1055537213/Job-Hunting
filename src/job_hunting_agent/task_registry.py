"""后台任务的独立发现和执行注册表。"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any, Protocol

from .task_queue import (
    GITHUB_PROJECT_ANALYSIS_TASK_TYPE,
    PROJECT_ARCHIVE_ANALYSIS_TASK_TYPE,
    RAG_INDEX_TASK_TYPE,
    RESUME_EXPORT_TASK_TYPE,
    RESUME_OCR_TASK_TYPE,
    SYSTEM_PROBE_TASK_TYPE,
    VISUAL_INDEX_TASK_TYPE,
)


class TaskRecord(Protocol):
    """任务注册表执行分发所需的最小记录接口。"""

    task_type: str
    task_key: str


TaskHandler = Callable[[Any, TaskRecord], dict[str, object]]


@dataclass(frozen=True, slots=True, kw_only=True)
class TaskMetadata:
    """不含 Worker handler 的后台任务目录项。"""

    task_type: str
    audit_label: str
    requires_candidate: bool = False
    trace_priority: int = 0


@dataclass(frozen=True, slots=True, kw_only=True)
class TaskSpec(TaskMetadata):
    """后台任务目录项和对应 Worker handler。"""

    handler: TaskHandler

    def __post_init__(self) -> None:
        if not self.task_type.strip():
            raise ValueError("后台任务类型不能为空。")
        if not self.audit_label.strip():
            raise ValueError(f"后台任务 {self.task_type} 缺少审计名称。")


class TaskRegistry:
    """集中后台任务发现和分发，同时保持 Worker 生命周期独立。"""

    def __init__(self, specs: Iterable[TaskSpec]):
        resolved: dict[str, TaskSpec] = {}
        for spec in specs:
            if spec.task_type in resolved:
                raise ValueError(f"后台任务类型重复：{spec.task_type}")
            resolved[spec.task_type] = spec
        if not resolved:
            raise ValueError("后台任务注册表不能为空。")
        self._specs = resolved

    def get(self, task_type: str) -> TaskSpec:
        """读取后台任务定义。"""

        try:
            return self._specs[task_type]
        except KeyError as error:
            raise KeyError(f"未知后台任务类型：{task_type}") from error

    def list_specs(self) -> tuple[TaskSpec, ...]:
        """按注册顺序返回任务定义。"""

        return tuple(self._specs.values())

    def execute(self, backend: Any, record: TaskRecord) -> dict[str, object]:
        """把已认领任务交给唯一注册的 handler。"""

        return self.get(record.task_type).handler(backend, record)


_BACKGROUND_TASK_CATALOG = (
    TaskMetadata(
        task_type=RESUME_OCR_TASK_TYPE,
        audit_label="识别扫描版简历",
        requires_candidate=True,
        trace_priority=77,
    ),
    TaskMetadata(
        task_type=GITHUB_PROJECT_ANALYSIS_TASK_TYPE,
        audit_label="分析 GitHub 项目",
        requires_candidate=True,
        trace_priority=79,
    ),
    TaskMetadata(
        task_type=PROJECT_ARCHIVE_ANALYSIS_TASK_TYPE,
        audit_label="扫描项目整包",
        requires_candidate=True,
        trace_priority=78,
    ),
    TaskMetadata(
        task_type=RESUME_EXPORT_TASK_TYPE,
        audit_label="生成职位定制简历文件",
        requires_candidate=True,
        trace_priority=76,
    ),
    TaskMetadata(
        task_type=VISUAL_INDEX_TASK_TYPE,
        audit_label="更新视觉知识索引",
        trace_priority=29,
    ),
    TaskMetadata(
        task_type=RAG_INDEX_TASK_TYPE,
        audit_label="更新 RAG 检索索引",
        trace_priority=30,
    ),
    TaskMetadata(
        task_type=SYSTEM_PROBE_TASK_TYPE,
        audit_label="检查 Worker 连通性",
        trace_priority=10,
    ),
)


def background_task_catalog() -> tuple[TaskMetadata, ...]:
    """返回 Worker、审计和运维共享的后台任务目录。"""

    return _BACKGROUND_TASK_CATALOG
