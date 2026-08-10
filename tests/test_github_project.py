"""公开 GitHub 仓库项目分析的安全边界与后台任务测试。"""

from __future__ import annotations

from io import BytesIO
from pathlib import Path
from zipfile import ZipFile

import pytest
from fastapi.testclient import TestClient

from job_hunting_agent import app as app_module
from job_hunting_agent.app import JobHuntingApp
from job_hunting_agent.background_tasks import run_registered_task
from job_hunting_agent.config import TaskQueueSettings
from job_hunting_agent.github_project import (
    InvalidGitHubRepositoryUrlError,
    analyze_public_github_repository,
    normalize_public_github_repository_url,
)
from job_hunting_agent.models import CandidateProfileInput, ProjectExperienceCard
from job_hunting_agent.resume_document import ResumeFileStore
from job_hunting_agent.task_queue import CeleryTaskQueue
from job_hunting_agent.web import create_web_app


class FakeCeleryProducer:
    """记录后台投递，避免测试依赖真实 Redis。"""

    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def send_task(self, name: str, **kwargs: object) -> None:
        self.calls.append({"name": name, **kwargs})


class RecordingQueue:
    """为 Web API 测试提供最小任务队列替身。"""

    def __init__(self) -> None:
        self.task_keys: list[str] = []

    def health_check(self) -> None:
        return None

    def enqueue(self, task_key: str) -> None:
        self.task_keys.append(task_key)


def candidate_input() -> CandidateProfileInput:
    """生成本组测试使用的最小候选人档案。"""

    return CandidateProfileInput(
        name="GitHub 测试候选人",
        status="待补充",
        education="本科",
        experience_years=1,
        skills={"Python": "项目使用"},
        preferred_cities=[],
        salary_floor_k=None,
        expected_salary_k=None,
        target_directions=["后端开发"],
        unacceptable=[],
    )


def build_repository_archive() -> bytes:
    """创建类似 GitHub codeload 输出的内存 ZIP，不写测试夹具到磁盘。"""

    output = BytesIO()
    with ZipFile(output, "w") as archive:
        archive.writestr(
            "sample-repository-main/README.md",
            "LangChain RAG Agent project with FastAPI and Docker.",
        )
        archive.writestr(
            "sample-repository-main/app.py",
            "from fastapi import FastAPI\nfrom langchain_core.messages import HumanMessage\n",
        )
        archive.writestr("sample-repository-main/.env", "API_KEY=must-not-be-read")
        archive.writestr("sample-repository-main/node_modules/large.js", "ignored")
        archive.writestr("sample-repository-main/../../outside.py", "unsafe")
    return output.getvalue()


def fake_github_fetch(url: str, _max_bytes: int) -> bytes:
    """按官方 API 与归档 URL 返回受控测试响应。"""

    if "api.github.com" in url:
        return b'{"default_branch":"main","private":false}'
    return build_repository_archive()


def test_public_github_repository_url_is_strictly_normalized() -> None:
    """只接受无参数的公开仓库首页 HTTPS 链接。"""

    reference = normalize_public_github_repository_url("https://www.github.com/openai/codex.git/")

    assert reference.owner == "openai"
    assert reference.repository == "codex"
    assert reference.canonical_url == "https://github.com/openai/codex"

    for invalid in (
        "http://github.com/openai/codex",
        "https://gitlab.com/openai/codex",
        "https://github.com/openai/codex/tree/main",
        "https://github.com/openai/codex?tab=readme",
        "https://user:password@github.com/openai/codex",
    ):
        with pytest.raises(InvalidGitHubRepositoryUrlError):
            normalize_public_github_repository_url(invalid)


def test_github_archive_analysis_filters_sensitive_and_unsafe_entries() -> None:
    """归档分析只读取受控文本文件，不执行或解压仓库代码。"""

    card = analyze_public_github_repository(
        "https://github.com/example/sample-repository",
        fetch_bytes=fake_github_fetch,
    )

    assert card.source_type == "github_public_repository"
    assert card.source_url == "https://github.com/example/sample-repository"
    assert card.source_ref == "main"
    assert card.read_files == ["README.md", "app.py"]
    assert {"LangChain", "FastAPI", "RAG", "Agent", "Docker"} <= set(card.detected_tech_stack)
    assert card.skipped_summary["sensitive_name"] == 1
    assert card.skipped_summary["dir:node_modules"] == 1
    assert card.skipped_summary["unsafe_path"] == 1


