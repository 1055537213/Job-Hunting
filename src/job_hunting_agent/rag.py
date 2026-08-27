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
            except Exception as error:
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
            except Exception as error:
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
            except Exception as error:
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


RAG_CHUNKING_VERSION = "semantic-v1"
# 这是软目标，不是切分边界；只有结构化块或句子超过硬上限时才会继续拆分。
RAG_CHUNK_TARGET_CHARACTERS = 900
RAG_CHUNK_MAX_CHARACTERS = 1_400

_MARKDOWN_HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s+(.+?)\s*#*\s*$")
_LIST_ITEM_RE = re.compile(r"^\s*(?:[-*+]\s+|\d+[.)]\s+|[一二三四五六七八九十]+[、.)]\s*)")
_FENCE_RE = re.compile(r"^\s*(```+|~~~+)")
_PAGE_MARKER_RE = re.compile(
    r"^\s*(?:\[page\s*=\s*(\d+)\]|\[第\s*(\d+)\s*页\])\s*$",
    re.IGNORECASE,
)
_SECTION_MARKER_RE = re.compile(r"^\s*\[([^\[\]\n]{1,80})\]\s*$")
_TABLE_SEPARATOR_RE = re.compile(r"^\s*\|?\s*:?-{3,}:?\s*(?:\|\s*:?-{3,}:?\s*)+\|?\s*$")
_PLAIN_SECTION_LABELS = frozenset(
    {
        "个人信息",
        "求职意向",
        "自我介绍",
        "自我评价",
        "教育背景",
        "教育经历",
        "工作经历",
        "实习经历",
        "职业经历",
        "项目经历",
        "项目背景",
        "项目描述",
        "项目职责",
        "个人职责",
        "主要职责",
        "工作内容",
        "技术栈",
        "专业技能",
        "技能清单",
        "核心功能",
        "实现方案",
        "项目亮点",
        "项目成果",
        "证书",
        "奖项",
        "职位描述",
        "岗位职责",
        "任职要求",
        "任职资格",
        "福利待遇",
        "公司介绍",
    }
)


@dataclass(frozen=True)
class _SemanticBlock:
    """切分器内部的结构化语义块；不暴露给 RAG 调用方。"""

    content: str
    semantic_type: str
    section_title: str | None
    heading: str | None
    source_page: int | None


def split_rag_documents(documents: list[Document]) -> list[Document]:
    """按语义边界优先、长度兜底的规则切分文档。

    外部接口保持为一个纯函数：调用方不需要知道 Markdown、OCR、项目材料和
    对话文本分别如何处理。实现先识别结构化语义块，再合并相邻短块，最后只对
    仍然超长的块按句子/行/字符递归拆分。
    """

    chunks: list[Document] = []
    chunk_indexes_by_long_text: dict[int, int] = {}
    for document in documents:
        blocks = _merge_semantic_blocks(_extract_semantic_blocks(document))
        for block_index, block in enumerate(blocks):
            fragments = _split_semantic_block(block)
            for fragment_index, content in enumerate(fragments):
                metadata = dict(document.metadata)
                long_text_id = int(metadata["long_text_id"])
                chunk_index = chunk_indexes_by_long_text.get(long_text_id, 0)
                chunk_indexes_by_long_text[long_text_id] = chunk_index + 1
                metadata.update(
                    {
                        "chunk_index": chunk_index,
                        "chunk_id": f"long-text-{long_text_id}-chunk-{chunk_index}",
                        "chunking_version": RAG_CHUNKING_VERSION,
                        "block_index": block_index,
                        "semantic_type": block.semantic_type,
                    }
                )
                if block.section_title is not None:
                    metadata["section_title"] = block.section_title
                if block.source_page is not None:
                    metadata["source_page"] = block.source_page
                if len(fragments) > 1:
                    metadata["fragment_index"] = fragment_index
                    metadata["fragment_count"] = len(fragments)
                chunks.append(Document(page_content=content, metadata=metadata))
    return chunks


