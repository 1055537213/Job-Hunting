"""可由多个 Web 和 Worker 副本共享的并发租约。"""

from __future__ import annotations

import hashlib
import logging
import threading
import time
import uuid
from collections import defaultdict
from collections.abc import Callable
from typing import Protocol

from .config import ConcurrencySettings

logger = logging.getLogger(__name__)


class ConcurrencyControlError(RuntimeError):
    """共享并发保护无法完成时的基础异常。"""


class ConcurrencyLimitExceeded(ConcurrencyControlError):
    """当前资源的全局或账号并发额度已用完。"""


class ConcurrencyBackendUnavailable(ConcurrencyControlError):
    """共享并发租约后端暂时不可用。"""


class ConcurrencyLease(Protocol):
    """一次可幂等释放的并发占位。"""

    def release(self) -> None:
        """释放当前占位；重复调用不产生副作用。"""


class ConcurrencyController(Protocol):
    """业务层依赖的最小并发控制接口。"""

    def acquire(
        self,
        resource: str,
        *,
        account_id: int | None,
        wait_timeout_seconds: float | None = None,
    ) -> ConcurrencyLease:
        """获取资源租约，超出等待时间时抛出 ``ConcurrencyLimitExceeded``。"""

    def metrics_snapshot(self) -> dict[str, object]:
        """返回低基数的租约观测指标。"""


class RedisScriptClient(Protocol):
    """Redis 并发控制实际使用的最小同步客户端接口。"""

    def eval(self, script: str, numkeys: int, *keys_and_args: object) -> object:
        """原子执行并发租约 Lua 脚本。"""


class ConcurrencyMetrics:
    """记录当前进程观察到的共享租约事件。

    Redis 中的额度是跨副本共享的，Prometheus 会把每个 Web 副本的计数器相加。
    这里不记录账号 ID 或租约 token，只保留固定资源名和事件类型，避免指标标签
    产生高基数或泄露租户信息。
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counts: dict[str, dict[str, int]] = defaultdict(
            lambda: defaultdict(int)
        )
        self._in_flight: dict[str, int] = defaultdict(int)

    def acquired(self, resource: str) -> None:
        """记录一次成功获取，并增加本进程当前持有数。"""

        with self._lock:
            self._counts[resource]["acquired"] += 1
            self._in_flight[resource] += 1

    def released(self, resource: str) -> None:
        """记录一次正常释放；重复释放由租约对象在上层拦截。"""

        with self._lock:
            self._counts[resource]["released"] += 1
            self._in_flight[resource] = max(0, self._in_flight[resource] - 1)

    def rejected(self, resource: str) -> None:
        """记录额度耗尽导致的最终拒绝。"""

        with self._lock:
            self._counts[resource]["rejected"] += 1

    def backend_error(self, resource: str) -> None:
        """记录 Redis 后端异常。"""

        with self._lock:
            self._counts[resource]["backend_errors"] += 1

    def release_error(self, resource: str) -> None:
        """记录释放脚本失败；租约仍会依赖 TTL 自动回收。"""

        with self._lock:
            self._counts[resource]["release_errors"] += 1

    def snapshot(self) -> dict[str, object]:
        """返回只包含固定资源和事件字段的可序列化快照。"""

        with self._lock:
            resources = sorted(set(self._counts) | set(self._in_flight))
            return {
                "resources": {
                    resource: {
                        "acquired": self._counts[resource].get("acquired", 0),
                        "released": self._counts[resource].get("released", 0),
                        "rejected": self._counts[resource].get("rejected", 0),
                        "backend_errors": self._counts[resource].get(
                            "backend_errors", 0
                        ),
                        "release_errors": self._counts[resource].get(
                            "release_errors", 0
                        ),
                        "in_flight": self._in_flight.get(resource, 0),
                    }
                    for resource in resources
                }
            }


class _NoopLease:
    def release(self) -> None:
        return None


class NoopConcurrencyController:
    """显式关闭并发保护时使用的空实现。"""

    def acquire(
        self,
        resource: str,
        *,
        account_id: int | None,
        wait_timeout_seconds: float | None = None,
    ) -> ConcurrencyLease:
        limits_for_resource(ConcurrencySettings(), resource)
        return _NoopLease()

    def metrics_snapshot(self) -> dict[str, object]:
        """空实现没有租约事件，仍返回稳定的指标结构。"""

        return {"resources": {}}


class _InMemoryLease:
    def __init__(
        self,
        controller: InMemoryConcurrencyController,
        resource: str,
        account_id: int | None,
    ) -> None:
        self._controller = controller
        self._resource = resource
        self._account_id = account_id
        self._released = False
        self._lock = threading.Lock()

    def release(self) -> None:
        with self._lock:
            if self._released:
                return
            self._released = True
        self._controller._release(self._resource, self._account_id)


class InMemoryConcurrencyController:
    """开发和测试使用的进程内并发控制器。"""

    def __init__(
        self,
        settings: ConcurrencySettings,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.settings = settings
        self.clock = clock
        self._condition = threading.Condition()
        self._resource_counts: dict[str, int] = defaultdict(int)
        self._account_counts: dict[tuple[str, int], int] = defaultdict(int)
        self._metrics = ConcurrencyMetrics()

    def acquire(
        self,
        resource: str,
        *,
        account_id: int | None,
        wait_timeout_seconds: float | None = None,
    ) -> ConcurrencyLease:
        global_limit, account_limit = limits_for_resource(self.settings, resource)
        timeout = (
            self.settings.wait_timeout_seconds
            if wait_timeout_seconds is None
            else max(0.0, wait_timeout_seconds)
        )
        deadline = self.clock() + timeout
        with self._condition:
            while not self._has_capacity(
                resource,
                account_id,
                global_limit,
                account_limit,
            ):
                remaining = deadline - self.clock()
                if remaining <= 0:
                    self._metrics.rejected(resource)
                    raise ConcurrencyLimitExceeded(concurrency_limit_message(resource))
                self._condition.wait(timeout=remaining)
            self._resource_counts[resource] += 1
            if account_id is not None:
                self._account_counts[(resource, account_id)] += 1
            self._metrics.acquired(resource)
        return _InMemoryLease(self, resource, account_id)

    def _has_capacity(
        self,
        resource: str,
        account_id: int | None,
        global_limit: int,
        account_limit: int,
    ) -> bool:
        if self._resource_counts[resource] >= global_limit:
            return False
        return account_id is None or self._account_counts[(resource, account_id)] < account_limit

    def _release(self, resource: str, account_id: int | None) -> None:
        with self._condition:
            self._resource_counts[resource] = max(
                0,
                self._resource_counts[resource] - 1,
            )
            if account_id is not None:
                key = (resource, account_id)
                self._account_counts[key] = max(0, self._account_counts[key] - 1)
            self._condition.notify_all()

    def metrics_snapshot(self) -> dict[str, object]:
        """返回当前进程内实现的租约指标。"""

        return self._metrics.snapshot()


# Redis TIME 让全部副本使用同一时钟。过期租约清理、两级额度判断和占位在一个脚本
# 中完成，避免 Web 与 Worker 分别读写计数时突破上限。
REDIS_ACQUIRE_LEASE_SCRIPT = """
local global_key = KEYS[1]
local account_key = KEYS[2]
local global_limit = tonumber(ARGV[1])
local account_limit = tonumber(ARGV[2])
local token = ARGV[3]
local ttl_ms = tonumber(ARGV[4])
local redis_time = redis.call('TIME')
local now_ms = (tonumber(redis_time[1]) * 1000) + math.floor(tonumber(redis_time[2]) / 1000)

