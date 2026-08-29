"""Isolated pgvector scale, ANN-plan, and concurrent retrieval benchmark."""

from __future__ import annotations

import argparse
import json
import math
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import sqlalchemy as sa
from pgvector.sqlalchemy import HALFVEC, Vector
from sqlalchemy.engine import Connection, Engine

from job_hunting_agent.config import DEFAULT_RAG_RETRIEVAL_TOP_K
from job_hunting_agent.pgvector_rag import (
    PGVECTOR_HNSW_EF_SEARCH,
    PGVECTOR_HNSW_OVERSAMPLING,
    PGVECTOR_MAX_VECTOR_HNSW_DIMENSIONS,
)

MIN_SCALE_CHUNKS = 100
MAX_SCALE_CHUNKS = 100_000
MIN_SCALE_DIMENSIONS = 8
MAX_SCALE_DIMENSIONS = 4_000
MAX_SYNTHETIC_VARIANT_COUNT = 512
MAX_ANN_OVERSAMPLING = 200


@dataclass(frozen=True)
class RAGScaleBenchmarkConfig:
    chunk_count: int = 10_000
    dimensions: int = 2_560
    tenant_count: int = 4
    cluster_count: int = 64
    query_count: int = 40
    top_k: int = DEFAULT_RAG_RETRIEVAL_TOP_K
    ann_oversampling: int = PGVECTOR_HNSW_OVERSAMPLING
    concurrency_levels: tuple[int, ...] = (1, 5, 10, 20)
    hnsw_m: int = 32
    hnsw_ef_construction: int = 128
    hnsw_ef_search_candidates: tuple[int, ...] = (
        PGVECTOR_HNSW_EF_SEARCH,
        800,
        1_000,
    )
    minimum_neighbor_recall: float = 0.65
    minimum_semantic_precision: float = 0.95
    minimum_semantic_coverage: float = 1.0
    maximum_p95_ms: float | None = None
    enforce_speedup: bool = True
    force_ann_index: bool = False

    def __post_init__(self) -> None:
        if not MIN_SCALE_CHUNKS <= self.chunk_count <= MAX_SCALE_CHUNKS:
            raise ValueError(
                f"Scale chunk count must be between {MIN_SCALE_CHUNKS} "
                f"and {MAX_SCALE_CHUNKS}."
            )
        if not MIN_SCALE_DIMENSIONS <= self.dimensions <= MAX_SCALE_DIMENSIONS:
            raise ValueError(
                f"Scale dimensions must be between {MIN_SCALE_DIMENSIONS} "
                f"and {MAX_SCALE_DIMENSIONS}."
            )
        if not 1 <= self.tenant_count <= 100:
            raise ValueError("Scale tenant count must be between 1 and 100.")
        if not 2 <= self.cluster_count <= 256:
            raise ValueError("Scale cluster count must be between 2 and 256.")
        if not 1 <= self.query_count <= 1_000:
            raise ValueError("Scale query count must be between 1 and 1000.")
        if not 1 <= self.top_k <= 100:
            raise ValueError("Scale Top-K must be between 1 and 100.")
        if not 1 <= self.ann_oversampling <= MAX_ANN_OVERSAMPLING:
            raise ValueError(
                f"ANN oversampling must be between 1 and {MAX_ANN_OVERSAMPLING}."
            )
        if not self.concurrency_levels or any(
            level < 1 or level > 64 for level in self.concurrency_levels
        ):
            raise ValueError("Scale concurrency levels must be between 1 and 64.")
        minimum_rows = self.tenant_count * self.cluster_count * self.top_k
        if self.chunk_count < minimum_rows:
            raise ValueError(
                "Scale chunk count must provide at least Top-K rows for every "
                f"tenant/cluster pair ({minimum_rows} required)."
            )
        if not 2 <= self.hnsw_m <= 100:
            raise ValueError("HNSW m must be between 2 and 100.")
        if not 4 <= self.hnsw_ef_construction <= 1_000:
            raise ValueError("HNSW ef_construction must be between 4 and 1000.")
        if self.hnsw_ef_construction < self.hnsw_m * 2:
            raise ValueError("HNSW ef_construction must be at least twice m.")
        if not self.hnsw_ef_search_candidates or any(
            value < 1 or value > 1_000
            for value in self.hnsw_ef_search_candidates
        ):
            raise ValueError("HNSW ef_search candidates must be between 1 and 1000.")
        if not 0.0 <= self.minimum_neighbor_recall <= 1.0:
            raise ValueError("Minimum neighbor recall must be between 0 and 1.")
        if not 0.0 <= self.minimum_semantic_precision <= 1.0:
            raise ValueError("Minimum semantic precision must be between 0 and 1.")
        if not 0.0 <= self.minimum_semantic_coverage <= 1.0:
            raise ValueError("Minimum semantic coverage must be between 0 and 1.")
        if self.maximum_p95_ms is not None and self.maximum_p95_ms <= 0:
            raise ValueError("Maximum P95 latency must be positive when configured.")


