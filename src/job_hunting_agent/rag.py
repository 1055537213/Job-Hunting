"""LangChain + Chroma RAG 知识库。

RAG 层负责把 SQLite `long_texts` 中的长文本材料同步到本地持久化 Chroma，
并提供带来源的语义检索结果。它不是事实源：学历、技能、年限等精确事实仍以
SQLite 结构化表为准；向量库只帮助找到“可能相关的证据片段”。
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any

from langchain_chroma import Chroma
from langchain_core.documents import Document
from langchain_core.embeddings import Embeddings
from langchain_text_splitters import RecursiveCharacterTextSplitter

from .config import DEFAULT_ENV_PATH, EmbeddingSettings, load_embedding_settings
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


class OpenAICompatibleEmbeddings(Embeddings):
    """OpenAI-compatible embeddings 适配器。

    许多向量模型供应商都兼容 OpenAI Embeddings 接口形态。这里保持一个最小实现：
    业务层只依赖 `Embeddings` 接口，不绑死具体 SDK。
    """

    def __init__(
        self,
        api_key: str,
        base_url: str,
        model: str,
        timeout_seconds: int = 60,
        batch_size: int = 64,
        dimensions: int | None = None,
        transport: Callable[[str, dict[str, str], dict[str, object], int], dict[str, object]] | None = None,
    ):
        """保存 embedding 调用配置。"""

        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout_seconds = timeout_seconds
        self.batch_size = batch_size
        self.dimensions = dimensions
        self.transport = transport or post_embeddings_json
        self.embeddings_url = normalize_embeddings_url(base_url)

    @classmethod
    def from_settings(cls, settings: EmbeddingSettings) -> "OpenAICompatibleEmbeddings":
        """从统一配置对象创建 embedding 客户端。"""

        return cls(
            api_key=settings.api_key,
            base_url=settings.base_url,
            model=settings.model,
            timeout_seconds=settings.timeout_seconds,
            batch_size=settings.batch_size,
            dimensions=settings.dimensions,
        )

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        """调用真实 embeddings API 批量生成向量。"""

        if not texts:
            return []
        vectors: list[list[float]] = []
        for start in range(0, len(texts), self.batch_size):
            vectors.extend(self._embed_batch(texts[start : start + self.batch_size]))
        return vectors

    def embed_query(self, text: str) -> list[float]:
        """生成单条查询向量。"""

        vectors = self._embed_batch([text])
        return vectors[0]

    def _embed_batch(self, texts: list[str]) -> list[list[float]]:
        """调用一次 OpenAI-compatible embeddings 接口。"""

        payload: dict[str, object] = {
            "model": self.model,
            "input": texts,
            "encoding_format": "float",
        }
        # OpenAI text-embedding-3 系列支持 dimensions；其它兼容供应商通常会忽略未知字段或自行报错。
        if self.dimensions is not None:
            payload["dimensions"] = self.dimensions
        response = self.transport(
            self.embeddings_url,
            {
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            payload,
            self.timeout_seconds,
        )
        return extract_embedding_vectors(response)


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


def normalize_embeddings_url(base_url: str) -> str:
    """把 `.env` 中的 base URL 转成 embeddings URL。"""

    stripped = base_url.rstrip("/")
    if stripped.endswith("/embeddings"):
        return stripped
    return f"{stripped}/embeddings"


def extract_embedding_vectors(response: dict[str, Any]) -> list[list[float]]:
    """从 OpenAI-compatible embeddings 响应中提取向量列表。"""

    data = response.get("data")
    if not isinstance(data, list) or not data:
        raise ValueError("Embedding API 响应缺少 data")
    vectors_by_index: dict[int, list[float]] = {}
    for item in data:
        if not isinstance(item, dict):
            raise ValueError("Embedding API data 项格式异常")
        index = int(item.get("index", len(vectors_by_index)))
        embedding = item.get("embedding")
        if not isinstance(embedding, list) or not all(isinstance(value, (int, float)) for value in embedding):
            raise ValueError("Embedding API 响应缺少 embedding 向量")
        vectors_by_index[index] = [float(value) for value in embedding]
    return [vectors_by_index[index] for index in sorted(vectors_by_index)]


class EmbeddingRequestError(RuntimeError):
    """调用真实 embedding 接口失败时抛出的业务异常。"""


def post_embeddings_json(
    url: str,
    headers: dict[str, str],
    payload: dict[str, object],
    timeout: int,
) -> dict[str, object]:
    """发送 embeddings JSON POST 请求。"""

    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace")
        raise EmbeddingRequestError(f"Embedding API HTTP {error.code}: {detail}") from error
    except urllib.error.URLError as error:
        raise EmbeddingRequestError(f"Embedding API 请求失败：{error.reason}") from error


def build_rag_embeddings(
    env_path: str | Path = DEFAULT_ENV_PATH,
) -> Embeddings:
    """根据 `.env` 构造 RAG embedding 实现。

    如果没有配置真实 embedding，就回退到本地 hash embedding，保证测试和离线教学
    场景仍可运行。
    """

    settings = load_embedding_settings(env_path)
    if settings is None:
        return LocalHashEmbeddings()
    if settings.provider in {"local", "local_hash"}:
        return LocalHashEmbeddings(dimensions=settings.dimensions or 384)
    if settings.provider in {"openai", "openai_compatible"}:
        return OpenAICompatibleEmbeddings.from_settings(settings)
    raise ValueError(f"暂不支持的 embedding provider：{settings.provider}")


class RAGKnowledgeBase:
    """本地持久化 RAG 知识库门面。

    外部只需要关心“重建索引”“追加索引”和“检索证据”。Chroma、文本切分、
    metadata 规范都封装在这里，避免这些细节散落到 App/CLI。
    """

    def __init__(
        self,
        persist_directory: str | Path = "data/chroma",
        collection_name: str = DEFAULT_RAG_COLLECTION,
        embeddings: Embeddings | None = None,
        env_path: str | Path = DEFAULT_ENV_PATH,
    ):
        """绑定 Chroma 持久化目录、集合名和 embedding 实现。"""

        self.persist_directory = Path(persist_directory)
        self.collection_name = collection_name
        self.embeddings = embeddings or build_rag_embeddings(env_path)

    def rebuild(self, long_texts: list[LongTextRecord]) -> RAGIndexStats:
        """用 SQLite 长文本重建 Chroma 索引。

        全量重建适合修复索引、切换 embedding 或怀疑 Chroma 与 SQLite 不一致时使用；
        日常对话新增资料优先走 `index_long_texts` 增量追加。
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
            mode="rebuild",
        )

    def index_long_texts(self, long_texts: list[LongTextRecord]) -> RAGIndexStats:
        """把新增长文本增量追加到现有 Chroma 索引。

        这个方法不会清空集合，只处理传入的长文本。chunk ID 由 `long_text_id` 和
        `chunk_index` 稳定生成；如果同一条长文本被重复索引，会先删除同 ID chunk
        再写入，避免重复记录。
        """

        documents = self._split_documents(self._to_documents(long_texts))
        if documents:
            vector_store = self._vector_store()
            ids = [document.metadata["chunk_id"] for document in documents]
            # Chroma 的 add 是追加语义；先按稳定 ID 删除，保证重复调用不会制造重复 chunk。
            vector_store.delete(ids=ids)
            vector_store.add_documents(documents, ids=ids)
        return RAGIndexStats(
            document_count=len(long_texts),
            chunk_count=len(documents),
            persist_directory=str(self.persist_directory),
            collection_name=self.collection_name,
            mode="incremental",
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
        chunk_indexes_by_long_text: dict[int, int] = {}
        for chunk in chunks:
            long_text_id = chunk.metadata["long_text_id"]
            chunk_index = chunk_indexes_by_long_text.get(long_text_id, 0)
            chunk_indexes_by_long_text[long_text_id] = chunk_index + 1
            chunk.metadata["chunk_index"] = chunk_index
            chunk.metadata["chunk_id"] = f"long-text-{long_text_id}-chunk-{chunk_index}"
        return chunks
