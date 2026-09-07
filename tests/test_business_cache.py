"""Redis 业务缓存和 Cache-Aside 集成回归测试。"""

from __future__ import annotations

from dataclasses import asdict

from fastapi.testclient import TestClient

from job_hunting_agent.app import JobHuntingApp
from job_hunting_agent.business_cache import BusinessCache, RedisBusinessCache
from job_hunting_agent.cached_embeddings import CachedEmbeddings
from job_hunting_agent.config import (
    BusinessCacheSettings,
    EmbeddingSettings,
    load_business_cache_settings,
)
from job_hunting_agent.model_gateway import ModelGateway
from job_hunting_agent.models import (
    CandidateProfileInput,
    CandidateProfilePatch,
    ConversationIngestionDecision,
)
from job_hunting_agent.web import create_web_app


class DictBusinessCache(BusinessCache):
    """测试用字典缓存，记录命中和失效但不依赖 Redis。"""

    def __init__(self) -> None:
        self.values: dict[str, object] = {}
        self.deleted: list[str] = []

    def get_json(self, key: str) -> object | None:
        return self.values.get(key)

    def get_many_json(self, keys: list[str]) -> list[object | None]:
        return [self.values.get(key) for key in keys]

    def set_json(self, key: str, value: object, ttl_seconds: int) -> None:
        self.values[key] = value

    def set_many_json(self, values: dict[str, object], ttl_seconds: int) -> None:
        self.values.update(values)

    def delete(self, *keys: str) -> None:
        self.deleted.extend(keys)
        for key in keys:
            self.values.pop(key, None)

    def health_snapshot(self) -> dict[str, object]:
        return {"enabled": True, "backend": "dict"}


class FakeRedisPipeline:
    def __init__(self, values: dict[str, bytes]) -> None:
        self.values = values
        self.pending: list[tuple[str, bytes]] = []

    def setex(self, key: str, ttl_seconds: int, value: bytes) -> FakeRedisPipeline:
        assert ttl_seconds > 0
        self.pending.append((key, value))
        return self

    def execute(self) -> None:
        self.values.update(self.pending)


class FakeRedis:
    def __init__(self) -> None:
        self.values: dict[str, bytes] = {}

    def get(self, key: str) -> bytes | None:
        return self.values.get(key)

    def mget(self, keys: list[str]) -> list[bytes | None]:
        return [self.values.get(key) for key in keys]

    def setex(self, key: str, ttl_seconds: int, value: bytes) -> None:
        assert ttl_seconds > 0
        self.values[key] = value

    def pipeline(self, transaction: bool = False) -> FakeRedisPipeline:
        assert transaction is False
        return FakeRedisPipeline(self.values)

    def delete(self, *keys: str) -> int:
        deleted = 0
        for key in keys:
            deleted += int(self.values.pop(key, None) is not None)
        return deleted

    def ping(self) -> bool:
        return True


class BrokenRedis(FakeRedis):
    def get(self, key: str) -> bytes | None:
        raise ConnectionError("redis unavailable")


class RecordingEmbeddings:
    model = "embedding-v1"
    endpoint = "https://embedding.example/v1"
    dimensions = 2

    def __init__(self) -> None:
        self.document_calls: list[list[str]] = []
        self.query_calls: list[str] = []
        self.image_calls: list[list[tuple[bytes, str]]] = []

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        self.document_calls.append(list(texts))
        return [[float(len(text)), float(index)] for index, text in enumerate(texts)]

    def embed_query(self, text: str) -> list[float]:
        self.query_calls.append(text)
        return [float(len(text)), 99.0]

    def embed_images(self, images: list[tuple[bytes, str]]) -> list[list[float]]:
        self.image_calls.append(list(images))
        return [[float(len(content)), float(index)] for index, (content, _) in enumerate(images)]


def enabled_settings(**overrides: object) -> BusinessCacheSettings:
    values: dict[str, object] = {
        "enabled": True,
        "redis_url": "redis://redis:6379/2",
        "key_prefix": "job_agent:cache:v1",
    }
    values.update(overrides)
    return BusinessCacheSettings(**values)


