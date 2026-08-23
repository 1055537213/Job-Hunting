"""可在多个 Web 副本之间共享的请求限流边界。"""

from __future__ import annotations

import hashlib
import math
import threading
import time
import uuid
from collections import defaultdict, deque
from collections.abc import Callable
from typing import Protocol

from starlette.concurrency import run_in_threadpool

from .config import WebSecuritySettings


class RateLimitBackendUnavailable(RuntimeError):
    """共享限流后端暂时不可用。"""


class RateLimiter(Protocol):
    """Web 中间件依赖的最小限流接口。"""

    async def check(self, *, client_id: str, group: str) -> int | None:
        """返回 ``None`` 放行，或返回应写入 Retry-After 的秒数。"""


class RedisScriptClient(Protocol):
    """Redis 限流器实际需要的最小同步客户端接口。"""

    def eval(self, script: str, numkeys: int, *keys_and_args: object) -> object:
        """原子执行限流 Lua 脚本。"""


class InMemoryRateLimiter:
    """开发和测试使用的进程内滑动窗口限流器。"""

    def __init__(
        self,
        settings: WebSecuritySettings,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.settings = settings
        self.clock = clock
        self._lock = threading.Lock()
        self._buckets: dict[tuple[str, str], deque[float]] = defaultdict(deque)

    async def check(self, *, client_id: str, group: str) -> int | None:
        """在当前进程内执行滑动窗口判断。"""

        if not self.settings.rate_limit_enabled:
            return None
        limit = rate_limit_for_group(self.settings, group)
        window = float(self.settings.rate_limit_window_seconds)
        now = self.clock()
        key = (client_id, group)
        with self._lock:
            bucket = self._buckets[key]
            cutoff = now - window
            while bucket and bucket[0] <= cutoff:
                bucket.popleft()
            if len(bucket) >= limit:
                return max(1, math.ceil(window - (now - bucket[0])))
            bucket.append(now)
        return None


# Redis TIME 提供所有 Web 副本共享的时钟；ZSET 裁剪、计数、写入和过期在同一个脚本内
# 完成，避免并发请求分别读写时突破额度。
REDIS_SLIDING_WINDOW_SCRIPT = """
local key = KEYS[1]
local window_ms = tonumber(ARGV[1])
local request_limit = tonumber(ARGV[2])
local member_suffix = ARGV[3]
local redis_time = redis.call('TIME')
local now_ms = (tonumber(redis_time[1]) * 1000) + math.floor(tonumber(redis_time[2]) / 1000)
local cutoff_ms = now_ms - window_ms

redis.call('ZREMRANGEBYSCORE', key, '-inf', cutoff_ms)
local count = redis.call('ZCARD', key)
if count >= request_limit then
    local oldest = redis.call('ZRANGE', key, 0, 0, 'WITHSCORES')
    local retry_ms = window_ms
    if #oldest >= 2 then
        retry_ms = math.max(1, tonumber(oldest[2]) + window_ms - now_ms)
    end
    redis.call('PEXPIRE', key, window_ms)
    return {0, retry_ms}
end

redis.call('ZADD', key, now_ms, tostring(now_ms) .. ':' .. member_suffix)
redis.call('PEXPIRE', key, window_ms)
return {1, 0}
""".strip()


class RedisRateLimiter:
    """使用 Redis 原子滑动窗口、供多个 Web 副本共享的限流器。"""

    def __init__(
        self,
        settings: WebSecuritySettings,
        *,
        redis_client: RedisScriptClient | None = None,
    ) -> None:
        self.settings = settings
        if redis_client is None:
            if not settings.rate_limit_redis_url:
                raise ValueError("Redis 限流器缺少连接地址。")
            from redis import Redis

            redis_client = Redis.from_url(
                settings.rate_limit_redis_url,
                socket_connect_timeout=settings.rate_limit_redis_timeout_seconds,
                socket_timeout=settings.rate_limit_redis_timeout_seconds,
                health_check_interval=30,
                retry_on_timeout=False,
                decode_responses=False,
            )
        self._redis = redis_client

    async def check(self, *, client_id: str, group: str) -> int | None:
        """在线程池中执行同步 Redis 客户端，避免阻塞 FastAPI 事件循环。"""

        if not self.settings.rate_limit_enabled:
            return None
        try:
            return await run_in_threadpool(
                self._check_sync,
                client_id,
                group,
            )
        except Exception as error:
            raise RateLimitBackendUnavailable("Redis 请求限流后端不可用。") from error

    def _check_sync(self, client_id: str, group: str) -> int | None:
        limit = rate_limit_for_group(self.settings, group)
        identity_digest = hashlib.sha256(client_id.encode("utf-8")).hexdigest()[:32]
        key = f"{self.settings.rate_limit_key_prefix}:{group}:{identity_digest}"
        result = self._redis.eval(
            REDIS_SLIDING_WINDOW_SCRIPT,
            1,
            key,
            self.settings.rate_limit_window_seconds * 1000,
            limit,
            uuid.uuid4().hex,
        )
        if not isinstance(result, (list, tuple)) or len(result) != 2:
            raise ValueError("Redis 限流脚本返回了无效结果。")
        allowed = int(result[0])
        retry_ms = int(result[1])
        if allowed == 1:
            return None
        if allowed != 0:
            raise ValueError("Redis 限流脚本返回了未知状态。")
        return max(1, math.ceil(max(1, retry_ms) / 1000))


def build_rate_limiter(settings: WebSecuritySettings) -> RateLimiter:
    """根据类型化配置构造限流器，不让 Web 层直接依赖 Redis SDK。"""

    if settings.rate_limit_backend == "redis" and settings.rate_limit_enabled:
        return RedisRateLimiter(settings)
    return InMemoryRateLimiter(settings)


def rate_limit_for_group(settings: WebSecuritySettings, group: str) -> int:
    """返回一个低基数请求组的窗口额度。"""

    limits = {
        "auth": settings.rate_limit_auth_requests,
        "model": settings.rate_limit_model_requests,
        "upload": settings.rate_limit_upload_requests,
        "admin": settings.rate_limit_admin_requests,
        "write": settings.rate_limit_write_requests,
        "default": settings.rate_limit_default_requests,
    }
    return limits.get(group, settings.rate_limit_default_requests)
