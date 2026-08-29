"""PostgreSQL + pgvector RAG 后端。

``long_texts`` 仍是长文本材料的事实源；本模块只把它们切分、嵌入并保存为
``rag_chunks`` 派生索引。它提供稳定的公共方法，应用层
可在 PostgreSQL 环境切换后端，而不改变简历生成、对话入库或检索调用。
"""

from __future__ import annotations

import hashlib
import math
import re
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

import sqlalchemy as sa
from langchain_core.documents import Document
from langchain_core.embeddings import Embeddings
from pgvector.sqlalchemy import HALFVEC, Vector
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.engine import Engine

from .config import (
    DEFAULT_RAG_RETRIEVAL_TOP_K,
    DEFAULT_RAG_RERANK_TOP_N,
    MAX_RAG_RETRIEVAL_TOP_K,
    MAX_RAG_RERANK_TOP_N,
)
from .database_schema import long_texts, rag_chunks, visual_knowledge_items
from .models import LongTextRecord, RAGIndexStats, RAGSearchResult
from .rag import (
    RAG_CHUNKING_VERSION,
    Reranker,
    build_rag_retrieval_query,
    build_rag_documents,
    decompose_rag_query,
    rag_embedding_model_name,
    rerank_rag_results,
    split_rag_documents,
)

PGVECTOR_COLLECTION_NAME = "rag_chunks"
PGVECTOR_PERSISTENCE_LABEL = "postgresql+pgvector"
PGVECTOR_HNSW_EF_SEARCH = 400
PGVECTOR_HNSW_OVERSAMPLING = 20
PGVECTOR_HNSW_MAX_CANDIDATES = 2_000
PGVECTOR_MAX_VECTOR_HNSW_DIMENSIONS = 2_000
PGVECTOR_MAX_HALFVEC_HNSW_DIMENSIONS = 4_000

# 含数字的编号、尺寸、公差、版本号和全大写业务标签是向量模型容易忽略的
# 高价值信号。只对这类 token 做精确补召回，避免把普通词变成低效模糊检索。
_EXACT_RETRIEVAL_TOKEN_PATTERN = re.compile(
    r"(?<![A-Za-z0-9])(?:"
    r"[A-Za-z][A-Za-z0-9_./+\-]*\d[A-Za-z0-9_./+\-]*|"
    r"\d+(?:\.\d+)?|"
    r"[A-Z][A-Z_./+\-]{2,}"
    r")(?![A-Za-z0-9])"
)


def _extract_exact_retrieval_tokens(query: str) -> tuple[str, ...]:
    """提取适合精确补召回的数字、编号或全大写业务标签。"""

    tokens: list[str] = []
    seen: set[str] = set()
    for match in _EXACT_RETRIEVAL_TOKEN_PATTERN.finditer(query):
        token = match.group(0).strip()
        normalized = token.casefold()
        if len(token) < 2 or normalized in seen:
            continue
        seen.add(normalized)
        tokens.append(token)
    return tuple(tokens)


def _text_row_key(row: Any) -> tuple[int, int, str]:
    """返回同一文字 chunk 在向量和精确通道中的稳定身份。"""

    return (int(row["long_text_id"]), int(row["chunk_index"]), str(row["content"]))


def _merge_text_retrieval_rows(
    vector_rows: Sequence[Any],
    exact_rows: Sequence[Any],
    limit: int,
) -> list[Any]:
    """在同一个文字 Top-K 预算内优先保留精确命中的行。"""

    merged: list[Any] = []
    seen: set[tuple[int, int, str]] = set()
    for row in (*exact_rows, *vector_rows):
        key = _text_row_key(row)
        if key in seen:
            continue
        seen.add(key)
        merged.append(row)
        if len(merged) >= limit:
            break
    return merged


