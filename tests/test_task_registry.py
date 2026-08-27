"""后台任务注册表的独立契约测试。"""

from types import SimpleNamespace

import pytest

from job_hunting_agent.task_registry import (
    TaskRegistry,
    TaskSpec,
    background_task_catalog,
)


def test_task_registry_dispatches_by_registered_type_and_rejects_unknown_tasks() -> None:
    """Worker 只通过注册表发现和执行任务，不再依赖大型类型分支。"""

    registry = TaskRegistry(
        [
            TaskSpec(
                task_type="example",
                audit_label="示例任务",
                handler=lambda _backend, record: {"task_key": record.task_key},
            )
        ]
    )
    record = SimpleNamespace(task_type="example", task_key="task-1")

    assert registry.execute(object(), record) == {"task_key": "task-1"}
    with pytest.raises(KeyError, match="unknown"):
        registry.execute(
            object(),
            SimpleNamespace(task_type="unknown", task_key="task-2"),
        )


def test_background_task_catalog_contains_all_worker_task_types() -> None:
    """任务审计、Worker 分发和后续运维从同一个目录读取类型。"""

    catalog = background_task_catalog()

    assert [item.task_type for item in catalog] == [
        "resume_ocr",
        "github_project_analysis",
        "project_archive_analysis",
        "resume_export",
        "visual_index",
        "rag_index",
        "system_probe",
    ]
    assert all(item.audit_label for item in catalog)

