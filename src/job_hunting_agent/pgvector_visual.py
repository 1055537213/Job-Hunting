"""项目视觉知识项的对象读取、图片向量化和 pgvector 写入。"""

from __future__ import annotations

import hashlib
import math
from datetime import UTC, datetime

import sqlalchemy as sa
from langchain_core.embeddings import Embeddings
from sqlalchemy.engine import Engine

from .database_schema import visual_knowledge_items
from .models import RAGIndexStats, VisualKnowledgeItemRecord
from .object_storage import ObjectStorage
from .rag import rag_embedding_model_name

VISUAL_COLLECTION_NAME = "visual_knowledge_items"
VISUAL_PERSISTENCE_LABEL = "postgresql+pgvector"
MAX_VISUAL_EMBEDDING_BYTES = 10 * 1024 * 1024


class PgVectorVisualKnowledgeBase:
    """把对象存储中的安全视觉副本同步成账号隔离的图片向量。"""

    def __init__(
        self,
        engine: Engine,
        object_storage: ObjectStorage,
        embeddings: Embeddings,
    ) -> None:
        if engine.dialect.name != "postgresql":
            raise ValueError("视觉知识索引只能连接 PostgreSQL 数据库。")
        self.engine = engine
        self.object_storage = object_storage
        self.embeddings = embeddings

    def index_items(
        self,
        items: list[VisualKnowledgeItemRecord],
        *,
        account_id: int,
    ) -> RAGIndexStats:
        """按稳定视觉项 ID 覆盖写入向量，重复任务不会创建第二份结果。"""

        if any(item.account_id != account_id for item in items):
            raise ValueError("视觉知识项与索引任务账号不一致。")
        if not items:
            return self._stats(0)
        try:
            embed_images = getattr(self.embeddings, "embed_images", None)
            if not callable(embed_images):
                raise ValueError("当前 Embedding 配置不支持图片向量。")
            payloads: list[tuple[bytes, str]] = []
            for item in items:
                content = self.object_storage.read(item.storage_key)
                if not content or len(content) != item.file_size:
                    raise ValueError("视觉知识对象大小与数据库记录不一致。")
                if len(content) > MAX_VISUAL_EMBEDDING_BYTES:
                    raise ValueError("视觉知识对象超过图片向量接口的大小限制。")
                if hashlib.sha256(content).hexdigest() != item.sha256:
                    raise ValueError("视觉知识对象摘要与数据库记录不一致。")
                payloads.append((content, item.media_type))
            vectors = embed_images(payloads)
            if len(vectors) != len(items):
                raise ValueError("图片 Embedding 返回的向量数量与视觉知识项不一致。")

            model_name = rag_embedding_model_name(self.embeddings)
            now = datetime.now(UTC)
            with self.engine.begin() as connection:
                for item, vector in zip(items, vectors, strict=True):
                    normalized = self._validate_vector(vector)
                    result = connection.execute(
                        sa.update(visual_knowledge_items)
                        .where(
                            visual_knowledge_items.c.id == item.id,
                            visual_knowledge_items.c.account_id == account_id,
                        )
                        .values(
                            embedding=normalized,
                            embedding_model=model_name,
                            embedding_dimensions=len(normalized),
                            index_status="indexed",
                            index_error_type=None,
                            updated_at=now,
                        )
                    )
                    if result.rowcount != 1:
                        raise ValueError("视觉知识项在索引期间已被删除或转移。")
        except Exception as error:
            self._mark_failed([item.id for item in items], account_id, type(error).__name__)
            raise
        return self._stats(len(items))

    def _mark_failed(
        self,
        item_ids: list[int],
        account_id: int,
        error_type: str,
    ) -> None:
        """只保存异常类型，不把供应商响应或对象内容写入数据库。"""

        if not item_ids:
            return
        with self.engine.begin() as connection:
            connection.execute(
                sa.update(visual_knowledge_items)
                .where(
                    visual_knowledge_items.c.id.in_(item_ids),
                    visual_knowledge_items.c.account_id == account_id,
                )
                .values(
                    index_status="failed",
                    index_error_type=str(error_type)[:128],
                    updated_at=datetime.now(UTC),
                )
            )

    @staticmethod
    def _validate_vector(vector: list[float]) -> list[float]:
        normalized = [float(value) for value in vector]
        if not normalized:
            raise ValueError("图片 Embedding 返回了空向量。")
        if not all(math.isfinite(value) for value in normalized):
            raise ValueError("图片 Embedding 包含非有限数值。")
        return normalized

    @staticmethod
    def _stats(item_count: int) -> RAGIndexStats:
        return RAGIndexStats(
            document_count=item_count,
            chunk_count=item_count,
            persist_directory=VISUAL_PERSISTENCE_LABEL,
            collection_name=VISUAL_COLLECTION_NAME,
            mode="incremental",
        )