@dataclass(frozen=True)
class LatencySummary:
    attempted: int
    completed: int
    errors: int
    isolation_violations: int
    result_count_violations: int
    mean_ms: float
    p50_ms: float
    p95_ms: float
    p99_ms: float
    maximum_ms: float
    throughput_qps: float


@dataclass(frozen=True)
class QuerySpec:
    account_id: int
    cluster_id: int
    embedding: list[float]


@dataclass(frozen=True)
class QueryOutcome:
    latency_ms: float
    rows: tuple[dict[str, Any], ...]
    error_type: str | None = None


@dataclass(frozen=True)
class ANNSearchTrial:
    ef_search: int
    concurrent: dict[int, LatencySummary]
    plan: dict[str, Any]
    index_used: bool
    neighbor_recall: float
    semantic_precision: float
    semantic_coverage: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "ef_search": self.ef_search,
            "concurrent": {
                str(level): asdict(summary)
                for level, summary in self.concurrent.items()
            },
            "plan": self.plan,
            "index_used": self.index_used,
            "neighbor_recall": self.neighbor_recall,
            "semantic_precision": self.semantic_precision,
            "semantic_coverage": self.semantic_coverage,
        }


@dataclass(frozen=True)
class RAGScaleBenchmarkResult:
    config: RAGScaleBenchmarkConfig
    pgvector_version: str
    index_kind: str
    index_name: str
    generation_ms: float
    index_build_ms: float
    table_size_bytes: int
    index_size_bytes: int
    exact: LatencySummary
    ann_ground_truth: LatencySummary
    ef_search_trials: dict[int, ANNSearchTrial]
    recommended_ef_search: int | None
    concurrent_ann: dict[int, LatencySummary]
    ann_plan: dict[str, Any]
    ann_index_used: bool
    neighbor_recall: float
    semantic_precision: float
    semantic_coverage: float
    completed_at: str
    duration_seconds: float

    @property
    def failures(self) -> tuple[str, ...]:
        failures: list[str] = []
        if self.recommended_ef_search is None:
            failures.append("no ef_search candidate passed ANN quality and performance gates")
        if self.exact.errors:
            failures.append(f"exact baseline had {self.exact.errors} errors")
        if self.exact.isolation_violations:
            failures.append(
                f"exact baseline had {self.exact.isolation_violations} "
                "tenant isolation violations"
            )
        if self.exact.result_count_violations:
            failures.append(
                f"exact baseline had {self.exact.result_count_violations} "
                "incomplete Top-K results"
            )
        if self.ann_ground_truth.errors:
            failures.append(
                f"halfvec exact ground truth had {self.ann_ground_truth.errors} errors"
            )
        if not self.ann_index_used:
            failures.append("planner did not use the expected HNSW index")
        if self.neighbor_recall < self.config.minimum_neighbor_recall:
            failures.append(
                f"neighbor recall {self.neighbor_recall:.3f} < "
                f"{self.config.minimum_neighbor_recall:.3f}"
            )
        if self.semantic_precision < self.config.minimum_semantic_precision:
            failures.append(
                f"semantic precision {self.semantic_precision:.3f} < "
                f"{self.config.minimum_semantic_precision:.3f}"
            )
        if self.semantic_coverage < self.config.minimum_semantic_coverage:
            failures.append(
                f"semantic coverage {self.semantic_coverage:.3f} < "
                f"{self.config.minimum_semantic_coverage:.3f}"
            )
        for concurrency, summary in self.concurrent_ann.items():
            if summary.errors:
                failures.append(f"concurrency {concurrency} had {summary.errors} errors")
            if summary.isolation_violations:
                failures.append(
                    f"concurrency {concurrency} had "
                    f"{summary.isolation_violations} tenant isolation violations"
                )
            if summary.result_count_violations:
                failures.append(
                    f"concurrency {concurrency} had "
                    f"{summary.result_count_violations} incomplete Top-K results"
                )
        single = self.concurrent_ann[min(self.concurrent_ann)]
        if self.config.enforce_speedup:
            ceiling = self.ann_ground_truth.p95_ms * 1.25 + 2.0
            if single.p95_ms > ceiling:
                failures.append(
                    f"ANN P95 {single.p95_ms:.1f} ms > exact ceiling {ceiling:.1f} ms"
                )
        if (
            self.config.maximum_p95_ms is not None
            and single.p95_ms > self.config.maximum_p95_ms
        ):
            failures.append(
                f"ANN P95 {single.p95_ms:.1f} ms > configured maximum "
                f"{self.config.maximum_p95_ms:.1f} ms"
            )
        return tuple(failures)

    @property
    def passed(self) -> bool:
        return not self.failures

    def to_dict(self) -> dict[str, Any]:
        return {
            "config": {
                **asdict(self.config),
                "concurrency_levels": list(self.config.concurrency_levels),
            },
            "pgvector_version": self.pgvector_version,
            "index_kind": self.index_kind,
            "index_name": self.index_name,
            "generation_ms": self.generation_ms,
            "index_build_ms": self.index_build_ms,
            "table_size_bytes": self.table_size_bytes,
            "index_size_bytes": self.index_size_bytes,
            "exact": asdict(self.exact),
            "ann_ground_truth": asdict(self.ann_ground_truth),
            "ef_search_trials": {
                str(value): trial.to_dict()
                for value, trial in self.ef_search_trials.items()
            },
            "recommended_ef_search": self.recommended_ef_search,
            "concurrent_ann": {
                str(level): asdict(summary)
                for level, summary in self.concurrent_ann.items()
            },
            "ann_plan": self.ann_plan,
            "ann_index_used": self.ann_index_used,
            "neighbor_recall": self.neighbor_recall,
            "semantic_precision": self.semantic_precision,
            "semantic_coverage": self.semantic_coverage,
            "completed_at": self.completed_at,
            "duration_seconds": self.duration_seconds,
            "failures": list(self.failures),
            "passed": self.passed,
        }


