"""整包项目 ZIP 的安全检查、文件清单和类型路由。

项目原件始终保留在对象存储中。本模块只在内存中读取受限 ZIP，并把每个受支持文件
交给共享证据解析器；成功结果带路径、页码或工作表来源进入长文本，无法解析的格式仍
保留在清单中并记录失败原因。
"""

from __future__ import annotations

import hashlib
import mimetypes
import zipfile
from collections import Counter
from dataclasses import dataclass, field
from io import BytesIO
from pathlib import Path

from .github_project import (
    MAX_ARCHIVE_ENTRIES,
    MAX_ARCHIVE_UNCOMPRESSED_BYTES,
    MAX_REPOSITORY_ARCHIVE_BYTES,
    archive_root_prefix,
    is_zip_symlink,
    safe_archive_path,
)
from .models import ProjectExperienceCard
from .project_analyzer import (
    SKIP_DIRS,
    build_project_experience_card,
    is_sensitive,
)
from .project_evidence import (
    MAX_FILE_BYTES_BY_KIND,
    MAX_SELECTED_FILES_BY_KIND,
    MAX_SELECTED_PROJECT_BYTES,
    MAX_SELECTED_PROJECT_FILES,
    ProjectEvidenceError,
    ProjectVisualArtifact,
    extract_project_evidence,
    project_file_kind,
)
from .project_visual import ProjectVisualAnalyzerProtocol

PROJECT_ARCHIVE_MEDIA_TYPE = "application/zip"
PROJECT_ARCHIVE_SUFFIX = ".zip"

class ProjectArchiveError(ValueError):
    """项目 ZIP 格式、体积或内部结构不符合安全边界。"""


@dataclass(frozen=True)
class ProjectArchiveFile:
    """一项待持久化的项目文件清单元数据。"""

    relative_path: str
    file_kind: str
    media_type: str
    file_size: int
    compressed_size: int
    sha256: str | None
    analysis_status: str
    skip_reason: str | None = None
    metadata: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class ProjectArchiveAnalysis:
    """整包分析结果：项目卡片、完整清单和待持久化证据。"""

    card: ProjectExperienceCard
    files: list[ProjectArchiveFile]
    evidence: list[ProjectArchiveEvidence] = field(default_factory=list)
    visual_artifacts: list[ProjectArchiveVisualArtifact] = field(default_factory=list)


@dataclass(frozen=True)
class ProjectArchiveEvidence:
    """从归档文件提取出的有来源定位的文本证据。"""

    relative_path: str
    text: str
    extraction_method: str
    metadata: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class ProjectArchiveVisualArtifact:
    """ZIP 内某个文件派生的一张安全视觉副本。"""

    relative_path: str
    artifact: ProjectVisualArtifact


def validate_project_archive_upload(filename: str, content: bytes) -> str:
    """校验上传文件名和压缩包体积，返回安全展示用文件名。"""

    normalized = Path(str(filename or "").strip()).name
    if not normalized or Path(normalized).suffix.lower() != PROJECT_ARCHIVE_SUFFIX:
        raise ProjectArchiveError("项目文件必须是 ZIP 压缩包。")
    if not content:
        raise ProjectArchiveError("项目 ZIP 不能为空。")
    if len(content) > MAX_REPOSITORY_ARCHIVE_BYTES:
        raise ProjectArchiveError("项目 ZIP 不能超过 30 MB。")
    return normalized[:512]


