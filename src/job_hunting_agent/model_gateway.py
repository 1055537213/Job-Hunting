"""内部 Model Gateway。

这个模块是业务代码与模型供应商之间的唯一调用边界。它暂时仍运行在 FastAPI
模块化单体内，不增加网络跳转；因此可以先统一配置、调用 ID 和 Token 用量，再在
将来按需要拆成独立服务。

业务层不需要知道 DeepSeek、OpenAI-compatible 中转站或 Embedding HTTP 请求的
细节，只需要提供操作名和当前账号上下文。Gateway 负责：

- 读取并复用类型化模型配置；
- 为每次上游调用生成可追踪、可幂等的 ``call_id``；
- 把供应商返回的 usage 追加到现有用量流水；
- 将聊天模型和 Embedding 适配器暴露为稳定的 LangChain 接口。

这里不会记录 prompt、简历正文或模型回复，避免计量与诊断链路变成隐私正文的
旁路存储。
"""

from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable, Protocol

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.embeddings import Embeddings
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage

from .auth import utc_now
from .concurrency_control import ConcurrencyController, ConcurrencyLease
from .config import (
    DEFAULT_ENV_PATH,
    EmbeddingSettings,
    IntentRouterSettings,
    LLMSettings,
    ModelGatewaySettings,
    RerankSettings,
    load_embedding_settings,
    load_intent_router_settings,
    load_llm_settings,
    load_model_gateway_settings,
    load_rerank_settings,
)
from .llm import LangChainLLMClient, LLMClient, build_chat_model
from .models import UsageEventRecord
from .model_resilience import CircuitBreaker, ModelCircuitCallbackHandler
from .rag import (
    Reranker,
    RerankResult,
    build_rag_embeddings,
    build_reranker,
    extract_embedding_usage,
)


class UsageEventStore(Protocol):
    """Gateway 所需的最小用量持久化接口。

    使用 Protocol 隔离用量写入边界，便于在 PostgreSQL 上测试和替换实现。
    Repository 时保持 Gateway 的业务接口不变。
    """

    def record_usage_event(self, event: UsageEventRecord) -> UsageEventRecord:
        """追加一条幂等的模型用量流水。"""


@dataclass(frozen=True)
class ModelCallContext:
    """一次模型调用的可审计上下文。

    ``root_request_id`` 关联一次用户操作中的多次调用；``call_id`` 标识一次真实的
    上游调用。二者都不包含用户输入或候选人资料正文。
    """

    operation: str
    account_id: int | None
    candidate_id: int | None
    session_id: str | None
    root_request_id: str
    call_id: str
    attempt: int = 1

    def next_attempt(self) -> ModelCallContext:
        """返回同一调用的下一次重试上下文。"""

        return replace(self, attempt=self.attempt + 1)


class ModelConcurrencyCallbackHandler(BaseCallbackHandler):
    """只在真实聊天模型调用期间持有共享模型租约。"""

    raise_error = True

    def __init__(
        self,
        controller: ConcurrencyController,
        *,
        account_id: int | None = None,
    ) -> None:
        self.controller = controller
        self.account_id = account_id
        self._leases: dict[object, ConcurrencyLease] = {}
        self._starting: set[object] = set()
        self._lock = threading.Lock()

    def on_chat_model_start(
        self,
        serialized: dict[str, object],
        messages: list[list[BaseMessage]],
        *,
        run_id: object,
        metadata: dict[str, object] | None = None,
        **kwargs: object,
    ) -> None:
        self._start(run_id, metadata)

    def on_llm_start(
        self,
        serialized: dict[str, object],
        prompts: list[str],
        *,
        run_id: object,
        metadata: dict[str, object] | None = None,
        **kwargs: object,
    ) -> None:
        self._start(run_id, metadata)

    def on_llm_end(
        self,
        response: object,
        *,
        run_id: object,
        **kwargs: object,
    ) -> None:
        self._finish(run_id)

    def on_llm_error(
        self,
        error: BaseException,
        *,
        run_id: object,
        **kwargs: object,
    ) -> None:
        self._finish(run_id)

    def _start(
        self,
        run_id: object,
        metadata: dict[str, object] | None,
    ) -> None:
        with self._lock:
            if run_id in self._leases or run_id in self._starting:
                return
            self._starting.add(run_id)
        try:
            lease = self.controller.acquire(
                "model",
                account_id=self._resolve_account_id(metadata),
            )
        except Exception:
            with self._lock:
                self._starting.discard(run_id)
            raise
        with self._lock:
            self._starting.discard(run_id)
            self._leases[run_id] = lease

    def _finish(self, run_id: object) -> None:
        with self._lock:
            lease = self._leases.pop(run_id, None)
            self._starting.discard(run_id)
        if lease is not None:
            lease.release()

    def _resolve_account_id(
        self,
        metadata: dict[str, object] | None,
    ) -> int | None:
        if self.account_id is not None:
            return self.account_id
        value = metadata.get("account_id") if metadata else None
        if isinstance(value, bool):
            return None
        if isinstance(value, int):
            return value
        if isinstance(value, str) and value.isdigit():
            return int(value)
        return None