redis.call('ZREMRANGEBYSCORE', global_key, '-inf', now_ms)
if account_limit > 0 then
    redis.call('ZREMRANGEBYSCORE', account_key, '-inf', now_ms)
end

if redis.call('ZCARD', global_key) >= global_limit then
    local oldest = redis.call('ZRANGE', global_key, 0, 0, 'WITHSCORES')
    local retry_ms = ttl_ms
    if #oldest >= 2 then
        retry_ms = math.max(1, tonumber(oldest[2]) - now_ms)
    end
    return {0, retry_ms}
end
if account_limit > 0 and redis.call('ZCARD', account_key) >= account_limit then
    local oldest = redis.call('ZRANGE', account_key, 0, 0, 'WITHSCORES')
    local retry_ms = ttl_ms
    if #oldest >= 2 then
        retry_ms = math.max(1, tonumber(oldest[2]) - now_ms)
    end
    return {0, retry_ms}
end

local expires_at_ms = now_ms + ttl_ms
redis.call('ZADD', global_key, expires_at_ms, token)
redis.call('PEXPIRE', global_key, ttl_ms + 1000)
if account_limit > 0 then
    redis.call('ZADD', account_key, expires_at_ms, token)
    redis.call('PEXPIRE', account_key, ttl_ms + 1000)
end
return {1, 0}
""".strip()


REDIS_RELEASE_LEASE_SCRIPT = """
local global_key = KEYS[1]
local account_key = KEYS[2]
local account_limit = tonumber(ARGV[1])
local token = ARGV[2]
local removed = redis.call('ZREM', global_key, token)
if account_limit > 0 then
    redis.call('ZREM', account_key, token)
