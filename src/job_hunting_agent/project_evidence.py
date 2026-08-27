"""Project file routing, manifest planning, and bounded evidence extraction.

The browser and GitHub import paths share these rules. A manifest can describe a
large directory, but the backend only requests files it can safely parse. Raw
files are never executed, and sensitive paths are rejected before upload.
"""

from __future__ import annotations

import csv
import hashlib
import json
import re
import zipfile
from collections import Counter
from dataclasses import dataclass, field
from io import BytesIO, StringIO
from pathlib import Path, PurePosixPath
from xml.etree import ElementTree

import pdfplumber
from PIL import Image, UnidentifiedImageError

from .project_analyzer import (
    IMPORTANT_NAMES,
    SKIP_DIRS,
    SOURCE_SUFFIXES,
    decode_text_bytes,
)
from .project_visual import (
    ProjectVisualAnalysisResult,
    ProjectVisualAnalyzerProtocol,
    ProjectVisualInput,
    normalize_project_visual_image,
)
from .resume_document import (
    MIN_PDF_TEXT_CHARS_PER_PAGE,
    ResumeDocumentError,
    extract_docx,
    normalize_extracted_text,
    render_and_ocr_pdf_pages,
    run_rapidocr,
)

PDF_SUFFIXES = {".pdf"}
IMAGE_SUFFIXES = {".bmp", ".gif", ".jpeg", ".jpg", ".png", ".tif", ".tiff", ".webp"}
SPREADSHEET_SUFFIXES = {".csv", ".xlsx"}
DOCUMENT_SUFFIXES = {".docx", ".pptx"}
DRAWING_SUFFIXES = {".dxf", ".iges", ".igs", ".step", ".stp", ".svg"}

MAX_MANIFEST_FILES = 20_000
MAX_MANIFEST_PATH_LENGTH = 2_048
MAX_PROJECT_PDF_PAGES = 100
MAX_IMAGE_PIXELS = 40_000_000
MAX_EXTRACTED_TEXT_CHARS = 400_000
MAX_SPREADSHEET_SHEETS = 100
MAX_SPREADSHEET_ROWS = 10_000
MAX_SPREADSHEET_CELLS_PER_SHEET = 20_000
MAX_SELECTED_PROJECT_FILES = 120
MAX_SELECTED_PROJECT_BYTES = 256 * 1024 * 1024

MAX_FILE_BYTES_BY_KIND = {
    "source_text": 2 * 1024 * 1024,
    "pdf": 50 * 1024 * 1024,
    "image": 20 * 1024 * 1024,
    "spreadsheet": 30 * 1024 * 1024,
    "document": 30 * 1024 * 1024,
    "engineering_drawing": 20 * 1024 * 1024,
}
MAX_SELECTED_FILES_BY_KIND = {
    "source_text": 160,
    "pdf": 30,
    "image": 50,
    "spreadsheet": 30,
    "document": 30,
    "engineering_drawing": 30,
}

_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_SENSITIVE_PART_PATTERN = re.compile(
    r"(?:^|[._-])(env|secret|credential|password|token|private[_-]?key|id_rsa)(?:$|[._-])",
    re.IGNORECASE,
)


class ProjectEvidenceError(ValueError):
    """A project manifest or evidence file violates the collection contract."""


@dataclass(frozen=True)
class ProjectManifestItem:
    """Client-provided metadata; hashes are untrusted until content upload."""

    relative_path: str
    file_size: int
    sha256: str | None
    media_type: str = "application/octet-stream"
    last_modified: int | None = None


@dataclass(frozen=True)
class PlannedProjectFile:
    """Backend decision for one manifest file."""

    item: ProjectManifestItem
    file_kind: str
    selection_status: str
    selection_reason: str


@dataclass(frozen=True)
class ExtractedProjectEvidence:
    """Bounded text plus source-structure metadata ready for long_texts."""

    text: str
    method: str
    metadata: dict[str, object] = field(default_factory=dict)
    visual_artifacts: tuple[ProjectVisualArtifact, ...] = ()


