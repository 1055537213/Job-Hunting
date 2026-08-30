"""文件上传入口的攻击边界回归。"""

from __future__ import annotations

import stat
from io import BytesIO
from zipfile import ZipFile, ZipInfo

import pytest
from docx import Document

import job_hunting_agent.project_archive as project_archive_module
import job_hunting_agent.resume_document as resume_document_module
from job_hunting_agent.project_archive import (
    ProjectArchiveError,
    analyze_project_archive,
    validate_project_archive_upload,
)
from job_hunting_agent.resume_document import (
    ResumeDocumentError,
    extract_resume_document,
    validate_resume_file_size,
)


def build_zip(entries: list[tuple[str | ZipInfo, bytes]]) -> bytes:
    output = BytesIO()
    with ZipFile(output, "w") as archive:
        for name, content in entries:
            archive.writestr(name, content)
    return output.getvalue()


def build_docx() -> bytes:
    document = Document()
    document.add_paragraph("Python FastAPI 项目经历与交付结果。")
    output = BytesIO()
    document.save(output)
    return output.getvalue()


def test_project_archive_filename_removes_windows_and_posix_parent_paths() -> None:
    content = build_zip([("project/README.md", b"safe project evidence")])

    assert validate_project_archive_upload("../../escape.zip", content) == "escape.zip"
    assert validate_project_archive_upload(r"..\..\escape.zip", content) == "escape.zip"


def test_project_archive_skips_traversal_and_symlink_entries() -> None:
    symlink = ZipInfo("project/link-to-secret")
    symlink.create_system = 3
    symlink.external_attr = (stat.S_IFLNK | 0o777) << 16
    content = build_zip(
        [
            ("project/README.md", b"safe project evidence"),
            ("../outside.py", b"must never be read"),
            (symlink, b"../../outside-secret"),
        ]
    )

    analysis = analyze_project_archive(filename="project.zip", content=content)

    paths = {item.relative_path for item in analysis.files}
    assert "README.md" in paths
    assert "../outside.py" not in paths
    assert "link-to-secret" not in paths
    assert analysis.card.skipped_summary["unsafe_path"] == 1
    assert analysis.card.skipped_summary["symlink"] == 1


def test_project_archive_rejects_duplicate_normalized_paths() -> None:
    with pytest.warns(UserWarning, match="Duplicate name"):
        content = build_zip(
            [
                ("project/README.md", b"first"),
                ("project/README.md", b"second"),
            ]
        )

    with pytest.raises(ProjectArchiveError, match="重复文件路径"):
        analyze_project_archive(filename="project.zip", content=content)


def test_project_archive_rejects_entry_and_uncompressed_size_budgets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(project_archive_module, "MAX_ARCHIVE_ENTRIES", 1)
    too_many = build_zip([("a.txt", b"a"), ("b.txt", b"b")])
    with pytest.raises(ProjectArchiveError, match="文件数量"):
        analyze_project_archive(filename="project.zip", content=too_many)

    monkeypatch.setattr(project_archive_module, "MAX_ARCHIVE_ENTRIES", 20_000)
    monkeypatch.setattr(project_archive_module, "MAX_ARCHIVE_UNCOMPRESSED_BYTES", 8)
    oversized = build_zip([("README.md", b"123456789")])
    with pytest.raises(ProjectArchiveError, match="解压后的内容"):
        analyze_project_archive(filename="project.zip", content=oversized)


def test_resume_rejects_extension_signature_mismatch_and_size_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(ResumeDocumentError, match="PDF 文件签名无效"):
        extract_resume_document("disguised.pdf", build_docx())

    monkeypatch.setattr(resume_document_module, "MAX_RESUME_FILE_BYTES", 8)
    with pytest.raises(ResumeDocumentError, match="不能超过"):
        validate_resume_file_size(b"123456789")