end
return removed
""".strip()


class _RedisLease:
    def __init__(
        self,
        controller: RedisConcurrencyController,
        global_key: str,
        account_key: str,
        account_limit: int,
        token: str,
        resource: str,
    ) -> None:
        self._controller = controller
        self._global_key = global_key
        self._account_key = account_key
        self._account_limit = account_limit
        self._token = token
        self._resource = resource
        self._released = False
        self._lock = threading.Lock()

    def release(self) -> None:
        with self._lock:
            if self._released:
                return
            self._released = True
        self._controller._release(
            self._global_key,
            self._account_key,
            self._account_limit,
            self._token,
            self._resource,
        )


class RedisConcurrencyController:
    """使用 Redis 原子租约、供 Web 和 Worker 共享的并发控制器。"""

    def __init__(
        self,
        settings: ConcurrencySettings,
        *,
        redis_client: RedisScriptClient | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        self.settings = settings
        self.clock = clock
        self.sleeper = sleeper
        self._metrics = ConcurrencyMetrics()
        if redis_client is None:
            if not settings.redis_url:
                raise ValueError("Redis 共享并发控制器缺少连接地址。")
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

    def acquire(
        self,
        resource: str,
        *,
        account_id: int | None,
        wait_timeout_seconds: float | None = None,
    ) -> ConcurrencyLease:
        global_limit, account_limit = limits_for_resource(self.settings, resource)
        timeout = (
            self.settings.wait_timeout_seconds
            if wait_timeout_seconds is None
            else max(0.0, wait_timeout_seconds)
        )
        deadline = self.clock() + timeout
        global_key, account_key = self._keys(resource, account_id)
        token = uuid.uuid4().hex
        while True:
            allowed, retry_ms = self._try_acquire(
                resource,
                global_key,
                account_key,
                global_limit,
                account_limit if account_id is not None else 0,
                token,
            )
            if allowed:
                self._metrics.acquired(resource)
                return _RedisLease(
                    self,
                    global_key,
                    account_key,
                    account_limit if account_id is not None else 0,
                    token,
                    resource,
                )
            remaining = deadline - self.clock()
            if remaining <= 0:
                self._metrics.rejected(resource)
                raise ConcurrencyLimitExceeded(concurrency_limit_message(resource))
            self.sleeper(min(remaining, max(0.01, min(retry_ms / 1000, 0.1))))

    def _try_acquire(
        self,
        resource: str,
        global_key: str,
        account_key: str,
        global_limit: int,
        account_limit: int,
        token: str,
    ) -> tuple[bool, int]:
        try:
            result = self._redis.eval(
                REDIS_ACQUIRE_LEASE_SCRIPT,
                2,
                global_key,
                account_key,
                global_limit,
                account_limit,
                token,
                self.settings.lease_ttl_seconds * 1000,
            )
            if not isinstance(result, (list, tuple)) or len(result) != 2:
                raise ValueError("Redis 并发租约脚本返回了无效结果。")
            allowed = int(result[0])
            retry_ms = max(1, int(result[1]))
            if allowed not in {0, 1}:
                raise ValueError("Redis 并发租约脚本返回了未知状态。")
            return allowed == 1, retry_ms
        except Exception as error:
            # Redis 故障不是额度耗尽；单独计数，便于告警区分容量压力和基础设施故障。
            self._metrics.backend_error(resource)
            raise ConcurrencyBackendUnavailable(
                "并发保护服务暂时不可用，请稍后重试。"
            ) from error

    def _release(
        self,
        global_key: str,
        account_key: str,
        account_limit: int,
        token: str,
        resource: str,
    ) -> None:
        try:
            result = self._redis.eval(
                REDIS_RELEASE_LEASE_SCRIPT,
                2,
                global_key,
                account_key,
                account_limit,
                token,
            )
            removed = int(result)
            if removed not in {0, 1}:
                raise ValueError("Redis 并发租约释放脚本返回了无效结果。")
            # 0 表示 token 已过期或并非当前持有者，不能把它计为本进程成功释放。
            if removed == 1:
                self._metrics.released(resource)
        except Exception as error:  # noqa: BLE001 - 租约会自动过期，不能覆盖成功业务结果。
            self._metrics.release_error(resource)
            logger.warning("Redis 并发租约释放失败：%s", type(error).__name__)

    def metrics_snapshot(self) -> dict[str, object]:
        """返回当前进程观察到的 Redis 租约指标。"""

        return self._metrics.snapshot()

    def _keys(self, resource: str, account_id: int | None) -> tuple[str, str]:
        global_key = f"{self.settings.key_prefix}:{resource}:global"
        identity = "anonymous"
        if account_id is not None:
            identity = hashlib.sha256(str(account_id).encode("ascii")).hexdigest()[:32]
        account_key = f"{self.settings.key_prefix}:{resource}:account:{identity}"
        return global_key, account_key


def build_concurrency_controller(settings: ConcurrencySettings) -> ConcurrencyController:
    """根据类型化配置构造共享并发控制器。"""

    if not settings.enabled:
        return NoopConcurrencyController()
    if settings.backend == "redis":
        return RedisConcurrencyController(settings)
    return InMemoryConcurrencyController(settings)


def limits_for_resource(
    settings: ConcurrencySettings,
    resource: str,
) -> tuple[int, int]:
    """返回已知资源的全局和单账号并发上限。"""

    limits = {
        "model": (settings.model_global_limit, settings.model_account_limit),
        "screenshot": (
            settings.screenshot_global_limit,
            settings.screenshot_account_limit,
        ),
    }
    try:
        return limits[resource]
    except KeyError as error:
        raise ValueError(f"未知并发资源：{resource}") from error


def concurrency_limit_message(resource: str) -> str:
    """返回不泄露内部额度的用户可读繁忙提示。"""

    if resource == "screenshot":
        return "职位截图识别请求较多，请稍后重试。"
    return "模型请求较多，请稍后重试。"