@dataclass(frozen=True)
class ProjectVisualArtifact:
    """一张可安全持久化并生成图像向量的项目视觉副本。"""

    source_id: str
    source_label: str
    content: bytes
    media_type: str
    width: int
    height: int
    page_number: int | None = None
    metadata: dict[str, object] = field(default_factory=dict)


def project_file_kind(path: Path) -> str:
    """Route a project file to a stable parser family."""

    suffix = path.suffix.lower()
    if path.name.lower() in IMPORTANT_NAMES or suffix in SOURCE_SUFFIXES:
        return "source_text"
    if suffix in PDF_SUFFIXES:
        return "pdf"
    if suffix in IMAGE_SUFFIXES:
        return "image"
    if suffix in SPREADSHEET_SUFFIXES:
        return "spreadsheet"
    if suffix in DOCUMENT_SUFFIXES:
        return "document"
    if suffix in DRAWING_SUFFIXES:
        return "engineering_drawing"
    return "unsupported"


def normalize_manifest_path(value: str) -> str:
    """Return a portable relative path and reject traversal or ambiguous paths."""

    raw = str(value or "").strip()
    if not raw or len(raw) > MAX_MANIFEST_PATH_LENGTH or "\\" in raw:
        raise ProjectEvidenceError("项目文件路径无效。")
    path = PurePosixPath(raw)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise ProjectEvidenceError("项目文件路径不能越过所选目录。")
    return path.as_posix()


def sensitive_or_ignored_reason(relative_path: str) -> str | None:
    """Mirror the browser deny-list and enforce it again on the server."""

    parts = PurePosixPath(relative_path).parts
    ignored = next((part for part in parts[:-1] if part.lower() in SKIP_DIRS), None)
    if ignored:
        return f"ignored_directory:{ignored}"
    if any(_SENSITIVE_PART_PATTERN.search(part) for part in parts):
        return "sensitive_path"
    suffix = Path(relative_path).suffix.lower()
    if suffix in {".pem", ".key", ".p12", ".pfx", ".crt", ".cer"}:
        return "sensitive_extension"
    return None


def plan_project_manifest(items: list[ProjectManifestItem]) -> list[PlannedProjectFile]:
    """Validate a directory manifest and select parseable evidence by bounded quotas."""

    if not items:
        raise ProjectEvidenceError("所选项目目录中没有可扫描文件。")
    if len(items) > MAX_MANIFEST_FILES:
        raise ProjectEvidenceError("项目文件数量不能超过 20000 个。")

    normalized: list[ProjectManifestItem] = []
    seen: set[str] = set()
    for item in items:
        path = normalize_manifest_path(item.relative_path)
        if path in seen:
            raise ProjectEvidenceError(f"项目清单包含重复路径：{path}")
        seen.add(path)
        size = int(item.file_size)
        if size < 0:
            raise ProjectEvidenceError(f"项目文件大小无效：{path}")
        digest = str(item.sha256 or "").strip().lower() or None
        if digest is not None and not _SHA256_PATTERN.fullmatch(digest):
            raise ProjectEvidenceError(f"项目文件摘要无效：{path}")
        normalized.append(
            ProjectManifestItem(
                relative_path=path,
                file_size=size,
                sha256=digest,
                media_type=str(item.media_type or "application/octet-stream")[:128],
                last_modified=item.last_modified,
            )
        )

    # Important manifests/configuration are selected before ordinary source files.
    normalized.sort(
        key=lambda item: (
            0 if Path(item.relative_path).name.lower() in IMPORTANT_NAMES else 1,
            item.relative_path.lower(),
        )
    )
    selected_counts: Counter[str] = Counter()
    selected_file_count = 0
    selected_bytes = 0
    plan: list[PlannedProjectFile] = []
    for item in normalized:
        path = Path(item.relative_path)
        kind = project_file_kind(path)
        reason = sensitive_or_ignored_reason(item.relative_path)
        if reason:
            status = "skipped"
        elif kind == "unsupported":
            status, reason = "skipped", "unsupported_type"
        elif item.sha256 is None:
            status, reason = "skipped", "missing_sha256"
        elif item.file_size == 0:
            status, reason = "skipped", "empty_file"
        elif item.file_size > MAX_FILE_BYTES_BY_KIND[kind]:
            status, reason = "skipped", "file_too_large"
        elif selected_file_count >= MAX_SELECTED_PROJECT_FILES:
            status, reason = "skipped", "project_file_budget_reached"
        elif selected_bytes + item.file_size > MAX_SELECTED_PROJECT_BYTES:
            status, reason = "skipped", "project_byte_budget_reached"
        elif selected_counts[kind] >= MAX_SELECTED_FILES_BY_KIND[kind]:
            status, reason = "skipped", "parser_quota_reached"
        else:
            status, reason = "selected", "supported_and_within_policy"
            selected_counts[kind] += 1
            selected_file_count += 1
            selected_bytes += item.file_size
        plan.append(
            PlannedProjectFile(
                item=item,
                file_kind=kind,
                selection_status=status,
                selection_reason=reason or "skipped",
            )
        )
    if not any(item.selection_status == "selected" for item in plan):
        raise ProjectEvidenceError("项目中没有找到当前能够安全分析的文件。")
    return sorted(plan, key=lambda item: item.item.relative_path.lower())


