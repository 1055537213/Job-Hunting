"""公开 GitHub 仓库的受控只读分析入口。

网页服务不接触用户电脑，因此项目分析只能读取用户主动提供的公开仓库。这里不
执行仓库代码、不调用 git、不解压到磁盘；只通过 GitHub 官方 API 和 codeload 归档
读取经过大小、文件数、路径和敏感文件规则限制的文本内容。
"""

from __future__ import annotations

import json
import re
import stat
import zipfile
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .models import ProjectExperienceCard
from .project_analyzer import (
    IMPORTANT_NAMES,
    MAX_FILE_BYTES,
    MAX_FILES,
    SKIP_DIRS,
    SKIP_SUFFIXES,
    SOURCE_SUFFIXES,
    build_project_experience_card,
    decode_text_bytes,
    is_sensitive,
)


# GitHub 仓库首页链接是唯一允许的用户输入网络地址。下载端点由程序从 owner/repo
# 自行构造，避免把任意 URL 变成 Worker 可访问的网络目标（SSRF）。
GITHUB_WEB_HOSTS = {"github.com", "www.github.com"}
GITHUB_NETWORK_HOSTS = {"api.github.com", "codeload.github.com"}
GITHUB_USER_AGENT = "job-hunting-agent-project-analysis/0.1"
MAX_GITHUB_METADATA_BYTES = 256 * 1024
MAX_REPOSITORY_ARCHIVE_BYTES = 30 * 1024 * 1024
MAX_ARCHIVE_ENTRIES = 20_000
MAX_ARCHIVE_UNCOMPRESSED_BYTES = 120 * 1024 * 1024
HTTP_TIMEOUT_SECONDS = 20
REPOSITORY_SEGMENT_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")


class GitHubRepositoryError(ValueError):
    """GitHub 仓库链接、访问或归档内容不符合分析边界。"""


class InvalidGitHubRepositoryUrlError(GitHubRepositoryError):
    """用户提供的仓库首页链接不在允许范围内。"""


class GitHubRepositoryNotFoundError(GitHubRepositoryError):
    """仓库不存在、私有或当前无法以公开方式读取。"""


class GitHubRepositoryUnavailableError(GitHubRepositoryError):
    """GitHub 网络、限流或归档服务暂时不可用。"""


@dataclass(frozen=True)
class GitHubRepositoryReference:
    """经校验后的公开 GitHub 仓库标识。"""

    owner: str
    repository: str

    @property
    def canonical_url(self) -> str:
        """返回可安全保存到项目卡片中的仓库首页地址。"""

        return f"https://github.com/{self.owner}/{self.repository}"

    @property
    def api_url(self) -> str:
        """返回官方仓库元数据 API 地址。"""

        return f"https://api.github.com/repos/{self.owner}/{self.repository}"


FetchBytes = Callable[[str, int], bytes]


def normalize_public_github_repository_url(value: str) -> GitHubRepositoryReference:
    """校验公开 GitHub 仓库首页链接，并拒绝任意外部 URL。

    第一版刻意只接受 ``https://github.com/<owner>/<repo>``。这样既能减少
    URL 解析歧义，也不会把 issues、pull、raw、git clone 等地址误当成源码来源。
    用户可以在 GitHub 仓库首页复制地址；私有仓库 OAuth/Token 将在后续版本独立接入。
    """

    raw = str(value or "").strip()
    if not raw:
        raise InvalidGitHubRepositoryUrlError("请填写公开 GitHub 仓库首页链接。")
    if any(character.isspace() for character in raw):
        raise InvalidGitHubRepositoryUrlError("GitHub 仓库链接不能包含空格或换行。")

    parsed = urlsplit(raw)
    hostname = (parsed.hostname or "").lower()
    if parsed.scheme.lower() != "https" or hostname not in GITHUB_WEB_HOSTS:
        raise InvalidGitHubRepositoryUrlError(
            "只支持公开 GitHub 仓库首页链接，例如 https://github.com/owner/repository 。"
        )
    try:
        parsed_port = parsed.port
    except ValueError as error:
        raise InvalidGitHubRepositoryUrlError("GitHub 仓库链接端口格式无效。") from error
    if parsed.username or parsed.password or parsed_port is not None:
        raise InvalidGitHubRepositoryUrlError("GitHub 仓库链接不能包含账号、密码或端口。")
    if parsed.query or parsed.fragment:
        raise InvalidGitHubRepositoryUrlError("请使用不带参数的 GitHub 仓库首页链接。")

    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) != 2:
        raise InvalidGitHubRepositoryUrlError(
            "请提供仓库首页链接，不要使用分支、文件、Issue 或 Pull Request 地址。"
        )
    owner, repository = parts
    if repository.lower().endswith(".git"):
        repository = repository[:-4]
    if not REPOSITORY_SEGMENT_PATTERN.fullmatch(owner) or not REPOSITORY_SEGMENT_PATTERN.fullmatch(repository):
        raise InvalidGitHubRepositoryUrlError("GitHub 仓库所有者或仓库名称格式无效。")
    return GitHubRepositoryReference(owner=owner, repository=repository)