def analyze_project_archive(
    *,
    filename: str,
    content: bytes,
    source_type: str = "uploaded_project_archive",
    source_url: str | None = None,
    source_ref: str | None = None,
    visual_analyzer: ProjectVisualAnalyzerProtocol | None = None,
    account_id: int | None = None,
    candidate_id: int | None = None,
) -> ProjectArchiveAnalysis:
    """安全读取 ZIP，路由每个文件，并用当前可读文本生成待确认卡片。"""

    normalized_filename = validate_project_archive_upload(filename, content)
    try:
        with zipfile.ZipFile(BytesIO(content)) as archive:
            infos = archive.infolist()
            if len(infos) > MAX_ARCHIVE_ENTRIES:
                raise ProjectArchiveError("项目文件数量超过 20000 个，请精简后重试。")
            if sum(max(0, info.file_size) for info in infos) > MAX_ARCHIVE_UNCOMPRESSED_BYTES:
                raise ProjectArchiveError("项目解压后的内容不能超过 120 MB。")
            root_prefix = archive_root_prefix(infos)
            (
                files,
                selected_text,
                evidence,
                visual_artifacts,
                skipped,
                discovered,
                deferred,
            ) = _route_archive_files(
                archive,
                infos,
                root_prefix,
                visual_analyzer=visual_analyzer,
                account_id=account_id,
                candidate_id=candidate_id,
            )
    except ProjectArchiveError:
        raise
    except (zipfile.BadZipFile, zipfile.LargeZipFile) as error:
        raise ProjectArchiveError("项目 ZIP 格式无效，无法安全分析。") from error
    except OSError as error:
        raise ProjectArchiveError("读取项目 ZIP 失败。") from error

    project_name = (root_prefix or Path(normalized_filename).stem).strip()[:256]
    card = build_project_experience_card(
        project_name=project_name or "未命名项目",
        selected_files=selected_text,
        skipped_summary=skipped,
        source_type=source_type,
        source_url=source_url,
        source_ref=source_ref,
    )
    card.discovered_file_kinds = dict(discovered)
    card.deferred_files = deferred[:100]
    return ProjectArchiveAnalysis(
        card=card,
        files=files,
        evidence=evidence,
        visual_artifacts=visual_artifacts,
    )


def _route_archive_files(
    archive: zipfile.ZipFile,
    infos: list[zipfile.ZipInfo],
    root_prefix: str | None,
    *,
    visual_analyzer: ProjectVisualAnalyzerProtocol | None,
    account_id: int | None,
    candidate_id: int | None,
) -> tuple[
    list[ProjectArchiveFile],
    list[tuple[Path, str]],
    list[ProjectArchiveEvidence],
    list[ProjectArchiveVisualArtifact],
    Counter[str],
    Counter[str],
    list[str],
]:
    """构建完整清单；只有当前可控文本进入项目卡片分析器。"""

    manifest: list[ProjectArchiveFile] = []
    selected_text: list[tuple[Path, str]] = []
    evidence: list[ProjectArchiveEvidence] = []
    visual_artifacts: list[ProjectArchiveVisualArtifact] = []
    skipped: Counter[str] = Counter()
    discovered: Counter[str] = Counter()
    selected_counts: Counter[str] = Counter()
    selected_file_count = 0
    selected_bytes = 0
    deferred: list[str] = []
    seen_paths: set[str] = set()

    for info in infos:
        if info.is_dir():
            continue
        path = safe_archive_path(info.filename)
        if path is None:
            skipped["unsafe_path"] += 1
            continue
        if is_zip_symlink(info):
            skipped["symlink"] += 1
            continue
        if info.flag_bits & 0x1:
            skipped["encrypted_file"] += 1
            continue

        parts = path.parts
        if root_prefix and parts and parts[0] == root_prefix:
            parts = parts[1:]
        if not parts:
            continue
        relative_path = Path(*parts)
        relative_key = relative_path.as_posix()
        if relative_key in seen_paths:
            raise ProjectArchiveError(f"项目 ZIP 包含重复文件路径：{relative_key}")
        seen_paths.add(relative_key)

        file_kind = project_file_kind(relative_path)
        media_type = mimetypes.guess_type(relative_path.name)[0] or "application/octet-stream"
        skip_reason: str | None = None
        analysis_status = "unsupported" if file_kind == "unsupported" else "pending_parser"
        digest: str | None = None
        extraction_metadata: dict[str, object] = {}

        skipped_dir = next((part for part in parts[:-1] if part.lower() in SKIP_DIRS), None)
        if skipped_dir is not None:
            skip_reason = f"dir:{skipped_dir}"
            analysis_status = "skipped"
            skipped[skip_reason] += 1
        elif is_sensitive(relative_path):
            skip_reason = "sensitive_name"
            analysis_status = "skipped"
            skipped[skip_reason] += 1
        elif file_kind == "unsupported":
            skip_reason = "unsupported_type"
            analysis_status = "unsupported"
            skipped[skip_reason] += 1
        elif info.file_size > MAX_FILE_BYTES_BY_KIND[file_kind]:
            skip_reason = "file_too_large"
            analysis_status = "skipped"
            skipped[skip_reason] += 1
        elif selected_file_count >= MAX_SELECTED_PROJECT_FILES:
            skip_reason = "project_file_budget_reached"
            analysis_status = "skipped"
            skipped[skip_reason] += 1
        elif selected_bytes + info.file_size > MAX_SELECTED_PROJECT_BYTES:
            skip_reason = "project_byte_budget_reached"
            analysis_status = "skipped"
            skipped[skip_reason] += 1
        elif selected_counts[file_kind] >= MAX_SELECTED_FILES_BY_KIND[file_kind]:
            skip_reason = "parser_quota_reached"
            analysis_status = "skipped"
            skipped[skip_reason] += 1
        else:
            digest = _archive_entry_sha256(archive, info)
            selected_counts[file_kind] += 1
            selected_file_count += 1
            selected_bytes += info.file_size
            try:
                raw = _read_bounded_entry(
                    archive,
                    info,
                    MAX_FILE_BYTES_BY_KIND[file_kind],
                )
                extracted = extract_project_evidence(
                    relative_key,
                    raw,
                    file_kind,
                    visual_analyzer=visual_analyzer,
                    account_id=account_id,
                    candidate_id=candidate_id,
                )
            except ProjectEvidenceError as error:
                analysis_status = "failed"
                skip_reason = "parser_error"
                deferred.append(relative_key)
                skipped[skip_reason] += 1
                extraction_metadata = {"error_summary": str(error)[:500]}
            else:
                selected_text.append((relative_path, extracted.text))
                analysis_status = "analyzed"
                if extracted.metadata.get("visual_analysis_status") in {"failed", "partial"}:
                    deferred.append(relative_key)
                extraction_metadata = {
                    **extracted.metadata,
                    "extraction_method": extracted.method,
                    "text_length": len(extracted.text),
                }
                evidence.append(
                    ProjectArchiveEvidence(
                        relative_path=relative_key,
                        text=extracted.text,
                        extraction_method=extracted.method,
                        metadata=extracted.metadata,
                    )
                )
                visual_artifacts.extend(
                    ProjectArchiveVisualArtifact(
                        relative_path=relative_key,
                        artifact=artifact,
                    )
                    for artifact in extracted.visual_artifacts
                )

        discovered[file_kind] += 1
        manifest.append(
            ProjectArchiveFile(
                relative_path=relative_key,
                file_kind=file_kind,
                media_type=media_type,
                file_size=max(0, int(info.file_size)),
                compressed_size=max(0, int(info.compress_size)),
                sha256=digest,
                analysis_status=analysis_status,
                skip_reason=skip_reason,
                metadata={
                    "suffix": relative_path.suffix.lower(),
                    **extraction_metadata,
                },
            )
        )
    return (
        manifest,
        selected_text,
        evidence,
        visual_artifacts,
        skipped,
        discovered,
        deferred,
    )


