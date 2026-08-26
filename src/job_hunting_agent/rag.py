"""LangChain RAG 支持组件。

本模块只提供 Embedding、Rerank、文本切分和来源元数据工具；唯一的持久化向量后端
是 PostgreSQL + pgvector，长文本事实源也由 PostgreSQL 保存。它不是结构化事实源：
学历、技能、年限等精确事实仍以关系表为准。
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import math
import re
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

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
from .model_resilience import (
    CircuitBreaker,
    is_transient_model_error,
    record_model_call_failure,
)
from .models import LongTextRecord, RAGSearchResult

logger = logging.getLogger(__name__)


class RAGProviderRequestError(RuntimeError):
    """RAG 依赖的远程模型服务请求失败时抛出的统一业务异常。"""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        self.status_code = status_code
        super().__init__(message)


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
        circuit_breaker: CircuitBreaker | None = None,
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
        self.circuit_breaker = circuit_breaker
        self.embeddings_url = normalize_embeddings_url(base_url)

    @classmethod
    def from_settings(
        cls,
        settings: EmbeddingSettings,
        usage_callback: Callable[[dict[str, object]], None] | None = None,
        usage_operation: str = "embedding",
        max_retries: int = 2,
        circuit_breaker: CircuitBreaker | None = None,
    ) -> OpenAICompatibleEmbeddings:
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
            circuit_breaker=circuit_breaker,
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
            if self.circuit_breaker is not None:
                self.circuit_breaker.before_call()
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
            except Exception as error:  # noqa: BLE001 - 统一转换并分类供应商错误。
                retryable = is_transient_model_error(error)
                if self.circuit_breaker is not None:
                    retryable = record_model_call_failure(self.circuit_breaker, error)
                if attempt >= self.max_retries or not retryable:
                    raise
            else:
                if self.circuit_breaker is not None:
                    self.circuit_breaker.record_success()
                break
        if response is None:  # pragma: no cover - 防御性兜底，循环应当已成功或抛异常。
            raise EmbeddingRequestError("Embedding API 未返回响应")
        if self.usage_callback is not None:
            try:
                self.usage_callback(response)
            except Exception as error:  # noqa: BLE001 - 计量旁路不能阻断向量索引。
                logger.debug("Embedding 用量记录失败：%s", type(error).__name__)
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
        dimensions: int | None = None,
        timeout_seconds: int = 60,
        batch_size: int = 16,
        transport: Callable[[str, dict[str, str], dict[str, object], int], dict[str, object]] | None = None,
        usage_callback: Callable[[dict[str, object]], None] | None = None,
        usage_operation: str = "embedding",
        max_retries: int = 2,
        circuit_breaker: CircuitBreaker | None = None,
    ):
        """保存 provider-native 向量模型配置，不在对象或日志中输出密钥。"""

        self.api_key = api_key
        self.endpoint = normalize_native_endpoint(base_url)
        self.model = model
        self.dimensions = dimensions
        self.timeout_seconds = timeout_seconds
        self.batch_size = batch_size
        self.transport = transport or post_embeddings_json
        self.usage_callback = usage_callback
        self.usage_operation = usage_operation
        self.max_retries = max(0, max_retries)
        self.circuit_breaker = circuit_breaker

    @classmethod
    def from_settings(
        cls,
        settings: EmbeddingSettings,
        usage_callback: Callable[[dict[str, object]], None] | None = None,
        usage_operation: str = "embedding",
        max_retries: int = 2,
        circuit_breaker: CircuitBreaker | None = None,
    ) -> NativeMultimodalEmbeddings:
        """从项目 Embedding 配置创建 provider-native 适配器。"""

        return cls(
            api_key=settings.api_key,
            base_url=settings.base_url,
            model=settings.model,
            dimensions=settings.dimensions,
            timeout_seconds=settings.timeout_seconds,
            batch_size=settings.batch_size,
            usage_callback=usage_callback,
            usage_operation=usage_operation,
            max_retries=max_retries,
            circuit_breaker=circuit_breaker,
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

    def embed_images(
        self,
        images: list[tuple[bytes, str]],
    ) -> list[list[float]]:
        """为安全重编码后的图片生成独立向量，与文本查询共享语义空间。"""

        if not images:
            return []
        contents: list[dict[str, str]] = []
        for content, media_type in images:
            if not content:
                raise ValueError("视觉 Embedding 图片不能为空。")
            normalized_media_type = str(media_type or "").lower()
            if normalized_media_type not in {
                "image/jpeg",
                "image/png",
                "image/webp",
                "image/bmp",
                "image/tiff",
            }:
                raise ValueError("视觉 Embedding 图片格式不受支持。")
            encoded = base64.b64encode(content).decode("ascii")
            contents.append(
                {"image": f"data:{normalized_media_type};base64,{encoded}"}
            )
        vectors: list[list[float]] = []
        # qwen3-vl-embedding 官方单次最多 10 张图片；同时服从本地批量上限。
        image_batch_size = min(self.batch_size, 10)
        for start in range(0, len(contents), image_batch_size):
            vectors.extend(self._embed_contents(contents[start : start + image_batch_size]))
        return vectors

    def _embed_batch(self, texts: list[str]) -> list[list[float]]:
        """调用 provider-native 多模态接口，并按输入顺序解析向量。"""

        return self._embed_contents([{"text": text} for text in texts])

    def _embed_contents(self, contents: list[dict[str, str]]) -> list[list[float]]:
        """发送独立文本或图片条目，不启用融合向量模式。"""

        payload: dict[str, object] = {
            "model": self.model,
            # provider-native 多模态协议要求把内容放在 input.contents 中。
            "input": {"contents": contents},
        }
        if self.dimensions is not None:
            payload["parameters"] = {"dimension": self.dimensions}
        response: dict[str, object] | None = None
        for attempt in range(self.max_retries + 1):
            if self.circuit_breaker is not None:
                self.circuit_breaker.before_call()
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
            except Exception as error:  # noqa: BLE001 - 统一转换并分类供应商错误。
                retryable = is_transient_model_error(error)
                if self.circuit_breaker is not None:
                    retryable = record_model_call_failure(self.circuit_breaker, error)
                if attempt >= self.max_retries or not retryable:
                    raise
            else:
                if self.circuit_breaker is not None:
                    self.circuit_breaker.record_success()
                break
        if response is None:  # pragma: no cover - 防御性兜底，循环应当已成功或抛异常。
            raise EmbeddingRequestError("Native Embedding API 未返回响应")
        if self.usage_callback is not None:
            try:
                self.usage_callback(response)
            except Exception as error:  # noqa: BLE001 - 计量旁路不能阻断 RAG 索引。
                logger.debug("原生 Embedding 用量记录失败：%s", type(error).__name__)
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
            raise TypeError("Embedding API data 项格式异常")
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
        circuit_breaker: CircuitBreaker | None = None,
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
        self.circuit_breaker = circuit_breaker

    @classmethod
    def from_settings(
        cls,
        settings: RerankSettings,
        usage_callback: Callable[[dict[str, object]], None] | None = None,
        max_retries: int = 2,
        circuit_breaker: CircuitBreaker | None = None,
    ) -> HttpReranker:
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
            circuit_breaker=circuit_breaker,
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
            if self.circuit_breaker is not None:
                self.circuit_breaker.before_call()
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
            except Exception as error:  # noqa: BLE001 - 统一转换并分类供应商错误。
                retryable = is_transient_model_error(error)
                if self.circuit_breaker is not None:
                    retryable = record_model_call_failure(self.circuit_breaker, error)
                if attempt >= self.max_retries or not retryable:
                    raise
            else:
                if self.circuit_breaker is not None:
                    self.circuit_breaker.record_success()
                break
        if response is None:  # pragma: no cover - 防御性兜底，循环应当已成功或抛异常。
            raise RerankRequestError("Rerank API 未返回响应")
        if self.usage_callback is not None:
            try:
                self.usage_callback(response)
            except Exception as error:  # noqa: BLE001 - 计量旁路不能阻断检索。
                logger.debug("Rerank 用量记录失败：%s", type(error).__name__)
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
        raise error_type(
            f"{operation_name} API HTTP {error.code}: {detail}",
            status_code=error.code,
        ) from error
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
    circuit_breaker: CircuitBreaker | None = None,
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
            circuit_breaker=circuit_breaker,
        )
    if resolved_settings.api_style == "native_multimodal":
        return NativeMultimodalEmbeddings.from_settings(
            resolved_settings,
            usage_callback=usage_callback,
            usage_operation=usage_operation,
            max_retries=max_retries,
            circuit_breaker=circuit_breaker,
        )
    raise ValueError(f"暂不支持的 Embedding API_STYLE：{resolved_settings.api_style}")


def build_reranker(
    env_path: str | Path = DEFAULT_ENV_PATH,
    usage_callback: Callable[[dict[str, object]], None] | None = None,
    *,
    settings: RerankSettings | None = None,
    max_retries: int = 2,
    circuit_breaker: CircuitBreaker | None = None,
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
        circuit_breaker=circuit_breaker,
    )


def rag_embedding_model_name(embeddings: Embeddings) -> str:
    """生成不含密钥的稳定 Embedding 身份，隔离不可直接比较的语义空间。"""

    configured_model = getattr(embeddings, "model", None)
    if isinstance(configured_model, str) and configured_model.strip():
        model_label = configured_model.strip()
    elif isinstance(embeddings, LocalHashEmbeddings):
        model_label = f"local-hash-{embeddings.dimensions}"
    else:
        model_label = f"{type(embeddings).__module__}.{type(embeddings).__qualname__}"
    # 相同模型名经由不同端点时不能默认表示相同模型；端点只参与哈希，不入库明文。
    endpoint = getattr(embeddings, "embeddings_url", None) or getattr(embeddings, "endpoint", None) or ""
    dimensions = getattr(embeddings, "dimensions", None)
    identity_material = "|".join(
        [
            f"{type(embeddings).__module__}.{type(embeddings).__qualname__}",
            model_label,
            str(endpoint),
            str(dimensions),
        ]
    )
    identity_suffix = hashlib.sha256(identity_material.encode("utf-8")).hexdigest()[:16]
    return f"{model_label[:220]}#{identity_suffix}"


def build_rag_documents(
    long_texts: list[LongTextRecord],
    account_id: int | None = None,
) -> list[Document]:
    """把长文本事实源转换为与具体向量后端无关的 LangChain 文档。"""

    documents: list[Document] = []
    for record in long_texts:
        if not record.text.strip():
            continue
        if account_id is not None and record.account_id is not None and record.account_id != account_id:
            # 调用方可以传 account_id 做检索隔离，但不能借此重写已登记材料的真实归属。
            raise ValueError("RAG 索引材料的账号归属与当前请求账号不一致。")
        resolved_account_id = account_id if account_id is not None else record.account_id
        metadata: dict[str, object] = {
            "long_text_id": record.id,
            "entity_type": record.entity_type,
            "entity_id": record.entity_id,
            "source_label": record.source_label,
            # 未登录的历史领域测试允许没有账号归属；Web/生产调用会显式传入账号 ID。
            "account_id": resolved_account_id,
        }
        if record.candidate_id is not None:
            metadata["candidate_id"] = record.candidate_id
        documents.append(Document(page_content=record.text, metadata=metadata))
    return documents


def split_rag_documents(documents: list[Document]) -> list[Document]:
    """按统一规则切分文档，并为每一块生成跨后端稳定的 chunk ID。"""

    splitter = RecursiveCharacterTextSplitter(
        chunk_size=500,
        chunk_overlap=80,
        separators=["\n\n", "\n", "。", "；", "，", " ", ""],
    )
    chunks = splitter.split_documents(documents)
    chunk_indexes_by_long_text: dict[int, int] = {}
    for chunk in chunks:
        long_text_id = int(chunk.metadata["long_text_id"])
        chunk_index = chunk_indexes_by_long_text.get(long_text_id, 0)
        chunk_indexes_by_long_text[long_text_id] = chunk_index + 1
        chunk.metadata["chunk_index"] = chunk_index
        chunk.metadata["chunk_id"] = f"long-text-{long_text_id}-chunk-{chunk_index}"
    return chunks


def rerank_rag_results(
    query: str,
    candidates: list[RAGSearchResult],
    top_k: int,
    reranker: Reranker | None,
) -> list[RAGSearchResult]:
    """把可选 Rerank 输出映射回向量召回结果，并保留来源与距离。"""

    if not candidates or top_k <= 0 or reranker is None:
        return candidates[:top_k]
    rankings = reranker.rerank(
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
