"""隔离 RAG 黄金集基准：临时建库、真实 pgvector 检索、报告和自动清理。"""

from __future__ import annotations

import argparse
import json
import time
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

import sqlalchemy as sa

from job_hunting_agent.config import (
    DEFAULT_ENV_PATH,
    load_database_settings,
    require_postgresql_database_url,
)
from job_hunting_agent.database_schema import accounts
from job_hunting_agent.evals.rag_eval import (
    RAGEvalCase,
    RAGEvalReport,
    RAGEvalThresholds,
    evaluate_rag_cases,
    format_rag_eval_report,
)
from job_hunting_agent.models import CandidateProfileInput
from job_hunting_agent.pgvector_rag import PgVectorKnowledgeBase
from job_hunting_agent.rag import (
    LocalHashEmbeddings,
    build_rag_embeddings,
    build_reranker,
    rag_embedding_model_name,
)
from job_hunting_agent.sqlalchemy_store import SQLAlchemyStore

SUPPORTED_ACCOUNT_SCOPES = frozenset({"primary", "foreign"})


@dataclass(frozen=True)
class RAGBenchmarkDocument:
    """一份固定评测材料；foreign 用于验证账号隔离。"""

    id: str
    source_label: str
    entity_type: str
    text: str
    account_scope: str = "primary"

    def __post_init__(self) -> None:
        for field_name in ("id", "source_label", "entity_type", "text"):
            if not str(getattr(self, field_name)).strip():
                raise ValueError(f"RAG benchmark document {field_name} cannot be empty.")
        if self.account_scope not in SUPPORTED_ACCOUNT_SCOPES:
            raise ValueError(
                "RAG benchmark document account_scope must be primary or foreign."
            )

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object]) -> RAGBenchmarkDocument:
        return cls(
            id=str(payload.get("id") or "").strip(),
            source_label=str(payload.get("source_label") or "").strip(),
            entity_type=str(payload.get("entity_type") or "").strip(),
            text=str(payload.get("text") or "").strip(),
            account_scope=str(payload.get("account_scope") or "primary").strip(),
        )


@dataclass(frozen=True)
class RAGBenchmarkSuite:
    """可版本化的固定语料、查询预期和上线质量门槛。"""

    name: str
    description: str
    documents: tuple[RAGBenchmarkDocument, ...]
    cases: tuple[RAGEvalCase, ...]
    thresholds: RAGEvalThresholds = RAGEvalThresholds()
    version: str = "1"

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("RAG benchmark suite name cannot be empty.")
        if not self.documents:
            raise ValueError("RAG benchmark suite must include documents.")
        if not self.cases:
            raise ValueError("RAG benchmark suite must include cases.")
        document_ids = [document.id for document in self.documents]
        source_labels = [document.source_label for document in self.documents]
        case_ids = [case.id for case in self.cases]
        if len(set(document_ids)) != len(document_ids):
            raise ValueError("RAG benchmark document id values must be unique.")
        if len(set(source_labels)) != len(source_labels):
            raise ValueError("RAG benchmark document source_label values must be unique.")
        if len(set(case_ids)) != len(case_ids):
            raise ValueError("RAG benchmark case id values must be unique.")
        known_labels = set(source_labels)
        primary_labels = {
            document.source_label
            for document in self.documents
            if document.account_scope == "primary"
        }
        for case in self.cases:
            expected_labels = {
                ref.source_label for ref in case.expected if ref.source_label is not None
            }
            forbidden_labels = {
                ref.source_label for ref in case.forbidden if ref.source_label is not None
            }
            unknown = (expected_labels | forbidden_labels) - known_labels
            if unknown:
                raise ValueError(
                    f"RAG benchmark case {case.id!r} references unknown source labels: "
                    + ", ".join(sorted(unknown))
                )
            foreign_expected = expected_labels - primary_labels
            if foreign_expected:
                raise ValueError(
                    f"RAG benchmark case {case.id!r} expects foreign-account evidence."
                )