def test_business_cache_settings_are_disabled_by_default_and_validate_redis(tmp_path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("", encoding="utf-8")

    assert load_business_cache_settings(env_file, environ={}).enabled is False

    configured = load_business_cache_settings(
        env_file,
        environ={
            "JOB_AGENT_BUSINESS_CACHE_ENABLED": "true",
            "JOB_AGENT_BUSINESS_CACHE_REDIS_URL": "redis://redis:6379/2",
            "JOB_AGENT_BUSINESS_CACHE_PROFILE_TTL_SECONDS": "45",
        },
    )
    assert configured.enabled is True
    assert configured.profile_ttl_seconds == 45


def test_redis_business_cache_round_trips_json_and_fails_open() -> None:
    redis = FakeRedis()
    cache = RedisBusinessCache(enabled_settings(), redis_client=redis)

    cache.set_json("public:config", {"enabled": True}, 60)
    assert cache.get_json("public:config") == {"enabled": True}
    assert not any("public:config" == key for key in redis.values)

    cache.set_many_json({"a": [1.0, 2.0], "b": {"value": 3}}, 60)
    assert cache.get_many_json(["a", "missing", "b"]) == [
        [1.0, 2.0],
        None,
        {"value": 3},
    ]
    cache.delete("a", "b")
    assert cache.get_many_json(["a", "b"]) == [None, None]

    broken = RedisBusinessCache(enabled_settings(), redis_client=BrokenRedis())
    assert broken.get_json("anything") is None
    broken.set_json("non-json", {"bad": object()}, 60)
    assert broken.health_snapshot()["errors"] == 2


def test_cached_embeddings_reuses_text_query_and_image_vectors() -> None:
    cache = DictBusinessCache()
    delegate = RecordingEmbeddings()
    embeddings = CachedEmbeddings(delegate, cache, ttl_seconds=3600)

    first = embeddings.embed_documents(["python", "rag", "python"])
    second = embeddings.embed_documents(["python", "rag"])
    first_query = embeddings.embed_query("python")
    second_query = embeddings.embed_query("python")
    first_images = embeddings.embed_images([(b"same-image", "image/png")])
    second_images = embeddings.embed_images([(b"same-image", "image/png")])

    assert delegate.document_calls == [["python", "rag"]]
    assert first == [[6.0, 0.0], [3.0, 1.0], [6.0, 0.0]]
    assert second == [[6.0, 0.0], [3.0, 1.0]]
    assert first_query == second_query == [6.0, 99.0]
    assert delegate.query_calls == ["python"]
    assert first_images == second_images == [[10.0, 0.0]]
    assert len(delegate.image_calls) == 1


def test_model_gateway_wraps_remote_embeddings_with_shared_cache(
    monkeypatch,
    tmp_path,
) -> None:
    cache = DictBusinessCache()
    delegate = RecordingEmbeddings()
    monkeypatch.setattr(
        "job_hunting_agent.model_gateway.build_rag_embeddings",
        lambda *args, **kwargs: delegate,
    )
    gateway = ModelGateway(
        tmp_path / ".env",
        embedding_settings=EmbeddingSettings(
            provider="test-provider",
            model="embedding-v1",
            api_key="test-key",
            base_url="https://embedding.example/v1",
            dimensions=2,
        ),
        business_cache=cache,
        embedding_cache_ttl_seconds=3600,
    )
    context = gateway.new_call_context("rag_query", authorize_spend=False)

    first = gateway.embeddings(context)
    second = gateway.embeddings(context)

    assert isinstance(first, CachedEmbeddings)
    assert first.embed_query("Python") == second.embed_query("Python")
    assert delegate.query_calls == ["Python"]


def test_profile_cache_is_invalidated_after_conversation_update(
    monkeypatch,
    tmp_path,
    database_url,
    account_id,
) -> None:
    cache = DictBusinessCache()
    app = JobHuntingApp(
        database_url=database_url,
        resume_dir=tmp_path / "files",
        business_cache=cache,
    )
    app.initialize()
    candidate_id = app.save_candidate_profile(
        CandidateProfileInput(
            name="小林",
            status="在职",
            education="本科",
            experience_years=2,
            skills={"Python": "熟练"},
            preferred_cities=["杭州"],
            acceptable_cities=[],
            salary_floor_k=12,
            expected_salary_k=16,
            target_directions=["后端开发"],
        ),
        account_id=account_id,
    )

    assert asdict(app.get_candidate_profile(candidate_id, account_id=account_id))["education"] == "本科"
    assert app.get_candidate_profile(candidate_id, account_id=account_id).education == "本科"

    monkeypatch.setattr(
        "job_hunting_agent.app.decide_conversation_ingestion",
        lambda candidate, message, llm_client: ConversationIngestionDecision(
            reply="已更新。",
            profile_updates=CandidateProfilePatch(education="硕士"),
            long_texts=[],
        ),
    )
    app.ingest_conversation_message(
        candidate_id,
        "学历改为硕士",
        account_id=account_id,
    )

    assert app.get_candidate_profile(candidate_id, account_id=account_id).education == "硕士"
    assert any(f"candidate:{candidate_id}" in key for key in cache.deleted)


def test_web_caches_public_config_and_invalidates_admin_summary_after_write(
    tmp_path,
) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            [
                "JOB_AGENT_BOOTSTRAP_ADMIN_EMAIL=cache-admin@example.com",
                "JOB_AGENT_BOOTSTRAP_ADMIN_PASSWORD=strong-password-123",
            ]
        ),
        encoding="utf-8",
    )
    cache = DictBusinessCache()
    web_app = create_web_app(env_file=env_file, business_cache=cache)
    client = TestClient(web_app)

    first_public = client.get("/api/auth/config")
    second_public = client.get("/api/auth/config")
    assert first_public.status_code == second_public.status_code == 200
    assert first_public.json() == second_public.json()
    assert "public:auth-config" in cache.values

    assert client.post(
        "/api/auth/login",
        json={
            "email": "cache-admin@example.com",
            "password": "strong-password-123",
        },
    ).status_code == 200
    first_summary = client.get("/api/admin/usage/summary")
    assert first_summary.status_code == 200
    assert "admin:usage-summary" in cache.values
    cache.deleted.clear()

    registered = client.post(
        "/api/auth/register",
        json={"email": "cache-user@example.com", "password": "password-123"},
    )
    assert registered.status_code == 200
    assert "admin:usage-summary" in cache.deleted
    assert "admin:usage-summary" not in cache.values

    refreshed = client.get("/api/admin/usage/summary")
    assert refreshed.status_code == 200
    assert refreshed.json()["billing"]["summary"]["account_count"] == 2
