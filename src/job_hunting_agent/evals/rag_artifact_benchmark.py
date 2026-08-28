"""固定 GitHub 真实文件的项目采集、提取和 RAG 端到端评测。"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.error import HTTPError
from urllib.parse import quote, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

import sqlalchemy as sa

from job_hunting_agent.app import JobHuntingApp
from job_hunting_agent.config import (
    DEFAULT_ENV_PATH,
    load_database_settings,
    require_postgresql_database_url,
)
from job_hunting_agent.database_schema import accounts
from job_hunting_agent.evals.rag_benchmark import _eval_candidate_input
from job_hunting_agent.evals.rag_eval import (
    RAGEvalCase,
    RAGEvalReport,
    RAGEvalThresholds,
    evaluate_rag_cases,
    format_rag_eval_report,
)
from job_hunting_agent.file_scanning import LocalSafetyScanner
from job_hunting_agent.pgvector_rag import PgVectorKnowledgeBase
from job_hunting_agent.project_evidence import (
    MAX_FILE_BYTES_BY_KIND,
    ProjectManifestItem,
    normalize_manifest_path,
    project_file_kind,
)
from job_hunting_agent.rag import (
    LocalHashEmbeddings,
    rag_embedding_model_name,
)

MAX_ARTIFACT_COUNT = 40
MAX_ARTIFACT_DOWNLOAD_BYTES = 32 * 1024 * 1024
MAX_ARTIFACT_TOTAL_BYTES = 128 * 1024 * 1024
DEFAULT_DOWNLOAD_TIMEOUT_SECONDS = 30.0
_COMMIT_PATTERN = re.compile(r"^[0-9a-f]{40}$")
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_GITHUB_PART_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+$")

ArtifactFetcher = Callable[["PinnedGitHubArtifact"], bytes]


@dataclass(frozen=True)
class PinnedGitHubArtifact:
    """固定到具体提交和内容摘要的公开 GitHub 测试文件。"""

    id: str
    industry: str
    repository_url: str
    commit_sha: str
    source_path: str
    relative_path: str
    license_spdx: str
    size_bytes: int
    sha256: str
    media_type: str
    expected_terms: tuple[str, ...] = ()
    require_visual_analysis: bool = False

    def __post_init__(self) -> None:
        if not self.id.strip() or not self.industry.strip():
            raise ValueError("GitHub artifact id and industry cannot be empty.")
        if not self.license_spdx.strip():
            raise ValueError(f"GitHub artifact {self.id!r} must declare a license.")
        normalized_source = normalize_manifest_path(self.source_path)
        normalized_relative = normalize_manifest_path(self.relative_path)
        if normalized_source != self.source_path or normalized_relative != self.relative_path:
            raise ValueError(f"GitHub artifact {self.id!r} paths must be normalized.")
        if not _COMMIT_PATTERN.fullmatch(self.commit_sha):
            raise ValueError(f"GitHub artifact {self.id!r} must pin a 40-character commit.")
        if not _SHA256_PATTERN.fullmatch(self.sha256):
            raise ValueError(f"GitHub artifact {self.id!r} has an invalid SHA-256.")
        repository_parts = _github_repository_parts(self.repository_url)
        if repository_parts is None:
            raise ValueError(f"GitHub artifact {self.id!r} repository URL is invalid.")
        if self.size_bytes <= 0 or self.size_bytes > MAX_ARTIFACT_DOWNLOAD_BYTES:
            raise ValueError(f"GitHub artifact {self.id!r} exceeds the download size policy.")
        kind = project_file_kind(Path(self.relative_path))
        if kind == "unsupported":
            raise ValueError(f"GitHub artifact {self.id!r} uses an unsupported file type.")
        if self.size_bytes > MAX_FILE_BYTES_BY_KIND[kind]:
            raise ValueError(f"GitHub artifact {self.id!r} exceeds its parser size policy.")
        if not self.media_type.strip() or len(self.media_type) > 128:
            raise ValueError(f"GitHub artifact {self.id!r} media type is invalid.")
        if any(not term.strip() for term in self.expected_terms):
            raise ValueError(f"GitHub artifact {self.id!r} has an empty expected term.")

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object]) -> PinnedGitHubArtifact:
        expected_terms = payload.get("expected_terms") or []
        if not isinstance(expected_terms, list):
            raise ValueError("GitHub artifact expected_terms must be a list.")
        return cls(
            id=str(payload.get("id") or "").strip(),
            industry=str(payload.get("industry") or "").strip(),
            repository_url=str(payload.get("repository_url") or "").strip(),
            commit_sha=str(payload.get("commit_sha") or "").strip().lower(),
            source_path=str(payload.get("source_path") or "").strip(),
            relative_path=str(payload.get("relative_path") or "").strip(),
            license_spdx=str(payload.get("license_spdx") or "").strip(),
            size_bytes=int(payload.get("size_bytes") or 0),
            sha256=str(payload.get("sha256") or "").strip().lower(),
            media_type=str(payload.get("media_type") or "application/octet-stream").strip(),
            expected_terms=tuple(str(item) for item in expected_terms),
            require_visual_analysis=bool(payload.get("require_visual_analysis", False)),
        )

    @property
    def raw_url(self) -> str:
        owner, repository = _github_repository_parts(self.repository_url) or ("", "")
        encoded_path = "/".join(quote(part, safe="") for part in self.source_path.split("/"))
        return (
            f"https://raw.githubusercontent.com/{owner}/{repository}/"
            f"{self.commit_sha}/{encoded_path}"
        )


@dataclass(frozen=True)
class GitHubArtifactBenchmarkSuite:
    """跨行业真实文件、提取断言、检索问题和质量门槛。"""

    name: str
    description: str
    artifacts: tuple[PinnedGitHubArtifact, ...]
    cases: tuple[RAGEvalCase, ...]
    thresholds: RAGEvalThresholds = RAGEvalThresholds()
    version: str = "1"
    benchmark_role: str = "smoke"

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("GitHub artifact benchmark name cannot be empty.")
        if self.benchmark_role not in {"smoke", "release"}:
            raise ValueError("GitHub artifact benchmark role must be smoke or release.")
        if not self.artifacts or len(self.artifacts) > MAX_ARTIFACT_COUNT:
            raise ValueError("GitHub artifact benchmark artifact count is invalid.")
        if not self.cases:
            raise ValueError("GitHub artifact benchmark must include retrieval cases.")
        if sum(item.size_bytes for item in self.artifacts) > MAX_ARTIFACT_TOTAL_BYTES:
            raise ValueError("GitHub artifact benchmark exceeds its total download budget.")
        artifact_ids = [item.id for item in self.artifacts]
        paths = [item.relative_path for item in self.artifacts]
        case_ids = [item.id for item in self.cases]
        if len(set(artifact_ids)) != len(artifact_ids):
            raise ValueError("GitHub artifact ids must be unique.")
        if len(set(paths)) != len(paths):
            raise ValueError("GitHub artifact relative paths must be unique.")
        if len(set(case_ids)) != len(case_ids):
            raise ValueError("GitHub artifact case ids must be unique.")
        known_paths = set(paths)
        for case in self.cases:
            referenced = {
                ref.source_label
                for ref in (*case.expected, *case.forbidden)
                if ref.source_label is not None
            }
            unknown = referenced - known_paths
            if unknown:
                raise ValueError(
                    f"GitHub artifact case {case.id!r} references unknown paths: "
                    + ", ".join(sorted(unknown))
                )
        if self.benchmark_role == "release":
            self._validate_release_shape()

    def _validate_release_shape(self) -> None:
        """防止小型冒烟集被误标成可用于上线结论的正式集。"""

        if len(self.artifacts) < 30 or len(self.cases) < 25:
            raise ValueError("Release artifact benchmark requires 30 artifacts and 25 cases.")
        if len({item.industry for item in self.artifacts}) < 6:
            raise ValueError("Release artifact benchmark must cover at least six industries.")
        if any(case.top_n is None for case in self.cases):
            raise ValueError("Release artifact benchmark cases must declare top_n.")
        max_top_n = max(int(case.top_n or 0) for case in self.cases)
        if max_top_n / len(self.artifacts) > 0.2:
            raise ValueError("Release artifact benchmark top_n cannot exceed 20% of corpus.")
        forbidden_count = sum(bool(case.forbidden) for case in self.cases)
        if forbidden_count / len(self.cases) < 0.25:
            raise ValueError("Release artifact benchmark requires forbidden hard negatives.")
        holdout_count = sum(case.split == "holdout" for case in self.cases)
        if holdout_count / len(self.cases) < 0.2:
            raise ValueError("Release artifact benchmark requires a holdout split.")
        if self.thresholds.min_mean_recall_at_1 <= 0:
            raise ValueError("Release artifact benchmark must gate Recall@1.")


@dataclass(frozen=True)
class ArtifactExtractionResult:
    artifact_id: str
    industry: str
    relative_path: str
    file_kind: str
    extraction_method: str
    extracted_characters: int
    expected_terms: tuple[str, ...]
    missing_terms: tuple[str, ...]
    visual_analysis_status: str | None
    visual_error_type: str | None
    visual_artifact_count: int
    passed: bool


@dataclass(frozen=True)
class GitHubArtifactBenchmarkResult:
    suite_name: str
    suite_version: str
    benchmark_role: str
    embedding_model: str
    reranker_model: str | None
    visual_mode: str
    visual_item_count: int
    visual_indexed_count: int
    artifact_count: int
    max_top_n_corpus_ratio: float
    downloaded_bytes: int
    extraction_pass_count: int
    duration_seconds: float
    completed_at: str
    extraction_results: tuple[ArtifactExtractionResult, ...]
    report: RAGEvalReport

    @property
    def extraction_pass_rate(self) -> float:
        if self.artifact_count <= 0:
            return 0.0
        return self.extraction_pass_count / self.artifact_count

    @property
    def visual_index_passed(self) -> bool:
        if self.visual_mode == "disabled":
            return True
        return self.visual_item_count > 0 and self.visual_indexed_count == self.visual_item_count

    @property
    def all_passed(self) -> bool:
        return (
            self.extraction_pass_count == self.artifact_count
            and self.visual_index_passed
            and self.report.all_passed
        )

    def to_dict(self) -> dict[str, object]:
        payload = asdict(self)
        payload["all_passed"] = self.all_passed
        payload["extraction_pass_rate"] = self.extraction_pass_rate
        payload["visual_index_passed"] = self.visual_index_passed
        payload["report"] = self.report.to_dict()
        return payload


def load_github_artifact_benchmark_suite(
    path: str | Path,
) -> GitHubArtifactBenchmarkSuite:
    """读取并验证仓库内的固定来源清单。"""

    suite_path = Path(path)
    payload = json.loads(suite_path.read_text(encoding="utf-8"))
    if not isinstance(payload, Mapping):
        raise ValueError("GitHub artifact benchmark suite must be a JSON object.")
    raw_artifacts = list(payload.get("artifacts") or [])
    base_name = str(payload.get("base_artifacts_from") or "").strip()
    if base_name:
        if Path(base_name).name != base_name or Path(base_name).suffix.lower() != ".json":
            raise ValueError("GitHub artifact benchmark base suite must be a sibling JSON file.")
        base_path = suite_path.parent / base_name
        base_payload = json.loads(base_path.read_text(encoding="utf-8"))
        if not isinstance(base_payload, Mapping):
            raise ValueError("GitHub artifact benchmark base suite must be a JSON object.")
        base_artifacts = base_payload.get("artifacts")
        if not isinstance(base_artifacts, list):
            raise ValueError("GitHub artifact benchmark base artifacts must be a list.")
        raw_artifacts = [*base_artifacts, *raw_artifacts]
    raw_cases = payload.get("cases")
    if not isinstance(raw_artifacts, list) or not all(
        isinstance(item, Mapping) for item in raw_artifacts
    ):
        raise ValueError("GitHub artifact benchmark artifacts must be a list of objects.")
    if not isinstance(raw_cases, list) or not all(
        isinstance(item, Mapping) for item in raw_cases
    ):
        raise ValueError("GitHub artifact benchmark cases must be a list of objects.")
    raw_thresholds = payload.get("thresholds")
    if raw_thresholds is not None and not isinstance(raw_thresholds, Mapping):
        raise ValueError("GitHub artifact benchmark thresholds must be an object.")
    return GitHubArtifactBenchmarkSuite(
        name=str(payload.get("name") or "").strip(),
        description=str(payload.get("description") or "").strip(),
        version=str(payload.get("version") or "1").strip(),
        benchmark_role=str(payload.get("benchmark_role") or "smoke").strip(),
        artifacts=tuple(PinnedGitHubArtifact.from_mapping(item) for item in raw_artifacts),
        cases=tuple(RAGEvalCase.from_mapping(item) for item in raw_cases),
        thresholds=RAGEvalThresholds.from_mapping(raw_thresholds),
    )


def download_pinned_github_artifact(
    artifact: PinnedGitHubArtifact,
    *,
    timeout_seconds: float = DEFAULT_DOWNLOAD_TIMEOUT_SECONDS,
) -> bytes:
    """只从 raw.githubusercontent.com 下载并再次校验最终地址、大小和哈希。"""

    opener = build_opener(_RawGitHubRedirectHandler())
    request = Request(
        artifact.raw_url,
        headers={"User-Agent": "job-hunting-agent-rag-artifact-benchmark/1.0"},
    )
    with opener.open(request, timeout=timeout_seconds) as response:
        final = urlsplit(response.geturl())
        if final.scheme != "https" or final.hostname != "raw.githubusercontent.com":
            raise ValueError("GitHub artifact download left the approved host.")
        declared_length = response.headers.get("Content-Length")
        if declared_length and int(declared_length) != artifact.size_bytes:
            raise ValueError(f"GitHub artifact {artifact.id!r} content length changed.")
        content = response.read(artifact.size_bytes + 1)
    return verify_pinned_github_artifact(artifact, content)


def verify_pinned_github_artifact(
    artifact: PinnedGitHubArtifact,
    content: bytes,
) -> bytes:
    """校验下载结果，避免上游文件漂移或截断进入模型和知识库。"""

    if len(content) != artifact.size_bytes:
        raise ValueError(f"GitHub artifact {artifact.id!r} size does not match the catalog.")
    digest = hashlib.sha256(content).hexdigest()
    if digest != artifact.sha256:
        raise ValueError(f"GitHub artifact {artifact.id!r} SHA-256 does not match the catalog.")
    return content


def run_github_artifact_benchmark(
    suite: GitHubArtifactBenchmarkSuite,
    *,
    database_url: str,
    env_path: str | Path = DEFAULT_ENV_PATH,
    embedding_mode: str = "configured",
    use_reranker: bool = True,
    visual_mode: str = "configured",
    fetcher: ArtifactFetcher = download_pinned_github_artifact,
) -> GitHubArtifactBenchmarkResult:
    """经真实项目采集入口处理文件，写入 pgvector 后评测并级联清理。"""

    if embedding_mode not in {"configured", "local_hash"}:
        raise ValueError("embedding_mode must be configured or local_hash.")
    if visual_mode not in {"configured", "disabled"}:
        raise ValueError("visual_mode must be configured or disabled.")
    if visual_mode == "configured" and embedding_mode != "configured":
        raise ValueError("Configured visual evaluation requires configured embeddings.")
    started = time.monotonic()
    downloaded = {
        artifact.id: verify_pinned_github_artifact(artifact, fetcher(artifact))
        for artifact in suite.artifacts
    }
    run_id = uuid.uuid4().hex
    account_ids: list[int] = []
    app: JobHuntingApp | None = None
    with TemporaryDirectory(prefix="job-agent-rag-artifact-") as object_directory:
        try:
            app = JobHuntingApp(
                env_path=env_path,
                resume_dir=object_directory,
                semantic_matching=False,
                database_url=database_url,
                task_queue=_DisabledTaskQueue(),
                file_scanner=LocalSafetyScanner(),
            )
            app.store.initialize()
            account = app.store.create_account(
                email=f"rag-artifact-eval-{run_id}@example.invalid",
                password_hash="rag-artifact-eval-unusable-password-hash",
                display_name="RAG 真实文件临时评测账号",
            )
            account_ids.append(account.id)
            app.store.create_simulated_recharge_order(
                account.id,
                1000,
                idempotency_key=f"rag-artifact-eval:{run_id}",
                description="临时评测账号模型调用额度",
            )
            candidate_id = app.save_candidate_profile(
                _eval_candidate_input(f"RAG 真实文件评测 {run_id[:8]}"),
                account_id=account.id,
            )
            if visual_mode == "disabled":
                app.project_visual_analyzer = None
            elif any(item.require_visual_analysis for item in suite.artifacts):
                if app.project_visual_analyzer is None:
                    raise ValueError("Artifact suite requires enabled project visual analysis.")

            manifest = [
                ProjectManifestItem(
                    relative_path=item.relative_path,
                    file_size=item.size_bytes,
                    sha256=item.sha256,
                    media_type=item.media_type,
                )
                for item in suite.artifacts
            ]
            collection, files = app.create_local_project_collection(
                candidate_id,
                f"GitHub 跨行业评测 {run_id[:8]}",
                manifest,
                account_id=account.id,
            )
            file_by_path = {item.relative_path: item for item in files}
            extraction_results: list[ArtifactExtractionResult] = []
            long_text_ids: list[int] = []
            for artifact in suite.artifacts:
                planned = file_by_path[artifact.relative_path]
                if planned.selection_status != "selected":
                    raise ValueError(
                        f"GitHub artifact {artifact.id!r} was rejected by the project plan: "
                        f"{planned.selection_reason}"
                    )
                processed = app.process_local_project_collection_file(
                    collection.id,
                    planned.id,
                    downloaded[artifact.id],
                    account_id=account.id,
                )
                if processed.long_text_id is None:
                    raise ValueError(f"GitHub artifact {artifact.id!r} produced no long text.")
                long_text_ids.append(int(processed.long_text_id))
                long_text = app.store.get_long_texts_by_ids(
                    [int(processed.long_text_id)],
                    account_id=account.id,
                )[0]
                missing_terms = tuple(
                    term for term in artifact.expected_terms if term.casefold() not in long_text.text.casefold()
                )
                visual_status = str(
                    processed.metadata.get("visual_analysis_status") or ""
                ).strip() or None
                visual_error_type = str(
                    processed.metadata.get("visual_error_type") or ""
                ).strip() or None
                visual_items = app.store.list_visual_knowledge_items(
                    account_id=account.id,
                    project_collection_file_ids=[processed.id],
                )
                visual_passed = (
                    visual_mode == "disabled"
                    or not artifact.require_visual_analysis
                    or visual_status in {"succeeded", "partial", "no_evidence"}
                )
                extraction_results.append(
                    ArtifactExtractionResult(
                        artifact_id=artifact.id,
                        industry=artifact.industry,
                        relative_path=artifact.relative_path,
                        file_kind=processed.file_kind,
                        extraction_method=processed.extraction_method or "unknown",
                        extracted_characters=len(long_text.text),
                        expected_terms=artifact.expected_terms,
                        missing_terms=missing_terms,
                        visual_analysis_status=visual_status,
                        visual_error_type=visual_error_type,
                        visual_artifact_count=len(visual_items),
                        passed=not missing_terms and visual_passed,
                    )
                )

            visual_items = app.store.list_visual_knowledge_items(
                account_id=account.id,
                project_collection_file_ids=[item.id for item in files],
            )
            visual_item_ids = [item.id for item in visual_items]
            visual_indexed_count = 0
            if embedding_mode == "local_hash":
                embeddings = LocalHashEmbeddings(dimensions=384)
                reranker = None
                knowledge_base = PgVectorKnowledgeBase(
                    app.store.engine,
                    embeddings=embeddings,
                    reranker=reranker,
                )
                knowledge_base.index_long_texts(
                    app.store.get_long_texts_by_ids(
                        long_text_ids,
                        account_id=account.id,
                    ),
                    account_id=account.id,
                )
                def search_backend(case: RAGEvalCase, top_n: int) -> Sequence[object]:
                    return knowledge_base.search(
                        case.query,
                        top_n=top_n,
                        entity_types=list(case.entity_types) or None,
                        account_id=account.id,
                    )
            else:
                app.index_rag_long_texts(
                    long_text_ids,
                    account_id=account.id,
                    candidate_id=candidate_id,
                    root_request_id=f"rag-artifact-eval:{run_id}",
                )
                if visual_mode == "configured":
                    if not visual_item_ids:
                        raise ValueError(
                            "Configured visual evaluation produced no visual knowledge items."
                        )
                    visual_stats = app.index_visual_knowledge_items(
                        visual_item_ids,
                        account_id=account.id,
                        candidate_id=candidate_id,
                        root_request_id=f"rag-artifact-visual-eval:{run_id}",
                    )
                    visual_indexed_count = visual_stats.document_count
                embedding_context = app.model_gateway.new_call_context(
                    "embedding_query",
                    account_id=account.id,
                    candidate_id=candidate_id,
                    root_request_id=f"rag-artifact-report:{run_id}",
                )
                embeddings = app.model_gateway.embeddings(embedding_context)
                if use_reranker:
                    rerank_context = app.model_gateway.new_call_context(
                        "rerank_query",
                        account_id=account.id,
                        candidate_id=candidate_id,
                        root_request_id=f"rag-artifact-report:{run_id}",
                    )
                    reranker = app.model_gateway.reranker(rerank_context)
                else:
                    reranker = None
                if use_reranker:
                    def search_backend(case: RAGEvalCase, top_n: int) -> Sequence[object]:
                        return app.search_rag(
                            case.query,
                            top_n=top_n,
                            entity_types=list(case.entity_types) or None,
                            account_id=account.id,
                            candidate_id=candidate_id,
                            root_request_id=f"rag-artifact-search:{run_id}:{case.id}",
                        )
                else:
                    knowledge_base = PgVectorKnowledgeBase(
                        app.store.engine,
                        embeddings=embeddings,
                        reranker=None,
                    )

                    def search_backend(case: RAGEvalCase, top_n: int) -> Sequence[object]:
                        results = knowledge_base.search(
                            case.query,
                            top_n=top_n,
                            entity_types=list(case.entity_types) or None,
                            account_id=account.id,
                            candidate_id=candidate_id,
                        )
                        return app._reinspect_visual_search_results(
                            case.query,
                            results,
                            account_id=account.id,
                            candidate_id=candidate_id,
                        )
            report = evaluate_rag_cases(
                suite.cases,
                search_backend,
                thresholds=suite.thresholds,
            )
            return GitHubArtifactBenchmarkResult(
                suite_name=suite.name,
                suite_version=suite.version,
                benchmark_role=suite.benchmark_role,
                embedding_model=rag_embedding_model_name(embeddings),
                reranker_model=(
                    str(getattr(reranker, "model", "")).strip() or None
                    if reranker is not None
                    else None
                ),
                visual_mode=visual_mode,
                visual_item_count=len(visual_item_ids),
                visual_indexed_count=visual_indexed_count,
                artifact_count=len(suite.artifacts),
                max_top_n_corpus_ratio=round(
                    max(int(case.top_n or 5) for case in suite.cases)
                    / len(suite.artifacts),
                    4,
                ),
                downloaded_bytes=sum(len(item) for item in downloaded.values()),
                extraction_pass_count=sum(item.passed for item in extraction_results),
                duration_seconds=round(time.monotonic() - started, 3),
                completed_at=datetime.now(UTC).isoformat(timespec="seconds"),
                extraction_results=tuple(extraction_results),
                report=report,
            )
        finally:
            if app is not None:
                if account_ids:
                    with app.store.engine.begin() as connection:
                        connection.execute(
                            sa.delete(accounts).where(accounts.c.id.in_(account_ids))
                        )
                app.store.close()


def format_github_artifact_benchmark_result(
    result: GitHubArtifactBenchmarkResult,
) -> str:
    """输出适合开发机和 CI 日志的无正文摘要。"""

    extraction_lines = [
        (
            f"artifact={item.artifact_id} industry={item.industry} "
            f"method={item.extraction_method} chars={item.extracted_characters} "
            f"visual={item.visual_analysis_status or 'n/a'} "
            f"visual_error={item.visual_error_type or 'n/a'} "
            f"status={'PASS' if item.passed else 'FAIL'}"
        )
        for item in result.extraction_results
    ]
    header = [
        f"GitHub artifact RAG benchmark: {result.suite_name} v{result.suite_version}",
        f"benchmark_role={result.benchmark_role}",
        f"embedding={result.embedding_model}",
        f"reranker={result.reranker_model or 'disabled'}",
        (
            f"visual_mode={result.visual_mode} "
            f"visual_indexed={result.visual_indexed_count}/{result.visual_item_count} "
            f"visual_index_passed={result.visual_index_passed}"
        ),
        f"artifacts={result.artifact_count} downloaded_bytes={result.downloaded_bytes}",
        f"max_top_n_corpus_ratio={result.max_top_n_corpus_ratio:.3f}",
        (
            f"extraction_passed={result.extraction_pass_count}/{result.artifact_count} "
            f"extraction_pass_rate={result.extraction_pass_rate:.3f}"
        ),
        f"duration_seconds={result.duration_seconds:.3f}",
    ]
    return "\n".join(header + extraction_lines + [format_rag_eval_report(result.report)])


def write_github_artifact_benchmark_report(
    result: GitHubArtifactBenchmarkResult,
    output_path: str | Path,
) -> Path:
    target = Path(output_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(result.to_dict(), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return target


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run pinned GitHub files through project ingestion and RAG retrieval."
    )
    parser.add_argument("--suite", required=True, help="Artifact suite JSON path.")
    parser.add_argument("--env-file", default=".env", help="Model configuration file.")
    parser.add_argument("--database-url", help="PostgreSQL URL override.")
    parser.add_argument(
        "--embedding-mode",
        choices=("configured", "local_hash"),
        default="configured",
    )
    parser.add_argument(
        "--visual-mode",
        choices=("configured", "disabled"),
        default="configured",
    )
    parser.add_argument("--no-rerank", action="store_true")
    parser.add_argument("--output", help="Optional JSON report path.")
    args = parser.parse_args(argv)

    database_url = args.database_url or require_postgresql_database_url(
        load_database_settings(args.env_file)
    )
    result = run_github_artifact_benchmark(
        load_github_artifact_benchmark_suite(args.suite),
        database_url=database_url,
        env_path=args.env_file,
        embedding_mode=args.embedding_mode,
        use_reranker=not args.no_rerank,
        visual_mode=args.visual_mode,
    )
    print(format_github_artifact_benchmark_result(result))
    if args.output:
        target = write_github_artifact_benchmark_report(result, args.output)
        print(f"report={target}")
    return 0 if result.all_passed else 1


def _github_repository_parts(repository_url: str) -> tuple[str, str] | None:
    parsed = urlsplit(repository_url)
    if parsed.scheme != "https" or parsed.hostname != "github.com":
        return None
    if parsed.query or parsed.fragment or parsed.username or parsed.password or parsed.port:
        return None
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) != 2 or not all(_GITHUB_PART_PATTERN.fullmatch(part) for part in parts):
        return None
    return parts[0], parts[1].removesuffix(".git")


class _RawGitHubRedirectHandler(HTTPRedirectHandler):
    """禁止 raw 下载在重定向时跳到未批准主机。"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        target = urlsplit(newurl)
        if target.scheme != "https" or target.hostname != "raw.githubusercontent.com":
            raise HTTPError(newurl, code, "redirect target is not approved", headers, fp)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class _DisabledTaskQueue:
    """评测不投递后台消息，但显式阻止 App 从环境自动构造 Celery。"""

    def health_check(self) -> None:
        return None

    def enqueue(self, task_key: str) -> None:
        raise RuntimeError(f"Artifact benchmark must not enqueue background task {task_key!r}.")


if __name__ == "__main__":
    raise SystemExit(main())