@dataclass(frozen=True)
class RAGBenchmarkResult:
    """一次隔离基准结果，不包含临时账号 ID 或语料正文。"""

    suite_name: str
    suite_version: str
    embedding_model: str
    reranker_model: str | None
    document_count: int
    primary_document_count: int
    foreign_document_count: int
    duration_seconds: float
    completed_at: str
    report: RAGEvalReport

    def to_dict(self) -> dict[str, object]:
        payload = asdict(self)
        payload["report"] = self.report.to_dict()
        return payload


def load_rag_benchmark_suite(path: str | Path) -> RAGBenchmarkSuite:
    """读取并验证仓库中的 JSON 黄金集。"""

    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, Mapping):
        raise ValueError("RAG benchmark suite must be a JSON object.")
    raw_documents = payload.get("documents")
    raw_cases = payload.get("cases")
    if not isinstance(raw_documents, list) or not all(
        isinstance(item, Mapping) for item in raw_documents
    ):
        raise ValueError("RAG benchmark documents must be a list of objects.")
    if not isinstance(raw_cases, list) or not all(
        isinstance(item, Mapping) for item in raw_cases
    ):
        raise ValueError("RAG benchmark cases must be a list of objects.")
    raw_thresholds = payload.get("thresholds")
    if raw_thresholds is not None and not isinstance(raw_thresholds, Mapping):
        raise ValueError("RAG benchmark thresholds must be an object.")
    return RAGBenchmarkSuite(
        name=str(payload.get("name") or "").strip(),
        description=str(payload.get("description") or "").strip(),
        version=str(payload.get("version") or "1").strip(),
        documents=tuple(
            RAGBenchmarkDocument.from_mapping(item) for item in raw_documents
        ),
        cases=tuple(RAGEvalCase.from_mapping(item) for item in raw_cases),
        thresholds=RAGEvalThresholds.from_mapping(raw_thresholds),
    )


def run_rag_benchmark(
    suite: RAGBenchmarkSuite,
    *,
    database_url: str,
    env_path: str | Path = DEFAULT_ENV_PATH,
    embedding_mode: str = "configured",
    use_reranker: bool = True,
) -> RAGBenchmarkResult:
    """在两个临时账号内执行真实 pgvector 基准，并在 finally 中级联清理。"""

    if embedding_mode not in {"configured", "local_hash"}:
        raise ValueError("embedding_mode must be configured or local_hash.")
    started = time.monotonic()
    run_id = uuid.uuid4().hex
    store = SQLAlchemyStore(database_url)
    account_ids: list[int] = []
    try:
        store.initialize()
        primary_account = store.create_account(
            email=f"rag-eval-primary-{run_id}@example.invalid",
            password_hash="rag-eval-unusable-password-hash",
            display_name="RAG 评测主账号",
        )
        account_ids.append(primary_account.id)
        foreign_account = store.create_account(
            email=f"rag-eval-foreign-{run_id}@example.invalid",
            password_hash="rag-eval-unusable-password-hash",
            display_name="RAG 评测隔离账号",
        )
        account_ids.append(foreign_account.id)
        primary_candidate_id = store.save_candidate_profile(
            _eval_candidate_input(f"RAG 评测候选人 {run_id[:8]}"),
            account_id=primary_account.id,
        )
        foreign_candidate_id = store.save_candidate_profile(
            _eval_candidate_input(f"RAG 隔离候选人 {run_id[8:16]}"),
            account_id=foreign_account.id,
        )
        long_text_ids: dict[str, list[int]] = {"primary": [], "foreign": []}
        for document in suite.documents:
            if document.account_scope == "primary":
                account_id = primary_account.id
                candidate_id = primary_candidate_id
            else:
                account_id = foreign_account.id
                candidate_id = foreign_candidate_id
            long_text_ids[document.account_scope].append(
                store.add_long_text(
                    document.entity_type,
                    candidate_id,
                    document.source_label,
                    document.text,
                    account_id=account_id,
                    candidate_id=candidate_id,
                )
            )

        if embedding_mode == "local_hash":
            embeddings = LocalHashEmbeddings(dimensions=384)
            reranker = None
        else:
            embeddings = build_rag_embeddings(env_path)
            reranker = build_reranker(env_path) if use_reranker else None
        knowledge_base = PgVectorKnowledgeBase(
            store.engine,
            embeddings=embeddings,
            reranker=reranker,
        )
        knowledge_base.index_long_texts(
            store.get_long_texts_by_ids(
                long_text_ids["primary"], account_id=primary_account.id
            ),
            account_id=primary_account.id,
        )
        knowledge_base.index_long_texts(
            store.get_long_texts_by_ids(
                long_text_ids["foreign"], account_id=foreign_account.id
            ),
            account_id=foreign_account.id,
        )
        report = evaluate_rag_cases(
            suite.cases,
            lambda case, limit: knowledge_base.search(
                case.query,
                top_k=limit,
                entity_types=list(case.entity_types) or None,
                account_id=primary_account.id,
            ),
            thresholds=suite.thresholds,
        )
        return RAGBenchmarkResult(
            suite_name=suite.name,
            suite_version=suite.version,
            embedding_model=rag_embedding_model_name(embeddings),
            reranker_model=(
                str(getattr(reranker, "model", "")).strip() or None
                if reranker is not None
                else None
            ),
            document_count=len(suite.documents),
            primary_document_count=sum(
                document.account_scope == "primary" for document in suite.documents
            ),
            foreign_document_count=sum(
                document.account_scope == "foreign" for document in suite.documents
            ),
            duration_seconds=round(time.monotonic() - started, 3),
            completed_at=datetime.now(UTC).isoformat(timespec="seconds"),
            report=report,
        )
    finally:
        if account_ids:
            with store.engine.begin() as connection:
                connection.execute(
                    sa.delete(accounts).where(accounts.c.id.in_(account_ids))
                )
        store.close()