def _extract_semantic_blocks(document: Document) -> list[_SemanticBlock]:
    """识别标题、段落、列表、代码、表格和页面边界。"""

    text = document.page_content.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not text:
        return []
    lines = text.split("\n")
    metadata = document.metadata
    source_page = _source_page_from_metadata(metadata)
    section_title: str | None = None
    heading: str | None = None
    blocks: list[_SemanticBlock] = []
    index = 0

    while index < len(lines):
        line = lines[index]
        if not line.strip():
            index += 1
            continue
        page_match = _PAGE_MARKER_RE.match(line)
        if page_match:
            source_page = int(page_match.group(1) or page_match.group(2))
            index += 1
            continue

        section_match = _SECTION_MARKER_RE.match(line)
        if section_match:
            section_title = section_match.group(1).strip()
            heading = line.strip()
            index += 1
            if index >= len(lines) or not any(item.strip() for item in lines[index:]):
                blocks.append(_SemanticBlock(heading, "heading", section_title, heading, source_page))
            continue

        heading_match = _MARKDOWN_HEADING_RE.match(line)
        if heading_match:
            section_title = heading_match.group(1).strip()
            heading = line.strip()
            index += 1
            # 标题只作为下一个语义块的上下文；没有正文时仍保留标题本身。
            if index >= len(lines) or not any(item.strip() for item in lines[index:]):
                blocks.append(_SemanticBlock(heading, "heading", section_title, heading, source_page))
            continue

        plain_section_title = _plain_section_title(line)
        if plain_section_title is not None:
            section_title = plain_section_title
            heading = line.strip()
            index += 1
            if index >= len(lines) or not any(item.strip() for item in lines[index:]):
                blocks.append(_SemanticBlock(heading, "heading", section_title, heading, source_page))
            continue

        fence_match = _FENCE_RE.match(line)
        if fence_match:
            fence = fence_match.group(1)[0]
            end = index + 1
            while end < len(lines) and not re.match(rf"^\s*{re.escape(fence)}{{3,}}\s*$", lines[end]):
                end += 1
            if end < len(lines):
                end += 1
            blocks.append(
                _make_semantic_block(
                    lines[index:end],
                    "code_block",
                    section_title,
                    heading,
                    source_page,
                )
            )
            index = end
            continue

        if _looks_like_table(lines, index):
            end = index + 1
            while end < len(lines) and lines[end].strip() and _is_table_row(lines[end]):
                end += 1
            blocks.append(
                _make_semantic_block(
                    lines[index:end],
                    "table",
                    section_title,
                    heading,
                    source_page,
                )
            )
            index = end
            continue

        if _LIST_ITEM_RE.match(line):
            end = index + 1
            while end < len(lines):
                candidate = lines[end]
                if not candidate.strip():
                    break
                if _LIST_ITEM_RE.match(candidate) or candidate.startswith((" ", "\t")):
                    end += 1
                    continue
                break
            blocks.append(
                _make_semantic_block(
                    lines[index:end],
                    "bullet_list",
                    section_title,
                    heading,
                    source_page,
                )
            )
            index = end
            continue

        end = index + 1
        while end < len(lines):
            candidate = lines[end]
            if (
                not candidate.strip()
                or _PAGE_MARKER_RE.match(candidate)
                or _SECTION_MARKER_RE.match(candidate)
                or _MARKDOWN_HEADING_RE.match(candidate)
                or _plain_section_title(candidate) is not None
                or _FENCE_RE.match(candidate)
                or _LIST_ITEM_RE.match(candidate)
                or _looks_like_table(lines, end)
            ):
                break
            end += 1
        blocks.append(
            _make_semantic_block(
                lines[index:end],
                "paragraph",
                section_title,
                heading,
                source_page,
            )
        )
        index = end

    return blocks


def _make_semantic_block(
    lines: list[str],
    semantic_type: str,
    section_title: str | None,
    heading: str | None,
    source_page: int | None,
) -> _SemanticBlock:
    """生成带章节上下文的语义块，并去掉 OCR 常见的行尾空白。"""

    body = "\n".join(line.rstrip() for line in lines).strip()
    if heading and body != heading:
        content = f"{heading}\n{body}"
    else:
        content = body
    return _SemanticBlock(content, semantic_type, section_title, heading, source_page)