def _read_bounded_entry(
    archive: zipfile.ZipFile,
    info: zipfile.ZipInfo,
    max_bytes: int,
) -> bytes:
    """读取单个受限项目文件，并阻止声明尺寸与实际读取不一致。"""

    try:
        with archive.open(info, "r") as source:
            raw = source.read(max_bytes + 1)
    except (RuntimeError, OSError, zipfile.BadZipFile) as error:
        raise ProjectArchiveError(f"无法读取项目文件：{info.filename}") from error
    if len(raw) > max_bytes:
        raise ProjectArchiveError(f"项目文件超过单文件读取上限：{info.filename}")
    return raw


def _archive_entry_sha256(archive: zipfile.ZipFile, info: zipfile.ZipInfo) -> str:
    """流式计算清单摘要，避免把二进制文件整体复制到第二份内存。"""

    digest = hashlib.sha256()
    received = 0
    try:
        with archive.open(info, "r") as source:
            while chunk := source.read(64 * 1024):
                received += len(chunk)
                if received > info.file_size or received > MAX_ARCHIVE_UNCOMPRESSED_BYTES:
                    raise ProjectArchiveError(f"项目文件解压尺寸异常：{info.filename}")
                digest.update(chunk)
    except ProjectArchiveError:
        raise
    except (RuntimeError, OSError, zipfile.BadZipFile) as error:
        raise ProjectArchiveError(f"无法校验项目文件：{info.filename}") from error
    return digest.hexdigest()