def manifest_fingerprint(items: list[ProjectManifestItem]) -> str:
    """Build a deterministic identity from paths, sizes, and browser hashes."""

    canonical = [
        {
            "path": normalize_manifest_path(item.relative_path),
            "size": int(item.file_size),
            "sha256": str(item.sha256 or "").lower(),
        }
        for item in items
    ]
    canonical.sort(key=lambda item: str(item["path"]).lower())
    payload = json.dumps(canonical, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def extract_project_evidence(
    relative_path: str,
    content: bytes,
    file_kind: str,
    *,
    visual_analyzer: ProjectVisualAnalyzerProtocol | None = None,
    account_id: int | None = None,
    candidate_id: int | None = None,
) -> ExtractedProjectEvidence:
    """Extract text without executing a file or trusting its extension alone."""

    path = Path(normalize_manifest_path(relative_path))
    expected_kind = project_file_kind(path)
    if expected_kind != file_kind or file_kind not in MAX_FILE_BYTES_BY_KIND:
        raise ProjectEvidenceError("项目文件类型与采集计划不一致。")
    if not content:
        raise ProjectEvidenceError("项目文件不能为空。")
    if len(content) > MAX_FILE_BYTES_BY_KIND[file_kind]:
        raise ProjectEvidenceError("项目文件超过该类型的单文件分析上限。")

    if file_kind == "source_text":
        text = decode_text_bytes(content)
        return _bounded_evidence(text, "source_text", {})
    if file_kind == "pdf":
        return _extract_pdf_pages(
            path,
            content,
            visual_analyzer=visual_analyzer,
            account_id=account_id,
            candidate_id=candidate_id,
        )
    if file_kind == "image":
        return _extract_image(
            path,
            content,
            visual_analyzer=visual_analyzer,
            account_id=account_id,
            candidate_id=candidate_id,
        )
    if file_kind == "spreadsheet":
        if path.suffix.lower() == ".csv":
            return _extract_csv(content)
        return _extract_xlsx(content)
    if file_kind == "document":
        if path.suffix.lower() == ".docx":
            try:
                extraction = extract_docx(content)
            except ResumeDocumentError as error:
                raise ProjectEvidenceError(str(error)) from error
            return _bounded_evidence(
                extraction.text,
                extraction.method,
                {"page_count": extraction.page_count},
            )
        return _extract_pptx(content)
    if file_kind == "engineering_drawing":
        return _extract_engineering_text(path, content)
    raise ProjectEvidenceError("当前文件类型没有可用解析器。")


def _bounded_evidence(
    text: str,
    method: str,
    metadata: dict[str, object],
    visual_artifacts: tuple[ProjectVisualArtifact, ...] = (),
) -> ExtractedProjectEvidence:
    normalized = normalize_extracted_text(text)[:MAX_EXTRACTED_TEXT_CHARS]
    if not normalized:
        raise ProjectEvidenceError("没有从项目文件中提取出可分析文字。")
    return ExtractedProjectEvidence(
        text=normalized,
        method=method,
        metadata=metadata,
        visual_artifacts=visual_artifacts,
    )


def _extract_pdf_pages(
    path: Path,
    content: bytes,
    *,
    visual_analyzer: ProjectVisualAnalyzerProtocol | None,
    account_id: int | None,
    candidate_id: int | None,
) -> ExtractedProjectEvidence:
    try:
        with pdfplumber.open(BytesIO(content)) as pdf:
            if not pdf.pages:
                raise ProjectEvidenceError("项目 PDF 没有页面。")
            if len(pdf.pages) > MAX_PROJECT_PDF_PAGES:
                raise ProjectEvidenceError("单个项目 PDF 不能超过 100 页。")
            page_texts = [normalize_extracted_text(page.extract_text() or "") for page in pdf.pages]
            visual_scores = [_pdf_page_visual_score(page) for page in pdf.pages]
    except ProjectEvidenceError:
        raise
    except Exception as error:  # pdfplumber exposes several parser-specific exceptions.
        raise ProjectEvidenceError("项目 PDF 损坏或格式不受支持。") from error

    missing = [index for index, text in enumerate(page_texts) if len(text) < MIN_PDF_TEXT_CHARS_PER_PAGE]
    if missing:
        try:
            ocr_pages = render_and_ocr_pdf_pages(content, missing, run_rapidocr)
        except ResumeDocumentError as error:
            raise ProjectEvidenceError(str(error)) from error
        for index, text in ocr_pages.items():
            page_texts[index] = normalize_extracted_text(text)
    method = "pdf_text_and_ocr" if missing and len(missing) < len(page_texts) else (
        "pdf_ocr" if missing else "pdf_text"
    )
    visual_result = ProjectVisualAnalysisResult(status="disabled")
    visual_pages: list[int] = []
    rendered: dict[int, bytes] = {}
    indexes: list[int] = []
    if visual_analyzer is not None:
        indexes = select_pdf_visual_pages(
            page_texts,
            visual_scores,
            max_pages=visual_analyzer.max_pdf_pages,
        )
        try:
            rendered = render_project_pdf_pages(content, indexes)
            visual_result = visual_analyzer.analyze(
                [
                    ProjectVisualInput(
                        source_id=f"page-{index + 1}",
                        source_label=f"{path.as_posix()}#page={index + 1}",
                        content=rendered[index],
                        extracted_text=page_texts[index],
                    )
                    for index in indexes
                ],
                account_id=account_id,
                candidate_id=candidate_id,
            )
        except Exception as error:  # noqa: BLE001 - PDF 文字/OCR 仍可作为降级证据。
            visual_result = ProjectVisualAnalysisResult(
                failed_source_ids=[f"page-{index + 1}" for index in indexes],
                status="failed",
                error_type=type(error).__name__,
            )
        visual_pages = sorted(
            int(source_id.removeprefix("page-"))
            for source_id in visual_result.findings
            if source_id.startswith("page-") and source_id.removeprefix("page-").isdigit()
        )
        if visual_pages:
            method = f"{method}_and_vlm"

    visual_artifacts = tuple(
        _visual_artifact(
            source_id=f"page-{index + 1}",
            source_label=f"{path.as_posix()}#page={index + 1}",
            content=rendered[index],
            page_number=index + 1,
            metadata={
                "visual_analysis_status": visual_result.status,
                "visual_finding": (
                    visual_result.findings[f"page-{index + 1}"].as_metadata()
                    if f"page-{index + 1}" in visual_result.findings
                    else None
                ),
            },
        )
        for index in indexes
        if index in rendered
    )

    joined = "\n\n".join(
        _pdf_page_evidence_text(index, text, visual_result)
        for index, text in enumerate(page_texts)
        if text or f"page-{index + 1}" in visual_result.findings
    )
    return _bounded_evidence(
        joined,
        method,
        {
            "page_count": len(page_texts),
            "ocr_pages": [index + 1 for index in missing],
            "visual_requested_pages": [index + 1 for index in indexes]
            if visual_analyzer is not None
            else [],
            "visual_pages": visual_pages,
            "visual_analysis_status": visual_result.status,
            "visual_failed_pages": [
                int(source_id.removeprefix("page-"))
                for source_id in visual_result.failed_source_ids
                if source_id.startswith("page-") and source_id.removeprefix("page-").isdigit()
            ],
            "visual_error_type": visual_result.error_type,
            "visual_findings": {
                source_id: finding.as_metadata()
                for source_id, finding in visual_result.findings.items()
            },
        },
        visual_artifacts,
    )


def _extract_image(
    path: Path,
    content: bytes,
    *,
    visual_analyzer: ProjectVisualAnalyzerProtocol | None,
    account_id: int | None,
    candidate_id: int | None,
) -> ExtractedProjectEvidence:
    try:
        with Image.open(BytesIO(content)) as image:
            width, height = image.size
            if width <= 0 or height <= 0 or width * height > MAX_IMAGE_PIXELS:
                raise ProjectEvidenceError("项目图片像素尺寸超过分析上限。")
            image.load()
            text = run_rapidocr(image.convert("RGB"))
    except ProjectEvidenceError:
        raise
    except (UnidentifiedImageError, OSError) as error:
        raise ProjectEvidenceError("项目图片损坏或格式不受支持。") from error

    visual_result = ProjectVisualAnalysisResult(status="disabled")
    if visual_analyzer is not None:
        try:
            visual_result = visual_analyzer.analyze(
                [
                    ProjectVisualInput(
                        source_id="image-1",
                        source_label=path.as_posix(),
                        content=content,
                        extracted_text=text,
                    )
                ],
                account_id=account_id,
                candidate_id=candidate_id,
            )
        except Exception as error:  # noqa: BLE001 - OCR 仍可作为降级证据。
            visual_result = ProjectVisualAnalysisResult(
                failed_source_ids=["image-1"],
                status="failed",
                error_type=type(error).__name__,
            )
    finding = visual_result.findings.get("image-1")
    sections = []
    if text:
        sections.append(f"[OCR 文字]\n{text}")
    if finding is not None:
        sections.append(finding.as_text())
    method = "image_ocr"
    if finding is not None:
        method = "image_ocr_and_vlm" if text else "image_vlm"
    visual_artifacts = ()
    if visual_analyzer is not None:
        visual_artifacts = (
            _visual_artifact(
                source_id="image-1",
                source_label=path.as_posix(),
                content=content,
                metadata={
                    "visual_analysis_status": visual_result.status,
                    "visual_finding": finding.as_metadata() if finding is not None else None,
                },
            ),
        )
    return _bounded_evidence(
        "\n\n".join(sections),
        method,
        {
            "width": width,
            "height": height,
            "visual_analysis_status": visual_result.status,
            "visual_error_type": visual_result.error_type,
            "visual_finding": finding.as_metadata() if finding is not None else None,
        },
        visual_artifacts,
    )


def _visual_artifact(
    *,
    source_id: str,
    source_label: str,
    content: bytes,
    page_number: int | None = None,
    metadata: dict[str, object] | None = None,
) -> ProjectVisualArtifact:
    """生成去元数据、受尺寸约束且可重复读取的视觉副本。"""

    media_type, normalized = normalize_project_visual_image(content)
    try:
        with Image.open(BytesIO(normalized)) as image:
            width, height = image.size
    except (UnidentifiedImageError, OSError) as error:  # pragma: no cover - 规范化已验证。
        raise ProjectEvidenceError("项目视觉副本生成失败。") from error
    return ProjectVisualArtifact(
        source_id=source_id,
        source_label=source_label,
        content=normalized,
        media_type=media_type,
        width=width,
        height=height,
        page_number=page_number,
        metadata=dict(metadata or {}),
    )


def _pdf_page_visual_score(page: object) -> int:
    """用低成本 PDF 对象数量估算页面是否含图表、工程线条或嵌入图片。"""

    images = len(getattr(page, "images", None) or [])
    lines = len(getattr(page, "lines", None) or [])
    rects = len(getattr(page, "rects", None) or [])
    curves = len(getattr(page, "curves", None) or [])
    return images * 100 + lines * 3 + rects * 2 + curves * 2


def select_pdf_visual_pages(
    page_texts: list[str],
    visual_scores: list[int],
    *,
    max_pages: int,
) -> list[int]:
    """优先选择扫描页、图表页和代表页，并保持最终页序。"""

    page_count = len(page_texts)
    if page_count <= max_pages:
        return list(range(page_count))
    priority: list[int] = []

    def add(indexes: list[int]) -> None:
        for index in indexes:
            if 0 <= index < page_count and index not in priority:
                priority.append(index)

    missing_text = [
        index
        for index, text in enumerate(page_texts)
        if len(text) < MIN_PDF_TEXT_CHARS_PER_PAGE
    ]
    add(sorted(missing_text, key=lambda index: visual_scores[index], reverse=True))
    visual_pages = [index for index, score in enumerate(visual_scores) if score > 0]
    add(sorted(visual_pages, key=lambda index: visual_scores[index], reverse=True))
    add([0, page_count // 4, page_count // 2, (page_count * 3) // 4, page_count - 1])
    add(sorted(range(page_count), key=lambda index: len(page_texts[index])))
    return sorted(priority[:max_pages])


def render_project_pdf_pages(content: bytes, page_indexes: list[int]) -> dict[int, bytes]:
    """将有限 PDF 页面渲染成 PNG 字节，供多模态模型读取。"""

    try:
        import pypdfium2 as pdfium

        document = pdfium.PdfDocument(content)
        rendered: dict[int, bytes] = {}
        try:
            for index in page_indexes:
                page = document[index]
                bitmap = page.render(scale=1.5)
                image = bitmap.to_pil().convert("RGB")
                try:
                    output = BytesIO()
                    image.save(output, format="PNG", optimize=True)
                    rendered[index] = output.getvalue()
                finally:
                    image.close()
                    bitmap.close()
                    page.close()
        finally:
            document.close()
        return rendered
    except Exception as error:
        raise ProjectEvidenceError("项目 PDF 页面无法渲染用于视觉分析。") from error


def _pdf_page_evidence_text(
    index: int,
    text: str,
    visual_result: ProjectVisualAnalysisResult,
) -> str:
    sections = [f"[第 {index + 1} 页]"]
    if text:
        sections.append(text)
    finding = visual_result.findings.get(f"page-{index + 1}")
    if finding is not None:
        sections.append(finding.as_text())
    return "\n".join(sections)


def _extract_csv(content: bytes) -> ExtractedProjectEvidence:
    decoded = decode_text_bytes(content)
    rows: list[str] = []
    reader = csv.reader(StringIO(decoded))
    for index, row in enumerate(reader):
        if index >= MAX_SPREADSHEET_ROWS:
            break
        rows.append("\t".join(str(value)[:2_000] for value in row[:100]))
    return _bounded_evidence(
        "[工作表 CSV]\n" + "\n".join(rows),
        "csv_rows",
        {"sheet_count": 1, "row_count": len(rows)},
    )


def _extract_xlsx(content: bytes) -> ExtractedProjectEvidence:
    try:
        with zipfile.ZipFile(BytesIO(content)) as archive:
            names = set(archive.namelist())
            if "[Content_Types].xml" not in names or "xl/workbook.xml" not in names:
                raise ProjectEvidenceError("XLSX 文件结构无效。")
            if len(names) > 5_000:
                raise ProjectEvidenceError("XLSX 内部文件数量超过分析上限。")
            shared = _xlsx_shared_strings(archive, names)
            sheets = _xlsx_sheet_targets(archive, names)
            sections: list[str] = []
            sheet_metadata: list[dict[str, object]] = []
            for sheet_name, target in sheets[:MAX_SPREADSHEET_SHEETS]:
                rows, cell_count = _xlsx_sheet_rows(archive, target, shared)
                sections.append(f"[工作表 {sheet_name}]\n" + "\n".join(rows))
                sheet_metadata.append(
                    {"name": sheet_name, "row_count": len(rows), "cell_count": cell_count}
                )
    except ProjectEvidenceError:
        raise
    except (zipfile.BadZipFile, KeyError, ElementTree.ParseError, OSError) as error:
        raise ProjectEvidenceError("XLSX 文件损坏或格式不受支持。") from error
    return _bounded_evidence(
        "\n\n".join(sections),
        "xlsx_sheets",
        {"sheet_count": len(sheet_metadata), "sheets": sheet_metadata},
    )


def _xlsx_shared_strings(archive: zipfile.ZipFile, names: set[str]) -> list[str]:
    if "xl/sharedStrings.xml" not in names:
        return []
    root = ElementTree.fromstring(archive.read("xl/sharedStrings.xml"))
    return ["".join(node.itertext()) for node in root]


def _xlsx_sheet_targets(
    archive: zipfile.ZipFile,
    names: set[str],
) -> list[tuple[str, str]]:
    relationship_path = "xl/_rels/workbook.xml.rels"
    relationships: dict[str, str] = {}
    if relationship_path in names:
        root = ElementTree.fromstring(archive.read(relationship_path))
        for node in root:
            relationship_id = node.attrib.get("Id", "")
            target = node.attrib.get("Target", "")
            if relationship_id and target:
                relationships[relationship_id] = "xl/" + target.lstrip("/").removeprefix("xl/")
    workbook = ElementTree.fromstring(archive.read("xl/workbook.xml"))
    result: list[tuple[str, str]] = []
    for index, sheet in enumerate(node for node in workbook.iter() if node.tag.endswith("}sheet")):
        relationship_id = next(
            (value for key, value in sheet.attrib.items() if key.endswith("}id")),
            "",
        )
        target = relationships.get(relationship_id, f"xl/worksheets/sheet{index + 1}.xml")
        if target in names:
            result.append((sheet.attrib.get("name", f"Sheet{index + 1}"), target))
    return result


def _xlsx_sheet_rows(
    archive: zipfile.ZipFile,
    target: str,
    shared: list[str],
) -> tuple[list[str], int]:
    root = ElementTree.fromstring(archive.read(target))
    rows: list[str] = []
    cell_count = 0
    for row in (node for node in root.iter() if node.tag.endswith("}row")):
        if len(rows) >= MAX_SPREADSHEET_ROWS or cell_count >= MAX_SPREADSHEET_CELLS_PER_SHEET:
            break
        values: list[str] = []
        for cell in (node for node in row if node.tag.endswith("}c")):
            if cell_count >= MAX_SPREADSHEET_CELLS_PER_SHEET:
                break
            cell_count += 1
            raw = next((node.text or "" for node in cell.iter() if node.tag.endswith("}v")), "")
            if cell.attrib.get("t") == "s" and raw.isdigit() and int(raw) < len(shared):
                raw = shared[int(raw)]
            elif cell.attrib.get("t") == "inlineStr":
                raw = "".join(node.text or "" for node in cell.iter() if node.tag.endswith("}t"))
            values.append(f"{cell.attrib.get('r', '')}={raw}" if raw else "")
        rows.append("\t".join(value for value in values if value))
    return rows, cell_count


def _extract_pptx(content: bytes) -> ExtractedProjectEvidence:
    try:
        with zipfile.ZipFile(BytesIO(content)) as archive:
            slide_names = sorted(
                name for name in archive.namelist()
                if re.fullmatch(r"ppt/slides/slide\d+\.xml", name)
            )
            sections = []
            for index, name in enumerate(slide_names[:200]):
                root = ElementTree.fromstring(archive.read(name))
                text = " ".join(node.text or "" for node in root.iter() if node.tag.endswith("}t"))
                if text.strip():
                    sections.append(f"[第 {index + 1} 页]\n{text}")
    except (zipfile.BadZipFile, ElementTree.ParseError, OSError) as error:
        raise ProjectEvidenceError("PPTX 文件损坏或格式不受支持。") from error
    return _bounded_evidence(
        "\n\n".join(sections),
        "pptx_slides",
        {"slide_count": len(slide_names)},
    )


def _extract_engineering_text(path: Path, content: bytes) -> ExtractedProjectEvidence:
    suffix = path.suffix.lower()
    decoded = decode_text_bytes(content)
    if suffix == ".svg":
        try:
            root = ElementTree.fromstring(content)
            decoded = "\n".join(text.strip() for text in root.itertext() if text.strip())
        except ElementTree.ParseError as error:
            raise ProjectEvidenceError("SVG 工程图损坏。") from error
    return _bounded_evidence(
        decoded,
        "engineering_text",
        {"format": suffix.lstrip(".")},
    )
