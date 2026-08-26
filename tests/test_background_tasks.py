"""Redis/Celery 后台任务基础设施测试。

数据库生命周期测试使用真实隔离 PostgreSQL schema；Celery 投递使用假的 producer，
避免单元测试必须启动 Redis。真实 Compose 回环会另外验证 broker 和 Worker。
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from job_hunting_agent.app import JobHuntingApp
from job_hunting_agent.background_tasks import (
    background_task_error_policy,
    purge_old_operational_audit_records,
    recover_stale_background_tasks,
    run_registered_task,
)
from job_hunting_agent.config import TaskQueueSettings, load_task_queue_settings
from job_hunting_agent.llm import StaticLLMClient
from job_hunting_agent.models import (
    CandidateProfileInput,
    RAGIndexStats,
    ToolCallTraceRecord,
    UsageEventRecord,
)
from job_hunting_agent.model_resilience import ModelCircuitOpenError
from job_hunting_agent.rag import RAGProviderRequestError
from job_hunting_agent.resume_document import ResumeFileStore
from job_hunting_agent.storage import InsufficientBalanceError
from job_hunting_agent.task_queue import (
    ACCOUNT_EMAIL_DELIVERY_TASK_NAME,
    CeleryAccountEmailQueue,
    CeleryTaskQueue,
    TaskQueueError,
    build_celery_app,
    maintenance_queue_name,
)
from job_hunting_agent.web import load_or_new_tool_trace, new_task_trace, owned_or_new_root_request_id


class FakeCeleryProducer:
    """记录 Celery send_task 参数，验证队列边界不携带业务正文。"""

    def __init__(self) -> None:
        """初始化投递记录。"""

        self.calls: list[dict[str, object]] = []

    def send_task(self, name: str, **kwargs: object) -> None:
        """保存一次投递调用。"""

        self.calls.append({"name": name, **kwargs})


def test_insufficient_balance_error_is_actionable_and_non_retryable() -> None:
    """余额不足是用户可处理的业务错误，必须显示固定提示且不重复消耗重试次数。"""

    summary, retryable = background_task_error_policy(
        InsufficientBalanceError(),
        "rag_index",
    )

    assert summary == "余额不足，请先充值后重试。"
    assert retryable is False


def test_model_circuit_error_is_safe_and_retryable() -> None:
    """模型熔断只返回低敏摘要，并允许后台任务退避重试。"""

    summary, retryable = background_task_error_policy(
        ModelCircuitOpenError(5),
        "rag_index",
    )

    assert summary == "模型服务暂时不可用，任务将在稍后自动重试。"
    assert retryable is True


def test_non_transient_rag_error_is_not_retried() -> None:
    """向量模型鉴权/响应错误应停止自动重试，避免持续打满任务队列。"""

    summary, retryable = background_task_error_policy(
        RAGProviderRequestError("invalid api key", status_code=401),
        "rag_index",
    )

    assert summary == "向量模型请求失败，请检查模型配置或响应格式。"
    assert retryable is False


class FailOnceCeleryProducer(FakeCeleryProducer):
    """第一次投递模拟 Redis 故障，第二次恢复，用于验证失败幂等任务可重投。"""

    def __init__(self) -> None:
        super().__init__()
        self.failed = False

    def send_task(self, name: str, **kwargs: object) -> None:
        if not self.failed:
            self.failed = True
            raise RuntimeError("redis unavailable")
        super().send_task(name, **kwargs)


def test_celery_beat_maintenance_tasks_use_a_separate_queue() -> None:
    """Beat 维护任务不能和业务任务共用队列，否则单 Worker 故障时无法回收任务。"""

    settings = TaskQueueSettings(
        enabled=True,
        redis_url="redis://:secret@redis:6379/0",
        queue_name="business_tasks",
    )

    celery_app = build_celery_app(settings)
    schedule = celery_app.conf.beat_schedule

    assert celery_app.conf.task_default_queue == "business_tasks"
    assert maintenance_queue_name(settings.queue_name) == "business_tasks_maintenance"
    assert (
        schedule["recover-stale-background-tasks"]["options"]["queue"]
        == "business_tasks_maintenance"
    )
    assert (
        schedule["prune-operational-ledgers-daily"]["options"]["queue"]
        == "business_tasks_maintenance"
    )
    assert (
        schedule["dispatch-due-account-emails"]["options"]["queue"]
        == "business_tasks_maintenance"
    )


def test_account_email_queue_sends_only_outbox_id() -> None:
    """账号邮件队列不能携带邮箱、操作链接或令牌。"""

    producer = FakeCeleryProducer()
    settings = TaskQueueSettings(
        enabled=True,
        redis_url="redis://:secret@redis:6379/0",
        queue_name="business_tasks",
    )

    CeleryAccountEmailQueue(settings, celery_app=producer).enqueue(42)

    assert producer.calls == [
        {
            "name": ACCOUNT_EMAIL_DELIVERY_TASK_NAME,
            "args": [42],
            "kwargs": {},
            "task_id": "account-email-42",
            "queue": "business_tasks",
        }
    ]


def test_task_queue_settings_require_redis_url_when_enabled(tmp_path: Path) -> None:
    """开启任务队列后必须显式提供 Redis URL。"""

    env_file = tmp_path / ".env"
    env_file.write_text("JOB_AGENT_TASK_QUEUE_ENABLED=true\n", encoding="utf-8")

    try:
        load_task_queue_settings(env_file, environ={})
    except ValueError as error:
        assert "JOB_AGENT_REDIS_URL" in str(error)
    else:  # pragma: no cover - 配置校验失效时才会执行
        raise AssertionError("启用后台任务队列却没有拒绝缺失 Redis URL")


def test_task_queue_settings_require_stale_window_after_hard_limit(tmp_path: Path) -> None:
    """失联回收必须晚于 Celery 硬超时，避免误回收仍在执行的任务。"""

    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            [
                "JOB_AGENT_TASK_QUEUE_ENABLED=true",
                "JOB_AGENT_REDIS_URL=redis://:secret@redis:6379/0",
                "JOB_AGENT_TASK_TIME_LIMIT_SECONDS=900",
                "JOB_AGENT_TASK_SOFT_TIME_LIMIT_SECONDS=840",
                "JOB_AGENT_TASK_STALE_AFTER_SECONDS=900",
            ]
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="STALE_AFTER_SECONDS"):
        load_task_queue_settings(env_file, environ={})


def test_celery_queue_sends_only_task_key() -> None:
    """业务队列消息只包含 task_key，不把 payload 正文交给 Redis。"""

    producer = FakeCeleryProducer()
    settings = TaskQueueSettings(
        enabled=True,
        redis_url="redis://:secret@redis:6379/0",
    )
    queue = CeleryTaskQueue(settings, celery_app=producer)

    queue.enqueue("task-key-1")

    assert producer.calls == [
        {
            "name": "job_hunting_agent.background_tasks.execute_background_task",
            "args": ["task-key-1"],
            "kwargs": {},
            "task_id": "task-key-1",
            "queue": "job_agent",
        }
    ]


def test_background_task_lifecycle_and_idempotency(database_url: str, account_id: int, tmp_path: Path) -> None:
    """任务登记、幂等复用、认领、进度和完成状态都落在 PostgreSQL。"""

    producer = FakeCeleryProducer()
    settings = TaskQueueSettings(enabled=True, redis_url="redis://:secret@redis:6379/0")
    app = JobHuntingApp(
        database_url=database_url,
        object_storage=ResumeFileStore(tmp_path / "resumes"),
        task_queue=CeleryTaskQueue(settings, celery_app=producer),
        semantic_matching=False,
    )

    first = app.enqueue_background_task(
        account_id=account_id,
        task_type="system_probe",
        payload={"purpose": "test-only"},
        idempotency_key="probe-once",
        max_attempts=1,
    )
    duplicate = app.enqueue_background_task(
        account_id=account_id,
        task_type="system_probe",
        payload={"purpose": "different-payload-is-ignored"},
        idempotency_key="probe-once",
        max_attempts=1,
    )

    assert duplicate.task_key == first.task_key
    assert len(producer.calls) == 1
    assert "different-payload" not in str(producer.calls[0])

    completed = run_registered_task(app, first.task_key)

    assert completed["status"] == "succeeded"
    saved = app.get_background_task(first.task_key, account_id=account_id)
    assert saved.status == "succeeded"
    assert saved.progress == 100
    assert saved.attempt == 1


def test_duplicate_worker_claim_does_not_execute_task_twice(
    database_url: str,
    account_id: int,
    tmp_path: Path,
) -> None:
    """同一个 task_key 被重复交付时，只有第一个 Worker 能取得执行权。"""

    producer = FakeCeleryProducer()
    app = JobHuntingApp(
        database_url=database_url,
        object_storage=ResumeFileStore(tmp_path / "resumes"),
        task_queue=CeleryTaskQueue(
            TaskQueueSettings(enabled=True, redis_url="redis://:secret@redis:6379/0"),
            celery_app=producer,
        ),
        semantic_matching=False,
    )
    task = app.enqueue_system_probe(account_id)

    first_claim = app.store.claim_background_task(task.task_key)
    second_claim = app.store.claim_background_task(task.task_key)

    assert first_claim is not None
    assert second_claim is None
    completed = run_registered_task(app, task.task_key, claimed_record=first_claim)
    duplicate_delivery = run_registered_task(app, task.task_key)
    assert completed["status"] == "succeeded"
    assert duplicate_delivery["status"] == "succeeded"
    assert app.get_background_task(task.task_key, account_id=account_id).attempt == 1


def test_stale_worker_task_is_requeued_and_redelivered(
    database_url: str,
    account_id: int,
    tmp_path: Path,
) -> None:
    """Worker 认领后崩溃时，Beat 回收任务并重新投递同一个 task_key。"""

    producer = FakeCeleryProducer()
    app = JobHuntingApp(
        database_url=database_url,
        object_storage=ResumeFileStore(tmp_path / "resumes"),
        task_queue=CeleryTaskQueue(
            TaskQueueSettings(enabled=True, redis_url="redis://:secret@redis:6379/0"),
            celery_app=producer,
        ),
        semantic_matching=False,
    )
    task = app.enqueue_background_task(
        account_id=account_id,
        task_type="system_probe",
        max_attempts=2,
    )
    claimed = app.store.claim_background_task(task.task_key)
    assert claimed is not None

    with app.store.connect() as conn:
        conn.execute(
            "UPDATE background_tasks SET updated_at = ? WHERE task_key = ?",
            ("2000-01-01T00:00:00+00:00", task.task_key),
        )

    counts = recover_stale_background_tasks(app)

    assert counts == {"requeued": 1, "failed": 0}
    recovered = app.get_background_task(task.task_key, account_id=account_id)
    assert recovered.status == "queued"
    assert recovered.attempt == 1
    assert recovered.started_at is None
    assert producer.calls[-1]["args"] == [task.task_key]

    second_claim = app.store.claim_background_task(task.task_key)
    assert second_claim is not None
    with app.store.connect() as conn:
        conn.execute(
            "UPDATE background_tasks SET updated_at = ? WHERE task_key = ?",
            ("2000-01-01T00:00:00+00:00", task.task_key),
        )

    final_counts = recover_stale_background_tasks(app)

    assert final_counts == {"requeued": 0, "failed": 1}
    failed = app.get_background_task(task.task_key, account_id=account_id)
    assert failed.status == "failed"
    assert failed.error_summary == "Worker 执行超时或进程失联，已达到最大重试次数。"
    assert len(producer.calls) == 2
    assert recover_stale_background_tasks(app) == {"requeued": 0, "failed": 0}


def test_failed_idempotent_task_is_restored_and_redelivered(
    database_url: str,
    account_id: int,
    tmp_path: Path,
) -> None:
    """Redis 首次投递失败后，同一个幂等请求应恢复原任务而不是永久失败。"""

    producer = FailOnceCeleryProducer()
    app = JobHuntingApp(
        database_url=database_url,
        object_storage=ResumeFileStore(tmp_path / "resumes"),
        task_queue=CeleryTaskQueue(
            TaskQueueSettings(enabled=True, redis_url="redis://:secret@redis:6379/0"),
            celery_app=producer,
        ),
        semantic_matching=False,
    )

    with pytest.raises(TaskQueueError):
        app.enqueue_background_task(
            account_id=account_id,
            task_type="system_probe",
            idempotency_key="retry-probe",
            max_attempts=1,
        )
    failed = app.store.get_background_task_by_idempotency(account_id, "retry-probe")
    assert failed is not None
    assert failed.status == "failed"

    restored = app.enqueue_background_task(
        account_id=account_id,
        task_type="system_probe",
        idempotency_key="retry-probe",
        max_attempts=1,
    )

    assert restored.task_key == failed.task_key
    assert restored.status == "queued"
    assert restored.attempt == 0
    assert restored.error_summary is None
    assert producer.calls[0]["args"] == [failed.task_key]


def test_rag_index_task_runs_with_scoped_context(
    database_url: str,
    account_id: int,
    tmp_path: Path,
) -> None:
    """RAG Worker 只读取资源 ID，并把任务归属和请求链路传给 Embedding。"""

    producer = FakeCeleryProducer()
    settings = TaskQueueSettings(enabled=True, redis_url="redis://:secret@redis:6379/0")
    app = JobHuntingApp(
        database_url=database_url,
        object_storage=ResumeFileStore(tmp_path / "resumes"),
        task_queue=CeleryTaskQueue(settings, celery_app=producer),
        semantic_matching=False,
    )
    observed: dict[str, object] = {}

    def fake_index(
        long_text_ids: list[int],
        account_id: int | None = None,
        candidate_id: int | None = None,
        session_id: str | None = None,
        root_request_id: str | None = None,
    ) -> RAGIndexStats:
        """替代真实 Embedding，专门记录 Worker 传入的安全上下文。"""

        observed.update(
            {
                "long_text_ids": long_text_ids,
                "account_id": account_id,
                "candidate_id": candidate_id,
                "session_id": session_id,
                "root_request_id": root_request_id,
            }
        )
        return RAGIndexStats(
            document_count=len(long_text_ids),
            chunk_count=2,
            persist_directory="postgresql+pgvector",
            collection_name="rag_chunks",
            mode="incremental",
        )

    app.index_rag_long_texts = fake_index  # type: ignore[method-assign]
    task = app.enqueue_rag_index_task(
        long_text_ids=[9, 9, 10],
        account_id=account_id,
        candidate_id=None,
        session_id="resume-upload-candidate-1",
        root_request_id="request-123",
        idempotency_key="rag-once",
    )

    completed = run_registered_task(app, task.task_key)

    assert completed["status"] == "succeeded"
    assert observed == {
        "long_text_ids": [9, 10],
        "account_id": account_id,
        "candidate_id": None,
        "session_id": "resume-upload-candidate-1",
        "root_request_id": "request-123",
    }
    saved = app.get_background_task(task.task_key, account_id=account_id)
    assert saved.result["index_stats"]["chunk_count"] == 2
    assert saved.progress == 100


def test_operational_audit_retention_task_purges_usage_events(database_url: str) -> None:
    """后台保留期维护任务在正常写入后应保持幂等。"""

    backend = JobHuntingApp(database_url=database_url, semantic_matching=False)
    backend.initialize()
    account = backend.store.create_account("usage-retention-task@example.com", "hashed-password")
    
    def ledger_timestamp(index: int) -> str:
        minute, second = divmod(index, 60)
        return f"2026-08-21T00:{minute:02d}:{second:02d}+00:00"

    def usage_event(call_id: str, total_tokens: int, created_at: str) -> UsageEventRecord:
        return UsageEventRecord(
            id=0,
            account_id=account.id,
            candidate_id=None,
            session_id=None,
            root_request_id=f"request-{call_id}",
            call_id=call_id,
            provider="test-provider",
            model="test-model",
            operation="agent_chat",
            input_tokens=total_tokens,
            output_tokens=0,
            total_tokens=total_tokens,
            usage_source="provider",
            status="succeeded",
            attempt=1,
            provider_request_id=None,
            raw_usage={"total_tokens": total_tokens},
            created_at=created_at,
            billable=True,
            pricing_version="test-v1",
        )

    for index in range(1, 502):
        backend.store.record_usage_event(
            usage_event(
                f"usage-task-{index:03d}",
                1,
                ledger_timestamp(index),
            )
        )

    deleted_counts = purge_old_operational_audit_records(backend)

    assert deleted_counts["deleted_usage_events"] == 0
    assert backend.store.count_usage_events(account.id) == 500
    assert [event.call_id for event in backend.store.list_usage_events(account.id, limit=100, offset=0)] == [
        f"usage-task-{index:03d}" for index in range(501, 401, -1)
    ]
    backend.store.close()


def test_tool_trace_helpers_respect_account_isolation_for_retained_records(
    database_url: str,
    tmp_path: Path,
) -> None:
    """分页窗口内的工具轨迹可以续接，但不能跨账号复用。"""

    backend = JobHuntingApp(
        database_url=database_url,
        object_storage=ResumeFileStore(tmp_path / "resumes"),
        semantic_matching=False,
    )
    backend.initialize()
    owner = backend.store.create_account("trace-owner@example.com", "hashed-password")
    foreign = backend.store.create_account("trace-foreign@example.com", "hashed-password")
    candidate_id = backend.save_candidate_profile(
        CandidateProfileInput(
            name="轨迹候选人",
            status="待补充",
            education="本科",
            experience_years=1.0,
            skills={},
            preferred_cities=[],
            acceptable_cities=[],
            salary_floor_k=None,
            expected_salary_k=None,
            target_directions=[],
            unacceptable=[],
        ),
        account_id=owner.id,
    )

    recent_root_id = "0123456789abcdef0123456789abcdef"
    recent_trace = new_task_trace(root_request_id=recent_root_id, source="chat")
    recent_trace["title"] = "最近任务"
    recent_trace["steps"] = [
        {
            "id": "step-1",
            "name": "ingest_candidate_message",
            "label": "记录候选人消息",
            "status": "completed",
            "summary": "已记录",
            "started_at": "2026-08-20T00:00:00+00:00",
            "finished_at": "2026-08-20T00:00:01+00:00",
            "attempts": [],
        }
    ]
    backend.store.record_tool_call_trace(
        ToolCallTraceRecord(
            id=0,
            account_id=owner.id,
            candidate_id=candidate_id,
            session_id="session-owner",
            root_request_id=recent_root_id,
            title="最近任务",
            status="completed",
            source="chat",
            step_count=1,
            attempt_count=1,
            last_step_name="ingest_candidate_message",
            last_error_summary=None,
            trace=recent_trace,
            created_at="2026-08-20T00:00:00+00:00",
            started_at="2026-08-20T00:00:00+00:00",
            finished_at="2026-08-20T00:00:01+00:00",
            updated_at="2026-08-20T00:00:01+00:00",
        )
    )

    old_root_id = "fedcba9876543210fedcba9876543210"
    old_trace = new_task_trace(root_request_id=old_root_id, source="project_confirmation")
    old_trace["title"] = "旧任务"
    old_trace["steps"] = [
        {
            "id": "step-1",
            "name": "confirm_project_card",
            "label": "确认项目",
            "status": "completed",
            "summary": "已确认",
            "started_at": "2000-01-01T00:00:00+00:00",
            "finished_at": "2000-01-01T00:00:01+00:00",
            "attempts": [],
        }
    ]
    backend.store.record_tool_call_trace(
        ToolCallTraceRecord(
            id=0,
            account_id=owner.id,
            candidate_id=candidate_id,
            session_id="session-owner",
            root_request_id=old_root_id,
            title="旧任务",
            status="completed",
            source="project_confirmation",
            step_count=1,
            attempt_count=1,
            last_step_name="confirm_project_card",
            last_error_summary=None,
            trace=old_trace,
            created_at="2000-01-01T00:00:00+00:00",
            started_at="2000-01-01T00:00:00+00:00",
            finished_at="2000-01-01T00:00:01+00:00",
            updated_at="2000-01-01T00:00:01+00:00",
        )
    )

    assert (
        owned_or_new_root_request_id(
            backend,
            account_id=owner.id,
            candidate_id=candidate_id,
            root_request_id=recent_root_id,
        )
        == recent_root_id
    )
    foreign_request_id = owned_or_new_root_request_id(
        backend,
        account_id=foreign.id,
        candidate_id=candidate_id,
        root_request_id=recent_root_id,
    )
    assert foreign_request_id != recent_root_id
    assert len(foreign_request_id) == 32

    recent_loaded = load_or_new_tool_trace(
        backend,
        root_request_id=recent_root_id,
        account_id=owner.id,
        source="chat",
    )
    expired_loaded = load_or_new_tool_trace(
        backend,
        root_request_id=old_root_id,
        account_id=owner.id,
        source="project_confirmation",
    )

    assert recent_loaded["title"] == "最近任务"
    assert recent_loaded["steps"][0]["name"] == "ingest_candidate_message"
    assert expired_loaded["title"] == "旧任务"
    assert expired_loaded["steps"][0]["name"] == "confirm_project_card"


def test_resume_ocr_task_creates_follow_up_rag_task(
    database_url: str,
    account_id: int,
    tmp_path: Path,
) -> None:
    """OCR Worker 处理受控简历引用后，应创建独立 RAG 任务而不传递文件正文。"""

    producer = FakeCeleryProducer()
    settings = TaskQueueSettings(enabled=True, redis_url="redis://:secret@redis:6379/0")
    resume_store = ResumeFileStore(tmp_path / "resumes")
    app = JobHuntingApp(
        database_url=database_url,
        object_storage=resume_store,
        task_queue=CeleryTaskQueue(settings, celery_app=producer),
        semantic_matching=False,
    )
    candidate_id = app.save_candidate_profile(
        CandidateProfileInput(
            name="OCR 测试候选人",
            status="待补充",
            education="本科",
            experience_years=0,
            skills={},
            preferred_cities=[],
            salary_floor_k=None,
            expected_salary_k=None,
            target_directions=[],
            unacceptable=[],
        ),
        account_id=account_id,
    )
    stored = resume_store.save(
        account_id=account_id,
        candidate_id=candidate_id,
        filename="scan.pdf",
        content=b"%PDF-test-only",
        media_type="application/pdf",
    )
    pending = app.store.save_resume_artifact(
        account_id=account_id,
        candidate_id=candidate_id,
        artifact_type="source",
        original_filename="scan.pdf",
        download_filename="scan.pdf",
        storage_key=stored.storage_key,
        media_type="application/pdf",
        file_size=stored.file_size,
        sha256=stored.sha256,
        extraction_method="pending_ocr",
        extracted_text="",
        page_count=1,
        status="processing",
    )
    observed: dict[str, object] = {}

    def fake_process(*, artifact_id: int, account_id: int, candidate_id: int):
        """模拟 OCR 成功，只返回不含正文的已完成文件元数据。"""

        observed.update(
            {
                "artifact_id": artifact_id,
                "account_id": account_id,
                "candidate_id": candidate_id,
            }
        )
        return replace(pending, status="ready", long_text_id=42)

    app.process_resume_ocr_artifact = fake_process  # type: ignore[method-assign]
    ocr_task = app.enqueue_resume_ocr_task(
        artifact_id=pending.id,
        account_id=account_id,
        candidate_id=candidate_id,
        session_id="resume-upload-candidate-test",
        root_request_id="ocr-request-123",
        idempotency_key="ocr-once",
    )

    completed = run_registered_task(app, ocr_task.task_key)

    assert completed["status"] == "succeeded"
    assert observed == {
        "artifact_id": pending.id,
        "account_id": account_id,
        "candidate_id": candidate_id,
    }
    saved_ocr_task = app.get_background_task(ocr_task.task_key, account_id=account_id)
    rag_task_key = str(saved_ocr_task.result["rag_task_key"])
    rag_task = app.get_background_task(rag_task_key, account_id=account_id)
    assert rag_task.task_type == "rag_index"
    assert rag_task.payload == {"long_text_ids": [42], "root_request_id": "ocr-request-123"}
    assert len(producer.calls) == 2
    assert all("scan.pdf" not in str(call) for call in producer.calls)


def test_resume_export_task_runs_in_worker_and_is_retry_safe(
    database_url: str,
    account_id: int,
    tmp_path: Path,
) -> None:
    """定制简历任务只传资源 ID，并在重试时复用同一草稿和两个文件。"""

    producer = FakeCeleryProducer()
    resume_store = ResumeFileStore(tmp_path / "resumes")
    app = JobHuntingApp(
        database_url=database_url,
        object_storage=resume_store,
        task_queue=CeleryTaskQueue(
            TaskQueueSettings(enabled=True, redis_url="redis://:secret@redis:6379/0"),
            celery_app=producer,
        ),
        semantic_matching=False,
    )
    candidate_id = app.save_candidate_profile(
        CandidateProfileInput(
            name="导出测试候选人",
            status="待补充",
            education="本科",
            experience_years=1,
            skills={"Python": "项目使用"},
            preferred_cities=[],
            salary_floor_k=None,
            expected_salary_k=None,
            target_directions=["Python 后端开发"],
            unacceptable=[],
        ),
        account_id=account_id,
    )
    job = app.import_job_text(
        "Python 后端开发工程师\n职位描述：负责 FastAPI 接口开发。",
        account_id=account_id,
    )
    stored = resume_store.save(
        account_id=account_id,
        candidate_id=candidate_id,
        filename="resume.docx",
        content=b"source resume",
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    )
    source = app.store.save_resume_artifact(
        account_id=account_id,
        candidate_id=candidate_id,
        job_id=None,
        artifact_type="source",
        original_filename="resume.docx",
        download_filename="resume.docx",
        storage_key=stored.storage_key,
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        file_size=stored.file_size,
        sha256=stored.sha256,
        extraction_method="docx",
        extracted_text="Python 与 FastAPI 项目经历",
        page_count=None,
    )
    llm_calls = 0

    def fake_llm(_context):
        nonlocal llm_calls
        llm_calls += 1
        return StaticLLMClient(
            "# 导出测试候选人\n\n## 求职目标\nPython 后端开发工程师\n\n## 项目经历\n- 使用 Python 与 FastAPI 开发接口。"
        )

    app.model_gateway.llm_client = fake_llm  # type: ignore[method-assign]
    task = app.enqueue_resume_export_task(
        source_artifact_id=source.id,
        job_id=job.id,
        account_id=account_id,
        candidate_id=candidate_id,
        use_rag=False,
        root_request_id="resume-export-request-123",
    )

    completed = run_registered_task(app, task.task_key)

    assert completed["status"] == "succeeded"
    assert llm_calls == 1
    saved_task = app.get_background_task(task.task_key, account_id=account_id)
    assert saved_task.result["artifact_count"] == 2
    generated = [
        artifact
        for artifact in app.list_resume_artifacts(candidate_id, account_id=account_id)
        if artifact.artifact_type == "tailored"
    ]
    assert len(generated) == 2
    assert len(app.store.list_resume_drafts(candidate_id, account_id=account_id)) == 1

    repeated = app.create_tailored_resume_from_artifact(
        candidate_id=candidate_id,
        source_artifact_id=source.id,
        job_id=job.id,
        llm_client=StaticLLMClient("should not be called"),
        use_rag=False,
        account_id=account_id,
        generation_key=task.task_key,
    )

    assert {artifact.id for artifact in repeated.artifacts} == {artifact.id for artifact in generated}
    assert len(app.store.list_resume_drafts(candidate_id, account_id=account_id)) == 1
    assert all("source resume" not in str(call) for call in producer.calls)
