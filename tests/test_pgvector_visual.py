"""视觉知识对象、图片向量任务和跨模态召回集成测试。"""

from __future__ import annotations

import hashlib
from io import BytesIO
from pathlib import Path

import sqlalchemy as sa
from PIL import Image

from job_hunting_agent.app import JobHuntingApp
from job_hunting_agent.background_tasks import run_registered_task
from job_hunting_agent.database_schema import visual_knowledge_items
from job_hunting_agent.models import CandidateProfileInput
from job_hunting_agent.project_evidence import ProjectManifestItem
from job_hunting_agent.project_visual import (
    ProjectVisualAnalysisResult,
    ProjectVisualFinding,
)
from job_hunting_agent.resume_document import ResumeFileStore


class RecordingQueue:
    def __init__(self) -> None:
        self.task_keys: list[str] = []

    def health_check(self) -> None:
        return None

    def enqueue(self, task_key: str) -> None:
        self.task_keys.append(task_key)


class StaticVisualAnalyzer:
    max_pdf_pages = 4

    def analyze(self, inputs, *, account_id, candidate_id):
        return ProjectVisualAnalysisResult(
            findings={
                item.source_id: ProjectVisualFinding(
                    source_id=item.source_id,
                    confidence=0.96,
                    summary="工业泵总装图展示叶轮与泵轴连接关系。",
                    element_relationships=("叶轮通过键槽固定在泵轴上",),
                )
                for item in inputs
            },
            status="succeeded",
        )


class QueryAwareVisualAnalyzer(StaticVisualAnalyzer):
    def __init__(self) -> None:
        self.queries: list[str] = []

    def analyze_for_query(self, inputs, query, *, account_id, candidate_id):
        self.queries.append(query)
        return ProjectVisualAnalysisResult(
            findings={
                item.source_id: ProjectVisualFinding(
                    source_id=item.source_id,
                    confidence=0.99,
                    summary="原图复核确认泵轴外径公差。",
                    element_relationships=("公差标注直接指向泵轴外径尺寸线",),
                )
                for item in inputs
            },
            status="succeeded",
        )


class StaticCrossModalEmbeddings:
    model = "test-cross-modal"
    endpoint = "https://embedding.test/multimodal"
    dimensions = 3

    def __init__(self) -> None:
        self.image_calls = 0

    def embed_images(self, images: list[tuple[bytes, str]]) -> list[list[float]]:
        self.image_calls += 1
        assert all(content and media_type.startswith("image/") for content, media_type in images)
        return [[1.0, 0.0, 0.0] for _ in images]

    def embed_query(self, text: str) -> list[float]:
        return [1.0, 0.0, 0.0]

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [[0.0, 1.0, 0.0] for _ in texts]


def candidate_input(name: str = "视觉知识候选人") -> CandidateProfileInput:
    return CandidateProfileInput(
        name=name,
        status="待补充",
        education="本科",
        experience_years=2,
        skills={},
        preferred_cities=[],
        salary_floor_k=None,
        expected_salary_k=None,
        target_directions=[],
        unacceptable=[],
    )


def project_image() -> bytes:
    output = BytesIO()
    Image.new("RGB", (160, 100), "white").save(output, format="PNG")
    return output.getvalue()


def persist_visual_item(
    app: JobHuntingApp,
    *,
    account_id: int,
    monkeypatch,
) -> tuple[int, int, int]:
    candidate_id = app.save_candidate_profile(candidate_input(), account_id=account_id)
    content = project_image()
    item = ProjectManifestItem(
        relative_path="drawings/pump-assembly.png",
        file_size=len(content),
        sha256=hashlib.sha256(content).hexdigest(),
        media_type="image/png",
    )
    app.project_visual_analyzer = StaticVisualAnalyzer()
    monkeypatch.setattr("job_hunting_agent.project_evidence.run_rapidocr", lambda _image: "")
    session, files = app.create_local_project_collection(
        candidate_id,
        "pump-assembly",
        [item],
        account_id=account_id,
    )
    processed = app.process_local_project_collection_file(
        session.id,
        files[0].id,
        content,
        account_id=account_id,
    )
    visual = app.store.list_visual_knowledge_items(
        account_id=account_id,
        project_collection_file_ids=[processed.id],
    )[0]
    return candidate_id, session.id, visual.id