class ConcurrencyLimitedEmbeddings(Embeddings):
    """在真实 Embedding HTTP 请求期间获取并释放模型租约。"""

    def __init__(
        self,
        delegate: Embeddings,
        controller: ConcurrencyController,
        account_id: int | None,
    ) -> None:
        self.delegate = delegate
        self.controller = controller
        self.account_id = account_id
        # 暴露稳定的模型身份，确保文本查询向量与图片索引向量只在同一空间比较。
        self.model = getattr(delegate, "model", None)
        self.endpoint = getattr(delegate, "endpoint", None)
        self.embeddings_url = getattr(delegate, "embeddings_url", None)
        self.dimensions = getattr(delegate, "dimensions", None)

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        lease = self.controller.acquire("model", account_id=self.account_id)
        try:
            return self.delegate.embed_documents(texts)
        finally:
            lease.release()

    def embed_query(self, text: str) -> list[float]:
        lease = self.controller.acquire("model", account_id=self.account_id)
        try:
            return self.delegate.embed_query(text)
        finally:
            lease.release()

    def embed_images(self, images: list[tuple[bytes, str]]) -> list[list[float]]:
        """在同一模型并发租约下代理 provider-native 图片向量调用。"""

        embed_images = getattr(self.delegate, "embed_images", None)
        if not callable(embed_images):
            raise ValueError("当前 Embedding 配置不支持图片向量。")
        lease = self.controller.acquire("model", account_id=self.account_id)
        try:
            return embed_images(images)
        finally:
            lease.release()


class ConcurrencyLimitedReranker:
    """在真实 Rerank HTTP 请求期间获取并释放模型租约。"""

    def __init__(
        self,
        delegate: Reranker,
        controller: ConcurrencyController,
        account_id: int | None,
    ) -> None:
        self.delegate = delegate
        self.controller = controller
        self.account_id = account_id
        self.candidate_multiplier = delegate.candidate_multiplier

    def rerank(
        self,
        query: str,
        documents: list[str],
        top_n: int,
    ) -> list[RerankResult]:
        lease = self.controller.acquire("model", account_id=self.account_id)
        try:
            return self.delegate.rerank(query, documents, top_n)
        finally:
            lease.release()


