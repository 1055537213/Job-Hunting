"""内部 Model Gateway 的配置、计量和重试回归测试。"""

from __future__ import annotations

import pytest

from job_hunting_agent.config import (
    EmbeddingSettings,
    LLMSettings,
    ModelGatewaySettings,
    RerankSettings,
    load_model_gateway_settings,
)
from job_hunting_agent.model_gateway import ModelGateway, extract_provider_request_id
from job_hunting_agent.rag import EmbeddingRequestError, OpenAICompatibleEmbeddings
from job_hunting_agent.sqlalchemy_store import SQLAlchemyStore
from job_hunting_agent.storage import IdempotencyConflictError, InsufficientBalanceError


def test_intent_router_disables_gateway_retries(monkeypatch, tmp_path):
    """路由器由总截止时间控制，不能叠加 SDK 逐次重试。"""

    captured: list[int] = []
    sentinel = object()

    def fake_build_chat_model(settings, temperature, max_retries, callbacks):
        captured.append(max_retries)
        return sentinel

    monkeypatch.setattr(
        "job_hunting_agent.model_gateway.build_chat_model",
        fake_build_chat_model,
    )
    gateway = ModelGateway(
        tmp_path / ".env",
        llm_settings=LLMSettings(
            provider="test",
            model="small",
            api_key="test-key",
            base_url="https://example.test/v1",
        ),
        settings=ModelGatewaySettings(environment="test", chat_max_retries=4),
    )

    model = gateway.chat_model("intent_router")
    main_model = gateway.chat_model("agent_chat")

    assert model is sentinel
    assert main_model is sentinel
    assert captured == [0, 4]


def test_gateway_settings_distinguish_runtime_environment_and_retry_policy(tmp_path):
    """Gateway 配置必须明确标记运行环境，并接受 0 次重试。"""

    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            [
                "JOB_AGENT_ENVIRONMENT=test",
                "JOB_AGENT_MODEL_GATEWAY_CHAT_MAX_RETRIES=0",
                "JOB_AGENT_MODEL_GATEWAY_EMBEDDING_MAX_RETRIES=3",
                "JOB_AGENT_MODEL_GATEWAY_RERANK_MAX_RETRIES=4",
                "JOB_AGENT_MODEL_CIRCUIT_FAILURE_THRESHOLD=7",
                "JOB_AGENT_MODEL_CIRCUIT_RECOVERY_SECONDS=45",
            ]
        ),
        encoding="utf-8",
    )

    settings = load_model_gateway_settings(env_file, environ={})

    assert settings.environment == "test"
    assert settings.chat_max_retries == 0
    assert settings.embedding_max_retries == 3
    assert settings.rerank_max_retries == 4
    assert settings.chat_circuit_failure_threshold == 7
    assert settings.chat_circuit_recovery_seconds == 45


def test_gateway_records_idempotent_provider_usage_without_prompt_content(tmp_path, database_url):
    """同一 call_id 重复上报时只保留一条可计费流水。"""

    store = SQLAlchemyStore(database_url)
    store.initialize()
    account = store.create_account("gateway@example.com", "not-used-by-this-test")
    store.create_simulated_recharge_order(
        account.id,
        1,
        idempotency_key="gateway-usage-test-funding",
        description="Gateway 测试充值",
    )
    gateway = ModelGateway(
        tmp_path / ".env",
        usage_store=store,
        llm_settings=LLMSettings(
            provider="relay",
            model="chat-model",
            api_key="secret-not-persisted",
            base_url="https://relay.example/v1",
        ),
        embedding_settings=EmbeddingSettings(
            provider="local_hash",
            model="local-hash",
            api_key="local",
            base_url="local",
        ),
        settings=ModelGatewaySettings(environment="test"),
    )
    context = gateway.new_call_context(
        "agent_chat",
        account_id=account.id,
        root_request_id="request-1",
        call_id="request-1-agent-chat-1",
    )

    first = gateway.record_chat_usage_summary(
        context,
        {"input_tokens": 11, "output_tokens": 7, "total_tokens": 18},
    )
    second = gateway.record_chat_usage_summary(
        context,
        {"input_tokens": 11, "output_tokens": 7, "total_tokens": 18},
    )
    events = store.list_usage_events(account_id=account.id)

    assert first["usage_source"] == "provider"
    assert second["usage_source"] == "provider"
    assert len(events) == 1
    assert events[0].call_id == "request-1-agent-chat-1"
    assert events[0].provider == "relay"
    assert events[0].model == "chat-model"
    assert events[0].raw_usage == {"input_tokens": 11, "output_tokens": 7, "total_tokens": 18}
    assert "secret-not-persisted" not in str(events[0].raw_usage)

    with pytest.raises(IdempotencyConflictError, match="另一笔用量记录"):
        gateway.record_chat_usage_summary(
            context,
            {"input_tokens": 999, "output_tokens": 999, "total_tokens": 1998},
        )


