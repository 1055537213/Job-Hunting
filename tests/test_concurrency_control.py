"""模型和截图共享并发租约回归。"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from langchain_core.language_models.fake_chat_models import FakeListChatModel

from job_hunting_agent.concurrency_control import (
    ConcurrencyBackendUnavailable,
    ConcurrencyLimitExceeded,
    InMemoryConcurrencyController,
    RedisConcurrencyController,
)
from job_hunting_agent.config import (
    ConcurrencySettings,
    load_concurrency_settings,
    masked_concurrency_settings,
)
from job_hunting_agent.model_gateway import (
    ConcurrencyLimitedEmbeddings,
    ConcurrencyLimitedReranker,
    ModelConcurrencyCallbackHandler,
)
from job_hunting_agent.rag import RerankResult


@dataclass
class SharedRedisLeaseState:
    """只模拟租约 Lua 脚本所需的共享原子状态。"""

    now_ms: int = 1_000_000
    leases: dict[str, dict[str, int]] = field(default_factory=dict)
    seen_keys: list[str] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def acquire(
        self,
        global_key: str,
        account_key: str,
        global_limit: int,
        account_limit: int,
        token: str,
        ttl_ms: int,
    ) -> list[int]:
        with self.lock:
            self.seen_keys.extend([global_key, account_key])
            global_leases = self._active(global_key)
            account_leases = self._active(account_key)
            if len(global_leases) >= global_limit:
                return [0, min(global_leases.values()) - self.now_ms]
            if account_limit > 0 and len(account_leases) >= account_limit:
                return [0, min(account_leases.values()) - self.now_ms]
            expires_at = self.now_ms + ttl_ms
            global_leases[token] = expires_at
            if account_limit > 0:
                account_leases[token] = expires_at
            return [1, 0]

    def release(
        self,
        global_key: str,
        account_key: str,
        account_limit: int,
        token: str,
    ) -> int:
        with self.lock:
            removed = self.leases.setdefault(global_key, {}).pop(token, None)
            if account_limit > 0:
                self.leases.setdefault(account_key, {}).pop(token, None)
            return 1 if removed is not None else 0

    def _active(self, key: str) -> dict[str, int]:
        active = {
            token: expires_at
            for token, expires_at in self.leases.get(key, {}).items()
            if expires_at > self.now_ms
        }
        self.leases[key] = active
        return active


class FakeRedisClient:
    def __init__(self, state: SharedRedisLeaseState) -> None:
        self.state = state

    def eval(self, script: str, numkeys: int, *keys_and_args: object) -> object:
        assert numkeys == 2
        global_key, account_key, *arguments = keys_and_args
        if "redis.call('TIME')" in script:
            global_limit, account_limit, token, ttl_ms = arguments
            return self.state.acquire(
                str(global_key),
                str(account_key),
                int(global_limit),
                int(account_limit),
                str(token),
                int(ttl_ms),
            )
        account_limit, token = arguments
        return self.state.release(
            str(global_key),
            str(account_key),
            int(account_limit),
            str(token),
        )


class BrokenRedisClient:
    def eval(self, script: str, numkeys: int, *keys_and_args: object) -> object:
        raise ConnectionError("redis unavailable")


def redis_settings(**overrides: object) -> ConcurrencySettings:
    values: dict[str, object] = {
        "backend": "redis",
        "redis_url": "redis://redis:6379/1",
        "model_global_limit": 2,
        "model_account_limit": 1,
        "screenshot_global_limit": 1,
        "screenshot_account_limit": 1,
        "lease_ttl_seconds": 10,
        "wait_timeout_seconds": 0,
    }
    values.update(overrides)
    return ConcurrencySettings(**values)


def test_two_redis_controllers_share_global_and_account_capacity() -> None:
    state = SharedRedisLeaseState()
    first = RedisConcurrencyController(
        redis_settings(),
        redis_client=FakeRedisClient(state),
    )
    second = RedisConcurrencyController(
        redis_settings(),
        redis_client=FakeRedisClient(state),
    )

    first_lease = first.acquire("model", account_id=7)
    second_lease = second.acquire("model", account_id=8)
    with pytest.raises(ConcurrencyLimitExceeded):
        second.acquire("model", account_id=9)
    second_lease.release()
    with pytest.raises(ConcurrencyLimitExceeded):
        second.acquire("model", account_id=7)
    third_lease = second.acquire("model", account_id=9)

    assert all(not key.endswith((":7", ":8", ":9")) for key in state.seen_keys)
    first_lease.release()
    third_lease.release()
    assert second.acquire("model", account_id=7) is not None


def test_redis_lease_expiry_recovers_capacity_after_process_loss() -> None:
    state = SharedRedisLeaseState()
    controller = RedisConcurrencyController(
        redis_settings(screenshot_global_limit=1, lease_ttl_seconds=2),
        redis_client=FakeRedisClient(state),
    )

    controller.acquire("screenshot", account_id=10)
    with pytest.raises(ConcurrencyLimitExceeded):
        controller.acquire("screenshot", account_id=11)
    state.now_ms += 2_001
    assert controller.acquire("screenshot", account_id=11) is not None


def test_redis_release_is_idempotent_and_backend_failures_are_explicit() -> None:
    state = SharedRedisLeaseState()
    controller = RedisConcurrencyController(
        redis_settings(screenshot_global_limit=1),
        redis_client=FakeRedisClient(state),
    )
    lease = controller.acquire("screenshot", account_id=10)
    lease.release()
    lease.release()
    assert controller.acquire("screenshot", account_id=11) is not None

    broken = RedisConcurrencyController(
        redis_settings(),
        redis_client=BrokenRedisClient(),
    )
    with pytest.raises(ConcurrencyBackendUnavailable):
        broken.acquire("model", account_id=10)


def test_redis_wrong_holder_cannot_release_another_lease() -> None:
    """释放脚本按随机 token 匹配，错误 token 不会腾出全局额度。"""

    state = SharedRedisLeaseState()
    controller = RedisConcurrencyController(
        redis_settings(model_global_limit=1, model_account_limit=1),
        redis_client=FakeRedisClient(state),
    )
    lease = controller.acquire("model", account_id=10)
    global_key, account_key = controller._keys("model", 10)

    controller._release(global_key, account_key, 1, "not-the-owner", "model")
    with pytest.raises(ConcurrencyLimitExceeded):
        controller.acquire("model", account_id=11)

    lease.release()
    assert controller.acquire("model", account_id=11) is not None


def test_concurrency_metrics_distinguish_rejection_and_backend_failure() -> None:
    state = SharedRedisLeaseState()
    controller = RedisConcurrencyController(
        redis_settings(model_global_limit=1, model_account_limit=1),
        redis_client=FakeRedisClient(state),
    )
    lease = controller.acquire("model", account_id=10)
    with pytest.raises(ConcurrencyLimitExceeded):
        controller.acquire("model", account_id=11)
    lease.release()

    metrics = controller.metrics_snapshot()["resources"]["model"]
    assert metrics["acquired"] == 1
    assert metrics["rejected"] == 1
    assert metrics["backend_errors"] == 0
    assert metrics["release_errors"] == 0

    broken = RedisConcurrencyController(
        redis_settings(),
        redis_client=BrokenRedisClient(),
    )
    with pytest.raises(ConcurrencyBackendUnavailable):
        broken.acquire("model", account_id=10)
    assert broken.metrics_snapshot()["resources"]["model"]["backend_errors"] == 1


def test_in_memory_controller_releases_capacity_after_exception() -> None:
    controller = InMemoryConcurrencyController(
        ConcurrencySettings(
            model_global_limit=1,
            model_account_limit=1,
            wait_timeout_seconds=0,
        )
    )
    lease = controller.acquire("model", account_id=3)
    with pytest.raises(ConcurrencyLimitExceeded):
        controller.acquire("model", account_id=4)
    lease.release()
    assert controller.acquire("model", account_id=4) is not None


class RecordingLease:
    def __init__(self) -> None:
        self.release_count = 0

    def release(self) -> None:
        self.release_count += 1


class RecordingController:
    def __init__(self) -> None:
        self.calls: list[tuple[str, int | None]] = []
        self.leases: list[RecordingLease] = []

    def acquire(
        self,
        resource: str,
        *,
        account_id: int | None,
        wait_timeout_seconds: float | None = None,
    ) -> RecordingLease:
        self.calls.append((resource, account_id))
        lease = RecordingLease()
        self.leases.append(lease)
        return lease


def test_chat_callback_uses_runtime_account_metadata_and_releases_once() -> None:
    controller = RecordingController()
    handler = ModelConcurrencyCallbackHandler(controller)

    handler.on_chat_model_start({}, [[]], run_id="run-1", metadata={"account_id": 42})
    handler.on_llm_start({}, ["prompt"], run_id="run-1", metadata={"account_id": 42})
    handler.on_llm_end(object(), run_id="run-1")
    handler.on_llm_error(RuntimeError("late callback"), run_id="run-1")

    assert controller.calls == [("model", 42)]
    assert controller.leases[0].release_count == 1


def test_langchain_propagates_runtime_account_metadata_to_concurrency_callback() -> None:
    controller = RecordingController()
    handler = ModelConcurrencyCallbackHandler(controller)
    model = FakeListChatModel(responses=["ok"], callbacks=[handler])

    response = model.invoke("hello", config={"metadata": {"account_id": 77}})

    assert response.content == "ok"
    assert controller.calls == [("model", 77)]
    assert controller.leases[0].release_count == 1


class RecordingEmbeddings:
    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [[float(len(text))] for text in texts]

    def embed_query(self, text: str) -> list[float]:
        return [float(len(text))]


class FailingReranker:
    retrieval_top_k = 20
    model = "rerank-model"
    min_relevance_score = 0.65
    relative_score_threshold = 0.86

    def rerank(
        self,
        query: str,
        documents: list[str],
        top_n: int,
    ) -> list[RerankResult]:
        raise RuntimeError("provider failed")


def test_embedding_and_rerank_wrappers_release_model_slots_on_all_paths() -> None:
    controller = RecordingController()
    embeddings = ConcurrencyLimitedEmbeddings(
        RecordingEmbeddings(),
        controller,
        account_id=12,
    )
    assert embeddings.embed_query("abc") == [3.0]
    assert embeddings.embed_documents(["a", "abcd"]) == [[1.0], [4.0]]

    reranker = ConcurrencyLimitedReranker(
        FailingReranker(),
        controller,
        account_id=12,
    )
    assert reranker.model == "rerank-model"
    assert reranker.min_relevance_score == 0.65
    assert reranker.relative_score_threshold == 0.86
    with pytest.raises(RuntimeError, match="provider failed"):
        reranker.rerank("query", ["document"], 1)

    assert controller.calls == [
        ("model", 12),
        ("model", 12),
        ("model", 12),
    ]
    assert [lease.release_count for lease in controller.leases] == [1, 1, 1]


def test_production_requires_redis_concurrency_and_mask_hides_url(
    tmp_path: Path,
) -> None:
    env_path = tmp_path / ".env"
    env_path.write_text("", encoding="utf-8")
    with pytest.raises(ValueError, match="Redis 共享并发租约"):
        load_concurrency_settings(
            env_path,
            environ={
                "JOB_AGENT_ENVIRONMENT": "production",
                "JOB_AGENT_CONCURRENCY_BACKEND": "memory",
            },
        )

    settings = load_concurrency_settings(
        env_path,
        environ={
            "JOB_AGENT_ENVIRONMENT": "production",
            "JOB_AGENT_CONCURRENCY_BACKEND": "redis",
            "JOB_AGENT_CONCURRENCY_REDIS_URL": "redis://:secret@redis:6379/1",
        },
    )
    masked = masked_concurrency_settings(settings)
    assert masked["backend"] == "redis"
    assert masked["redis_configured"] is True
    assert "secret" not in str(masked)
    assert "redis_url" not in str(masked)
