"""Redis 业务缓存边界。

缓存只保存可重建的派生数据。PostgreSQL、配置文件和模型供应商响应仍是权威来源；
Redis 超时、数据损坏或容量不足时一律按未命中处理，不能阻断主业务。
"""

from __future__ import annotations

import json
import logging
import threading
import zlib
from typing import Any, Protocol

from .config import BusinessCacheSettings

logger = logging.getLogger(__name__)
_PAYLOAD_MARKER = b"z1"


class RedisCacheClient(Protocol):
    """业务缓存实际依赖的最小 redis-py 接口。"""

    def get(self, key: str) -> object: ...

    def mget(self, keys: list[str]) -> list[object]: ...

    def setex(self, key: str, ttl_seconds: int, value: bytes) -> object: ...

    def pipeline(self, transaction: bool = False) -> Any: ...

    def delete(self, *keys: str) -> object: ...

    def ping(self) -> object: ...


class BusinessCache(Protocol):
    """应用服务使用的 Cache-Aside 最小接口。"""

    def get_json(self, key: str) -> object | None: ...

    def get_many_json(self, keys: list[str]) -> list[object | None]: ...

    def set_json(self, key: str, value: object, ttl_seconds: int) -> None: ...

    def set_many_json(self, values: dict[str, object], ttl_seconds: int) -> None: ...

    def delete(self, *keys: str) -> None: ...

    def health_snapshot(self) -> dict[str, object]: ...


class DisabledBusinessCache:
    """本地直跑和测试默认使用的无缓存实现。"""

    def get_json(self, key: str) -> object | None:
        return None

    def get_many_json(self, keys: list[str]) -> list[object | None]:
        return [None] * len(keys)

    def set_json(self, key: str, value: object, ttl_seconds: int) -> None:
        return None

    def set_many_json(self, values: dict[str, object], ttl_seconds: int) -> None:
        return None

    def delete(self, *keys: str) -> None:
        return None

    def health_snapshot(self) -> dict[str, object]:
        return {"enabled": False, "backend": "disabled"}