def _deduplicate_rag_results(
    candidates: Sequence[RAGSearchResult],
) -> list[RAGSearchResult]:
    """合并子查询候选时按来源片段去重，保留第一次出现的结果。"""

    deduplicated: list[RAGSearchResult] = []
    seen: set[tuple[int, int, int | None, str]] = set()
    for candidate in candidates:
        key = (
            candidate.long_text_id,
            candidate.chunk_index,
            candidate.visual_item_id,
            candidate.content,
        )
        if key in seen:
            continue
        seen.add(key)
        deduplicated.append(candidate)
    return deduplicated


def _visual_evidence_text(row: Any) -> str:
    """把结构化视觉元数据转换成受控检索摘要，不返回整份项目文件。"""

    metadata = row.get("metadata_json")
    metadata = metadata if isinstance(metadata, dict) else {}
    finding = metadata.get("visual_finding")
    finding = finding if isinstance(finding, dict) else {}
    lines: list[str] = []
    summary = str(finding.get("summary") or "").strip()
    if summary:
        lines.append(f"视觉摘要：{summary[:2_000]}")
    for label, key in (
        ("元素关系", "element_relationships"),
        ("表格", "tables"),
    ):
        values = finding.get(key)
        if isinstance(values, list):
            lines.extend(
                f"{label}：{str(value)[:1_000]}"
                for value in values[:20]
                if str(value).strip()
            )
    parameters = finding.get("parameters")
    if isinstance(parameters, list):
        for parameter in parameters[:40]:
            if not isinstance(parameter, dict):
                continue
            fields = [
                f"{key}={str(parameter.get(key) or '')[:256]}"
                for key in ("name", "value", "unit", "tolerance", "applies_to")
                if str(parameter.get(key) or "").strip()
            ]
            if fields:
                lines.append("参数：" + "；".join(fields))
    if not lines:
        fallback = str(row.get("text") or "").strip()
        lines.append(f"视觉来源关联文字：{fallback[:2_000]}")
    return "\n".join(lines)


def _text_evidence_page_number(row: Any) -> int | None:
    """从文字 Chunk 元数据恢复页码，供引用和前端定位原文。"""

    metadata = row.get("metadata_json")
    metadata = metadata if isinstance(metadata, dict) else {}
    source_page = metadata.get("source_page")
    if source_page is None or not str(source_page).isdigit():
        return None
    page_number = int(str(source_page))
    return page_number if page_number > 0 else None


def _indexed_vector_type(vector_dimension: int) -> Vector | HALFVEC:
    """返回与 pgvector HNSW 维度上限兼容的查询类型。

    pgvector 的 ``vector`` HNSW 最多支持 2000 维，``halfvec`` 最多支持
    4000 维。正式 2560 维模型因此只在距离计算和 ANN 索引中转成 halfvec；
    原始向量仍以完整精度保存在 ``vector`` 列中。
    """

    if PGVECTOR_MAX_VECTOR_HNSW_DIMENSIONS < vector_dimension <= (
        PGVECTOR_MAX_HALFVEC_HNSW_DIMENSIONS
    ):
        return HALFVEC(vector_dimension)
    return Vector(vector_dimension)


def _cosine_distance_expression(
    column: Any,
    *,
    parameter_name: str,
    vector: list[float],
    vector_dimension: int,
) -> Any:
    indexed_type = _indexed_vector_type(vector_dimension)
    return sa.type_coerce(
        sa.cast(column, indexed_type).op("<=>")(
            sa.bindparam(
                parameter_name,
                value=vector,
                type_=indexed_type,
            )
        ),
        sa.Float(),
    )


def _full_precision_cosine_distance_expression(
    column: Any,
    *,
    parameter_name: str,
    vector: list[float],
    vector_dimension: int,
) -> Any:
    vector_type = Vector(vector_dimension)
    return sa.type_coerce(
        sa.cast(column, vector_type).op("<=>")(
            sa.bindparam(
                parameter_name,
                value=vector,
                type_=vector_type,
            )
        ),
        sa.Float(),
    )


def _ann_candidate_limit(result_limit: int) -> int:
    """扩大 ANN 候选池，同时限制异常 Top-K 配置的数据库开销。"""

    return min(
        result_limit * PGVECTOR_HNSW_OVERSAMPLING,
        max(result_limit, PGVECTOR_HNSW_MAX_CANDIDATES),
    )