def test_gateway_extracts_provider_request_id_from_response_headers():
    """诊断可关联供应商请求，但不需要保存模型响应正文。"""

    request_id = extract_provider_request_id({"headers": {"x-request-id": "upstream-42"}})

    assert request_id == "upstream-42"


def test_gateway_records_rerank_usage_under_the_rerank_model_identity(tmp_path, database_url):
    """Rerank 的供应商 Token 用量必须进入现有账号级流水，供后续按量计费。"""

    store = SQLAlchemyStore(database_url)
    store.initialize()
    account = store.create_account("rerank@example.com", "not-used-by-this-test")
    store.create_simulated_recharge_order(
        account.id,
        1,
        idempotency_key="rerank-usage-test-funding",
        description="Rerank 测试充值",
    )
    gateway = ModelGateway(
        tmp_path / ".env",
        usage_store=store,
        rerank_settings=RerankSettings(
            provider="rerank-provider",
            model="rerank-model",
            api_key="secret-not-persisted",
            base_url="https://rerank.example/v1/rerank",
        ),
        settings=ModelGatewaySettings(environment="test"),
    )
    context = gateway.new_call_context(
        "rerank_query",
        account_id=account.id,
        root_request_id="request-rerank-1",
        call_id="request-rerank-1-rerank-query-1",
    )

    summary = gateway.record_rerank_response(
        context,
        {
            "request_id": "rerank-request-42",
            "usage": {"input_tokens": 12, "total_tokens": 12},
        },
    )
    events = store.list_usage_events(account_id=account.id)

    assert summary["usage_source"] == "provider"
    assert len(events) == 1
    assert events[0].operation == "rerank_query"
    assert events[0].provider == "rerank-provider"
    assert events[0].model == "rerank-model"
    assert events[0].provider_request_id == "rerank-request-42"
    assert events[0].total_tokens == 12
    assert "secret-not-persisted" not in str(events[0].raw_usage)


def test_gateway_can_create_post_call_context_after_balance_is_exhausted(tmp_path, database_url):
    """已经发生的供应商调用必须能落账，不能在记录阶段再次被余额准入拦截。"""

    store = SQLAlchemyStore(database_url)
    store.initialize()
    account = store.create_account("post-call-usage@example.com", "not-used-by-this-test")
    gateway = ModelGateway(
        tmp_path / ".env",
        usage_store=store,
        settings=ModelGatewaySettings(environment="test"),
    )

    with pytest.raises(InsufficientBalanceError):
        gateway.new_call_context("agent_chat", account_id=account.id)

    context = gateway.new_call_context(
        "agent_chat",
        account_id=account.id,
        authorize_spend=False,
    )

    assert context.account_id == account.id
    store.close()


def test_embedding_adapter_retries_transient_gateway_failure_once():
    """Embedding 的临时网络异常应遵循 Gateway 指定的有限重试次数。"""

    calls = 0

    def transport(url, headers, payload, timeout):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise EmbeddingRequestError("temporary upstream failure", status_code=503)
        return {
            "data": [{"index": 0, "embedding": [0.1, 0.2]}],
            "usage": {"prompt_tokens": 4, "total_tokens": 4},
        }

    embeddings = OpenAICompatibleEmbeddings(
        api_key="test-key",
        base_url="https://embedding.example/v1",
        model="embedding-model",
        transport=transport,
        max_retries=1,
    )

    assert embeddings.embed_query("候选人项目经历") == [0.1, 0.2]
    assert calls == 2


def test_gateway_exposes_independent_embedding_and_rerank_circuit_snapshots(tmp_path):
    """Gateway 应为远程 Embedding/Rerank 创建独立熔断状态。"""

    gateway = ModelGateway(
        tmp_path / ".env",
        llm_settings=LLMSettings(
            provider="relay",
            model="chat-model",
            api_key="chat-key",
            base_url="https://chat.example/v1",
        ),
        embedding_settings=EmbeddingSettings(
            provider="embedding-provider",
            model="embedding-model",
            api_key="embedding-key",
            base_url="https://embedding.example/v1",
        ),
        rerank_settings=RerankSettings(
            provider="rerank-provider",
            model="rerank-model",
            api_key="rerank-key",
            base_url="https://rerank.example/v1",
        ),
        settings=ModelGatewaySettings(
            environment="test",
            chat_circuit_failure_threshold=1,
            chat_circuit_recovery_seconds=10,
        ),
    )
    context = gateway.new_call_context("rag_probe", authorize_spend=False)

    gateway.embeddings(context)
    gateway.reranker(context)
    snapshot = gateway.circuit_snapshot()

    assert snapshot["state"] == "closed"
    assert snapshot["embedding"]["state"] == "closed"
    assert snapshot["rerank"]["state"] == "closed"
    assert snapshot["chat"]["state"] == "not_started"