def _source_page_from_metadata(metadata: dict[str, object]) -> int | None:
    """从来源元数据或 ``#page=N`` 标签中恢复页码。"""

    for key in ("source_page", "page_number"):
        value = metadata.get(key)
        if value is not None and str(value).isdigit():
            return int(str(value))
    source_label = str(metadata.get("source_label") or "")
    match = re.search(r"#page\s*=\s*(\d+)", source_label, re.IGNORECASE)
    return int(match.group(1)) if match else None


def _plain_section_title(line: str) -> str | None:
    """识别简历、职位和项目材料中的常见无 Markdown 章节标题。"""

    candidate = line.strip().rstrip("：:").strip()
    candidate = re.sub(r"^(?:\d+[.)、]|[一二三四五六七八九十]+[、.)])\s*", "", candidate)
    return candidate if candidate in _PLAIN_SECTION_LABELS else None


def _looks_like_table(lines: list[str], index: int) -> bool:
    """识别 Markdown 或项目提取器产生的制表符表格。"""

    if index + 1 >= len(lines):
        return False
    if "|" in lines[index] and _TABLE_SEPARATOR_RE.match(lines[index + 1]):
        return True
    return "\t" in lines[index] and "\t" in lines[index + 1]


def _is_table_row(line: str) -> bool:
    return "|" in line or "\t" in line


def _merge_semantic_blocks(blocks: list[_SemanticBlock]) -> list[_SemanticBlock]:
    """合并同一章节内的相邻短段落，避免一个事实被过度碎片化。"""

    merged: list[_SemanticBlock] = []
    for block in blocks:
        if not merged:
            merged.append(block)
            continue
        previous = merged[-1]
        compatible_type = previous.semantic_type == block.semantic_type == "paragraph"
        compatible_heading = previous.heading == block.heading
        compatible_section = previous.section_title == block.section_title
        compatible_page = previous.source_page == block.source_page
        combined_length = len(previous.content) + len(block.content) + 2
        if compatible_type and compatible_heading and compatible_section and compatible_page and combined_length <= RAG_CHUNK_TARGET_CHARACTERS:
            body = _block_body(previous) + "\n\n" + _block_body(block)
            merged[-1] = _SemanticBlock(
                _with_heading(body, previous.heading),
                "paragraph",
                previous.section_title,
                previous.heading,
                previous.source_page,
            )
        elif previous.semantic_type == "heading" and compatible_section:
            merged[-1] = _SemanticBlock(
                block.content,
                block.semantic_type,
                block.section_title,
                block.heading,
                block.source_page,
            )
        else:
            merged.append(block)
    return merged


def _block_body(block: _SemanticBlock) -> str:
    """移除重复的章节标题，供相邻段落合并使用。"""

    if block.heading and block.content.startswith(f"{block.heading}\n"):
        return block.content[len(block.heading) + 1 :]
    return block.content


def _with_heading(body: str, heading: str | None) -> str:
    return f"{heading}\n{body}" if heading else body


def _split_semantic_block(block: _SemanticBlock) -> list[str]:
    """只在结构化块过长时拆分，并尽量保留标题、表头和代码围栏。"""

    if len(block.content) <= RAG_CHUNK_MAX_CHARACTERS:
        return [block.content]
    heading = block.heading
    body = _block_body(block)
    prefix_length = len(heading) + 1 if heading else 0
    budget = RAG_CHUNK_MAX_CHARACTERS - prefix_length
    # 极端长标题若继续复制到每个分片，会挤占全部正文预算并突破硬上限。
    # 此时退化为对完整块做一次硬切，保留全部内容但不重复病态前缀。
    if budget < 80:
        return _hard_split(block.content, RAG_CHUNK_MAX_CHARACTERS)
    if block.semantic_type == "table":
        fragments = _split_table_body(body, budget)
    elif block.semantic_type == "code_block":
        fragments = _split_code_body(body, budget)
    elif block.semantic_type == "bullet_list":
        fragments = _pack_units(body.splitlines(), budget, joiner="\n")
    else:
        fragments = _pack_units(_sentence_units(body), budget, joiner=" ")
    contents = [_with_heading(fragment, heading) for fragment in fragments if fragment.strip()]
    # 表头、代码围栏等结构前缀也可能异常超长；最终出口再次执行硬上限兜底。
    return [
        part
        for content in contents
        for part in _hard_split(content, RAG_CHUNK_MAX_CHARACTERS)
        if part.strip()
    ]