def _configure_hnsw_search(connection: sa.Connection) -> None:
    """启用带租户过滤时的迭代 HNSW 扫描，并提高候选探索预算。"""

    connection.exec_driver_sql(f"SET LOCAL hnsw.ef_search = {PGVECTOR_HNSW_EF_SEARCH}")
    connection.exec_driver_sql("SET LOCAL hnsw.iterative_scan = 'strict_order'")
    connection.exec_driver_sql("SET LOCAL hnsw.max_scan_tuples = 100000")
    connection.exec_driver_sql("SET LOCAL hnsw.scan_mem_multiplier = 4")


class PgVectorKnowledgeBase:
    """在 PostgreSQL 内维护账号隔离的 pgvector RAG 索引。

    外部接口保持稳定：调用方只需要提供长文本、查询和可选的
    账号过滤条件。向量 SQL、稳定 ID、模型隔离和事务性 upsert 都留在此模块内部。
    """

    def __init__(
        self,
        engine: Engine,
        embeddings: Embeddings | None = None,
        reranker: Reranker | None = None,
    ):
        """绑定已迁移的 PostgreSQL Engine 与可选的 Embedding/Rerank 实现。"""

        if engine.dialect.name != "postgresql":
            raise ValueError("PgVectorKnowledgeBase 只能连接 PostgreSQL 数据库。")
        self.engine = engine
        self.embeddings = embeddings
        self.reranker = reranker
        # 兼容现有 RAGIndexStats 的字段语义，同时不会泄露数据库连接串。
        self.collection_name = PGVECTOR_COLLECTION_NAME
        self.persist_directory = PGVECTOR_PERSISTENCE_LABEL

    def rebuild(
        self,
        long_texts: list[LongTextRecord],
        account_id: int | None = None,
    ) -> RAGIndexStats:
        """原子替换指定账号（或全部账号）的派生 RAG chunk。"""

        documents = split_rag_documents(build_rag_documents(long_texts, account_id=account_id))
        rows = self._rows_for_documents(documents)
        with self.engine.begin() as connection:
            statement = sa.delete(rag_chunks)
            if account_id is not None:
                statement = statement.where(rag_chunks.c.account_id == account_id)
            connection.execute(statement)
            self._upsert_rows(connection, rows)
        return self._stats(len(long_texts), len(documents), mode="rebuild")

    def index_long_texts(
        self,
        long_texts: list[LongTextRecord],
        account_id: int | None = None,
    ) -> RAGIndexStats:
        """事务性替换指定长文本的全部 Chunk，避免旧尾块残留。"""

        documents = split_rag_documents(build_rag_documents(long_texts, account_id=account_id))
        rows = self._rows_for_documents(documents)
        # Chunk 数量可能因正文修改或切分版本升级而减少；仅 upsert 会遗留旧尾块，
        # 因此删除与本批来源对应的旧派生索引后，再在同一事务中写入新行。
        long_text_ids = sorted(
            {int(document.metadata["long_text_id"]) for document in documents}
            | {int(record.id) for record in long_texts}
        )
        if long_text_ids:
            with self.engine.begin() as connection:
                self._lock_long_text_ids(connection, long_text_ids)
                delete_statement = sa.delete(rag_chunks).where(rag_chunks.c.long_text_id.in_(long_text_ids))
                if account_id is not None:
                    delete_statement = delete_statement.where(rag_chunks.c.account_id == account_id)
                connection.execute(delete_statement)
                self._upsert_rows(connection, rows)
        return self._stats(len(long_texts), len(documents), mode="incremental")

    def delete_long_texts(
        self,
        long_text_ids: list[int],
        account_id: int | None = None,
    ) -> int:
        """删除指定长文本的派生 chunk，不请求 Embedding 模型。"""

        normalized_ids = sorted({int(item_id) for item_id in long_text_ids if int(item_id) > 0})
        if not normalized_ids:
            return 0
        statement = sa.delete(rag_chunks).where(rag_chunks.c.long_text_id.in_(normalized_ids))
        if account_id is not None:
            statement = statement.where(rag_chunks.c.account_id == account_id)
        with self.engine.begin() as connection:
            self._lock_long_text_ids(connection, normalized_ids)
            result = connection.execute(statement)
        return max(0, result.rowcount or 0)

    def search(
        self,
        query: str,
        top_n: int = DEFAULT_RAG_RERANK_TOP_N,
        entity_types: list[str] | None = None,
        account_id: int | None = None,
        candidate_id: int | None = None,
        retrieval_top_k: int | None = None,
    ) -> list[RAGSearchResult]:
        """按需拆解多步骤查询，再合并候选并统一重排。"""

        if not query.strip() or top_n <= 0:
            return []
        resolved_top_n = min(int(top_n), MAX_RAG_RERANK_TOP_N)
        positive_query = build_rag_retrieval_query(query)
        subqueries = decompose_rag_query(positive_query)
        if len(subqueries) > 1:
            candidates: list[RAGSearchResult] = []
            for subquery in subqueries:
                candidates.extend(
                    self._search_single(
                        subquery,
                        # 每个阶段保留两个候选，给相邻表/报告一个纠错机会，避免
                        # 阶段 Top-1 偶发偏移后导致整个流程缺少一个来源。
                        top_n=2,
                        entity_types=entity_types,
                        account_id=account_id,
                        candidate_id=candidate_id,
                        retrieval_top_k=retrieval_top_k,
                    )
                )
            candidates = _deduplicate_rag_results(candidates)
            return rerank_rag_results(
                query,
                candidates,
                resolved_top_n,
                self.reranker,
            )
        return self._search_single(
            query,
            top_n=resolved_top_n,
            entity_types=entity_types,
            account_id=account_id,
            candidate_id=candidate_id,
            retrieval_top_k=retrieval_top_k,
        )

    def _search_single(
        self,
        query: str,
        top_n: int,
        entity_types: list[str] | None = None,
        account_id: int | None = None,
        candidate_id: int | None = None,
        retrieval_top_k: int | None = None,
        *,
        _rerank: bool = True,
    ) -> list[RAGSearchResult]:
        """先按 Retriever Top-K 粗排，再由 Reranker 输出最终 Top-N。"""

        if not query.strip() or top_n <= 0:
            return []
        embeddings = self._require_embeddings()
        retrieval_query = build_rag_retrieval_query(query)
        query_vector = self._validate_vector(
            embeddings.embed_query(retrieval_query),
            "查询",
        )
        vector_dimension = len(query_vector)
        ann_distance = _cosine_distance_expression(
            rag_chunks.c.embedding,
            parameter_name="ann_query_embedding",
            vector=query_vector,
            vector_dimension=vector_dimension,
        )
        resolved_top_n = min(int(top_n), MAX_RAG_RERANK_TOP_N)
        configured_top_k = getattr(self.reranker, "retrieval_top_k", DEFAULT_RAG_RETRIEVAL_TOP_K)
        requested_top_k = configured_top_k if retrieval_top_k is None else retrieval_top_k
        resolved_top_k = min(MAX_RAG_RETRIEVAL_TOP_K, max(1, int(requested_top_k)))
        candidate_limit = max(resolved_top_k, resolved_top_n)
        normalized_entity_types = [item for item in (entity_types or []) if item]
        scope_conditions = [
            rag_chunks.c.embedding.is_not(None),
            rag_chunks.c.embedding_dimensions == vector_dimension,
            rag_chunks.c.embedding_model == rag_embedding_model_name(embeddings),
        ]
        if account_id is not None:
            scope_conditions.append(rag_chunks.c.account_id == account_id)
        if candidate_id is not None:
            scope_conditions.append(rag_chunks.c.candidate_id == candidate_id)
        if normalized_entity_types:
            scope_conditions.append(rag_chunks.c.entity_type.in_(normalized_entity_types))
        ann_candidates = (
            sa.select(
                rag_chunks.c.content,
                rag_chunks.c.entity_type,
                rag_chunks.c.entity_id,
                rag_chunks.c.source_label,
                rag_chunks.c.long_text_id,
                rag_chunks.c.chunk_index,
                rag_chunks.c.metadata_json,
                rag_chunks.c.embedding,
            )
            .where(*scope_conditions)
            .order_by(ann_distance.asc())
            .limit(_ann_candidate_limit(candidate_limit))
            .cte("rag_ann_candidates")
            .prefix_with("MATERIALIZED", dialect="postgresql")
        )
        exact_distance = _full_precision_cosine_distance_expression(
            ann_candidates.c.embedding,
            parameter_name="exact_query_embedding",
            vector=query_vector,
            vector_dimension=vector_dimension,
        )
        statement = (
            sa.select(
                ann_candidates.c.content,
                ann_candidates.c.entity_type,
                ann_candidates.c.entity_id,
                ann_candidates.c.source_label,
                ann_candidates.c.long_text_id,
                ann_candidates.c.chunk_index,
                ann_candidates.c.metadata_json,
                exact_distance.label("distance"),
            )
            .order_by(exact_distance.asc())
            .limit(candidate_limit)
        )
        exact_tokens = _extract_exact_retrieval_tokens(retrieval_query)
        lexical_rows: list[Any] = []
        if exact_tokens:
            lexical_distance = _full_precision_cosine_distance_expression(
                rag_chunks.c.embedding,
                parameter_name="lexical_query_embedding",
                vector=query_vector,
                vector_dimension=vector_dimension,
            )
            lexical_conditions = [
                rag_chunks.c.content.ilike(f"%{token}%") for token in exact_tokens
            ]
            lexical_score: Any = sa.literal(0)
            for condition in lexical_conditions:
                lexical_score = lexical_score + sa.case((condition, 1), else_=0)
            lexical_statement = (
                sa.select(
                    rag_chunks.c.content,
                    rag_chunks.c.entity_type,
                    rag_chunks.c.entity_id,
                    rag_chunks.c.source_label,
                    rag_chunks.c.long_text_id,
                    rag_chunks.c.chunk_index,
                    rag_chunks.c.metadata_json,
                    lexical_distance.label("distance"),
                    lexical_score.label("lexical_score"),
                )
                .where(*scope_conditions, sa.or_(*lexical_conditions))
                .order_by(lexical_score.desc(), lexical_distance.asc())
                .limit(candidate_limit)
            )
        with self.engine.connect() as connection:
            _configure_hnsw_search(connection)
            rows = connection.execute(statement).mappings().all()
            if exact_tokens:
                lexical_rows = connection.execute(lexical_statement).mappings().all()
            visual_rows = connection.execute(
                self._visual_search_statement(
                    query_vector,
                    vector_dimension,
                    rag_embedding_model_name(embeddings),
                    candidate_limit,
                    account_id=account_id,
                    candidate_id=candidate_id,
                    entity_types=normalized_entity_types,
                )
            ).mappings().all()
        text_rows = _merge_text_retrieval_rows(rows, lexical_rows, candidate_limit)
        candidates = [
            RAGSearchResult(
                content=str(row["content"]),
                entity_type=str(row["entity_type"]),
                entity_id=int(row["entity_id"]),
                source_label=str(row["source_label"]),
                long_text_id=int(row["long_text_id"]),
                chunk_index=int(row["chunk_index"]),
                distance=float(row["distance"]),
                page_number=_text_evidence_page_number(row),
            )
            for row in text_rows
        ]
        candidates.extend(
            RAGSearchResult(
                content=_visual_evidence_text(row),
                entity_type=str(row["entity_type"]),
                entity_id=int(row["entity_id"]),
                source_label=str(row["source_label"]),
                long_text_id=int(row["long_text_id"]),
                chunk_index=-1,
                distance=float(row["distance"]),
                evidence_kind="visual",
                visual_item_id=int(row["visual_item_id"]),
                page_number=(
                    int(row["page_number"])
                    if row["page_number"] is not None
                    else None
                ),
            )
            for row in visual_rows
        )
        if not lexical_rows:
            candidates.sort(key=lambda item: item.distance)
        if not _rerank:
            return candidates
        return rerank_rag_results(query, candidates, resolved_top_n, self.reranker)

    @staticmethod
    def _lock_long_text_ids(connection: sa.Connection, long_text_ids: list[int]) -> None:
        """按稳定顺序锁定来源，防止并发替换产生跨版本混合 Chunk。"""

        lock_statement = sa.text(
            "SELECT pg_advisory_xact_lock(hashtextextended(:lock_key, 0))"
        )
        for long_text_id in sorted(set(long_text_ids)):
            connection.execute(
                lock_statement,
                {"lock_key": f"rag-long-text:{long_text_id}"},
            )

    @staticmethod
    def _visual_search_statement(
        query_vector: list[float],
        vector_dimension: int,
        embedding_model: str,
        candidate_limit: int,
        *,
        account_id: int | None,
        candidate_id: int | None,
        entity_types: list[str],
    ) -> sa.Select:
        """构建与文字查询向量同空间的视觉召回 SQL。"""

        ann_distance = _cosine_distance_expression(
            visual_knowledge_items.c.embedding,
            parameter_name="visual_ann_query_embedding",
            vector=query_vector,
            vector_dimension=vector_dimension,
        )
        scope_conditions = [
            visual_knowledge_items.c.index_status == "indexed",
            visual_knowledge_items.c.embedding.is_not(None),
            visual_knowledge_items.c.embedding_dimensions == vector_dimension,
            visual_knowledge_items.c.embedding_model == embedding_model,
        ]
        if account_id is not None:
            scope_conditions.append(visual_knowledge_items.c.account_id == account_id)
        if candidate_id is not None:
            scope_conditions.append(visual_knowledge_items.c.candidate_id == candidate_id)
        if entity_types:
            scope_conditions.append(long_texts.c.entity_type.in_(entity_types))
        ann_candidates = (
            sa.select(
                visual_knowledge_items.c.id.label("visual_item_id"),
                visual_knowledge_items.c.source_label,
                visual_knowledge_items.c.page_number,
                visual_knowledge_items.c.metadata_json,
                long_texts.c.id.label("long_text_id"),
                long_texts.c.entity_type,
                long_texts.c.entity_id,
                long_texts.c.text,
                visual_knowledge_items.c.embedding,
            )
            .select_from(
                visual_knowledge_items.join(
                    long_texts,
                    visual_knowledge_items.c.long_text_id == long_texts.c.id,
                )
            )
            .where(*scope_conditions)
            .order_by(ann_distance.asc())
            .limit(_ann_candidate_limit(candidate_limit))
            .cte("visual_ann_candidates")
            .prefix_with("MATERIALIZED", dialect="postgresql")
        )
        exact_distance = _full_precision_cosine_distance_expression(
            ann_candidates.c.embedding,
            parameter_name="visual_exact_query_embedding",
            vector=query_vector,
            vector_dimension=vector_dimension,
        )
        return (
            sa.select(
                ann_candidates.c.visual_item_id,
                ann_candidates.c.source_label,
                ann_candidates.c.page_number,
                ann_candidates.c.metadata_json,
                ann_candidates.c.long_text_id,
                ann_candidates.c.entity_type,
                ann_candidates.c.entity_id,
                ann_candidates.c.text,
                exact_distance.label("distance"),
            )
            .order_by(exact_distance.asc())
            .limit(candidate_limit)
        )

    def _rows_for_documents(self, documents: list[Document]) -> list[dict[str, Any]]:
        """先完成所有 Embedding 调用，再生成可在一个事务中写入的 chunk 行。"""

        if not documents:
            return []
        embeddings = self._require_embeddings()
        vectors = embeddings.embed_documents([document.page_content for document in documents])
        if len(vectors) != len(documents):
            raise ValueError("Embedding 返回的向量数量与待索引文本数量不一致。")
        model_name = rag_embedding_model_name(embeddings)
        now = datetime.now(UTC)
        rows: list[dict[str, Any]] = []
        for document, vector in zip(documents, vectors, strict=True):
            normalized_vector = self._validate_vector(vector, "索引")
            metadata = document.metadata
            raw_account_id = metadata.get("account_id")
            account_id = int(raw_account_id) if raw_account_id is not None else None
            if account_id is not None and account_id <= 0:
                raise ValueError("pgvector RAG 索引必须关联有效的账号。")
            candidate_id = metadata.get("candidate_id")
            content = document.page_content
            rows.append(
                {
                    "id": str(metadata["chunk_id"]),
                    "account_id": account_id,
                    "candidate_id": int(candidate_id) if candidate_id is not None else None,
                    "long_text_id": int(metadata["long_text_id"]),
                    "entity_type": str(metadata["entity_type"]),
                    "entity_id": int(metadata["entity_id"]),
                    "source_label": str(metadata["source_label"]),
                    "chunk_index": int(metadata["chunk_index"]),
                    "content": content,
                    "content_sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
                    "metadata_json": {
                        "account_id": account_id,
                        "candidate_id": candidate_id,
                        "chunk_id": str(metadata["chunk_id"]),
                        "chunking_version": str(metadata.get("chunking_version") or RAG_CHUNKING_VERSION),
                        "block_index": int(metadata["block_index"]),
                        "semantic_type": str(metadata["semantic_type"]),
                        "section_title": metadata.get("section_title"),
                        "source_page": metadata.get("source_page"),
                        "fragment_index": metadata.get("fragment_index"),
                        "fragment_count": metadata.get("fragment_count"),
                    },
                    "embedding": normalized_vector,
                    "embedding_model": model_name,
                    "embedding_dimensions": len(normalized_vector),
                    "created_at": now,
                    "updated_at": now,
                }
            )
        return rows

    def _upsert_rows(self, connection: sa.Connection, rows: list[dict[str, Any]]) -> None:
        """按稳定 chunk ID 写入或替换内容，保留首次创建时间。"""

        if not rows:
            return
        insert_statement = postgresql_insert(rag_chunks).values(rows)
        mutable_columns = (
            "account_id",
            "candidate_id",
            "long_text_id",
            "entity_type",
            "entity_id",
            "source_label",
            "chunk_index",
            "content",
            "content_sha256",
            "metadata_json",
            "embedding",
            "embedding_model",
            "embedding_dimensions",
            "updated_at",
        )
        statement = insert_statement.on_conflict_do_update(
            index_elements=[rag_chunks.c.id],
            set_={column: getattr(insert_statement.excluded, column) for column in mutable_columns},
        )
        connection.execute(statement)

    def _require_embeddings(self) -> Embeddings:
        """在真正需要向量时延迟检查配置，删除空索引不要求模型可用。"""

        if self.embeddings is None:
            raise ValueError("pgvector RAG 索引和检索需要可用的 Embedding 配置。")
        return self.embeddings

    def _validate_vector(self, vector: list[float], purpose: str) -> list[float]:
        """验证向量维度和值，避免把损坏响应写入数据库或发送给 pgvector。"""

        normalized = [float(value) for value in vector]
        if not normalized:
            raise ValueError(f"{purpose} Embedding 返回了空向量。")
        if not all(math.isfinite(value) for value in normalized):
            raise ValueError(f"{purpose} Embedding 包含非有限数值。")
        return normalized

    def _stats(self, document_count: int, chunk_count: int, mode: str) -> RAGIndexStats:
        """构造 PostgreSQL + pgvector 的索引统计结果。"""

        return RAGIndexStats(
            document_count=document_count,
            chunk_count=chunk_count,
            persist_directory=self.persist_directory,
            collection_name=self.collection_name,
            mode=mode,
        )