def analyze_public_github_repository(
    repository_url: str,
    *,
    fetch_bytes: FetchBytes | None = None,
) -> ProjectExperienceCard:
    """下载并分析一份公开 GitHub 仓库，返回尚待候选人确认的项目卡片。"""

    reference = normalize_public_github_repository_url(repository_url)
    fetch = fetch_bytes or fetch_github_https_bytes
    metadata = _read_repository_metadata(reference, fetch)
    branch = metadata.get("default_branch")
    if not isinstance(branch, str) or not branch.strip() or len(branch) > 255:
        raise GitHubRepositoryError("GitHub 仓库没有可读取的默认分支。")
    if metadata.get("private") is True:
        raise GitHubRepositoryNotFoundError("当前只支持公开 GitHub 仓库。")

    archive_url = (
        f"https://codeload.github.com/{reference.owner}/{reference.repository}/zip/"
        f"refs/heads/{quote(branch.strip(), safe='/')}"
    )
    archive_content = fetch(archive_url, MAX_REPOSITORY_ARCHIVE_BYTES)
    return analyze_github_archive(
        reference=reference,
        default_branch=branch.strip(),
        archive_content=archive_content,
    )


def analyze_github_archive(
    *,
    reference: GitHubRepositoryReference,
    default_branch: str,
    archive_content: bytes,
) -> ProjectExperienceCard:
    """从已经下载的 GitHub ZIP 归档中筛选文本文件并构造项目卡片。

    此函数独立于网络，便于测试 ZIP 路径穿越、压缩炸弹和敏感文件过滤规则。
    """

    if len(archive_content) > MAX_REPOSITORY_ARCHIVE_BYTES:
        raise GitHubRepositoryError("仓库归档超过 30 MB 分析上限，请精简后重试。")
    try:
        with zipfile.ZipFile(BytesIO(archive_content)) as archive:
            infos = archive.infolist()
            if len(infos) > MAX_ARCHIVE_ENTRIES:
                raise GitHubRepositoryError("仓库文件数量超过分析上限，请精简后重试。")
            if sum(max(0, item.file_size) for item in infos) > MAX_ARCHIVE_UNCOMPRESSED_BYTES:
                raise GitHubRepositoryError("仓库解压后的内容超过分析上限，请精简后重试。")
            root_prefix = archive_root_prefix(infos)
            selected, skipped = select_archive_project_files(archive, infos, root_prefix)
    except GitHubRepositoryError:
        raise
    except (zipfile.BadZipFile, zipfile.LargeZipFile) as error:
        raise GitHubRepositoryError("GitHub 返回的仓库归档无效，无法安全分析。") from error
    except OSError as error:
        raise GitHubRepositoryError("读取 GitHub 仓库归档失败。") from error

    return build_project_experience_card(
        project_name=reference.repository,
        selected_files=selected,
        skipped_summary=skipped,
        source_type="github_public_repository",
        source_url=reference.canonical_url,
        source_ref=default_branch,
    )


def _read_repository_metadata(
    reference: GitHubRepositoryReference,
    fetch: FetchBytes,
) -> dict[str, Any]:
    """读取并校验官方 API 返回的最小仓库元数据。"""

    try:
        payload = json.loads(fetch(reference.api_url, MAX_GITHUB_METADATA_BYTES).decode("utf-8"))
    except GitHubRepositoryError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise GitHubRepositoryError("GitHub 仓库元数据格式无效。") from error
    if not isinstance(payload, dict):
        raise GitHubRepositoryError("GitHub 仓库元数据格式无效。")
    return payload


def archive_root_prefix(infos: list[zipfile.ZipInfo]) -> str | None:
    """返回 GitHub ZIP 共同的顶层目录，避免卡片文件路径包含随机提交号。"""

    top_level_parts: set[str] = set()
    for info in infos:
        path = safe_archive_path(info.filename)
        if path is None or not path.parts:
            continue
        top_level_parts.add(path.parts[0])
        if len(top_level_parts) > 1:
            return None
    return next(iter(top_level_parts), None)


