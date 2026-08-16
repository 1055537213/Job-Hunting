"""PostgreSQL + pgvector RAG 后端。

``long_texts`` 仍是长文本材料的事实源；本模块只把它们切分、嵌入并保存为
``rag_chunks`` 派生索引。它提供稳定的公共方法，应用层
可在 PostgreSQL 环境切换后端，而不改变简历生成、对话入库或检索调用。
"""

from __future__ import annotations

import hashlib
import math
from datetime import UTC, datetime
from typing import Any

import sqlalchemy as sa
from langchain_core.documents import Document
from langchain_core.embeddings import Embeddings
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.engine import Engine

from .database_schema import rag_chunks
from .models import LongTextRecord, RAGIndexStats, RAGSearchResult
from .rag import (
    Reranker,
    build_rag_documents,
    rag_embedding_model_name,
    rerank_rag_results,
    split_rag_documents,
)

PGVECTOR_COLLECTION_NAME = "rag_chunks"
PGVECTOR_PERSISTENCE_LABEL = "postgresql+pgvector"


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
        """增量 upsert 指定长文本；稳定 chunk ID 防止重复索引产生重复证据。"""

        documents = split_rag_documents(build_rag_documents(long_texts, account_id=account_id))
        rows = self._rows_for_documents(documents)
        if rows:
            with self.engine.begin() as connection:
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
            result = connection.execute(statement)
        return max(0, result.rowcount or 0)

    def search(
        self,
        query: str,
        top_k: int = 5,
        entity_types: list[str] | None = None,
        account_id: int | None = None,
    ) -> list[RAGSearchResult]:
        """在 PostgreSQL 内完成账号过滤和余弦距离排序，再可选调用 Rerank。"""

        if not query.strip() or top_k <= 0:
            return []
        embeddings = self._require_embeddings()
        query_vector = self._validate_vector(embeddings.embed_query(query), "查询")
        vector_dimension = len(query_vector)
        # ``<=>`` 的左操作数是 vector，因此 SQLAlchemy 默认会沿用 Vector 结果处理器；
        # 但 pgvector 实际返回的是余弦距离浮点数，必须在 Python 侧显式标记结果类型。
        distance = sa.type_coerce(
            rag_chunks.c.embedding.op("<=>")(
                sa.bindparam("query_embedding", value=query_vector, type_=Vector(vector_dimension))
            ),
            sa.Float(),
        )
        candidate_multiplier = getattr(self.reranker, "candidate_multiplier", 1) if self.reranker else 1
        candidate_limit = max(top_k * max(1, int(candidate_multiplier)), top_k)
        statement = (
            sa.select(
                rag_chunks.c.content,
                rag_chunks.c.entity_type,
                rag_chunks.c.entity_id,
                rag_chunks.c.source_label,
                rag_chunks.c.long_text_id,
                rag_chunks.c.chunk_index,
                distance.label("distance"),
            )
            .where(
                rag_chunks.c.embedding.is_not(None),
                rag_chunks.c.embedding_dimensions == vector_dimension,
                rag_chunks.c.embedding_model == rag_embedding_model_name(embeddings),
            )
            .order_by(distance.asc())
            .limit(candidate_limit)
        )
        if account_id is not None:
            statement = statement.where(rag_chunks.c.account_id == account_id)
        normalized_entity_types = [item for item in (entity_types or []) if item]
        if normalized_entity_types:
            statement = statement.where(rag_chunks.c.entity_type.in_(normalized_entity_types))
        with self.engine.connect() as connection:
            rows = connection.execute(statement).mappings().all()
        candidates = [
            RAGSearchResult(
                content=str(row["content"]),
                entity_type=str(row["entity_type"]),
                entity_id=int(row["entity_id"]),
                source_label=str(row["source_label"]),
                long_text_id=int(row["long_text_id"]),
                chunk_index=int(row["chunk_index"]),
                distance=float(row["distance"]),
            )
            for row in rows
        ]
        return rerank_rag_results(query, candidates, top_k, self.reranker)

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