def test_visual_index_is_idempotent_and_participates_in_text_query_recall(
    database_url: str,
    account_id: int,
    tmp_path: Path,
    monkeypatch,
) -> None:
    app = JobHuntingApp(
        database_url=database_url,
        object_storage=ResumeFileStore(tmp_path / "visual-index"),
        semantic_matching=False,
    )
    embeddings = StaticCrossModalEmbeddings()
    app.model_gateway.embeddings = lambda _context: embeddings
    app.model_gateway.reranker = lambda _context: None
    candidate_id, _, visual_item_id = persist_visual_item(
        app,
        account_id=account_id,
        monkeypatch=monkeypatch,
    )

    first = app.index_visual_knowledge_items(
        [visual_item_id],
        account_id=account_id,
        candidate_id=candidate_id,
    )
    second = app.index_visual_knowledge_items(
        [visual_item_id],
        account_id=account_id,
        candidate_id=candidate_id,
    )
    results = app.search_rag(
        "叶轮和泵轴如何连接",
        account_id=account_id,
        candidate_id=candidate_id,
    )

    assert first.collection_name == "visual_knowledge_items"
    assert second.document_count == 1
    assert embeddings.image_calls == 2
    assert len(results) == 1
    assert results[0].evidence_kind == "visual"
    assert results[0].visual_item_id == visual_item_id
    assert "叶轮通过键槽固定在泵轴上" in results[0].content


def test_visual_search_reopens_bounded_original_image_for_query_verification(
    database_url: str,
    account_id: int,
    tmp_path: Path,
    monkeypatch,
) -> None:
    app = JobHuntingApp(
        database_url=database_url,
        object_storage=ResumeFileStore(tmp_path / "visual-reinspection"),
        semantic_matching=False,
    )
    embeddings = StaticCrossModalEmbeddings()
    app.model_gateway.embeddings = lambda _context: embeddings
    app.model_gateway.reranker = lambda _context: None
    candidate_id, _, visual_item_id = persist_visual_item(
        app,
        account_id=account_id,
        monkeypatch=monkeypatch,
    )
    app.index_visual_knowledge_items(
        [visual_item_id],
        account_id=account_id,
        candidate_id=candidate_id,
    )
    analyzer = QueryAwareVisualAnalyzer()
    app.project_visual_analyzer = analyzer

    results = app.search_rag(
        "泵轴外径公差是多少",
        account_id=account_id,
        candidate_id=candidate_id,
    )

    assert analyzer.queries == ["泵轴外径公差是多少"]
    assert "[原图复核，优先于入库摘要]" in results[0].content
    assert "公差标注直接指向泵轴外径尺寸线" in results[0].content


def test_visual_background_task_contains_only_ids_and_duplicate_message_does_not_reindex(
    database_url: str,
    account_id: int,
    tmp_path: Path,
    monkeypatch,
) -> None:
    queue = RecordingQueue()
    app = JobHuntingApp(
        database_url=database_url,
        object_storage=ResumeFileStore(tmp_path / "visual-task"),
        task_queue=queue,
        semantic_matching=False,
    )
    embeddings = StaticCrossModalEmbeddings()
    app.model_gateway.embeddings = lambda _context: embeddings
    candidate_id, _, visual_item_id = persist_visual_item(
        app,
        account_id=account_id,
        monkeypatch=monkeypatch,
    )

    task = app.enqueue_visual_index_task(
        visual_item_ids=[visual_item_id],
        account_id=account_id,
        candidate_id=candidate_id,
        idempotency_key=f"visual-test:{visual_item_id}",
    )
    first = run_registered_task(app, task.task_key)
    duplicate = run_registered_task(app, task.task_key)

    assert task.payload == {"visual_item_ids": [visual_item_id]}
    assert queue.task_keys == [task.task_key]
    assert first["status"] == "succeeded"
    assert duplicate["status"] == "succeeded"
    assert embeddings.image_calls == 1


def test_visual_item_failure_is_recorded_without_provider_payload(
    database_url: str,
    account_id: int,
    tmp_path: Path,
    monkeypatch,
) -> None:
    app = JobHuntingApp(
        database_url=database_url,
        object_storage=ResumeFileStore(tmp_path / "visual-failure"),
        semantic_matching=False,
    )
    candidate_id, _, visual_item_id = persist_visual_item(
        app,
        account_id=account_id,
        monkeypatch=monkeypatch,
    )
    app.model_gateway.embeddings = lambda _context: object()

    try:
        app.index_visual_knowledge_items(
            [visual_item_id],
            account_id=account_id,
            candidate_id=candidate_id,
        )
    except ValueError:
        pass
    else:  # pragma: no cover - 明确要求不支持图片的适配器失败。
        raise AssertionError("视觉索引应拒绝纯文本 Embedding")

    with app.store.engine.connect() as connection:
        row = connection.execute(
            sa.select(
                visual_knowledge_items.c.index_status,
                visual_knowledge_items.c.index_error_type,
            ).where(visual_knowledge_items.c.id == visual_item_id)
        ).mappings().one()
    assert row["index_status"] == "failed"
    assert row["index_error_type"] == "ValueError"