def select_archive_project_files(
    archive: zipfile.ZipFile,
    infos: list[zipfile.ZipInfo],
    root_prefix: str | None,
) -> tuple[list[tuple[Path, str]], Counter[str]]:
    """按本地分析器同一套过滤规则读取 ZIP 中有限数量的文本文件。"""

    selected: list[tuple[Path, str]] = []
    skipped: Counter[str] = Counter()
    recorded_skip_dirs: set[str] = set()

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
        skipped_dir = next((part for part in parts[:-1] if part.lower() in SKIP_DIRS), None)
        if skipped_dir is not None:
            key = f"dir:{skipped_dir}"
            if key not in recorded_skip_dirs:
                skipped[key] += 1
                recorded_skip_dirs.add(key)
            continue
        if is_sensitive(relative_path):
            skipped["sensitive_name"] += 1
            continue
        if relative_path.suffix.lower() in SKIP_SUFFIXES:
            skipped["skipped_suffix"] += 1
            continue
        if (
            relative_path.name.lower() not in IMPORTANT_NAMES
            and relative_path.suffix.lower() not in SOURCE_SUFFIXES
        ):
            skipped["unsupported_type"] += 1
            continue
        if info.file_size > MAX_FILE_BYTES:
            skipped["large_file"] += 1
            continue
        try:
            with archive.open(info, "r") as source:
                raw = source.read(MAX_FILE_BYTES + 1)
        except (RuntimeError, OSError, zipfile.BadZipFile):
            skipped["unreadable_file"] += 1
            continue
        if len(raw) > MAX_FILE_BYTES:
            skipped["large_file"] += 1
            continue
        selected.append((relative_path, decode_text_bytes(raw)))
        if len(selected) >= MAX_FILES:
            skipped["max_files_reached"] += 1
            break
    return selected, skipped


def safe_archive_path(filename: str) -> PurePosixPath | None:
    """校验 ZIP 内部路径，拒绝 Windows 分隔符、绝对路径和回退路径。"""

    if not filename or "\\" in filename:
        return None
    path = PurePosixPath(filename)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        return None
    return path


def is_zip_symlink(info: zipfile.ZipInfo) -> bool:
    """拒绝 ZIP 中的 POSIX 符号链接，避免随后被误解释成可读文件。"""

    mode = info.external_attr >> 16
    return stat.S_ISLNK(mode)


class GitHubOnlyRedirectHandler(HTTPRedirectHandler):
    """仅允许 GitHub 官方网络主机之间的 HTTPS 重定向。"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001,D401
        """拒绝重定向到任意第三方或内网地址。"""

        parsed = urlsplit(newurl)
        if parsed.scheme.lower() != "https" or (parsed.hostname or "").lower() not in GITHUB_NETWORK_HOSTS:
            raise GitHubRepositoryUnavailableError("GitHub 下载地址发生了不受支持的跳转。")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def fetch_github_https_bytes(url: str, max_bytes: int) -> bytes:
    """通过受限 HTTPS 请求读取 GitHub 官方响应，并严格限制响应体大小。"""

    parsed = urlsplit(url)
    if parsed.scheme.lower() != "https" or (parsed.hostname or "").lower() not in GITHUB_NETWORK_HOSTS:
        raise GitHubRepositoryError("GitHub 下载地址不在允许范围内。")
    if max_bytes <= 0:
        raise ValueError("GitHub 响应大小上限必须大于 0。")

    request = Request(
        url,
        headers={
            "Accept": "application/vnd.github+json, application/zip",
            "User-Agent": GITHUB_USER_AGENT,
        },
    )
    opener = build_opener(GitHubOnlyRedirectHandler())
    try:
        with opener.open(request, timeout=HTTP_TIMEOUT_SECONDS) as response:
            final_url = urlsplit(response.geturl())
            if (
                final_url.scheme.lower() != "https"
                or (final_url.hostname or "").lower() not in GITHUB_NETWORK_HOSTS
            ):
                raise GitHubRepositoryUnavailableError("GitHub 下载地址不在允许范围内。")
            content_length = response.headers.get("Content-Length")
            if content_length and content_length.isdigit() and int(content_length) > max_bytes:
                raise GitHubRepositoryError("GitHub 仓库归档超过分析上限，请精简后重试。")
            chunks: list[bytes] = []
            received = 0
            while True:
                chunk = response.read(min(64 * 1024, max_bytes + 1 - received))
                if not chunk:
                    break
                received += len(chunk)
                if received > max_bytes:
                    raise GitHubRepositoryError("GitHub 仓库归档超过分析上限，请精简后重试。")
                chunks.append(chunk)
            return b"".join(chunks)
    except GitHubRepositoryError:
        raise
    except HTTPError as error:
        if error.code == 404:
            raise GitHubRepositoryNotFoundError("仓库不存在、已删除或不是公开仓库。") from error
        if error.code in {401, 403, 429}:
            raise GitHubRepositoryUnavailableError("GitHub 暂时拒绝读取该仓库，请稍后重试。") from error
        raise GitHubRepositoryUnavailableError("GitHub 仓库暂时无法访问，请稍后重试。") from error
    except (URLError, TimeoutError, OSError) as error:
        raise GitHubRepositoryUnavailableError("无法连接 GitHub，请检查网络后重试。") from error