class ModelGateway:
    """模块化单体中的模型调用门面。

    Gateway 不保存业务状态；业务数据和用量仍然由调用方注入的 PostgreSQL 存储负责。
    这样后续把 Gateway 单独部署时，只需要替换这一层的持久化上报实现。
    """

    def __init__(
        self,
        env_path: str | Path = DEFAULT_ENV_PATH,
        usage_store: UsageEventStore | None = None,
        *,
        llm_settings: LLMSettings | None = None,
        embedding_settings: EmbeddingSettings | None = None,
        rerank_settings: RerankSettings | None = None,
        intent_router_settings: IntentRouterSettings | None = None,
        settings: ModelGatewaySettings | None = None,
        concurrency_controller: ConcurrencyController | None = None,
    ):
        """绑定配置位置和可选的用量流水存储。

        配置采用惰性加载：离线测试、规则链和没有调用模型的网页操作无需提供
        ``.env`` 中的 API Key。
        """

        self.env_path = Path(env_path)
        self.usage_store = usage_store
        self._llm_settings = llm_settings
        self._embedding_settings = embedding_settings
        self._rerank_settings = rerank_settings
        self._intent_router_settings = intent_router_settings
        self._settings = settings
        self.concurrency_controller = concurrency_controller
        self._chat_circuit_breaker: CircuitBreaker | None = None
        self._embedding_circuit_breaker: CircuitBreaker | None = None
        self._rerank_circuit_breaker: CircuitBreaker | None = None

    @property
    def settings(self) -> ModelGatewaySettings:
        """读取 Gateway 策略配置。"""

        if self._settings is None:
            self._settings = load_model_gateway_settings(self.env_path)
        return self._settings

    @property
    def llm_settings(self) -> LLMSettings:
        """按需读取聊天模型配置。"""

        if self._llm_settings is None:
            self._llm_settings = load_llm_settings(self.env_path)
        return self._llm_settings

    @property
    def embedding_settings(self) -> EmbeddingSettings | None:
        """按需读取 Embedding 配置；None 表示本地 hash fallback。"""

        if self._embedding_settings is None:
            self._embedding_settings = load_embedding_settings(self.env_path)
        return self._embedding_settings

    @property
    def rerank_settings(self) -> RerankSettings | None:
        """按需读取 Rerank 配置；未配置时 RAG 保持纯向量检索。"""

        if self._rerank_settings is None:
            self._rerank_settings = load_rerank_settings(self.env_path)
        return self._rerank_settings

    @property
    def intent_router_settings(self) -> IntentRouterSettings:
        """按需读取可选的轻量意图路由模型配置。"""

        if self._intent_router_settings is None:
            self._intent_router_settings = load_intent_router_settings(self.env_path)
        return self._intent_router_settings

    def new_call_context(
        self,
        operation: str,
        *,
        account_id: int | None = None,
        candidate_id: int | None = None,
        session_id: str | None = None,
        root_request_id: str | None = None,
        call_id: str | None = None,
        attempt: int = 1,
        authorize_spend: bool = True,
    ) -> ModelCallContext:
        """创建调用上下文；真正发起调用前可执行一次余额准入。"""

        normalized_operation = normalize_operation(operation)
        if authorize_spend and self.usage_store is not None and account_id is not None:
            can_spend = getattr(self.usage_store, "assert_account_can_spend", None)
            if callable(can_spend):
                can_spend(account_id)
        resolved_root_request_id = root_request_id or uuid.uuid4().hex
        return ModelCallContext(
            operation=normalized_operation,
            account_id=account_id,
            candidate_id=candidate_id,
            session_id=session_id or None,
            root_request_id=resolved_root_request_id,
            call_id=call_id
            or f"{resolved_root_request_id}-{normalized_operation}-{uuid.uuid4().hex}",
            attempt=max(1, attempt),
        )

    def chat_model(
        self,
        operation: str,
        temperature: float = 0,
        *,
        account_id: int | None = None,
        llm_settings: LLMSettings | None = None,
    ) -> BaseChatModel:
        """构造供 LangChain Agent 使用的聊天模型。

        当前所有供应商仍通过 OpenAI-compatible 接口接入；未来新增供应商路由时只
        改这里，不让 Agent、简历改写或 Web 路由直接 import SDK。
        """

        normalized_operation = normalize_operation(operation)
        if self._chat_circuit_breaker is None:
            gateway_settings = self.settings
            self._chat_circuit_breaker = CircuitBreaker(
                failure_threshold=gateway_settings.chat_circuit_failure_threshold,
                recovery_seconds=gateway_settings.chat_circuit_recovery_seconds,
            )
        callbacks = [ModelCircuitCallbackHandler(self._chat_circuit_breaker)]
        if self.concurrency_controller is not None:
            callbacks.append(
                ModelConcurrencyCallbackHandler(
                    self.concurrency_controller,
                    account_id=account_id,
                )
            )
        return build_chat_model(
            llm_settings or self.llm_settings,
            temperature=temperature,
            # 路由器已有总截止时间，不能让 SDK 的逐次重试把用户等待放大到数倍。
            max_retries=(
                0 if normalized_operation == "intent_router" else self.settings.chat_max_retries
            ),
            callbacks=callbacks,
        )

    def circuit_snapshot(self) -> dict[str, object]:
        """返回各远程模型熔断状态，不触发惰性模型配置加载。"""

        snapshots = {
            "chat": self._circuit_snapshot(self._chat_circuit_breaker),
            "embedding": self._circuit_snapshot(self._embedding_circuit_breaker),
            "rerank": self._circuit_snapshot(self._rerank_circuit_breaker),
        }
        states = [snapshot["state"] for snapshot in snapshots.values()]
        if "open" in states:
            state = "open"
        elif "half_open" in states:
            state = "half_open"
        elif all(item == "not_started" for item in states):
            state = "not_started"
        else:
            state = "closed"
        return {
            "state": state,
            "consecutive_failures": max(
                int(snapshot["consecutive_failures"]) for snapshot in snapshots.values()
            ),
            "retry_after_seconds": max(
                int(snapshot["retry_after_seconds"]) for snapshot in snapshots.values()
            ),
            **snapshots,
        }

    def _get_embedding_circuit_breaker(self) -> CircuitBreaker:
        if self._embedding_circuit_breaker is None:
            gateway_settings = self.settings
            self._embedding_circuit_breaker = CircuitBreaker(
                failure_threshold=gateway_settings.chat_circuit_failure_threshold,
                recovery_seconds=gateway_settings.chat_circuit_recovery_seconds,
            )
        return self._embedding_circuit_breaker

    def _get_rerank_circuit_breaker(self) -> CircuitBreaker:
        if self._rerank_circuit_breaker is None:
            gateway_settings = self.settings
            self._rerank_circuit_breaker = CircuitBreaker(
                failure_threshold=gateway_settings.chat_circuit_failure_threshold,
                recovery_seconds=gateway_settings.chat_circuit_recovery_seconds,
            )
        return self._rerank_circuit_breaker

    @staticmethod
    def _circuit_snapshot(breaker: CircuitBreaker | None) -> dict[str, int | str]:
        if breaker is None:
            return {
                "state": "not_started",
                "consecutive_failures": 0,
                "retry_after_seconds": 0,
            }
        snapshot = breaker.snapshot()
        return {
            "state": snapshot.state,
            "consecutive_failures": snapshot.consecutive_failures,
            "retry_after_seconds": snapshot.retry_after_seconds,
        }

    def llm_client(
        self,
        context: ModelCallContext,
        temperature: float = 0,
        *,
        llm_settings: LLMSettings | None = None,
        usage_sink: Callable[[dict[str, int | str]], None] | None = None,
    ) -> LLMClient:
        """返回适合单次 prompt 场景的业务级 LLM 客户端。"""

        def record_response(response: BaseMessage | object) -> None:
            result = self.record_chat_response(context, response, llm_settings=llm_settings)
            if usage_sink is not None:
                usage_sink(result)

        return LangChainLLMClient(
            self.chat_model(
                context.operation,
                temperature=temperature,
                account_id=context.account_id,
                llm_settings=llm_settings,
            ),
            usage_callback=record_response,
        )

    def embeddings(self, context: ModelCallContext) -> Embeddings:
        """返回带 Gateway 用量回调的 LangChain Embeddings 实现。"""

        embedding_settings = self.embedding_settings
        circuit_breaker = None
        if embedding_settings is not None and embedding_settings.api_style != "local_hash":
            circuit_breaker = self._get_embedding_circuit_breaker()
        embeddings = build_rag_embeddings(
            self.env_path,
            settings=embedding_settings,
            usage_callback=lambda response: self.record_embedding_response(context, response),
            usage_operation=context.operation,
            max_retries=self.settings.embedding_max_retries,
            circuit_breaker=circuit_breaker,
        )
        if self.concurrency_controller is None or embedding_settings is None:
            return embeddings
        if embedding_settings.api_style == "local_hash":
            return embeddings
        return ConcurrencyLimitedEmbeddings(
            embeddings,
            self.concurrency_controller,
            context.account_id,
        )

    def reranker(self, context: ModelCallContext) -> Reranker | None:
        """返回带 Gateway 计量回调的可选 Rerank 实现。"""

        rerank_settings = self.rerank_settings
        if rerank_settings is None:
            return None
        reranker = build_reranker(
            self.env_path,
            settings=rerank_settings,
            usage_callback=lambda response: self.record_rerank_response(context, response),
            max_retries=self.settings.rerank_max_retries,
            circuit_breaker=self._get_rerank_circuit_breaker(),
        )
        if reranker is None or self.concurrency_controller is None:
            return reranker
        return ConcurrencyLimitedReranker(
            reranker,
            self.concurrency_controller,
            context.account_id,
        )

    def record_chat_response(
        self,
        context: ModelCallContext,
        response: BaseMessage | object,
        *,
        llm_settings: LLMSettings | None = None,
    ) -> dict[str, int | str]:
        """记录单次聊天模型响应的供应商 usage。"""

        return self.record_usage(
            context,
            extract_chat_usage(response),
            provider=self._chat_identity(llm_settings)[0],
            model=self._chat_identity(llm_settings)[1],
            provider_request_id=extract_provider_request_id(response),
        )

    def record_chat_messages(
        self,
        *,
        operation: str,
        messages: list[BaseMessage],
        account_id: int | None,
        candidate_id: int | None,
        session_id: str | None,
        root_request_id: str,
    ) -> dict[str, int | str]:
        """记录 Agent 一轮内每个真实 AI 响应的 usage，并返回总计。

        一个 Agent 回合可能先调用工具、再生成最终回答。将 AI 消息拆成独立流水能
        保留真实的上游调用数量，同时仍给页面返回本轮总 Token 数。
        """

        total = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
        recorded = 0
        for index, message in enumerate(messages):
            if not isinstance(message, AIMessage):
                continue
            usage = extract_chat_usage(message)
            for key in total:
                total[key] += usage[key]
            context = self.new_call_context(
                operation,
                account_id=account_id,
                candidate_id=candidate_id,
                session_id=session_id,
                root_request_id=root_request_id,
                call_id=f"{root_request_id}-{normalize_operation(operation)}-{index}",
                authorize_spend=False,
            )
            self.record_usage(
                context,
                usage,
                provider=self._chat_identity()[0],
                model=self._chat_identity()[1],
                provider_request_id=extract_provider_request_id(message),
            )
            recorded += 1

        if recorded == 0:
            # 即使上游异常或测试替身没有产生 AIMessage，也留下缺失 usage 的可追踪
            # 记录，便于区分“没有调用”和“调用结果未携带 usage”。
            context = self.new_call_context(
                operation,
                account_id=account_id,
                candidate_id=candidate_id,
                session_id=session_id,
                root_request_id=root_request_id,
                call_id=f"{root_request_id}-{normalize_operation(operation)}-missing",
                authorize_spend=False,
            )
            self.record_usage(
                context,
                total,
                provider=self._chat_identity()[0],
                model=self._chat_identity()[1],
            )
        return {**total, "usage_source": usage_source_for(total, status="succeeded")}

    def record_chat_usage_summary(
        self,
        context: ModelCallContext,
        usage: dict[str, int],
    ) -> dict[str, int | str]:
        """记录流式 Agent 聚合后的 usage。"""

        return self.record_usage(
            context,
            usage,
            provider=self._chat_identity()[0],
            model=self._chat_identity()[1],
        )

    def record_embedding_response(
        self,
        context: ModelCallContext,
        response: dict[str, object],
    ) -> dict[str, int | str]:
        """记录单次 Embedding HTTP 响应的供应商 usage。"""

        provider, model = self._embedding_identity()
        return self.record_usage(
            context,
            extract_embedding_usage(response),
            provider=provider,
            model=model,
            provider_request_id=extract_provider_request_id(response),
        )

    def record_rerank_response(
        self,
        context: ModelCallContext,
        response: dict[str, object],
    ) -> dict[str, int | str]:
        """记录一次 Rerank HTTP 响应的供应商用量。"""

        provider, model = self._rerank_identity()
        return self.record_usage(
            context,
            extract_embedding_usage(response),
            provider=provider,
            model=model,
            provider_request_id=extract_provider_request_id(response),
        )

    def record_usage(
        self,
        context: ModelCallContext,
        usage: dict[str, int] | None,
        *,
        provider: str,
        model: str,
        status: str = "succeeded",
        provider_request_id: str | None = None,
    ) -> dict[str, int | str]:
        """规范化 usage 并写入追加式流水。

        缺失 usage 只能标记为 ``missing``，不能用字符数或估算值伪造可计费用量。
        这条规则让后续账单能和供应商账单逐项对账。
        """

        normalized = normalize_usage(usage)
        source = usage_source_for(normalized, status=status)
        if self.usage_store is not None and context.account_id is not None:
            self.usage_store.record_usage_event(
                UsageEventRecord(
                    id=0,
                    account_id=context.account_id,
                    candidate_id=context.candidate_id,
                    session_id=context.session_id,
                    root_request_id=context.root_request_id,
                    call_id=context.call_id,
                    provider=provider,
                    model=model,
                    operation=context.operation,
                    input_tokens=normalized["input_tokens"],
                    output_tokens=normalized["output_tokens"],
                    total_tokens=normalized["total_tokens"],
                    usage_source=source,
                    status=status,
                    attempt=context.attempt,
                    provider_request_id=provider_request_id,
                    raw_usage=normalized,
                    created_at=utc_now().isoformat(timespec="seconds"),
                    billable=source == "provider",
                    pricing_version=None,
                )
            )
        return {**normalized, "usage_source": source}

    def _chat_identity(self, llm_settings: LLMSettings | None = None) -> tuple[str, str]:
        """返回不含密钥的聊天模型计量标签。"""

        if llm_settings is not None:
            return llm_settings.provider, llm_settings.model
        try:
            settings = self.llm_settings
        except ValueError:
            # 注入 FakeChatModel 的离线测试没有 .env，仍可验证业务和用量结构。
            return "unconfigured-llm", "agent"
        return settings.provider, settings.model

    def _embedding_identity(self) -> tuple[str, str]:
        """返回不含密钥的 Embedding 计量标签。"""

        settings = self.embedding_settings
        if settings is None:
            return "local_hash", "local-hash"
        return settings.provider, settings.model

    def _rerank_identity(self) -> tuple[str, str]:
        """返回不含密钥的 Rerank 计量标签。"""

        settings = self.rerank_settings
        if settings is None:
            return "disabled", "rerank"
        return settings.provider, settings.model


