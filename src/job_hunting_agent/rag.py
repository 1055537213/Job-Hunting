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
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import chromadb
from chromadb.errors import NotFoundError
from langchain_chroma import Chroma
from langchain_core.documents import Document
from langchain_core.embeddings import Embeddings
from langchain_text_splitters import RecursiveCharacterTextSplitter

from .config import (
    DEFAULT_ENV_PATH,
    EmbeddingSettings,
    RerankSettings,
    load_embedding_settings,
    load_rerank_settings,
)
from .models import LongTextRecord, RAGIndexStats, RAGSearchResult


DEFAULT_RAG_COLLECTION = "job_hunting_agent"


class RAGProviderRequestError(RuntimeError):
    """RAG 依赖的远程模型服务请求失败时抛出的统一业务异常。"""


class EmbeddingRequestError(RAGProviderRequestError):
    """调用真实 Embedding 接口失败时抛出。"""


class RerankRequestError(RAGProviderRequestError):
    """调用 Rerank 接口失败时抛出。"""


@dataclass(frozen=True)
class RerankResult:
    """一条 Rerank 输出在候选列表中的位置和相关性分数。"""

    index: int
    relevance_score: float | None = None


class Reranker(Protocol):
    """RAG 重排器的最小接口，避免业务层依赖具体供应商 SDK。"""

    candidate_multiplier: int

    def rerank(self, query: str, documents: list[str], top_n: int) -> list[RerankResult]:
        """按照查询相关性返回候选文本的排序结果。"""


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
        usage_callback: Callable[[dict[str, object]], None] | None = None,
        usage_operation: str = "embedding",
        max_retries: int = 2,
    ):
        """保存 embedding 调用配置。"""

        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout_seconds = timeout_seconds
        self.batch_size = batch_size
        self.dimensions = dimensions
        self.transport = transport or post_embeddings_json
        self.usage_callback = usage_callback
        self.usage_operation = usage_operation
        # 由内部 Model Gateway 传入；0 表示失败后不重试。
        self.max_retries = max(0, max_retries)
        self.embeddings_url = normalize_embeddings_url(base_url)

    @classmethod
    def from_settings(
        cls,
        settings: EmbeddingSettings,
        usage_callback: Callable[[dict[str, object]], None] | None = None,
        usage_operation: str = "embedding",
        max_retries: int = 2,
    ) -> "OpenAICompatibleEmbeddings":
        """从统一配置对象创建 embedding 客户端。"""

        return cls(
            api_key=settings.api_key,
            base_url=settings.base_url,
            model=settings.model,
            timeout_seconds=settings.timeout_seconds,
            batch_size=settings.batch_size,
            dimensions=settings.dimensions,
            usage_callback=usage_callback,
            usage_operation=usage_operation,
            max_retries=max_retries,
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
        response: dict[str, object] | None = None
        for attempt in range(self.max_retries + 1):
            try:
                response = self.transport(
                    self.embeddings_url,
                    {
                        "Authorization": f"Bearer {self.api_key}",
                        "Content-Type": "application/json",
                    },
                    payload,
                    self.timeout_seconds,
                )
                break
            except EmbeddingRequestError:
                if attempt >= self.max_retries:
                    raise
        if response is None:  # pragma: no cover - 防御性兜底，循环应当已成功或抛异常。
            raise EmbeddingRequestError("Embedding API 未返回响应")
        if self.usage_callback is not None:
            try:
                self.usage_callback(response)
            except Exception:  # noqa: BLE001 - 计量旁路不能阻断向量索引。
                pass
        return extract_embedding_vectors(response)


class NativeMultimodalEmbeddings(Embeddings):
    """通用 provider-native 多模态 Embedding 协议的 LangChain 适配器。

    该协议把文本放在 ``input.contents``，并从 ``output.embeddings`` 读取结果；
    供应商标签、模型名和完整端点都由配置提供，业务层不绑定具体厂商。
    """

    def __init__(
        self,
        api_key: str,
        base_url: str,
        model: str,
        timeout_seconds: int = 60,
        batch_size: int = 16,
        transport: Callable[[str, dict[str, str], dict[str, object], int], dict[str, object]] | None = None,
        usage_callback: Callable[[dict[str, object]], None] | None = None,
        usage_operation: str = "embedding",
        max_retries: int = 2,
    ):
        """保存 provider-native 向量模型配置，不在对象或日志中输出密钥。"""

        self.api_key = api_key
        self.endpoint = normalize_native_endpoint(base_url)
        self.model = model
        self.timeout_seconds = timeout_seconds
        self.batch_size = batch_size
        self.transport = transport or post_embeddings_json
        self.usage_callback = usage_callback
        self.usage_operation = usage_operation
        self.max_retries = max(0, max_retries)

    @classmethod
    def from_settings(
        cls,
        settings: EmbeddingSettings,
        usage_callback: Callable[[dict[str, object]], None] | None = None,
        usage_operation: str = "embedding",
        max_retries: int = 2,
    ) -> "NativeMultimodalEmbeddings":
        """从项目 Embedding 配置创建 provider-native 适配器。"""

        return cls(
            api_key=settings.api_key,
            base_url=settings.base_url,
            model=settings.model,
            timeout_seconds=settings.timeout_seconds,
            batch_size=settings.batch_size,
            usage_callback=usage_callback,
            usage_operation=usage_operation,
            max_retries=max_retries,
        )

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        """以 provider-native 多模态输入格式批量生成文本向量。"""

        if not texts:
            return []
        vectors: list[list[float]] = []
        for start in range(0, len(texts), self.batch_size):
            vectors.extend(self._embed_batch(texts[start : start + self.batch_size]))
        return vectors

    def embed_query(self, text: str) -> list[float]:
        """生成单条查询文本的向量。"""

        return self._embed_batch([text])[0]

    def _embed_batch(self, texts: list[str]) -> list[list[float]]:
        """调用 provider-native 多模态接口，并按输入顺序解析向量。"""

        payload: dict[str, object] = {
            "model": self.model,
            # provider-native 多模态协议要求把内容放在 input.contents 中。
            "input": {"contents": [{"text": text} for text in texts]},
        }
        response: dict[str, object] | None = None
        for attempt in range(self.max_retries + 1):
            try:
                response = self.transport(
                    self.endpoint,
                    {
                        "Authorization": f"Bearer {self.api_key}",
                        "Content-Type": "application/json",
                    },
                    payload,
                    self.timeout_seconds,
                )
                break
            except EmbeddingRequestError:
                if attempt >= self.max_retries:
                    raise
        if response is None:  # pragma: no cover - 防御性兜底，循环应当已成功或抛异常。
            raise EmbeddingRequestError("Native Embedding API 未返回响应")
        if self.usage_callback is not None:
            try:
                self.usage_callback(response)
            except Exception:  # noqa: BLE001 - 计量旁路不能阻断 RAG 索引。
                pass
        return extract_native_multimodal_embedding_vectors(response)


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


def normalize_native_endpoint(base_url: str) -> str:
    """规范化 provider-native 配置的完整 HTTP 端点。"""

    normalized = base_url.strip().rstrip("/")
    if not normalized:
        raise ValueError("provider-native API 端点不能为空")
    return normalized


def normalize_rerank_endpoint(base_url: str, api_style: str) -> str:
    """根据协议样式规范化 Rerank HTTP 端点。"""

    normalized = base_url.strip().rstrip("/")
    if not normalized:
        raise ValueError("Rerank API 端点不能为空")
    if api_style == "standard" and not normalized.endswith("/rerank"):
        return f"{normalized}/rerank"
    return normalized


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


def extract_native_multimodal_embedding_vectors(response: dict[str, Any]) -> list[list[float]]:
    """从 provider-native 多模态响应中提取与输入顺序一致的向量。"""

    output = response.get("output")
    embeddings = output.get("embeddings") if isinstance(output, dict) else None
    if not isinstance(embeddings, list) or not embeddings:
        raise EmbeddingRequestError("Native Embedding 响应缺少 output.embeddings")
    vectors_by_index: dict[int, list[float]] = {}
    for default_index, item in enumerate(embeddings):
        if not isinstance(item, dict):
            raise EmbeddingRequestError("Native Embedding 响应条目格式异常")
        raw_index = item.get("text_index", item.get("index", default_index))
        try:
            index = int(raw_index)
        except (TypeError, ValueError) as error:
            raise EmbeddingRequestError("Native Embedding 响应缺少有效索引") from error
        embedding = item.get("embedding")
        if not isinstance(embedding, list) or not all(isinstance(value, (int, float)) for value in embedding):
            raise EmbeddingRequestError("Native Embedding 响应缺少向量")
        vectors_by_index[index] = [float(value) for value in embedding]
    return [vectors_by_index[index] for index in sorted(vectors_by_index)]


def extract_embedding_usage(response: dict[str, Any]) -> dict[str, int]:
    """读取 Embedding 或 Rerank 响应中通用的 usage 字段。"""

    usage = response.get("usage")
    if not isinstance(usage, dict):
        return {}
    input_tokens = int(usage.get("prompt_tokens", usage.get("input_tokens", 0)) or 0)
    output_tokens = int(usage.get("completion_tokens", usage.get("output_tokens", 0)) or 0)
    total_tokens = int(usage.get("total_tokens", input_tokens + output_tokens) or 0)
    return {
        "input_tokens": max(0, input_tokens),
        "output_tokens": max(0, output_tokens),
        "total_tokens": max(0, total_tokens),
    }


def _parse_rerank_results(results: object, response_label: str) -> list[RerankResult]:
    """解析通用重排结果数组，并统一不同协议的分数字段名称。"""

    if not isinstance(results, list):
        raise RerankRequestError(f"{response_label} 响应缺少 results")
    ranked: list[RerankResult] = []
    for item in results:
        if not isinstance(item, dict):
            raise RerankRequestError(f"{response_label} 响应条目格式异常")
        try:
            index = int(item["index"])
        except (KeyError, TypeError, ValueError) as error:
            raise RerankRequestError(f"{response_label} 响应缺少有效索引") from error
        raw_score = item.get("relevance_score", item.get("score"))
        if raw_score is None:
            score = None
        elif isinstance(raw_score, (int, float)):
            score = float(raw_score)
        else:
            raise RerankRequestError(f"{response_label} 响应相关性分数格式异常")
        ranked.append(RerankResult(index=index, relevance_score=score))
    return ranked


def extract_standard_rerank_results(response: dict[str, Any]) -> list[RerankResult]:
    """从常见 `/rerank` 响应中提取候选索引和相关性分数。"""

    return _parse_rerank_results(response.get("results"), "Standard Rerank")


def extract_native_rerank_results(response: dict[str, Any]) -> list[RerankResult]:
    """从 provider-native 响应的 `output.results` 中提取重排结果。"""

    output = response.get("output")
    results = output.get("results") if isinstance(output, dict) else None
    return _parse_rerank_results(results, "Native Rerank")


class HttpReranker:
    """通过配置化 HTTP 协议调用重排模型的 LangChain 无关适配器。"""

    def __init__(
        self,
        api_key: str,
        base_url: str,
        model: str,
        timeout_seconds: int = 60,
        candidate_multiplier: int = 4,
        api_style: str = "standard",
        transport: Callable[[str, dict[str, str], dict[str, object], int], dict[str, object]] | None = None,
        usage_callback: Callable[[dict[str, object]], None] | None = None,
        max_retries: int = 2,
    ):
        """保存 Rerank 调用配置；候选倍数控制向量召回后送入模型的数量。"""

        self.api_key = api_key
        self.api_style = api_style
        self.endpoint = normalize_rerank_endpoint(base_url, api_style)
        self.model = model
        self.timeout_seconds = timeout_seconds
        self.candidate_multiplier = max(1, candidate_multiplier)
        self.transport = transport or post_rerank_json
        self.usage_callback = usage_callback
        self.max_retries = max(0, max_retries)

    @classmethod
    def from_settings(
        cls,
        settings: RerankSettings,
        usage_callback: Callable[[dict[str, object]], None] | None = None,
        max_retries: int = 2,
    ) -> "HttpReranker":
        """从项目 Rerank 配置创建 HTTP 重排适配器。"""

        return cls(
            api_key=settings.api_key,
            base_url=settings.base_url,
            model=settings.model,
            timeout_seconds=settings.timeout_seconds,
            candidate_multiplier=settings.candidate_multiplier,
            api_style=settings.api_style,
            usage_callback=usage_callback,
            max_retries=max_retries,
        )

    def rerank(self, query: str, documents: list[str], top_n: int) -> list[RerankResult]:
        """根据查询重排候选文本，返回原候选列表的索引。"""

        if not query.strip() or not documents or top_n <= 0:
            return []
        if self.api_style == "standard":
            payload: dict[str, object] = {
                "model": self.model,
                "query": query,
                "documents": documents,
                "top_n": min(top_n, len(documents)),
                "return_documents": False,
            }
        elif self.api_style == "native":
            payload = {
                "model": self.model,
                "input": {"query": query, "documents": documents},
                "parameters": {
                    # 调用方已有原始 chunk，关闭正文回传可以减小响应体和隐私暴露面。
                    "return_documents": False,
                    "top_n": min(top_n, len(documents)),
                },
            }
        else:  # pragma: no cover - 配置加载器已提前完成协议归一化。
            raise RerankRequestError(f"不支持的 Rerank API_STYLE：{self.api_style}")
        response: dict[str, object] | None = None
        for attempt in range(self.max_retries + 1):
            try:
                response = self.transport(
                    self.endpoint,
                    {
                        "Authorization": f"Bearer {self.api_key}",
                        "Content-Type": "application/json",
                    },
                    payload,
                    self.timeout_seconds,
                )
                break
            except RerankRequestError:
                if attempt >= self.max_retries:
                    raise
        if response is None:  # pragma: no cover - 防御性兜底，循环应当已成功或抛异常。
            raise RerankRequestError("Rerank API 未返回响应")
        if self.usage_callback is not None:
            try:
                self.usage_callback(response)
            except Exception:  # noqa: BLE001 - 计量旁路不能阻断检索。
                pass
        if self.api_style == "standard":
            return extract_standard_rerank_results(response)
        return extract_native_rerank_results(response)


def post_json(
    url: str,
    headers: dict[str, str],
    payload: dict[str, object],
    timeout: int,
    *,
    error_type: type[RAGProviderRequestError],
    operation_name: str,
) -> dict[str, object]:
    """发送 JSON POST 请求，并把网络或上游 HTTP 错误转换为 RAG 领域异常。"""

    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace")
        raise error_type(f"{operation_name} API HTTP {error.code}: {detail}") from error
    except urllib.error.URLError as error:
        raise error_type(f"{operation_name} API 请求失败：{error.reason}") from error
    except json.JSONDecodeError as error:
        raise error_type(f"{operation_name} API 返回了无效 JSON") from error


def post_embeddings_json(
    url: str,
    headers: dict[str, str],
    payload: dict[str, object],
    timeout: int,
) -> dict[str, object]:
    """发送 embeddings JSON POST 请求。"""

    return post_json(
        url,
        headers,
        payload,
        timeout,
        error_type=EmbeddingRequestError,
        operation_name="Embedding",
    )


def post_rerank_json(
    url: str,
    headers: dict[str, str],
    payload: dict[str, object],
    timeout: int,
) -> dict[str, object]:
    """发送通用 Rerank JSON POST 请求。"""

    return post_json(
        url,
        headers,
        payload,
        timeout,
        error_type=RerankRequestError,
        operation_name="Rerank",
    )


def build_rag_embeddings(
    env_path: str | Path = DEFAULT_ENV_PATH,
    usage_callback: Callable[[dict[str, object]], None] | None = None,
    usage_operation: str = "embedding",
    *,
    settings: EmbeddingSettings | None = None,
    max_retries: int = 2,
) -> Embeddings:
    """根据 `.env` 构造 RAG embedding 实现。

    如果没有配置真实 embedding，就回退到本地 hash embedding，保证测试和离线教学
    场景仍可运行。
    """

    resolved_settings = settings if settings is not None else load_embedding_settings(env_path)
    if resolved_settings is None:
        return LocalHashEmbeddings()
    if resolved_settings.api_style == "local_hash":
        return LocalHashEmbeddings(dimensions=resolved_settings.dimensions or 384)
    if resolved_settings.api_style == "openai_compatible":
        return OpenAICompatibleEmbeddings.from_settings(
            resolved_settings,
            usage_callback=usage_callback,
            usage_operation=usage_operation,
            max_retries=max_retries,
        )
    if resolved_settings.api_style == "native_multimodal":
        return NativeMultimodalEmbeddings.from_settings(
            resolved_settings,
            usage_callback=usage_callback,
            usage_operation=usage_operation,
            max_retries=max_retries,
        )
    raise ValueError(f"暂不支持的 Embedding API_STYLE：{resolved_settings.api_style}")


def build_reranker(
    env_path: str | Path = DEFAULT_ENV_PATH,
    usage_callback: Callable[[dict[str, object]], None] | None = None,
    *,
    settings: RerankSettings | None = None,
    max_retries: int = 2,
) -> Reranker | None:
    """根据 `.env` 构造可选 Rerank 适配器；未配置时保持纯向量检索。"""

    resolved_settings = settings if settings is not None else load_rerank_settings(env_path)
    if resolved_settings is None:
        return None
    if resolved_settings.api_style not in {"standard", "native"}:
        raise ValueError(f"暂不支持的 Rerank API_STYLE：{resolved_settings.api_style}")
    return HttpReranker.from_settings(
        resolved_settings,
        usage_callback=usage_callback,
        max_retries=max_retries,
    )


def resolve_collection_name_for_embeddings(
    persist_directory: str | Path,
    collection_name: str,
    embeddings: Embeddings,
) -> str:
    """在已有向量维度不兼容时，为当前 Embedding 选择独立集合。

    Chroma 的同一个 collection 只能保存一种向量维度。真实模型索引与本地 hash
    fallback 共用持久化目录时，如果仍使用同名 collection，就会在查询或增量写入时
    抛出维度不一致错误。这里仅在能提前得知当前维度且发现冲突时切换集合，既保留原
    索引，也让离线 fallback 可以继续工作。
    """

    expected_dimension = getattr(embeddings, "dimensions", None)
    if not isinstance(expected_dimension, int) or expected_dimension <= 0:
        return collection_name

    existing_dimension = read_collection_dimension(persist_directory, collection_name)
    if existing_dimension is None or existing_dimension == expected_dimension:
        return collection_name

    model = str(getattr(embeddings, "model", "local-hash"))
    identity = f"{type(embeddings).__module__}.{type(embeddings).__qualname__}:{model}:{expected_dimension}"
    identity_suffix = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:8]
    return f"{collection_name}_dim{expected_dimension}_{identity_suffix}"