def run_rag_scale_benchmark(
    database_url: str,
    config: RAGScaleBenchmarkConfig,
) -> RAGScaleBenchmarkResult:
    """Build and drop an isolated synthetic corpus around one complete benchmark."""

    started = time.perf_counter()
    suffix = uuid.uuid4().hex[:12]
    table_name = f"rag_scale_bench_{suffix}"
    account_index_name = f"idx_{table_name}_account"
    ann_index_name = f"idx_{table_name}_ann"
    maximum_concurrency = max(config.concurrency_levels)
    engine = sa.create_engine(
        database_url,
        pool_pre_ping=True,
        pool_size=maximum_concurrency,
        max_overflow=0,
    )
    try:
        pgvector_version = _prepare_table(engine, table_name, account_index_name)
        generation_started = time.perf_counter()
        _populate_table(engine, table_name, config)
        generation_ms = (time.perf_counter() - generation_started) * 1000
        specs = _load_query_specs(engine, table_name, config)
        for spec in specs[: min(3, len(specs))]:
            _run_query(engine, table_name, config, spec, approximate=False)
        exact_outcomes = [
            _run_query(engine, table_name, config, spec, approximate=False)
            for spec in specs
        ]
        exact = _summarize_outcomes(
            exact_outcomes,
            attempted=len(specs),
            wall_seconds=sum(item.latency_ms for item in exact_outcomes) / 1000,
            expected_top_k=config.top_k,
            specs=list(specs),
        )
        ann_ground_truth_outcomes = [
            _run_query(
                engine,
                table_name,
                config,
                spec,
                approximate=True,
                configure_ann=False,
            )
            for spec in specs
        ]
        ann_ground_truth = _summarize_outcomes(
            ann_ground_truth_outcomes,
            attempted=len(specs),
            wall_seconds=(
                sum(item.latency_ms for item in ann_ground_truth_outcomes) / 1000
            ),
            expected_top_k=config.top_k,
            specs=list(specs),
        )

        index_kind, index_type, operator_class = _ann_index_definition(
            config.dimensions
        )
        index_started = time.perf_counter()
        _create_ann_index(
            engine,
            table_name,
            ann_index_name,
            config,
            index_type=index_type,
            operator_class=operator_class,
        )
        index_build_ms = (time.perf_counter() - index_started) * 1000
        ef_search_trials: dict[int, ANNSearchTrial] = {}
        for ef_search in sorted(set(config.hnsw_ef_search_candidates)):
            plan = _explain_ann_query(
                engine,
                table_name,
                config,
                specs[0],
                ef_search=ef_search,
            )
            index_names = _plan_index_names(plan)
            quality_outcomes = [
                _run_query(
                    engine,
                    table_name,
                    config,
                    spec,
                    approximate=True,
                    ef_search=ef_search,
                )
                for spec in specs
            ]
            concurrent = {
                level: _run_concurrent_level(
                    engine,
                    table_name,
                    config,
                    specs,
                    concurrency=level,
                    ef_search=ef_search,
                )
                for level in config.concurrency_levels
            }
            ef_search_trials[ef_search] = ANNSearchTrial(
                ef_search=ef_search,
                concurrent=concurrent,
                plan=_compact_plan(plan),
                index_used=ann_index_name in index_names,
                neighbor_recall=_neighbor_recall(
                    specs,
                    quality_outcomes,
                    ann_ground_truth_outcomes,
                ),
                semantic_precision=_semantic_precision(specs, quality_outcomes),
                semantic_coverage=_semantic_coverage(
                    specs,
                    quality_outcomes,
                    required_matches=1,
                ),
            )
        recommended_ef_search = next(
            (
                value
                for value, trial in ef_search_trials.items()
                if _ann_trial_passes(trial, ann_ground_truth, config)
            ),
            None,
        )
        selected_ef_search = recommended_ef_search or min(ef_search_trials)
        selected_trial = ef_search_trials[selected_ef_search]
        with engine.connect() as connection:
            table_size_bytes = int(
                connection.scalar(
                    sa.text("SELECT pg_total_relation_size(:table_name)"),
                    {"table_name": table_name},
                )
                or 0
            )
            index_size_bytes = int(
                connection.scalar(
                    sa.text("SELECT pg_relation_size(:index_name)"),
                    {"index_name": ann_index_name},
                )
                or 0
            )
        return RAGScaleBenchmarkResult(
            config=config,
            pgvector_version=pgvector_version,
            index_kind=index_kind,
            index_name=ann_index_name,
            generation_ms=round(generation_ms, 3),
            index_build_ms=round(index_build_ms, 3),
            table_size_bytes=table_size_bytes,
            index_size_bytes=index_size_bytes,
            exact=exact,
            ann_ground_truth=ann_ground_truth,
            ef_search_trials=ef_search_trials,
            recommended_ef_search=recommended_ef_search,
            concurrent_ann=selected_trial.concurrent,
            ann_plan=selected_trial.plan,
            ann_index_used=selected_trial.index_used,
            neighbor_recall=selected_trial.neighbor_recall,
            semantic_precision=selected_trial.semantic_precision,
            semantic_coverage=selected_trial.semantic_coverage,
            completed_at=datetime.now(UTC).isoformat(timespec="seconds"),
            duration_seconds=round(time.perf_counter() - started, 3),
        )
    finally:
        try:
            with engine.begin() as connection:
                connection.exec_driver_sql(f'DROP TABLE IF EXISTS "{table_name}"')
        finally:
            engine.dispose()


