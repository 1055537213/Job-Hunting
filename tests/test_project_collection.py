"""Manifest-first local project collection and structured evidence tests."""

from __future__ import annotations

import hashlib
from io import BytesIO
from pathlib import Path
from zipfile import ZipFile

import pytest
from PIL import Image
from reportlab.pdfgen import canvas

from job_hunting_agent.app import JobHuntingApp
from job_hunting_agent.deduplication import DuplicateResourceError
from job_hunting_agent.models import CandidateProfileInput
from job_hunting_agent.project_analyzer import build_project_experience_card
from job_hunting_agent.project_evidence import (
    ProjectManifestItem,
    extract_project_evidence,
    plan_project_manifest,
)
from job_hunting_agent.project_visual import (
    ProjectVisualAnalysisResult,
    ProjectVisualFinding,
    ProjectVisualParameter,
)
from job_hunting_agent.resume_document import ResumeFileStore


def candidate_input() -> CandidateProfileInput:
    return CandidateProfileInput(
        name="本地项目候选人",
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


def manifest_item(path: str, content: bytes, media_type: str = "text/plain") -> ProjectManifestItem:
    return ProjectManifestItem(
        relative_path=path,
        file_size=len(content),
        sha256=hashlib.sha256(content).hexdigest(),
        media_type=media_type,
    )


def build_text_pdf() -> bytes:
    output = BytesIO()
    document = canvas.Canvas(output)
    document.drawString(72, 760, "Pump pressure tolerance: 0.02 mm")
    document.showPage()
    document.drawString(72, 760, "Acceptance pressure: 16 MPa")
    document.save()
    return output.getvalue()


def build_xlsx() -> bytes:
    output = BytesIO()
    with ZipFile(output, "w") as archive:
        archive.writestr(
            "[Content_Types].xml",
            """<?xml version="1.0" encoding="UTF-8"?>
            <Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types" />""",
        )
        archive.writestr(
            "xl/workbook.xml",
            """<?xml version="1.0" encoding="UTF-8"?>
            <workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"
              xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
              <sheets><sheet name="Parameters" sheetId="1" r:id="rId1" /></sheets>
            </workbook>""",
        )
        archive.writestr(
            "xl/_rels/workbook.xml.rels",
            """<?xml version="1.0" encoding="UTF-8"?>
            <Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
              <Relationship Id="rId1" Target="worksheets/sheet1.xml"
                Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" />
            </Relationships>""",
        )
        archive.writestr(
            "xl/worksheets/sheet1.xml",
            """<?xml version="1.0" encoding="UTF-8"?>
            <worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
              <sheetData>
                <row r="1"><c r="A1" t="inlineStr"><is><t>Tolerance</t></is></c>
                  <c r="B1" t="inlineStr"><is><t>0.02 mm</t></is></c></row>
              </sheetData>
            </worksheet>""",
        )
    return output.getvalue()


class StaticProjectVisualAnalyzer:
    """不请求外部模型的视觉分析替身。"""

    max_pdf_pages = 8

    def __init__(self) -> None:
        self.requests: list[tuple[list[object], int | None, int | None]] = []

    def analyze(
        self,
        inputs: list[object],
        *,
        account_id: int | None,
        candidate_id: int | None,
    ) -> ProjectVisualAnalysisResult:
        self.requests.append((inputs, account_id, candidate_id))
        findings = {
            item.source_id: ProjectVisualFinding(
                source_id=item.source_id,
                confidence=0.95,
                summary="工业图中展示泵轴与轴承装配关系。",
                element_relationships=("尺寸标注指向泵轴外径",),
                tables=(),
                parameters=(
                    ProjectVisualParameter(
                        name="泵轴外径",
                        value="25.00",
                        unit="mm",
                        tolerance="±0.02",
                        applies_to="泵轴",
                    ),
                ),
                warnings=(),
            )
            for item in inputs
        }
        return ProjectVisualAnalysisResult(
            findings=findings,
            failed_source_ids=[],
            status="succeeded",
        )


def test_manifest_plan_blocks_sensitive_paths_and_selects_cross_industry_evidence() -> None:
    source = b"FastAPI service"
    pdf = build_text_pdf()
    plan = plan_project_manifest(
        [
            manifest_item("README.md", source, "text/markdown"),
            manifest_item("specs/tolerance.pdf", pdf, "application/pdf"),
            manifest_item(".env", b"TOKEN=secret"),
            manifest_item("node_modules/vendor.js", b"generated"),
            ProjectManifestItem("raw/model.bin", 100, None),
        ]
    )
    by_path = {item.item.relative_path: item for item in plan}

    assert by_path["README.md"].selection_status == "selected"
    assert by_path["specs/tolerance.pdf"].file_kind == "pdf"
    assert by_path[".env"].selection_reason == "sensitive_path"
    assert by_path["node_modules/vendor.js"].selection_reason == "ignored_directory:node_modules"
    assert by_path["raw/model.bin"].selection_reason == "unsupported_type"


def test_manifest_plan_enforces_whole_project_byte_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("job_hunting_agent.project_evidence.MAX_SELECTED_PROJECT_BYTES", 15)
    first = b"1234567890"
    second = b"abcdefghij"
    plan = plan_project_manifest(
        [
            manifest_item("a.txt", first),
            manifest_item("b.txt", second),
        ]
    )

    assert [item.selection_status for item in plan] == ["selected", "skipped"]
    assert plan[1].selection_reason == "project_byte_budget_reached"


def test_skipped_sensitive_file_is_rejected_before_visual_analysis(
    database_url: str,
    account_id: int,
    tmp_path: Path,
) -> None:
    """A forged upload cannot send a server-skipped image to the multimodal model."""

    app = JobHuntingApp(
        database_url=database_url,
        object_storage=ResumeFileStore(tmp_path / "objects"),
        semantic_matching=False,
    )
    analyzer = StaticProjectVisualAnalyzer()
    app.project_visual_analyzer = analyzer
    candidate_id = app.save_candidate_profile(candidate_input(), account_id=account_id)
    image = BytesIO()
    Image.new("RGB", (32, 32), "white").save(image, format="PNG")
    sensitive = image.getvalue()
    safe = b"project overview"
    session, files = app.create_local_project_collection(
        candidate_id,
        "sensitive-boundary",
        [
            manifest_item("README.md", safe, "text/markdown"),
            manifest_item("secrets/password-diagram.png", sensitive, "image/png"),
        ],
        account_id=account_id,
    )
    blocked = next(item for item in files if item.relative_path.endswith("password-diagram.png"))

    with pytest.raises(ValueError, match="不在后端采集计划"):
        app.process_local_project_collection_file(
            session.id,
            blocked.id,
            sensitive,
            account_id=account_id,
        )

    assert analyzer.requests == []


def test_pdf_and_xlsx_keep_page_and_sheet_boundaries() -> None:
    pdf = extract_project_evidence("specs/tolerance.pdf", build_text_pdf(), "pdf")
    workbook = extract_project_evidence("data/parameters.xlsx", build_xlsx(), "spreadsheet")

    assert "[第 1 页]" in pdf.text
    assert "[第 2 页]" in pdf.text
    assert "0.02 mm" in pdf.text
    assert pdf.metadata["page_count"] == 2
    assert "[工作表 Parameters]" in workbook.text
    assert "Tolerance" in workbook.text
    assert "0.02 mm" in workbook.text
    assert workbook.metadata["sheet_count"] == 1


def test_ifc_is_collected_as_engineering_text() -> None:
    content = b"\n".join(
        [
            b"ISO-10303-21;",
            b"FILE_SCHEMA(('IFC4'));",
            b"#100=IFCWALL('wall-guid',$,'Ground Floor East Shear Wall',$);",
            b"END-ISO-10303-21;",
        ]
    )

    evidence = extract_project_evidence(
        "models/northstar.ifc",
        content,
        "engineering_drawing",
    )

    assert evidence.method == "engineering_text"
    assert evidence.metadata["format"] == "ifc"
    assert "Ground Floor East Shear Wall" in evidence.text


def test_image_visual_analysis_preserves_relationships_and_exact_parameters(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    analyzer = StaticProjectVisualAnalyzer()
    image = BytesIO()
    Image.new("RGB", (160, 100), "white").save(image, format="PNG")
    monkeypatch.setattr("job_hunting_agent.project_evidence.run_rapidocr", lambda _image: "")

    evidence = extract_project_evidence(
        "drawings/pump.png",
        image.getvalue(),
        "image",
        visual_analyzer=analyzer,
        account_id=7,
        candidate_id=11,
    )

    assert evidence.method == "image_vlm"
    assert "尺寸标注指向泵轴外径" in evidence.text
    assert "25.00" in evidence.text
    assert "±0.02" in evidence.text
    assert evidence.metadata["visual_analysis_status"] == "succeeded"
    assert evidence.metadata["visual_finding"]["parameters"][0] == {
        "name": "泵轴外径",
        "value": "25.00",
        "unit": "mm",
        "tolerance": "±0.02",
        "applies_to": "泵轴",
    }
    assert len(evidence.visual_artifacts) == 1
    assert evidence.visual_artifacts[0].source_label == "drawings/pump.png"
    assert evidence.visual_artifacts[0].media_type == "image/png"
    assert analyzer.requests[0][1:] == (7, 11)


def test_industrial_visual_evidence_produces_confirmable_project_card_content() -> None:
    card = build_project_experience_card(
        project_name="泵轴夹具设计",
        selected_files=[
            (
                Path("drawings/pump.png"),
                "\n".join(
                    [
                        "[视觉语义]",
                        "摘要：泵轴夹具总装图包含定位销、基准面和夹紧机构",
                        "元素关系：尺寸标注指向泵轴外径",
                        "参数：名称=泵轴外径；数值=25.00；单位=mm；公差=±0.02；适用对象=泵轴",
                    ]
                ),
            )
        ],
        skipped_summary={},
        source_type="local_directory_collection",
    )

    assert any("泵轴夹具总装图" in item for item in card.detected_core_features)
    assert any("设计、分析、实施或交付" in item for item in card.responsibility_draft)
    assert any("25.00" in item and "±0.02" in item for item in card.highlight_draft)


def test_local_collection_persists_visual_evidence(
    database_url: str,
    account_id: int,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    file_store = ResumeFileStore(tmp_path / "visual-objects")
    app = JobHuntingApp(
        database_url=database_url,
        object_storage=file_store,
        semantic_matching=False,
    )
    analyzer = StaticProjectVisualAnalyzer()
    app.project_visual_analyzer = analyzer
    candidate_id = app.save_candidate_profile(candidate_input(), account_id=account_id)
    image = BytesIO()
    Image.new("RGB", (160, 100), "white").save(image, format="PNG")
    content = image.getvalue()
    monkeypatch.setattr("job_hunting_agent.project_evidence.run_rapidocr", lambda _image: "")
    session, files = app.create_local_project_collection(
        candidate_id,
        "pump-drawing",
        [manifest_item("drawings/pump.png", content, "image/png")],
        account_id=account_id,
    )

    processed = app.process_local_project_collection_file(
        session.id,
        files[0].id,
        content,
        account_id=account_id,
    )
    long_text = app.store.get_long_texts_by_ids(
        [int(processed.long_text_id)],
        account_id=account_id,
    )[0]
    visual_items = app.store.list_visual_knowledge_items(
        account_id=account_id,
        project_collection_file_ids=[processed.id],
    )

    assert processed.extraction_method == "image_vlm"
    assert processed.metadata["visual_analysis_status"] == "succeeded"
    assert "尺寸标注指向泵轴外径" in long_text.text
    assert len(visual_items) == 1
    assert visual_items[0].index_status == "pending"
    assert visual_items[0].long_text_id == processed.long_text_id
    assert file_store.path_for(visual_items[0].storage_key).is_file()
    assert analyzer.requests[0][1:] == (account_id, candidate_id)

    card = app.complete_local_project_collection(session.id, account_id=account_id)
    result = app.delete_project_card(card.id, account_id=account_id)
    assert visual_items[0].storage_key in result["storage_keys"]
    assert not file_store.path_for(visual_items[0].storage_key).exists()


def test_pdf_visual_analysis_is_page_scoped() -> None:
    analyzer = StaticProjectVisualAnalyzer()

    evidence = extract_project_evidence(
        "specs/tolerance.pdf",
        build_text_pdf(),
        "pdf",
        visual_analyzer=analyzer,
        account_id=7,
        candidate_id=11,
    )

    assert evidence.method == "pdf_text_and_vlm"
    assert evidence.metadata["visual_pages"] == [1, 2]
    assert [item.page_number for item in evidence.visual_artifacts] == [1, 2]
    assert "[第 1 页]" in evidence.text
    assert "[视觉语义]" in evidence.text
    assert "±0.02" in evidence.text


def test_local_collection_saves_evidence_without_raw_original(
    database_url: str,
    account_id: int,
    tmp_path: Path,
) -> None:
    file_store = ResumeFileStore(tmp_path / "objects")
    app = JobHuntingApp(
        database_url=database_url,
        object_storage=file_store,
        semantic_matching=False,
    )
    candidate_id = app.save_candidate_profile(candidate_input(), account_id=account_id)
    source = b"# Pump station\nFastAPI pressure monitoring"
    session, files = app.create_local_project_collection(
        candidate_id,
        "pump-station",
        [manifest_item("README.md", source, "text/markdown")],
        account_id=account_id,
    )

    processed = app.process_local_project_collection_file(
        session.id,
        files[0].id,
        source,
        account_id=account_id,
    )
    card = app.complete_local_project_collection(session.id, account_id=account_id)

    assert processed.long_text_id is not None
    assert list((tmp_path / "objects").rglob("*")) == []
    assert card.card.read_files == ["README.md"]
    assert app.store.get_project_collection(session.id, account_id=account_id).status == "ready"

    result = app.delete_project_card(card.id, account_id=account_id)
    assert processed.long_text_id in result["long_text_ids"]
    assert result["storage_keys"] == []
    assert app.store.get_long_texts_by_ids([processed.long_text_id], account_id=account_id) == []


def test_local_collection_rejects_content_changed_after_manifest(
    database_url: str,
    account_id: int,
    tmp_path: Path,
) -> None:
    file_store = ResumeFileStore(tmp_path / "objects")
    app = JobHuntingApp(
        database_url=database_url,
        object_storage=file_store,
        semantic_matching=False,
    )
    candidate_id = app.save_candidate_profile(candidate_input(), account_id=account_id)
    source = b"Python quality inspection service"
    session, files = app.create_local_project_collection(
        candidate_id,
        "inspection-service",
        [manifest_item("service.py", source, "text/x-python")],
        account_id=account_id,
    )

    with pytest.raises(ValueError, match="预扫描清单"):
        app.process_local_project_collection_file(
            session.id,
            files[0].id,
            b"changed content with same-ish purpose",
            account_id=account_id,
        )

    app.process_local_project_collection_file(
        session.id,
        files[0].id,
        source,
        account_id=account_id,
    )
    card = app.complete_local_project_collection(session.id, account_id=account_id)
    app.delete_project_card(card.id, account_id=account_id)
    assert list((tmp_path / "objects").rglob("*")) == []


def test_same_local_manifest_resumes_until_ready_then_is_duplicate(
    database_url: str,
    account_id: int,
    tmp_path: Path,
) -> None:
    app = JobHuntingApp(
        database_url=database_url,
        object_storage=ResumeFileStore(tmp_path / "objects"),
        semantic_matching=False,
    )
    first_candidate = app.save_candidate_profile(candidate_input(), account_id=account_id)
    second = candidate_input()
    second.name = "第二位本地项目候选人"
    second_candidate = app.save_candidate_profile(second, account_id=account_id)
    source = b"FastAPI service"
    manifest = [manifest_item("README.md", source)]

    first_session, first_files = app.create_local_project_collection(
        first_candidate,
        "service",
        manifest,
        account_id=account_id,
    )
    resumed, resumed_files = app.create_local_project_collection(
        first_candidate,
        "renamed-service",
        manifest,
        account_id=account_id,
    )
    assert resumed.id == first_session.id
    assert resumed.project_name == "renamed-service"
    assert [item.id for item in resumed_files] == [item.id for item in first_files]

    app.process_local_project_collection_file(
        first_session.id,
        first_files[0].id,
        source,
        account_id=account_id,
    )
    app.complete_local_project_collection(first_session.id, account_id=account_id)
    with pytest.raises(DuplicateResourceError, match="本地项目"):
        app.create_local_project_collection(
            first_candidate,
            "service-after-ready",
            manifest,
            account_id=account_id,
        )
    other, _ = app.create_local_project_collection(
        second_candidate,
        "service",
        manifest,
        account_id=account_id,
    )
    assert other.candidate_id == second_candidate


def test_failed_local_file_can_resume_and_incomplete_collection_can_be_deleted(
    database_url: str,
    account_id: int,
    tmp_path: Path,
) -> None:
    app = JobHuntingApp(
        database_url=database_url,
        object_storage=ResumeFileStore(tmp_path / "objects"),
        semantic_matching=False,
    )
    candidate_id = app.save_candidate_profile(candidate_input(), account_id=account_id)
    source = b"FastAPI service"
    manifest = [manifest_item("README.md", source)]
    session, files = app.create_local_project_collection(
        candidate_id,
        "resumable-service",
        manifest,
        account_id=account_id,
    )
    failed = app.store.fail_project_collection_file(
        files[0].id,
        collection_id=session.id,
        account_id=account_id,
        reason="temporary parser failure",
    )
    assert failed.selection_status == "failed"

    resumed, resumed_files = app.create_local_project_collection(
        candidate_id,
        "resumable-service",
        manifest,
        account_id=account_id,
    )
    assert resumed.id == session.id
    assert resumed_files[0].selection_status == "selected"

    queued_task = app.store.create_background_task(
        account_id=account_id,
        task_type="rag_index",
        payload={"long_text_ids": [999999]},
        candidate_id=candidate_id,
        session_id=f"local-project-collection-{session.id}",
    )

    result = app.delete_incomplete_local_project_collection(session.id, account_id=account_id)
    assert result["collection_id"] == session.id
    assert app.store.get_background_task(
        queued_task.task_key,
        account_id=account_id,
    ).status == "cancelled"
    with pytest.raises(KeyError):
        app.store.get_project_collection(session.id, account_id=account_id)