def normalize_operation(operation: str) -> str:
    """校验操作名，避免空值或任意空白写入计量流水。"""

    normalized = operation.strip().lower().replace(" ", "_")
    if not normalized:
        raise ValueError("模型调用 operation 不能为空")
    return normalized


def normalize_usage(usage: dict[str, int] | None) -> dict[str, int]:
    """统一 OpenAI/DeepSeek 常见 usage 字段并消除负数。"""

    raw = usage or {}
    input_tokens = int(raw.get("input_tokens", raw.get("prompt_tokens", 0)) or 0)
    output_tokens = int(raw.get("output_tokens", raw.get("completion_tokens", 0)) or 0)
    total_tokens = int(raw.get("total_tokens", input_tokens + output_tokens) or 0)
    return {
        "input_tokens": max(0, input_tokens),
        "output_tokens": max(0, output_tokens),
        "total_tokens": max(0, total_tokens),
    }


def usage_source_for(usage: dict[str, int], status: str) -> str:
    """仅成功且供应商确认了 Token 的调用才可以计费。"""

    if status == "succeeded" and usage.get("total_tokens", 0) > 0:
        return "provider"
    return "missing"


def extract_chat_usage(response: BaseMessage | object) -> dict[str, int]:
    """兼容 LangChain AIMessage 的 usage_metadata 和 response_metadata。"""

    usage = getattr(response, "usage_metadata", None)
    if not isinstance(usage, dict):
        response_metadata = getattr(response, "response_metadata", None)
        usage = response_metadata.get("token_usage") if isinstance(response_metadata, dict) else None
    return normalize_usage(usage if isinstance(usage, dict) else None)


def extract_provider_request_id(response: object) -> str | None:
    """尽可能读取供应商返回的 request ID，不读取或保存正文。"""

    metadata: object
    if isinstance(response, dict):
        metadata = response
    else:
        metadata = getattr(response, "response_metadata", None)
    if not isinstance(metadata, dict):
        return None
    for key in ("request_id", "id", "x_request_id"):
        value = metadata.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    headers = metadata.get("headers")
    if isinstance(headers, dict):
        for key in ("x-request-id", "request-id", "x_request_id"):
            value = headers.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return None