def _prepare_table(engine: Engine, table_name: str, account_index_name: str) -> str:
    with engine.begin() as connection:
        pgvector_version = str(
            connection.scalar(
                sa.text("SELECT extversion FROM pg_extension WHERE extname = 'vector'")
            )
            or ""
        )
        if not pgvector_version:
            raise ValueError("Scale benchmark requires the pgvector extension.")
        connection.exec_driver_sql(
            f'''CREATE UNLOGGED TABLE "{table_name}" (
                id BIGINT PRIMARY KEY,
                account_id INTEGER NOT NULL,
                cluster_id INTEGER NOT NULL,
                embedding vector NOT NULL,
                embedding_dimensions INTEGER NOT NULL
            )'''
        )
        connection.exec_driver_sql(
            f'CREATE INDEX "{account_index_name}" '
            f'ON "{table_name}" (account_id)'
        )
    return pgvector_version


def _populate_table(
    engine: Engine,
    table_name: str,
    config: RAGScaleBenchmarkConfig,
) -> None:
    variant_count = min(
        MAX_SYNTHETIC_VARIANT_COUNT,
        math.ceil(
            config.chunk_count / (config.tenant_count * config.cluster_count)
        ),
    )
    statement = sa.text(
        f'''
        WITH prototypes AS MATERIALIZED (
            SELECT cluster_id, variant_id,
                   ARRAY(
                       SELECT sin((cluster_id + 1) * coordinate * 0.017)
                            + cos((cluster_id + 3) * coordinate * 0.011)
                            + 0.05 * sin((variant_id + 1) * coordinate * 0.023)
                       FROM generate_series(1, :dimensions) AS dims(coordinate)
                       ORDER BY coordinate
                   )::vector({config.dimensions}) AS embedding
            FROM generate_series(0, :cluster_count - 1) AS clusters(cluster_id)
            CROSS JOIN generate_series(
                0, {variant_count - 1}
            ) AS variants(variant_id)
        )
        INSERT INTO "{table_name}" (
            id, account_id, cluster_id, embedding, embedding_dimensions
        )
        SELECT row_id,
               (((row_id / :cluster_count)::BIGINT % :tenant_count) + 1)::INTEGER,
               (row_id % :cluster_count)::INTEGER,
               prototypes.embedding,
               :dimensions
        FROM generate_series(0, :chunk_count - 1) AS rows(row_id)
        JOIN prototypes
          ON prototypes.cluster_id = row_id % :cluster_count
         AND prototypes.variant_id = (
             (row_id / (:cluster_count * :tenant_count))::BIGINT
             % {variant_count}
         )
        '''
    )
    with engine.begin() as connection:
        connection.execute(
            statement,
            {
                "dimensions": config.dimensions,
                "cluster_count": config.cluster_count,
                "tenant_count": config.tenant_count,
                "chunk_count": config.chunk_count,
            },
        )
        connection.exec_driver_sql(f'ANALYZE "{table_name}"')


