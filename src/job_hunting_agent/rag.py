"""LangChain + Chroma RAG 知识库。

RAG 层负责把 SQLite `long_texts` 中的长文本材料同步到本地持久化 Chroma，
并提供带来源的语义检索结果。它不是事实源：学历、技能、年限等精确事实仍以
SQLite 结构化表为准；向量库只帮助找到“可能相关的证据片段”。
"""

from __future__ import annotations

import hashlib
import math
import re
from pathlib import Path

from langchain_chroma import Chroma
from langchain_core.documents import Document
from langchain_core.embeddings import Embeddings
from langchain_text_splitters import RecursiveCharacterTextSplitter

from .models import LongTextRecord, RAGIndexStats, RAGSearchResult


DEFAULT_RAG_COLLECTION = "job_hunting_agent"


class LocalHashEmbeddings(Embeddings):
    """本地确定性 embedding，适合 MVP、测试和离线教学。

    它实现了 LangChain 的 `Embeddings` 接口，因此以后可以替换为真实 embedding
    模型，而不改 RAG 索引和业务调用代码。注意：它只是关键词/字符哈希近似，
    语义质量不等同于真正 embedding 模型。
    """

    def __init__(self, dimensions: int = 384):
        """设置向量维度。"""

        self.dimensions = dimensions

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        """把多段文本转成向量。"""

        return [self._embed(text) for text in texts]

    def embed_query(self, text: str) -> list[float]:
        """把查询文本转成向量。"""

        return self._embed(text)

    def _embed(self, text: str) -> list[float]:
        """用 token 哈希构造归一化向量。"""

        vector = [0.0] * self.dimensions
        for token in tokenize(text):
            index = stable_hash(token) % self.dimensions
            vector[index] += 1.0
        norm = math.sqrt(sum(value * value for value in vector)) or 1.0
        return [value / norm for value in vector]


def tokenize(text: str) -> list[str]:
    """生成中英文都能覆盖的轻量 token。

    英文按词切，中文按单字和相邻双字切；这样查询“职位解析”能和包含同样词片段
    的项目摘要产生基本重合。
    """

    lowered = text.lower()
    latin_tokens = re.findall(r"[a-z0-9_+#.\-]+", lowered)
    chinese_chars = re.findall(r"[\u4e00-\u9fff]", lowered)
    chinese_bigrams = [
        "".join(chinese_chars[index : index + 2])
        for index in range(max(0, len(chinese_chars) - 1))
    ]
    return latin_tokens + chinese_chars + chinese_bigrams


def stable_hash(token: str) -> int:
    """返回稳定哈希，避免 Python 内置 hash 的随机种子影响测试结果。"""

    return int(hashlib.sha256(token.encode("utf-8")).hexdigest(), 16)


class RAGKnowledgeBase:
    """本地持久化 RAG 知识库门面。

    外部只需要关心“重建索引”和“检索证据”。Chroma、文本切分、metadata 规范都
    封装在这里，避免这些细节散落到 App/CLI。
    """

    def __init__(
        self,
        persist_directory: str | Path = "data/chroma",
        collection_name: str = DEFAULT_RAG_COLLECTION,
        embeddings: Embeddings | None = None,
    ):
        """绑定 Chroma 持久化目录、集合名和 embedding 实现。"""

        self.persist_directory = Path(persist_directory)
        self.collection_name = collection_name
        self.embeddings = embeddings or LocalHashEmbeddings()

    def rebuild(self, long_texts: list[LongTextRecord]) -> RAGIndexStats:
        """用 SQLite 长文本重建 Chroma 索引。

        第一版采用全量重建，简单可靠；后续数据量变大时再做增量同步。
        """

        vector_store = self._vector_store()
        # reset_collection 会清空当前集合，但不删除整个持久化目录，避免误删其它数据。
        vector_store.reset_collection()
        documents = self._split_documents(self._to_documents(long_texts))
        if documents:
            vector_store.add_documents(documents, ids=[document.metadata["chunk_id"] for document in documents])
        return RAGIndexStats(
            document_count=len(long_texts),
            chunk_count=len(documents),
            persist_directory=str(self.persist_directory),
            collection_name=self.collection_name,
        )

    def search(
        self,
        query: str,
        top_k: int = 5,
        entity_types: list[str] | None = None,
    ) -> list[RAGSearchResult]:
        """检索相关证据片段，并保留来源 metadata。"""

        if not query.strip():
            return []
        vector_store = self._vector_store()
        # 先多取一些，再在 Python 侧做 entity_type 过滤，避免依赖不同向量库的过滤语法。
        docs_with_scores = vector_store.similarity_search_with_score(query, k=max(top_k * 3, top_k))
        results: list[RAGSearchResult] = []
        allowed = set(entity_types or [])
        for document, distance in docs_with_scores:
            metadata = document.metadata
            if allowed and metadata.get("entity_type") not in allowed:
                continue
            results.append(
                RAGSearchResult(
                    content=document.page_content,
                    entity_type=str(metadata.get("entity_type", "")),
                    entity_id=int(metadata.get("entity_id", 0)),
                    source_label=str(metadata.get("source_label", "")),
                    long_text_id=int(metadata.get("long_text_id", 0)),
                    chunk_index=int(metadata.get("chunk_index", 0)),
                    distance=float(distance),
                )
            )
            if len(results) >= top_k:
                break
        return results

    def _vector_store(self) -> Chroma:
        """创建 Chroma 向量库对象。"""

        self.persist_directory.mkdir(parents=True, exist_ok=True)
        return Chroma(
            collection_name=self.collection_name,
            embedding_function=self.embeddings,
            persist_directory=str(self.persist_directory),
        )

    def _to_documents(self, long_texts: list[LongTextRecord]) -> list[Document]:
        """把 SQLite 长文本记录转换为 LangChain Document。"""

        documents = []
        for record in long_texts:
            if not record.text.strip():
                continue
            documents.append(
                Document(
                    page_content=record.text,
                    metadata={
                        "long_text_id": record.id,
                        "entity_type": record.entity_type,
                        "entity_id": record.entity_id,
                        "source_label": record.source_label,
                    },
                )
            )
        return documents

    def _split_documents(self, documents: list[Document]) -> list[Document]:
        """使用 LangChain 文本切分器切分文档，并补充 chunk metadata。"""

        splitter = RecursiveCharacterTextSplitter(
            chunk_size=500,
            chunk_overlap=80,
            separators=["\n\n", "\n", "。", "；", "，", " ", ""],
        )
        chunks = splitter.split_documents(documents)
        for index, chunk in enumerate(chunks):
            long_text_id = chunk.metadata["long_text_id"]
            chunk.metadata["chunk_index"] = index
            chunk.metadata["chunk_id"] = f"long-text-{long_text_id}-chunk-{index}"
        return chunks
