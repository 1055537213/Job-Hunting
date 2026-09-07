"""按模型身份和内容哈希复用 Embedding 结果。"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Callable

from langchain_core.embeddings import Embeddings

from .business_cache import BusinessCache


class CachedEmbeddings(Embeddings):
    """Cache-Aside Embeddings 包装器；缓存命中不会调用供应商。"""

    def __init__(
        self,
        delegate: Embeddings,
        cache: BusinessCache,
        *,
        ttl_seconds: int,
    ) -> None:
        self.delegate = delegate
        self.cache = cache
        self.ttl_seconds = max(1, ttl_seconds)
        self.model = getattr(delegate, "model", None)
        self.endpoint = getattr(delegate, "endpoint", None)
        self.embeddings_url = getattr(delegate, "embeddings_url", None)
        self.dimensions = getattr(delegate, "dimensions", None)
        identity_source = getattr(delegate, "delegate", delegate)
        identity = json.dumps(
            {
                "type": (
                    f"{type(identity_source).__module__}."
                    f"{type(identity_source).__qualname__}"
                ),
                "provider": getattr(identity_source, "provider", None),
                "api_style": getattr(identity_source, "api_style", None),
                "model": self.model,
                "endpoint": self.endpoint or self.embeddings_url,
                "dimensions": self.dimensions,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        self._identity_hash = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:32]

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return self._embed_many(
            list(texts),
            kind="document",
            fingerprint=lambda text: hashlib.sha256(text.encode("utf-8")).hexdigest(),
            loader=self.delegate.embed_documents,
        )

    def embed_query(self, text: str) -> list[float]:
        return self._embed_many(
            [text],
            kind="query",
            fingerprint=lambda value: hashlib.sha256(value.encode("utf-8")).hexdigest(),
            loader=lambda values: [self.delegate.embed_query(values[0])],
        )[0]

    def embed_images(self, images: list[tuple[bytes, str]]) -> list[list[float]]:
        embed_images = getattr(self.delegate, "embed_images", None)
        if not callable(embed_images):
            raise ValueError("当前 Embedding 配置不支持图片向量。")  # noqa: TRY004
        return self._embed_many(
            list(images),
            kind="image",
            fingerprint=lambda item: hashlib.sha256(
                item[1].encode("utf-8") + b"\0" + item[0]
            ).hexdigest(),
            loader=embed_images,
        )

    def _embed_many(
        self,
        items: list[object],
        *,
        kind: str,
        fingerprint: Callable[[object], str],
        loader: Callable[[list[object]], list[list[float]]],
    ) -> list[list[float]]:
        if not items:
            return []
        keys = [self._cache_key(kind, fingerprint(item)) for item in items]
        cached_values = self.cache.get_many_json(keys)
        results: list[list[float] | None] = [
            _valid_vector(value, self.dimensions) for value in cached_values
        ]

        missing_by_key: dict[str, object] = {}
        for key, item, result in zip(keys, items, results, strict=True):
            if result is None:
                missing_by_key.setdefault(key, item)
        if missing_by_key:
            missing_keys = list(missing_by_key)
            loaded = loader([missing_by_key[key] for key in missing_keys])
            if len(loaded) != len(missing_keys):
                raise ValueError("Embedding 返回数量与输入数量不一致。")
            cache_values: dict[str, object] = {}
            loaded_by_key: dict[str, list[float]] = {}
            for key, vector in zip(missing_keys, loaded, strict=True):
                valid = _valid_vector(vector, self.dimensions)
                if valid is None:
                    raise ValueError("Embedding 返回了无效向量。")
                loaded_by_key[key] = valid
                cache_values[key] = valid
            self.cache.set_many_json(cache_values, self.ttl_seconds)
            for index, key in enumerate(keys):
                if results[index] is None:
                    results[index] = loaded_by_key[key]

        if any(result is None for result in results):
            raise ValueError("Embedding 缓存未能解析完整结果。")
        return [result for result in results if result is not None]

    def _cache_key(self, kind: str, content_hash: str) -> str:
        return f"embedding:{self._identity_hash}:{kind}:{content_hash}"


def _valid_vector(value: object, dimensions: object) -> list[float] | None:
    if not isinstance(value, list) or not value:
        return None
    if not all(
        isinstance(item, (int, float))
        and not isinstance(item, bool)
        and math.isfinite(float(item))
        for item in value
    ):
        return None
    vector = [float(item) for item in value]
    if isinstance(dimensions, int) and dimensions > 0 and len(vector) != dimensions:
        return None
    return vector