class RedisBusinessCache:
    """使用独立 Redis DB 和有限 TTL 的可降级业务缓存。"""

    def __init__(
        self,
        settings: BusinessCacheSettings,
        *,
        redis_client: RedisCacheClient | None = None,
    ) -> None:
        if not settings.enabled or not settings.redis_url:
            raise ValueError("Redis 业务缓存缺少连接配置。")
        self.settings = settings
        self._lock = threading.Lock()
        self._hits = 0
        self._misses = 0
        self._writes = 0
        self._invalidations = 0
        self._errors = 0
        self._skipped_oversize = 0
        if redis_client is None:
            from redis import Redis

            redis_client = Redis.from_url(
                settings.redis_url,
                socket_connect_timeout=settings.redis_timeout_seconds,
                socket_timeout=settings.redis_timeout_seconds,
                health_check_interval=30,
                retry_on_timeout=False,
                decode_responses=False,
            )
        self._redis = redis_client

    def get_json(self, key: str) -> object | None:
        try:
            raw = self._redis.get(self._full_key(key))
            value = self._decode(raw)
        except Exception as error:  # noqa: BLE001 - 缓存必须失败开放。
            self._record("error")
            logger.warning("Redis 业务缓存读取失败：%s", type(error).__name__)
            return None
        self._record("hit" if value is not None else "miss")
        return value

    def get_many_json(self, keys: list[str]) -> list[object | None]:
        if not keys:
            return []
        try:
            raw_values = self._redis.mget([self._full_key(key) for key in keys])
            values = [self._decode(raw) for raw in raw_values]
        except Exception as error:  # noqa: BLE001 - 缓存必须失败开放。
            self._record("error")
            logger.warning("Redis 业务缓存批量读取失败：%s", type(error).__name__)
            return [None] * len(keys)
        for value in values:
            self._record("hit" if value is not None else "miss")
        return values

    def set_json(self, key: str, value: object, ttl_seconds: int) -> None:
        try:
            encoded = self._encode(value)
        except Exception as error:  # noqa: BLE001 - 非缓存数据不能影响权威写入。
            self._record("error")
            logger.warning("业务缓存值序列化失败：%s", type(error).__name__)
            return
        if encoded is None:
            return
        try:
            self._redis.setex(self._full_key(key), max(1, ttl_seconds), encoded)
        except Exception as error:  # noqa: BLE001 - 数据已在权威来源中提交。
            self._record("error")
            logger.warning("Redis 业务缓存写入失败：%s", type(error).__name__)
            return
        self._record("write")

    def set_many_json(self, values: dict[str, object], ttl_seconds: int) -> None:
        try:
            encoded_values = {
                self._full_key(key): encoded
                for key, value in values.items()
                if (encoded := self._encode(value)) is not None
            }
        except Exception as error:  # noqa: BLE001 - 非缓存数据不能影响权威写入。
            self._record("error")
            logger.warning("业务缓存值批量序列化失败：%s", type(error).__name__)
            return
        if not encoded_values:
            return
        try:
            pipeline = self._redis.pipeline(transaction=False)
            for key, encoded in encoded_values.items():
                pipeline.setex(key, max(1, ttl_seconds), encoded)
            pipeline.execute()
        except Exception as error:  # noqa: BLE001 - 数据已在权威来源中提交。
            self._record("error")
            logger.warning("Redis 业务缓存批量写入失败：%s", type(error).__name__)
            return
        for _ in encoded_values:
            self._record("write")

    def delete(self, *keys: str) -> None:
        normalized = [self._full_key(key) for key in keys if key]
        if not normalized:
            return
        try:
            self._redis.delete(*normalized)
        except Exception as error:  # noqa: BLE001 - TTL 是最终一致性兜底。
            self._record("error")
            logger.warning("Redis 业务缓存失效失败：%s", type(error).__name__)
            return
        with self._lock:
            self._invalidations += len(normalized)

    def health_snapshot(self) -> dict[str, object]:
        with self._lock:
            return {
                "enabled": True,
                "backend": "redis",
                "hits": self._hits,
                "misses": self._misses,
                "writes": self._writes,
                "invalidations": self._invalidations,
                "errors": self._errors,
                "skipped_oversize": self._skipped_oversize,
            }

    def _full_key(self, key: str) -> str:
        normalized = str(key).strip(": ")
        if not normalized:
            raise ValueError("业务缓存键不能为空。")
        return f"{self.settings.key_prefix}:{normalized}"

    def _encode(self, value: object) -> bytes | None:
        raw = json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        encoded = _PAYLOAD_MARKER + zlib.compress(raw, level=6)
        if len(encoded) <= self.settings.max_value_bytes:
            return encoded
        with self._lock:
            self._skipped_oversize += 1
        return None

    @staticmethod
    def _decode(raw: object) -> object | None:
        if raw is None:
            return None
        if isinstance(raw, str):
            payload = raw.encode("utf-8")
        elif isinstance(raw, bytes):
            payload = raw
        else:
            raise TypeError("Redis 缓存值不是字节或字符串。")
        if not payload.startswith(_PAYLOAD_MARKER):
            raise ValueError("Redis 缓存值版本无效。")
        return json.loads(zlib.decompress(payload[len(_PAYLOAD_MARKER) :]).decode("utf-8"))

    def _record(self, event: str) -> None:
        with self._lock:
            if event == "hit":
                self._hits += 1
            elif event == "miss":
                self._misses += 1
            elif event == "write":
                self._writes += 1
            else:
                self._errors += 1


def build_business_cache(settings: BusinessCacheSettings) -> BusinessCache:
    """根据配置构造可选 Redis 缓存。"""

    if not settings.enabled:
        return DisabledBusinessCache()
    return RedisBusinessCache(settings)