def read_collection_dimension(
    persist_directory: str | Path,
    collection_name: str,
) -> int | None:
    """读取现有 Chroma 集合首条向量的维度；集合不存在或为空时返回 ``None``。"""

    directory = Path(persist_directory)
    if not directory.exists():
        return None

    client = chromadb.PersistentClient(path=str(directory))
    try:
        collection = client.get_collection(collection_name)
    except NotFoundError:
        return None
    if collection.count() == 0:
        return None

    payload = collection.get(limit=1, include=["embeddings"])
    vectors = payload.get("embeddings")
    if vectors is None or len(vectors) == 0:
        return None
    return len(vectors[0])


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
        usage_callback: Callable[[dict[str, object]], None] | None = None,
        usage_operation: str = "embedding",
        *,
        embedding_settings: EmbeddingSettings | None = None,
        embedding_max_retries: int = 2,
        reranker: Reranker | None = None,
    ):
        """绑定 Chroma 持久化目录、集合名、Embedding 和可选的 Rerank 实现。"""

        self.persist_directory = Path(persist_directory)
        self.embeddings = embeddings or build_rag_embeddings(
            env_path,
            settings=embedding_settings,
            usage_callback=usage_callback,
            usage_operation=usage_operation,
            max_retries=embedding_max_retries,
        )
        self.collection_name = resolve_collection_name_for_embeddings(
            self.persist_directory,
            collection_name,
            self.embeddings,
        )
        self.reranker = reranker

    def rebuild(
        self,
        long_texts: list[LongTextRecord],
        account_id: int | None = None,
    ) -> RAGIndexStats:
        """用 SQLite 长文本重建 Chroma 索引。

        全量重建适合修复索引、切换 embedding 或怀疑 Chroma 与 SQLite 不一致时使用；
        日常对话新增资料优先走 `index_long_texts` 增量追加。
        """

        vector_store = self._vector_store()
        # 账号级重建只能替换当前账号的 chunk；共享 Chroma 集合中其它账号的资料
        # 必须保留。只有 CLI/维护命令明确不传 account_id 时才重置整个集合。
        if account_id is None:
            vector_store.reset_collection()
        else:
            try:
                vector_store.delete(where={"account_id": account_id})
            except (TypeError, ValueError):
                # 兼容旧版 Chroma 不接受 where 的情况：删除当前账号的稳定 chunk ID。
                existing_ids = vector_store.get(where={"account_id": account_id}).get("ids", [])
                if existing_ids:
                    vector_store.delete(ids=existing_ids)
        documents = self._split_documents(self._to_documents(long_texts, account_id=account_id))
        if documents:
            vector_store.add_documents(documents, ids=[document.metadata["chunk_id"] for document in documents])
        return RAGIndexStats(
            document_count=len(long_texts),
            chunk_count=len(documents),
            persist_directory=str(self.persist_directory),
            collection_name=self.collection_name,
            mode="rebuild",
        )

    def index_long_texts(
        self,
        long_texts: list[LongTextRecord],
        account_id: int | None = None,
    ) -> RAGIndexStats:
        """把新增长文本增量追加到现有 Chroma 索引。

        这个方法不会清空集合，只处理传入的长文本。chunk ID 由 `long_text_id` 和
        `chunk_index` 稳定生成；如果同一条长文本被重复索引，会先删除同 ID chunk
        再写入，避免重复记录。
        """

        documents = self._split_documents(self._to_documents(long_texts, account_id=account_id))
        if documents:
            vector_store = self._vector_store()
            ids = [document.metadata["chunk_id"] for document in documents]
            # LangChain Chroma 的 add_documents 最终使用 upsert；稳定 ID 会原位替换旧 chunk。
            # 不先删除可以避免 Embedding 或写入失败时把上一版可用向量提前移除。
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
        account_id: int | None = None,
    ) -> list[RAGSearchResult]:
        """检索相关证据片段，并保留来源 metadata。"""

        if not query.strip():
            return []
        vector_store = self._vector_store()
        # 先多取一些，再在 Python 侧做 entity_type 过滤，避免依赖不同向量库的过滤语法。
        # 启用 Rerank 时按配置扩展候选池，再将重排后的前 top_k 条返回给调用方。
        candidate_multiplier = self.reranker.candidate_multiplier if self.reranker is not None else 3
        candidate_limit = max(top_k * max(1, candidate_multiplier), top_k)
        search_kwargs: dict[str, object] = {"k": candidate_limit}
        if account_id is not None:
            # 账号 metadata 过滤必须在 Chroma 查询层执行，不能先取全局 top-k 再在 Python 侧过滤。
            search_kwargs["filter"] = {"account_id": account_id}
        docs_with_scores = vector_store.similarity_search_with_score(query, **search_kwargs)
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
            if len(results) >= candidate_limit:
                break
        if self.reranker is None:
            return results[:top_k]
        return self._rerank_results(query, results, top_k)

    def _rerank_results(
        self,
        query: str,
        candidates: list[RAGSearchResult],
        top_k: int,
    ) -> list[RAGSearchResult]:
        """将向量召回候选映射回 Rerank 返回的原始索引，保留来源和向量距离。"""

        if not candidates or top_k <= 0 or self.reranker is None:
            return candidates[:top_k]
        rankings = self.reranker.rerank(
            query,
            [candidate.content for candidate in candidates],
            top_n=min(top_k, len(candidates)),
        )
        selected_indices: set[int] = set()
        reranked: list[RAGSearchResult] = []
        for ranking in rankings:
            if ranking.index in selected_indices or not 0 <= ranking.index < len(candidates):
                continue
            selected_indices.add(ranking.index)
            reranked.append(candidates[ranking.index])
            if len(reranked) >= top_k:
                return reranked

        # 上游偶发缺项时仍返回已经可靠召回的证据，避免无故丢失上下文。
        for index, candidate in enumerate(candidates):
            if index not in selected_indices:
                reranked.append(candidate)
            if len(reranked) >= top_k:
                break
        return reranked

    def _vector_store(self) -> Chroma:
        """创建 Chroma 向量库对象。"""

        self.persist_directory.mkdir(parents=True, exist_ok=True)
        return Chroma(
            collection_name=self.collection_name,
            embedding_function=self.embeddings,
            persist_directory=str(self.persist_directory),
        )

    def _to_documents(
        self,
        long_texts: list[LongTextRecord],
        account_id: int | None = None,
    ) -> list[Document]:
        """把 SQLite 长文本记录转换为 LangChain Document。"""

        documents = []
        for record in long_texts:
            if not record.text.strip():
                continue
            resolved_account_id = account_id if account_id is not None else record.account_id
            documents.append(
                Document(
                    page_content=record.text,
                    metadata={
                        "long_text_id": record.id,
                        "entity_type": record.entity_type,
                        "entity_id": record.entity_id,
                        "source_label": record.source_label,
                        "account_id": resolved_account_id if resolved_account_id is not None else -1,
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