def _load_query_specs(
    engine: Engine,
    table_name: str,
    config: RAGScaleBenchmarkConfig,
) -> tuple[QuerySpec, ...]:
    pairs = [
        (
            (index % config.tenant_count) + 1,
            (index * 17 + index // config.tenant_count) % config.cluster_count,
        )
        for index in range(config.query_count)
    ]
    statement = sa.text(
        f'SELECT embedding::text FROM "{table_name}" '
        "WHERE account_id = :account_id AND cluster_id = :cluster_id LIMIT 1"
    )
    specs: list[QuerySpec] = []
    with engine.connect() as connection:
        for account_id, cluster_id in pairs:
            embedding = connection.scalar(
                statement,
                {"account_id": account_id, "cluster_id": cluster_id},
            )
            if embedding is None:
                raise ValueError("Scale corpus did not produce a requested query pair.")
            specs.append(
                QuerySpec(
                    account_id=account_id,
                    cluster_id=cluster_id,
                    embedding=_parse_vector_text(str(embedding)),
                )
            )
    return tuple(specs)


def _parse_vector_text(value: str) -> list[float]:
    normalized = value.strip()
    if not normalized.startswith("[") or not normalized.endswith("]"):
        raise ValueError("Scale corpus returned an invalid vector value.")
    items = normalized[1:-1].split(",")
    try:
        return [float(item) for item in items]
    except ValueError as error:
        raise ValueError("Scale corpus returned a non-numeric vector value.") from error


def _ann_index_definition(dimensions: int) -> tuple[str, str, str]:
    if dimensions > PGVECTOR_MAX_VECTOR_HNSW_DIMENSIONS:
        return "halfvec_hnsw", f"halfvec({dimensions})", "halfvec_cosine_ops"
    return "vector_hnsw", f"vector({dimensions})", "vector_cosine_ops"


def _create_ann_index(
    engine: Engine,
    table_name: str,
    index_name: str,
    config: RAGScaleBenchmarkConfig,
    *,
    index_type: str,
    operator_class: str,
) -> None:
    with engine.begin() as connection:
        connection.exec_driver_sql(
            f'CREATE INDEX "{index_name}" ON "{table_name}" USING hnsw '
            f'((embedding::{index_type}) {operator_class}) '
            f"WITH (m = {config.hnsw_m}, "
            f"ef_construction = {config.hnsw_ef_construction}) "
            f"WHERE embedding IS NOT NULL "
            f"AND embedding_dimensions = {config.dimensions}"
        )
        connection.exec_driver_sql(f'ANALYZE "{table_name}"')


def _query_statement(
    table_name: str,
    config: RAGScaleBenchmarkConfig,
    *,
    approximate: bool,
    explain: bool = False,
) -> sa.TextClause:
    if approximate:
        _, index_type, _ = _ann_index_definition(config.dimensions)
        ann_distance = f"(embedding::{index_type}) <=> :ann_query_embedding"
        exact_distance = "embedding <=> :exact_query_embedding"
        bind_type: Vector | HALFVEC = (
            HALFVEC(config.dimensions)
            if index_type.startswith("halfvec")
            else Vector(config.dimensions)
        )
    else:
        distance = "embedding <=> :query_embedding"
        bind_type = Vector(config.dimensions)
    prefix = "EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) " if explain else ""
    if approximate:
        ann_limit = config.top_k * config.ann_oversampling
        statement = sa.text(
            f"{prefix}WITH ann_candidates AS MATERIALIZED ("
            f"SELECT id, account_id, cluster_id, embedding FROM \"{table_name}\" "
            "WHERE account_id = :account_id "
            "AND embedding IS NOT NULL "
            "AND embedding_dimensions = :dimensions "
            f"ORDER BY {ann_distance} LIMIT {ann_limit}"
            ") "
            f"SELECT id, account_id, cluster_id, {exact_distance} AS distance "
            "FROM ann_candidates "
            f"ORDER BY {exact_distance} LIMIT {config.top_k}"
        )
        return statement.bindparams(
            sa.bindparam("ann_query_embedding", type_=bind_type),
            sa.bindparam(
                "exact_query_embedding",
                type_=Vector(config.dimensions),
            ),
        )
    statement = sa.text(
        f'{prefix}SELECT id, account_id, cluster_id, {distance} AS distance '
        f'FROM "{table_name}" '
        "WHERE account_id = :account_id "
        "AND embedding IS NOT NULL "
        "AND embedding_dimensions = :dimensions "
        f"ORDER BY {distance} LIMIT {config.top_k}"
    )
    return statement.bindparams(sa.bindparam("query_embedding", type_=bind_type))


def _configure_ann_connection(
    connection: Connection,
    config: RAGScaleBenchmarkConfig,
    ef_search: int,
) -> None:
    connection.exec_driver_sql(f"SET LOCAL hnsw.ef_search = {ef_search}")
    connection.exec_driver_sql("SET LOCAL hnsw.iterative_scan = 'strict_order'")
    connection.exec_driver_sql("SET LOCAL hnsw.max_scan_tuples = 100000")
    connection.exec_driver_sql("SET LOCAL hnsw.scan_mem_multiplier = 4")
    if config.force_ann_index:
        connection.exec_driver_sql("SET LOCAL enable_seqscan = off")
        connection.exec_driver_sql("SET LOCAL enable_bitmapscan = off")
        connection.exec_driver_sql("SET LOCAL enable_sort = off")


def _run_query(
    engine: Engine,
    table_name: str,
    config: RAGScaleBenchmarkConfig,
    spec: QuerySpec,
    *,
    approximate: bool,
    ef_search: int | None = None,
    configure_ann: bool = True,
) -> QueryOutcome:
    started = time.perf_counter()
    try:
        with engine.connect() as connection:
            if approximate and configure_ann:
                if ef_search is None:
                    raise ValueError("Approximate queries require ef_search.")
                _configure_ann_connection(connection, config, ef_search)
            query_parameters: dict[str, Any] = {
                "account_id": spec.account_id,
                "dimensions": config.dimensions,
            }
            if approximate:
                query_parameters.update(
                    {
                        "ann_query_embedding": spec.embedding,
                        "exact_query_embedding": spec.embedding,
                    }
                )
            else:
                query_parameters["query_embedding"] = spec.embedding
            rows = connection.execute(
                _query_statement(
                    table_name,
                    config,
                    approximate=approximate,
                ),
                query_parameters,
            ).mappings().all()
        return QueryOutcome(
            latency_ms=(time.perf_counter() - started) * 1000,
            rows=tuple(dict(row) for row in rows),
        )
    except Exception as error:  # noqa: BLE001 - report only the safe exception type.
        return QueryOutcome(
            latency_ms=(time.perf_counter() - started) * 1000,
            rows=(),
            error_type=type(error).__name__,
        )


def _run_concurrent_level(
    engine: Engine,
    table_name: str,
    config: RAGScaleBenchmarkConfig,
    specs: tuple[QuerySpec, ...],
    *,
    concurrency: int,
    ef_search: int,
) -> LatencySummary:
    started = time.perf_counter()
    outcomes: list[tuple[QuerySpec, QueryOutcome]] = []
    with ThreadPoolExecutor(max_workers=concurrency) as executor:
        futures = {
            executor.submit(
                _run_query,
                engine,
                table_name,
                config,
                spec,
                approximate=True,
                ef_search=ef_search,
            ): spec
            for spec in specs
        }
        for future in as_completed(futures):
            outcomes.append((futures[future], future.result()))
    wall_seconds = time.perf_counter() - started
    return _summarize_outcomes(
        [outcome for _, outcome in outcomes],
        attempted=len(specs),
        wall_seconds=wall_seconds,
        expected_top_k=config.top_k,
        specs=[spec for spec, _ in outcomes],
    )


def _summarize_outcomes(
    outcomes: list[QueryOutcome],
    *,
    attempted: int,
    wall_seconds: float,
    expected_top_k: int,
    specs: list[QuerySpec] | None = None,
) -> LatencySummary:
    successful = [item for item in outcomes if item.error_type is None]
    latencies = [item.latency_ms for item in successful]
    isolation_violations = 0
    if specs is not None:
        isolation_violations = sum(
            1
            for spec, outcome in zip(specs, outcomes, strict=True)
            if any(int(row["account_id"]) != spec.account_id for row in outcome.rows)
        )
    result_count_violations = sum(
        len(item.rows) != expected_top_k for item in successful
    )
    return LatencySummary(
        attempted=attempted,
        completed=len(successful),
        errors=attempted - len(successful),
        isolation_violations=isolation_violations,
        result_count_violations=result_count_violations,
        mean_ms=round(sum(latencies) / len(latencies), 3) if latencies else 0.0,
        p50_ms=round(_percentile(latencies, 0.50), 3),
        p95_ms=round(_percentile(latencies, 0.95), 3),
        p99_ms=round(_percentile(latencies, 0.99), 3),
        maximum_ms=round(max(latencies, default=0.0), 3),
        throughput_qps=round(len(successful) / max(wall_seconds, 1e-9), 3),
    )


def _neighbor_recall(
    specs: tuple[QuerySpec, ...],
    outcomes: list[QueryOutcome],
    ground_truth_outcomes: list[QueryOutcome],
) -> float:
    matched = 0
    total = 0
    for spec, outcome, ground_truth in zip(
        specs,
        outcomes,
        ground_truth_outcomes,
        strict=True,
    ):
        expected_ids = {
            int(row["id"])
            for row in ground_truth.rows
            if int(row["account_id"]) == spec.account_id
        }
        actual_ids = {
            int(row["id"])
            for row in outcome.rows
            if int(row["account_id"]) == spec.account_id
        }
        matched += len(expected_ids & actual_ids)
        total += len(expected_ids)
    return round(matched / total, 6) if total else 0.0


def _semantic_precision(
    specs: tuple[QuerySpec, ...],
    outcomes: list[QueryOutcome],
) -> float:
    matched = 0
    total = 0
    for spec, outcome in zip(specs, outcomes, strict=True):
        for row in outcome.rows:
            total += 1
            if (
                int(row["account_id"]) == spec.account_id
                and int(row["cluster_id"]) == spec.cluster_id
            ):
                matched += 1
    return round(matched / total, 6) if total else 0.0


def _semantic_coverage(
    specs: tuple[QuerySpec, ...],
    outcomes: list[QueryOutcome],
    *,
    required_matches: int,
) -> float:
    if not specs or required_matches <= 0:
        return 0.0
    covered = 0
    for spec, outcome in zip(specs, outcomes, strict=True):
        relevant_count = sum(
            1
            for row in outcome.rows
            if int(row["account_id"]) == spec.account_id
            and int(row["cluster_id"]) == spec.cluster_id
        )
        if relevant_count >= required_matches:
            covered += 1
    return round(covered / len(specs), 6)


def _ann_trial_passes(
    trial: ANNSearchTrial,
    exact: LatencySummary,
    config: RAGScaleBenchmarkConfig,
) -> bool:
    if (
        not trial.index_used
        or trial.neighbor_recall < config.minimum_neighbor_recall
        or trial.semantic_precision < config.minimum_semantic_precision
        or trial.semantic_coverage < config.minimum_semantic_coverage
    ):
        return False
    if any(
        summary.errors
        or summary.isolation_violations
        or summary.result_count_violations
        for summary in trial.concurrent.values()
    ):
        return False
    single = trial.concurrent[min(trial.concurrent)]
    if config.enforce_speedup and single.p95_ms > exact.p95_ms * 1.25 + 2.0:
        return False
    return not (
        config.maximum_p95_ms is not None
        and single.p95_ms > config.maximum_p95_ms
    )


def _explain_ann_query(
    engine: Engine,
    table_name: str,
    config: RAGScaleBenchmarkConfig,
    spec: QuerySpec,
    *,
    ef_search: int,
) -> dict[str, Any]:
    with engine.connect() as connection:
        _configure_ann_connection(connection, config, ef_search)
        payload = connection.scalar(
            _query_statement(
                table_name,
                config,
                approximate=True,
                explain=True,
            ),
            {
                "ann_query_embedding": spec.embedding,
                "exact_query_embedding": spec.embedding,
                "account_id": spec.account_id,
                "dimensions": config.dimensions,
            },
        )
    if not isinstance(payload, list) or not payload or not isinstance(payload[0], dict):
        raise ValueError("PostgreSQL returned an invalid JSON query plan.")
    return payload[0]


def _plan_index_names(plan: dict[str, Any]) -> set[str]:
    names: set[str] = set()

    def visit(node: Any) -> None:
        if isinstance(node, dict):
            index_name = node.get("Index Name")
            if isinstance(index_name, str):
                names.add(index_name)
            for value in node.values():
                visit(value)
        elif isinstance(node, list):
            for value in node:
                visit(value)

    visit(plan)
    return names


def _compact_plan(plan: dict[str, Any]) -> dict[str, Any]:
    root = plan.get("Plan") if isinstance(plan.get("Plan"), dict) else {}
    return {
        "node_type": root.get("Node Type"),
        "execution_time_ms": plan.get("Execution Time"),
        "planning_time_ms": plan.get("Planning Time"),
        "index_names": sorted(_plan_index_names(plan)),
        "shared_hit_blocks": int(root.get("Shared Hit Blocks") or 0),
        "shared_read_blocks": int(root.get("Shared Read Blocks") or 0),
    }


def _percentile(values: list[float], quantile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    position = (len(ordered) - 1) * quantile
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    weight = position - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


def format_rag_scale_benchmark(result: RAGScaleBenchmarkResult) -> str:
    lines = [
        "RAG pgvector scale benchmark",
        (
            f"chunks={result.config.chunk_count} dimensions={result.config.dimensions} "
            f"tenants={result.config.tenant_count} top_k={result.config.top_k} "
            f"oversampling={result.config.ann_oversampling}"
        ),
        f"index={result.index_kind} build_ms={result.index_build_ms:.1f}",
        (
            f"exact p50={result.exact.p50_ms:.1f}ms "
            f"p95={result.exact.p95_ms:.1f}ms p99={result.exact.p99_ms:.1f}ms"
        ),
        (
            f"halfvec_exact p50={result.ann_ground_truth.p50_ms:.1f}ms "
            f"p95={result.ann_ground_truth.p95_ms:.1f}ms "
            f"p99={result.ann_ground_truth.p99_ms:.1f}ms"
        ),
    ]
    for ef_search, trial in result.ef_search_trials.items():
        single = trial.concurrent[min(trial.concurrent)]
        lines.append(
            f"trial ef_search={ef_search} used={trial.index_used} "
            f"neighbor_recall={trial.neighbor_recall:.3f} "
            f"semantic_precision={trial.semantic_precision:.3f} "
            f"semantic_coverage={trial.semantic_coverage:.3f} "
            f"single_p95={single.p95_ms:.1f}ms"
        )
    lines.append(f"recommended_ef_search={result.recommended_ef_search or 'none'}")
    for concurrency, summary in result.concurrent_ann.items():
        lines.append(
            f"ann concurrency={concurrency} p50={summary.p50_ms:.1f}ms "
            f"p95={summary.p95_ms:.1f}ms p99={summary.p99_ms:.1f}ms "
            f"qps={summary.throughput_qps:.1f} errors={summary.errors} "
            f"isolation={summary.isolation_violations}"
        )
    lines.extend(f"failure={failure}" for failure in result.failures)
    lines.append(f"result={'PASS' if result.passed else 'FAIL'}")
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-url", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--chunk-count", type=int, default=10_000)
    parser.add_argument("--dimensions", type=int, default=2_560)
    parser.add_argument("--tenants", type=int, default=4)
    parser.add_argument("--clusters", type=int, default=64)
    parser.add_argument("--queries", type=int, default=40)
    parser.add_argument("--top-k", type=int, default=DEFAULT_RAG_RETRIEVAL_TOP_K)
    parser.add_argument(
        "--ann-oversampling",
        type=int,
        default=PGVECTOR_HNSW_OVERSAMPLING,
    )
    parser.add_argument("--concurrency", default="1,5,10,20")
    parser.add_argument("--hnsw-m", type=int, default=32)
    parser.add_argument("--hnsw-ef-construction", type=int, default=128)
    parser.add_argument("--hnsw-ef-search-values", default="400,800,1000")
    parser.add_argument("--minimum-neighbor-recall", type=float, default=0.65)
    parser.add_argument("--minimum-semantic-precision", type=float, default=0.95)
    parser.add_argument("--minimum-semantic-coverage", type=float, default=1.0)
    parser.add_argument("--maximum-p95-ms", type=float)
    parser.add_argument("--skip-speedup-gate", action="store_true")
    parser.add_argument("--force-ann-index", action="store_true")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    concurrency_levels = tuple(
        sorted({int(value.strip()) for value in args.concurrency.split(",") if value.strip()})
    )
    ef_search_candidates = tuple(
        sorted(
            {
                int(value.strip())
                for value in args.hnsw_ef_search_values.split(",")
                if value.strip()
            }
        )
    )
    config = RAGScaleBenchmarkConfig(
        chunk_count=args.chunk_count,
        dimensions=args.dimensions,
        tenant_count=args.tenants,
        cluster_count=args.clusters,
        query_count=args.queries,
        top_k=args.top_k,
        ann_oversampling=args.ann_oversampling,
        concurrency_levels=concurrency_levels,
        hnsw_m=args.hnsw_m,
        hnsw_ef_construction=args.hnsw_ef_construction,
        hnsw_ef_search_candidates=ef_search_candidates,
        minimum_neighbor_recall=args.minimum_neighbor_recall,
        minimum_semantic_precision=args.minimum_semantic_precision,
        minimum_semantic_coverage=args.minimum_semantic_coverage,
        maximum_p95_ms=args.maximum_p95_ms,
        enforce_speedup=not args.skip_speedup_gate,
        force_ann_index=args.force_ann_index,
    )
    result = run_rag_scale_benchmark(args.database_url, config)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(result.to_dict(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(format_rag_scale_benchmark(result))
    print(f"report={args.output.resolve()}")
    return 0 if result.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