def format_rag_benchmark_result(result: RAGBenchmarkResult) -> str:
    """输出适合终端和 CI 日志的低敏摘要。"""

    header = [
        f"RAG benchmark: {result.suite_name} v{result.suite_version}",
        f"embedding={result.embedding_model}",
        f"reranker={result.reranker_model or 'disabled'}",
        (
            f"documents={result.document_count} "
            f"(primary={result.primary_document_count}, "
            f"foreign={result.foreign_document_count})"
        ),
        f"duration_seconds={result.duration_seconds:.3f}",
    ]
    return "\n".join(header + [format_rag_eval_report(result.report)])


def write_rag_benchmark_report(
    result: RAGBenchmarkResult,
    output_path: str | Path,
) -> Path:
    """保存 JSON 报告，正文只包含查询与命中片段，不含密钥和连接串。"""

    target = Path(output_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(result.to_dict(), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return target


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run an isolated PostgreSQL/pgvector RAG golden benchmark."
    )
    parser.add_argument("--suite", required=True, help="Benchmark suite JSON path.")
    parser.add_argument("--env-file", default=".env", help="Model configuration file.")
    parser.add_argument("--database-url", help="PostgreSQL URL override.")
    parser.add_argument(
        "--embedding-mode",
        choices=("configured", "local_hash"),
        default="configured",
    )
    parser.add_argument("--no-rerank", action="store_true")
    parser.add_argument("--output", help="Optional JSON report path.")
    args = parser.parse_args(argv)

    database_url = args.database_url or require_postgresql_database_url(
        load_database_settings(args.env_file)
    )
    result = run_rag_benchmark(
        load_rag_benchmark_suite(args.suite),
        database_url=database_url,
        env_path=args.env_file,
        embedding_mode=args.embedding_mode,
        use_reranker=not args.no_rerank,
    )
    print(format_rag_benchmark_result(result))
    if args.output:
        target = write_rag_benchmark_report(result, args.output)
        print(f"report={target}")
    return 0 if result.report.all_passed else 1


def _eval_candidate_input(name: str) -> CandidateProfileInput:
    return CandidateProfileInput(
        name=name,
        status="评测临时数据",
        education="本科",
        experience_years=3.0,
        skills={"RAG evaluation": "fixture"},
        preferred_cities=["杭州"],
        salary_floor_k=10,
        expected_salary_k=15,
        target_directions=["跨行业项目评测"],
    )


if __name__ == "__main__":
    raise SystemExit(main())