def test_github_project_card_stays_pending_and_does_not_overwrite_profile(
    account_id: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """GitHub 代码线索与旧本地分析一样，只能进入待确认项目卡片。"""

    card = ProjectExperienceCard(
        card_type="待确认项目经历卡片",
        project_name="sample-repository",
        read_files=["README.md"],
        skipped_summary={},
        detected_tech_stack=["FastAPI"],
        detected_core_features=["接口/API 服务"],
        responsibility_draft=["可能负责接口/API 服务设计"],
        highlight_draft=[],
        resume_expression_draft=["等待确认"],
        questions_for_candidate=["你负责什么？"],
        source_type="github_public_repository",
        source_url="https://github.com/example/sample-repository",
        source_ref="main",
    )
    monkeypatch.setattr(app_module, "analyze_public_github_repository", lambda _url: card)
    app = JobHuntingApp(semantic_matching=False)
    app.initialize()
    candidate_id = app.save_candidate_profile(candidate_input(), account_id=account_id)

    record = app.analyze_github_project_for_candidate(
        candidate_id,
        "https://github.com/example/sample-repository",
        account_id=account_id,
    )

    assert record.status == "待确认"
    assert record.card.source_url == "https://github.com/example/sample-repository"
    assert app.get_candidate_profile(candidate_id, account_id=account_id).skills == {"Python": "项目使用"}


def test_github_project_worker_reads_only_repository_url(
    database_url: str,
    account_id: int,
    tmp_path: Path,
) -> None:
    """Worker 从 PostgreSQL 读取安全 URL，任务消息和 payload 不携带仓库正文。"""

    producer = FakeCeleryProducer()
    queue = CeleryTaskQueue(
        TaskQueueSettings(enabled=True, redis_url="redis://:secret@redis:6379/0"),
        celery_app=producer,
    )
    app = JobHuntingApp(
        database_url=database_url,
        object_storage=ResumeFileStore(tmp_path / "resume-files"),
        task_queue=queue,
        semantic_matching=False,
    )
    candidate_id = app.save_candidate_profile(candidate_input(), account_id=account_id)
    observed: dict[str, object] = {}

    def fake_analyze(candidate: int, repository_url: str, account_id: int | None = None):
        observed.update(
            {"candidate_id": candidate, "repository_url": repository_url, "account_id": account_id}
        )
        return app.store.save_project_card(
            candidate,
            ProjectExperienceCard(
                card_type="待确认项目经历卡片",
                project_name="sample-repository",
                read_files=["README.md"],
                skipped_summary={},
                detected_tech_stack=["Python"],
                detected_core_features=[],
                responsibility_draft=["需要候选人补充本人职责边界"],
                highlight_draft=[],
                resume_expression_draft=["等待确认"],
                questions_for_candidate=["你负责什么？"],
                source_type="github_public_repository",
                source_url=repository_url,
                source_ref="main",
            ),
            account_id=account_id,
        )

    app.analyze_github_project_for_candidate = fake_analyze  # type: ignore[method-assign]
    task = app.enqueue_github_project_analysis_task(
        repository_url="https://github.com/example/sample-repository",
        account_id=account_id,
        candidate_id=candidate_id,
    )
    completed = run_registered_task(app, task.task_key)

    assert completed["status"] == "succeeded"
    assert observed == {
        "candidate_id": candidate_id,
        "repository_url": "https://github.com/example/sample-repository",
        "account_id": account_id,
    }
    assert task.payload == {"repository_url": "https://github.com/example/sample-repository"}
    assert producer.calls[0]["args"] == [task.task_key]
    assert "README.md" not in str(producer.calls[0])


def test_github_project_submission_and_confirmation_are_idempotent(
    database_url: str,
    account_id: int,
    tmp_path: Path,
) -> None:
    """同一仓库重复提交只投递一次，重复确认只保存一份项目证据和一个 RAG 任务。"""

    producer = FakeCeleryProducer()
    app = JobHuntingApp(
        database_url=database_url,
        object_storage=ResumeFileStore(tmp_path / "resume-files"),
        task_queue=CeleryTaskQueue(
            TaskQueueSettings(enabled=True, redis_url="redis://:secret@redis:6379/0"),
            celery_app=producer,
        ),
        semantic_matching=False,
    )
    candidate_id = app.save_candidate_profile(candidate_input(), account_id=account_id)

    first = app.enqueue_github_project_analysis_task(
        repository_url="https://github.com/Example/Sample-Repository",
        account_id=account_id,
        candidate_id=candidate_id,
    )
    duplicate = app.enqueue_github_project_analysis_task(
        repository_url="https://github.com/example/sample-repository",
        account_id=account_id,
        candidate_id=candidate_id,
    )

    assert duplicate.task_key == first.task_key
    assert len(producer.calls) == 1

    card = app.store.save_project_card(
        candidate_id,
        ProjectExperienceCard(
            card_type="待确认项目经历卡片",
            project_name="sample-repository",
            read_files=["README.md"],
            skipped_summary={},
            detected_tech_stack=["Python"],
            detected_core_features=["接口/API 服务"],
            responsibility_draft=["负责接口设计"],
            highlight_draft=[],
            resume_expression_draft=["使用 Python 开发接口"],
            questions_for_candidate=[],
            source_type="github_public_repository",
            source_url="https://github.com/example/sample-repository",
            source_ref="main",
        ),
        account_id=account_id,
    )
    confirmed, rag_task = app.confirm_project_card_and_enqueue_rag(
        card.id,
        "本人负责接口设计与实现。",
        account_id=account_id,
    )
    repeated, repeated_rag_task = app.confirm_project_card_and_enqueue_rag(
        card.id,
        "这次重复提交不应覆盖原确认内容。",
        account_id=account_id,
    )

    project_texts = app.store.list_long_texts(
        ["project_experience_card"],
        account_id=account_id,
        candidate_id=candidate_id,
    )
    assert confirmed.status == "已确认"
    assert repeated.confirmed_summary == "本人负责接口设计与实现。"
    assert len(project_texts) == 1
    assert rag_task is not None
    assert repeated_rag_task is not None
    assert repeated_rag_task.task_key == rag_task.task_key
    assert len(producer.calls) == 2


def test_web_queues_github_project_analysis_and_rejects_non_github_url() -> None:
    """网页 API 验证链接后只登记异步任务，前端无需上传本地目录。"""

    queue = RecordingQueue()
    client = TestClient(create_web_app(task_queue=queue))
    registered = client.post(
        "/api/auth/register",
        json={"email": "github-web@example.com", "password": "password-123"},
    )
    assert registered.status_code == 200
    logged_in = client.post(
        "/api/auth/login",
        json={"email": "github-web@example.com", "password": "password-123"},
    )
    assert logged_in.status_code == 200
    profile = client.post(
        "/api/profiles",
        json={
            "name": "GitHub Web",
            "education": "本科",
            "experience_years": 0,
            "skills": {},
            "preferred_cities": [],
            "acceptable_cities": [],
            "target_directions": [],
            "unacceptable": [],
        },
    )
    candidate_id = int(profile.json()["candidate_id"])

    invalid = client.post(
        "/api/projects/github",
        json={"candidate_id": candidate_id, "repository_url": "https://gitlab.com/example/project"},
    )
    assert invalid.status_code == 400

    queued = client.post(
        "/api/projects/github",
        json={
            "candidate_id": candidate_id,
            "repository_url": "https://github.com/example/sample-repository",
        },
    )
    assert queued.status_code == 200
    payload = queued.json()
    assert payload["processing_async"] is True
    assert payload["task"]["task_type"] == "github_project_analysis"
    assert queue.task_keys == [payload["task"]["task_key"]]