def _sentence_units(text: str) -> list[str]:
    """按中英文句末标点切分，不把句子中间的逗号当成首选边界。"""

    units: list[str] = []
    start = 0
    index = 0
    while index < len(text):
        character = text[index]
        is_english_period = character == "." and (index + 1 == len(text) or text[index + 1].isspace())
        if character in "。！？!?；;" or is_english_period:
            end = index + 1
            while end < len(text) and text[end] in "\"'”’」』】）》)":
                end += 1
            unit = text[start:end].strip()
            if unit:
                units.append(unit)
            start = end
            index = end
            continue
        index += 1
    tail = text[start:].strip()
    if tail:
        units.append(tail)
    return units or [text.strip()]


def _pack_units(units: list[str], budget: int, *, joiner: str) -> list[str]:
    """在自然单元之间打包，单元本身过长时才递归硬切。"""

    packed: list[str] = []
    current = ""
    for unit in units:
        for part in _hard_split(unit, budget):
            candidate = f"{current}{joiner if current else ''}{part}"
            if current and len(candidate) > min(RAG_CHUNK_TARGET_CHARACTERS, budget):
                packed.append(current.strip())
                current = part
            else:
                current = candidate
    if current.strip():
        packed.append(current.strip())
    return packed


def _hard_split(text: str, budget: int) -> list[str]:
    """对单个异常超长句子使用标点、空白和最终字符边界兜底。"""

    remaining = text.strip()
    parts: list[str] = []
    while len(remaining) > budget:
        window = remaining[: budget + 1]
        cut = max((window.rfind(marker) for marker in ("，", ",", "、", "：", ":", " ", "\n")), default=-1)
        if cut < max(1, budget // 2):
            cut = budget
        else:
            cut = min(cut + 1, budget)
        parts.append(remaining[:cut].strip())
        remaining = remaining[cut:].strip()
    if remaining:
        parts.append(remaining)
    return parts or [text.strip()]


def _split_table_body(body: str, budget: int) -> list[str]:
    """按完整数据行拆表，并在每个片段重复表头。"""

    lines = [line.strip() for line in body.splitlines() if line.strip()]
    if len(lines) < 3:
        return _pack_units(lines, budget, joiner="\n")
    header_end = 2 if _TABLE_SEPARATOR_RE.match(lines[1]) else 1
    header = "\n".join(lines[:header_end])
    row_budget = budget - len(header) - 1
    if row_budget < 80:
        return _hard_split(body, budget)
    groups = _pack_units(lines[header_end:], row_budget, joiner="\n")
    return [f"{header}\n{group}" for group in groups] or [header]


def _split_code_body(body: str, budget: int) -> list[str]:
    """超长代码按完整行拆分，并为每段补齐代码围栏。"""

    lines = body.splitlines()
    if len(lines) < 2:
        return _pack_units([body], budget, joiner="\n")
    opening = lines[0] if lines[0].lstrip().startswith(("```", "~~~")) else ""
    closing = lines[-1] if opening and lines[-1].lstrip().startswith(opening.lstrip()[0] * 3) else ""
    code_lines = lines[1:-1] if opening and closing else lines
    overhead = len(opening) + len(closing) + 2 if opening else 0
    content_budget = budget - overhead
    if content_budget < 80:
        return _hard_split(body, budget)
    groups = _pack_units(code_lines, content_budget, joiner="\n")
    if not opening:
        return groups
    return [f"{opening}\n{group}\n{closing}" for group in groups]


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
