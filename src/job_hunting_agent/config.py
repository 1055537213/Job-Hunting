"""项目配置加载。

模型 API Key、base URL、模型名等容易变化且包含敏感信息的内容，都从 `.env`
或系统环境变量读取，不写死在代码里。当前项目不依赖第三方 dotenv 包，
这里实现一个小型 `.env` 解析器，足够覆盖 `KEY=value` 这类常见配置。
"""

from __future__ import annotations

import math
import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

DEFAULT_ENV_PATH = Path(".env")

# RAG 采用标准两阶段漏斗：Retriever 先召回候选 Top-K，Reranker 再输出最终 Top-N。
DEFAULT_RAG_RETRIEVAL_TOP_K = 10
DEFAULT_RAG_RERANK_TOP_N = 5
DEFAULT_RAG_RERANK_MIN_RELEVANCE_SCORE = 0.65
DEFAULT_RAG_RERANK_RELATIVE_SCORE_THRESHOLD = 0.86
MAX_RAG_RETRIEVAL_TOP_K = 500
MAX_RAG_RERANK_TOP_N = 50

# 向量和重排接口的请求结构通过 `.env` 中的 API_STYLE 显式选择，地址由用户配置。


@dataclass(frozen=True)
class LLMSettings:
    """LLM 供应商配置。

    `api_key` 是敏感字段，只在内存中用于请求头，管理 API 和日志都不应该打印它。
    `provider` 是用于日志和计量的标签，不参与供应商白名单判断；只要接口兼容
    OpenAI Chat Completions，就可以通过 `.env` 切换官方服务、本地服务或中转站。
    """

    provider: str
    model: str
    api_key: str
    base_url: str
    timeout_seconds: int = 60
    enable_thinking: bool | None = None
    # 兼容旧版 DeepSeek 风格的 `thinking: {"type": "enabled"}` 透传字段。
    thinking: str | None = None
    reasoning_effort: str | None = None


@dataclass(frozen=True)
class ModelGatewaySettings:
    """内部 Model Gateway 的运行配置。

    这里的 ``environment`` 让同一套业务代码能明确区分本地开发、测试和生产运行，
    而不再依赖调用方自己猜测。模型供应商的密钥仍然由 ``LLMSettings`` 和
    ``EmbeddingSettings`` 管理，Gateway 只负责调用策略和统一入口。
    """

    environment: str = "development"
    chat_max_retries: int = 2
    embedding_max_retries: int = 2
    rerank_max_retries: int = 2
    chat_circuit_failure_threshold: int = 5
    chat_circuit_recovery_seconds: float = 30.0


@dataclass(frozen=True)
class ConcurrencySettings:
    """模型供应商与截图处理的共享并发配置。

    请求频率限制解决“单位时间内能发多少次”，这里解决“同一时刻有多少个昂贵操作
    正在执行”。Docker/生产使用 Redis 租约，开发和测试默认使用进程内实现。
    """

    enabled: bool = True
    environment: str = "development"
    backend: str = "memory"
    redis_url: str | None = None
    redis_timeout_seconds: float = 1.0
    key_prefix: str = "job_agent:concurrency"
    model_global_limit: int = 8
    model_account_limit: int = 2
    screenshot_global_limit: int = 2
    screenshot_account_limit: int = 1
    lease_ttl_seconds: int = 900
    wait_timeout_seconds: float = 5.0


@dataclass(frozen=True)
class DatabaseSettings:
    """生产数据库连接配置。

    Web 运行入口、迁移任务和测试均使用 PostgreSQL URL；没有配置时会直接报错，
    避免用户数据悄悄写入本地文件数据库。
    """

    url: str | None = None

    @property
    def configured(self) -> bool:
        """返回是否显式提供了数据库 URL。"""

        return bool(self.url)

    @property
    def dialect(self) -> str | None:
        """返回配置使用的数据库方言标签。"""

        if self.url is None:
            return None
        return self.url.split(":", 1)[0].split("+", 1)[0]

    @property
    def masked_url(self) -> str | None:
        """返回可显示的 URL 摘要，绝不回显数据库密码。"""

        if self.url is None:
            return None
        return mask_database_url(self.url)


@dataclass(frozen=True)
class ObjectStorageSettings:
    """受控二进制文件的对象存储配置。

    `local` 仅用于显式传入临时目录的单元测试或离线兼容场景；Docker Web
    服务使用 `s3`，并通过 MinIO 提供本地 S3-compatible API。
    """

    backend: str = "local"
    endpoint_url: str | None = None
    bucket: str | None = None
    access_key: str | None = None
    secret_key: str | None = None
    region: str = "us-east-1"
    force_path_style: bool = True
    auto_create_bucket: bool = False


@dataclass(frozen=True)
class FileScanningSettings:
    """上传文件安全扫描配置。

    `local` 只用于开发和测试；生产环境必须使用外部 ClamAV/clamd，避免把未扫描
    文件交给 OCR、解压、模型或 RAG。
    """

    backend: str = "local"
    host: str = "127.0.0.1"
    port: int = 3310
    timeout_seconds: float = 10.0


@dataclass(frozen=True)
class ProjectVisualAnalysisSettings:
    """项目图片和复杂 PDF 页面的多模态分析配置。

    视觉分析是本地文本/OCR 解析后的增强层。页数与单次图片数必须保持有界，避免
    一个大型工业文档把模型上下文、请求时长和用户余额一次性耗尽。
    """

    enabled: bool = True
    max_pdf_pages: int = 8
    max_images_per_call: int = 4
    batch_timeout_seconds: float = 90.0
    total_timeout_seconds: float = 300.0


@dataclass(frozen=True)
class TaskQueueSettings:
    """后台任务队列配置。

    Redis 的数据库 0 默认承担 Celery broker 的短期消息传递；任务状态、归属、进度和
    错误摘要始终写入 PostgreSQL。共享限流使用独立键空间，Compose 默认放在数据库 1。
    这样 Redis 重启或过期后，网页仍能从数据库恢复任务状态。
    """

    enabled: bool = False
    redis_url: str | None = None
    queue_name: str = "job_agent"
    task_time_limit_seconds: int = 900
    task_soft_time_limit_seconds: int = 840
    task_stale_after_seconds: int = 1800


@dataclass(frozen=True)
class WebSecuritySettings:
    """Web 边缘安全和基础观测配置。"""

    environment: str = "development"
    csrf_enabled: bool = True
    security_headers_enabled: bool = True
    rate_limit_enabled: bool = True
    rate_limit_window_seconds: int = 60
    rate_limit_default_requests: int = 240
    rate_limit_auth_requests: int = 20
    rate_limit_model_requests: int = 60
    rate_limit_upload_requests: int = 20
    rate_limit_admin_requests: int = 120
    rate_limit_write_requests: int = 120
    rate_limit_backend: str = "memory"
    rate_limit_redis_url: str | None = None
    rate_limit_redis_timeout_seconds: float = 1.0
    rate_limit_key_prefix: str = "job_agent:rate_limit"


@dataclass(frozen=True)
class ObservabilitySettings:
    """结构化日志和分布式追踪配置。

    日志只写到标准输出，由部署侧采集；Trace 通过内部 OTLP 端点发送。业务请求
    不依赖遥测后端成功响应，因此 Loki、Tempo 或 Alloy 故障不会阻断用户请求。
    """

    environment: str = "development"
    log_format: str = "console"
    log_level: str = "INFO"
    tracing_enabled: bool = False
    otlp_traces_endpoint: str = "http://alloy:4318/v1/traces"
    trace_sample_ratio: float = 0.1
    export_timeout_seconds: float = 5.0


@dataclass(frozen=True)
class AccountLifecycleSettings:
    """注册验证、协议留痕和账号找回配置。"""

    environment: str = "development"
    registration_enabled: bool = True
    email_verification_required: bool = False
    consent_required: bool = False
    public_base_url: str = "http://127.0.0.1:8000"
    email_backend: str = "console"
    smtp_host: str | None = None
    smtp_port: int = 587
    smtp_username: str | None = None
    smtp_password: str | None = None
    smtp_from_email: str | None = None
    smtp_use_starttls: bool = True
    action_secret: str = "development-only-account-action-secret"
    email_request_cooldown_seconds: int = 60
    email_account_hourly_limit: int = 5
    email_source_hourly_limit: int = 20
    email_outbox_max_attempts: int = 5
    email_retry_base_seconds: int = 30
    email_claim_timeout_seconds: int = 300
    email_outbox_retention_days: int = 14
    verification_token_ttl_minutes: int = 1440
    password_reset_token_ttl_minutes: int = 30
    terms_version: str = "development"
    privacy_version: str = "development"


@dataclass(frozen=True)
class BillingSettings:
    """实时扣费与余额展示配置。

    余额以“微元”计量，便于把 `25/M token` 这类价格精确换算成逐次扣费。
    `starting_balance_yuan` 仅用于新账号初始化和历史回填；`low_balance_threshold_yuan`
    用于前端状态提示，不直接改变可用余额。
    """

    price_per_million_tokens_yuan: float = 25.0
    starting_balance_yuan: float = 0.0
    low_balance_threshold_yuan: float = 10.0


@dataclass(frozen=True)
class BootstrapAdminSettings:
    """首次启动时创建管理员账号的一次性配置。

    该密码只在数据库尚无管理员账号时读取并写入 Argon2id 哈希，不会出现在
    API 响应、健康检查或日志中。常规管理员登录后应从 `.env` 删除密码配置。
    """

    email: str
    password: str
    display_name: str | None = None


@dataclass(frozen=True)
class EmbeddingSettings:
    """Embedding 供应商配置。

    这组配置与聊天模型配置分开保存：很多供应商提供聊天模型但不提供 embedding，
    或者两者的计费、模型名、接口地址并不相同。
    """

    provider: str
    model: str
    api_key: str
    base_url: str
    timeout_seconds: int = 60
    # provider 只作为计量标签；api_style 决定 HTTP 请求/响应结构。
    api_style: str = "openai_compatible"
    batch_size: int = 64
    dimensions: int | None = None


@dataclass(frozen=True)
class RerankSettings:
    """Rerank 供应商配置。

    Rerank 只在向量检索拿到候选证据后调用，用于按“查询 + 证据正文”重新排序；
    它不参与 pgvector 建库，因此更换 rerank 模型无需重建向量索引。
    """

    provider: str
    model: str
    api_key: str
    base_url: str
    # Rerank 没有跨供应商统一标准，必须声明端点采用的协议样式。
    api_style: str = "standard"
    timeout_seconds: int = 60
    retrieval_top_k: int = DEFAULT_RAG_RETRIEVAL_TOP_K
    min_relevance_score: float = DEFAULT_RAG_RERANK_MIN_RELEVANCE_SCORE
    relative_score_threshold: float = DEFAULT_RAG_RERANK_RELATIVE_SCORE_THRESHOLD

    @property
    def candidate_multiplier(self) -> int:
        """兼容旧调用方的只读别名；新代码应直接使用 retrieval_top_k。"""

        return max(
            1,
            math.ceil(self.retrieval_top_k / DEFAULT_RAG_RERANK_TOP_N),
        )


@dataclass(frozen=True)
class AgentMemorySettings:
    """Agent 对话记忆配置。

    `restore_history_limit` 控制启动恢复时最多读取多少条 PostgreSQL 聊天记录。
    `restore_trigger_tokens` 控制恢复历史过长时何时先压缩再交给 Agent。
    `summary_trigger_tokens` 控制 LangChain 运行中何时触发自动总结。
    `summary_keep_messages` 表示总结后保留最近多少条原文消息。
    """

    enabled: bool = True
    checkpoint_backend: str = "database"
    restore_history_limit: int = 200
    restore_trigger_tokens: int = 12000
    restore_keep_messages: int = 24
    restore_summary_chars: int = 6000
    summary_trigger_tokens: int = 12000
    summary_keep_messages: int = 24
    summary_trim_tokens: int = 6000


@dataclass(frozen=True)
class IntentRouterSettings:
    """轻量意图路由模型配置。

    路由模型默认关闭，保持现有 Agent 行为不变；只有显式配置独立模型并启用后，
    才会在主 Agent 前尝试处理高置信度的简单请求。
    """

    enabled: bool = False
    llm: LLMSettings | None = None
    confidence_threshold: float = 0.9
    history_messages: int = 6
    hard_timeout_seconds: float = 3.0


def load_dotenv_values(env_path: str | Path = DEFAULT_ENV_PATH) -> dict[str, str]:
    """读取 `.env` 文件并返回键值字典。

    解析器支持空行、注释、`export KEY=value` 和单双引号包裹的值。它不会把值写入
    `os.environ`，避免测试或多项目运行时互相污染。
    """

    path = Path(env_path)
    if not path.exists():
        return {}

    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line.removeprefix("export ").strip()
        key, value = line.split("=", 1)
        key = key.strip()
        value = strip_env_value(value.strip())
        if key:
            values[key] = value
    return values


def strip_env_value(value: str) -> str:
    """去掉 `.env` 值外层引号，并保留内部内容。"""

    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value


def load_llm_settings(
    env_path: str | Path = DEFAULT_ENV_PATH,
    environ: Mapping[str, str] | None = None,
) -> LLMSettings:
    """从 `.env` 和系统环境变量加载 LLM 配置。

    优先级是系统环境变量高于 `.env`。项目专用 `JOB_AGENT_LLM_*` 优先，其次兼容
    通用 `OPENAI_*` 和已有学习项目使用的 `DEEPSEEK_*` 键名。
    """

    file_values = load_dotenv_values(env_path)
    environment = os.environ if environ is None else environ

    def get(*keys: str, default: str | None = None) -> str | None:
        """按优先级读取多个候选键名。"""

        for key in keys:
            if environment.get(key):
                return environment[key]
            if file_values.get(key):
                return file_values[key]
        return default

    provider = get("JOB_AGENT_LLM_PROVIDER")
    model = get("JOB_AGENT_LLM_MODEL", "OPENAI_MODEL", "DEEPSEEK_MODEL")
    api_key = get("JOB_AGENT_LLM_API_KEY", "OPENAI_API_KEY", "DEEPSEEK_API_KEY")
    base_url = get("JOB_AGENT_LLM_BASE_URL", "OPENAI_BASE_URL", "DEEPSEEK_BASE_URL")
    timeout = int(get("JOB_AGENT_LLM_TIMEOUT_SECONDS", default="60") or 60)
    # 新键采用布尔值，适合 OpenAI-compatible 中转站；保留旧键是为了让已经配置
    # DeepSeek `thinking: {type: ...}` 的本地 `.env` 不会在升级后被静默忽略。
    raw_enable_thinking = get("JOB_AGENT_LLM_ENABLE_THINKING")
    enable_thinking = parse_bool(raw_enable_thinking) if raw_enable_thinking is not None else None
    thinking = get("JOB_AGENT_LLM_THINKING")
    reasoning_effort = get("JOB_AGENT_LLM_REASONING_EFFORT")

    if not provider:
        raise ValueError("缺少 LLM provider：请在 .env 中配置 JOB_AGENT_LLM_PROVIDER")
    if not api_key:
        raise ValueError("缺少 LLM API Key：请在 .env 中配置 JOB_AGENT_LLM_API_KEY")
    if not base_url:
        raise ValueError("缺少 LLM base URL：请在 .env 中配置 JOB_AGENT_LLM_BASE_URL")
    if not model:
        raise ValueError("缺少 LLM 模型名：请在 .env 中配置 JOB_AGENT_LLM_MODEL")

    return LLMSettings(
        provider=provider.lower(),
        model=model,
        api_key=api_key,
        base_url=base_url,
        timeout_seconds=timeout,
        enable_thinking=enable_thinking,
        thinking=thinking,
        reasoning_effort=reasoning_effort,
    )


def load_intent_router_settings(
    env_path: str | Path = DEFAULT_ENV_PATH,
    environ: Mapping[str, str] | None = None,
) -> IntentRouterSettings:
    """加载可选的轻量意图路由模型配置。

    路由模型默认复用主模型的 provider、API key 和 base URL，只覆盖模型名；
    也支持为路由器配置完全独立的 OpenAI-compatible 端点。
    """

    file_values = load_dotenv_values(env_path)
    environment = os.environ if environ is None else environ

    def get(*keys: str, default: str | None = None) -> str | None:
        """按系统环境变量优先、再到 `.env` 的顺序读取配置。"""

        for key in keys:
            if environment.get(key):
                return environment[key]
            if file_values.get(key):
                return file_values[key]
        return default

    enabled = parse_bool(get("JOB_AGENT_INTENT_ROUTER_ENABLED", default="false"))
    model = get("JOB_AGENT_INTENT_ROUTER_MODEL")
    if not enabled or not model:
        return IntentRouterSettings(enabled=False)

    provider = get("JOB_AGENT_INTENT_ROUTER_PROVIDER", "JOB_AGENT_LLM_PROVIDER")
    api_key = get("JOB_AGENT_INTENT_ROUTER_API_KEY", "JOB_AGENT_LLM_API_KEY", "OPENAI_API_KEY")
    base_url = get("JOB_AGENT_INTENT_ROUTER_BASE_URL", "JOB_AGENT_LLM_BASE_URL", "OPENAI_BASE_URL")
    if not provider or not api_key or not base_url:
        raise ValueError(
            "意图路由模型配置不完整：请配置 JOB_AGENT_INTENT_ROUTER_PROVIDER、"
            "JOB_AGENT_INTENT_ROUTER_API_KEY 和 JOB_AGENT_INTENT_ROUTER_BASE_URL，"
            "或复用 JOB_AGENT_LLM_* 配置。"
        )

    timeout = int(
        get("JOB_AGENT_INTENT_ROUTER_TIMEOUT_SECONDS", "JOB_AGENT_LLM_TIMEOUT_SECONDS", default="10")
        or 10
    )
    threshold = float(get("JOB_AGENT_INTENT_ROUTER_CONFIDENCE_THRESHOLD", default="0.9") or 0.9)
    history_messages = int(get("JOB_AGENT_INTENT_ROUTER_HISTORY_MESSAGES", default="6") or 6)
    hard_timeout_seconds = parse_positive_float(
        get("JOB_AGENT_INTENT_ROUTER_HARD_TIMEOUT_SECONDS", default="3") or "3",
        "JOB_AGENT_INTENT_ROUTER_HARD_TIMEOUT_SECONDS",
    )
    if not 0.0 <= threshold <= 1.0:
        raise ValueError("JOB_AGENT_INTENT_ROUTER_CONFIDENCE_THRESHOLD 必须在 0 到 1 之间。")
    if history_messages < 0 or history_messages > 20:
        raise ValueError("JOB_AGENT_INTENT_ROUTER_HISTORY_MESSAGES 必须在 0 到 20 之间。")

    return IntentRouterSettings(
        enabled=True,
        llm=LLMSettings(
            provider=provider.lower(),
            model=model,
            api_key=api_key,
            base_url=base_url,
            timeout_seconds=timeout,
            enable_thinking=False,
            thinking=None,
            reasoning_effort=None,
        ),
        confidence_threshold=threshold,
        history_messages=history_messages,
        hard_timeout_seconds=hard_timeout_seconds,
    )


def masked_llm_settings(settings: LLMSettings) -> dict[str, object]:
    """返回适合管理健康检查展示的配置摘要，不泄露 API Key。"""

    return {
        "provider": settings.provider,
        "model": settings.model,
        "base_url": settings.base_url,
        "api_key_set": bool(settings.api_key),
        "timeout_seconds": settings.timeout_seconds,
        "enable_thinking": settings.enable_thinking,
        "thinking": settings.thinking,
        "reasoning_effort": settings.reasoning_effort,
    }


def load_model_gateway_settings(
    env_path: str | Path = DEFAULT_ENV_PATH,
    environ: Mapping[str, str] | None = None,
) -> ModelGatewaySettings:
    """读取内部 Model Gateway 的非敏感运行策略。

    ``JOB_AGENT_ENVIRONMENT`` 目前支持 ``development``、``test`` 和
    ``production``。重试次数可设为 0，表示只尝试一次；它们只控制 Gateway
    自己管理的调用，具体模型供应商仍会保留其 SDK 的必要保护逻辑。
    """

    file_values = load_dotenv_values(env_path)
    environment = os.environ if environ is None else environ

    def get(*keys: str, default: str | None = None) -> str | None:
        """按系统环境变量优先级读取 Gateway 配置。"""

        for key in keys:
            if environment.get(key):
                return environment[key]
            if file_values.get(key):
                return file_values[key]
        return default

    runtime_environment = (get("JOB_AGENT_ENVIRONMENT", default="development") or "development").lower()
    if runtime_environment not in {"development", "test", "production"}:
        raise ValueError(
            "JOB_AGENT_ENVIRONMENT 只能是 development、test 或 production"
        )
    return ModelGatewaySettings(
        environment=runtime_environment,
        chat_max_retries=parse_non_negative_int(
            get("JOB_AGENT_MODEL_GATEWAY_CHAT_MAX_RETRIES", default="2"),
            "JOB_AGENT_MODEL_GATEWAY_CHAT_MAX_RETRIES",
        ),
        embedding_max_retries=parse_non_negative_int(
            get("JOB_AGENT_MODEL_GATEWAY_EMBEDDING_MAX_RETRIES", default="2"),
            "JOB_AGENT_MODEL_GATEWAY_EMBEDDING_MAX_RETRIES",
        ),
        rerank_max_retries=parse_non_negative_int(
            get("JOB_AGENT_MODEL_GATEWAY_RERANK_MAX_RETRIES", default="2"),
            "JOB_AGENT_MODEL_GATEWAY_RERANK_MAX_RETRIES",
        ),
        chat_circuit_failure_threshold=parse_positive_int(
            get("JOB_AGENT_MODEL_CIRCUIT_FAILURE_THRESHOLD", default="5"),
            "JOB_AGENT_MODEL_CIRCUIT_FAILURE_THRESHOLD",
        ),
        chat_circuit_recovery_seconds=parse_positive_float(
            get("JOB_AGENT_MODEL_CIRCUIT_RECOVERY_SECONDS", default="30"),
            "JOB_AGENT_MODEL_CIRCUIT_RECOVERY_SECONDS",
        ),
    )


def load_concurrency_settings(
    env_path: str | Path = DEFAULT_ENV_PATH,
    environ: Mapping[str, str] | None = None,
) -> ConcurrencySettings:
    """读取模型和截图共享并发租约配置。"""

    file_values = load_dotenv_values(env_path)
    environment = os.environ if environ is None else environ

    def get(*keys: str, default: str | None = None) -> str | None:
        for key in keys:
            if environment.get(key):
                return environment[key]
            if file_values.get(key):
                return file_values[key]
        return default

    runtime_environment = (
        get("JOB_AGENT_ENVIRONMENT", default="development") or "development"
    ).lower()
    if runtime_environment not in {"development", "test", "production"}:
        raise ValueError(
            "JOB_AGENT_ENVIRONMENT 只能是 development、test 或 production"
        )
    backend = (
        get(
            "JOB_AGENT_CONCURRENCY_BACKEND",
            default="redis" if runtime_environment == "production" else "memory",
        )
        or "memory"
    ).strip().lower()
    if backend not in {"memory", "redis"}:
        raise ValueError("JOB_AGENT_CONCURRENCY_BACKEND 只能是 memory 或 redis")
    redis_url = get(
        "JOB_AGENT_CONCURRENCY_REDIS_URL",
        "JOB_AGENT_RATE_LIMIT_REDIS_URL",
        "JOB_AGENT_REDIS_URL",
    )
    if redis_url:
        parsed_url = urlsplit(redis_url)
        if parsed_url.scheme not in {"redis", "rediss"} or not parsed_url.netloc:
            raise ValueError(
                "JOB_AGENT_CONCURRENCY_REDIS_URL 必须使用 redis:// 或 rediss:// 地址。"
            )
    key_prefix = (
        get("JOB_AGENT_CONCURRENCY_KEY_PREFIX", default="job_agent:concurrency")
        or "job_agent:concurrency"
    ).strip(": ")
    if not key_prefix or len(key_prefix) > 80:
        raise ValueError("JOB_AGENT_CONCURRENCY_KEY_PREFIX 必须为 1 到 80 个字符")

    settings = ConcurrencySettings(
        enabled=parse_bool(get("JOB_AGENT_CONCURRENCY_ENABLED", default="true")),
        environment=runtime_environment,
        backend=backend,
        redis_url=redis_url,
        redis_timeout_seconds=parse_positive_float(
            get("JOB_AGENT_CONCURRENCY_REDIS_TIMEOUT_SECONDS", default="1"),
            "JOB_AGENT_CONCURRENCY_REDIS_TIMEOUT_SECONDS",
        ),
        key_prefix=key_prefix,
        model_global_limit=parse_positive_int(
            get("JOB_AGENT_MODEL_GLOBAL_CONCURRENCY", default="8"),
            "JOB_AGENT_MODEL_GLOBAL_CONCURRENCY",
        ),
        model_account_limit=parse_positive_int(
            get("JOB_AGENT_MODEL_ACCOUNT_CONCURRENCY", default="2"),
            "JOB_AGENT_MODEL_ACCOUNT_CONCURRENCY",
        ),
        screenshot_global_limit=parse_positive_int(
            get("JOB_AGENT_SCREENSHOT_GLOBAL_CONCURRENCY", default="2"),
            "JOB_AGENT_SCREENSHOT_GLOBAL_CONCURRENCY",
        ),
        screenshot_account_limit=parse_positive_int(
            get("JOB_AGENT_SCREENSHOT_ACCOUNT_CONCURRENCY", default="1"),
            "JOB_AGENT_SCREENSHOT_ACCOUNT_CONCURRENCY",
        ),
        lease_ttl_seconds=parse_positive_int(
            get("JOB_AGENT_CONCURRENCY_LEASE_TTL_SECONDS", default="900"),
            "JOB_AGENT_CONCURRENCY_LEASE_TTL_SECONDS",
        ),
        wait_timeout_seconds=parse_non_negative_float(
            get("JOB_AGENT_CONCURRENCY_WAIT_TIMEOUT_SECONDS", default="5"),
            "JOB_AGENT_CONCURRENCY_WAIT_TIMEOUT_SECONDS",
        ),
    )
    if settings.environment == "production":
        if not settings.enabled:
            raise ValueError("生产环境不能关闭共享并发保护。")
        if settings.backend != "redis":
            raise ValueError("生产环境必须使用 Redis 共享并发租约。")
    if settings.enabled and settings.backend == "redis" and not settings.redis_url:
        raise ValueError(
            "启用 Redis 共享并发租约时必须配置 JOB_AGENT_CONCURRENCY_REDIS_URL。"
        )
    return settings


def masked_concurrency_settings(settings: ConcurrencySettings) -> dict[str, object]:
    """返回不含 Redis 地址和账号标识的共享并发配置摘要。"""

    return {
        "enabled": settings.enabled,
        "backend": settings.backend,
        "redis_configured": bool(settings.redis_url),
        "model_global_limit": settings.model_global_limit,
        "model_account_limit": settings.model_account_limit,
        "screenshot_global_limit": settings.screenshot_global_limit,
        "screenshot_account_limit": settings.screenshot_account_limit,
        "lease_ttl_seconds": settings.lease_ttl_seconds,
        "wait_timeout_seconds": settings.wait_timeout_seconds,
    }


def load_database_settings(
    env_path: str | Path = DEFAULT_ENV_PATH,
    environ: Mapping[str, str] | None = None,
) -> DatabaseSettings:
    """从 .env 或系统环境变量读取数据库 URL。

    PostgreSQL 统一归一化为 postgresql+psycopg，避免运行时因 SQLAlchemy
    自动选择不同驱动而产生行为差异。未配置时返回空 Settings，由运行入口决定
    是否应当拒绝启动。
    """

    file_values = load_dotenv_values(env_path)
    environment = os.environ if environ is None else environ
    raw_url = environment.get("JOB_AGENT_DATABASE_URL") or file_values.get(
        "JOB_AGENT_DATABASE_URL"
    )
    if not raw_url or not raw_url.strip():
        return DatabaseSettings()
    return DatabaseSettings(url=normalize_database_url(raw_url.strip()))


def load_bootstrap_admin_settings(
    env_path: str | Path = DEFAULT_ENV_PATH,
    environ: Mapping[str, str] | None = None,
) -> BootstrapAdminSettings | None:
    """读取可选的首次管理员引导配置。

    普通用户仍只能通过网页注册为 `user`。只有数据库尚无管理员时，Web 启动会
    使用此配置创建一个 `admin`，因此不需要保留公开或日常使用的终端创建入口。
    """

    file_values = load_dotenv_values(env_path)
    environment = os.environ if environ is None else environ

    def get(key: str) -> str | None:
        return environment.get(key) or file_values.get(key)

    email = get("JOB_AGENT_BOOTSTRAP_ADMIN_EMAIL")
    password = get("JOB_AGENT_BOOTSTRAP_ADMIN_PASSWORD")
    display_name = get("JOB_AGENT_BOOTSTRAP_ADMIN_DISPLAY_NAME")
    if not email and not password:
        return None
    if not email or not password:
        raise ValueError(
            "首次管理员配置必须同时提供 JOB_AGENT_BOOTSTRAP_ADMIN_EMAIL 和 "
            "JOB_AGENT_BOOTSTRAP_ADMIN_PASSWORD。"
        )
    return BootstrapAdminSettings(
        email=email.strip(),
        password=password,
        display_name=display_name.strip() if display_name else None,
    )


def require_postgresql_database_url(settings: DatabaseSettings) -> str:
    """返回可用的 PostgreSQL URL，拒绝缺失配置和非 PostgreSQL 方言。"""

    if not settings.configured or settings.url is None:
        raise ValueError("缺少 JOB_AGENT_DATABASE_URL；运行时必须连接 PostgreSQL。")
    if settings.dialect != "postgresql":
        raise ValueError("项目只支持 PostgreSQL 数据库 URL。")
    return settings.url


def normalize_database_url(value: str) -> str:
    """校验并归一化 SQLAlchemy 数据库 URL。

    项目统一使用 psycopg 驱动的 PostgreSQL URL；其他方言会在配置阶段被拒绝。
    """

    normalized = value.strip()
    if normalized.startswith("postgres://"):
        normalized = "postgresql+psycopg://" + normalized.removeprefix("postgres://")
    elif normalized.startswith("postgresql://"):
        normalized = "postgresql+psycopg://" + normalized.removeprefix("postgresql://")

    if not normalized.startswith("postgresql+psycopg://"):
        raise ValueError("数据库 URL 只支持 postgresql+psycopg 或 postgresql 协议。")
    return normalized


def mask_database_url(value: str) -> str:
    """掩码数据库 URL 中的密码，供日志和健康检查展示。"""

    parts = urlsplit(value)
    if "@" not in parts.netloc:
        return value
    credentials, host = parts.netloc.rsplit("@", 1)
    username = credentials.split(":", 1)[0]
    masked_credentials = username + ":***" if ":" in credentials else username
    return urlunsplit(
        (parts.scheme, masked_credentials + "@" + host, parts.path, parts.query, parts.fragment)
    )


def load_object_storage_settings(
    env_path: str | Path = DEFAULT_ENV_PATH,
    environ: Mapping[str, str] | None = None,
) -> ObjectStorageSettings:
    """从环境变量或 `.env` 读取对象存储配置。

    环境变量优先于文件；启用 S3 时缺少 endpoint 或凭证会立即报错，避免
    上传请求运行到一半才发现文件没有可写的持久化位置。
    """

    file_values = load_dotenv_values(env_path)
    environment = os.environ if environ is None else environ

    def get(key: str, default: str | None = None) -> str | None:
        """按环境变量优先级读取一个对象存储配置项。"""

        value = environment.get(key) or file_values.get(key)
        return value if value not in {None, ""} else default

    raw_backend = get("JOB_AGENT_OBJECT_STORAGE_BACKEND")
    if raw_backend is None:
        raise ValueError(
            "必须显式配置 JOB_AGENT_OBJECT_STORAGE_BACKEND；生产使用 s3，测试可使用 local。"
        )
    backend_aliases = {
        "file": "local",
        "filesystem": "local",
        "local": "local",
        "minio": "s3",
        "s3": "s3",
    }
    backend = backend_aliases.get(raw_backend.strip().lower())
    if backend is None:
        raise ValueError(
            "JOB_AGENT_OBJECT_STORAGE_BACKEND 只能是 local、s3 或 minio。"
        )
    if backend == "local":
        return ObjectStorageSettings(backend="local")

    endpoint_url = get("JOB_AGENT_OBJECT_STORAGE_ENDPOINT")
    bucket = get("JOB_AGENT_OBJECT_STORAGE_BUCKET", "job-agent-files")
    access_key = get("JOB_AGENT_OBJECT_STORAGE_ACCESS_KEY")
    secret_key = get("JOB_AGENT_OBJECT_STORAGE_SECRET_KEY")
    if not endpoint_url:
        raise ValueError(
            "启用 S3 对象存储时必须配置 JOB_AGENT_OBJECT_STORAGE_ENDPOINT。"
        )
    if not access_key or not secret_key:
        raise ValueError(
            "启用 S3 对象存储时必须配置 JOB_AGENT_OBJECT_STORAGE_ACCESS_KEY 和 "
            "JOB_AGENT_OBJECT_STORAGE_SECRET_KEY。"
        )
    return ObjectStorageSettings(
        backend="s3",
        endpoint_url=endpoint_url.rstrip("/"),
        bucket=bucket,
        access_key=access_key,
        secret_key=secret_key,
        region=get("JOB_AGENT_OBJECT_STORAGE_REGION", "us-east-1") or "us-east-1",
        force_path_style=parse_bool(
            get("JOB_AGENT_OBJECT_STORAGE_FORCE_PATH_STYLE", "true")
        ),
        auto_create_bucket=parse_bool(
            get("JOB_AGENT_OBJECT_STORAGE_AUTO_CREATE_BUCKET", "false")
        ),
    )


def load_file_scanning_settings(
    env_path: str | Path = DEFAULT_ENV_PATH,
    environ: Mapping[str, str] | None = None,
) -> FileScanningSettings:
    """读取上传文件恶意扫描配置，并在生产环境禁止本地伪扫描。"""

    file_values = load_dotenv_values(env_path)
    environment = os.environ if environ is None else environ

    def get(key: str, default: str | None = None) -> str | None:
        value = environment.get(key) or file_values.get(key)
        return value if value not in {None, ""} else default

    runtime_environment = (get("JOB_AGENT_ENVIRONMENT", "development") or "development").lower()
    if runtime_environment not in {"development", "test", "production"}:
        raise ValueError("JOB_AGENT_ENVIRONMENT 只能是 development、test 或 production")
    default_backend = "clamav" if runtime_environment == "production" else "local"
    backend = (get("JOB_AGENT_FILE_SCAN_BACKEND", default_backend) or default_backend).strip().lower()
    if backend not in {"local", "clamav"}:
        raise ValueError("JOB_AGENT_FILE_SCAN_BACKEND 只能是 local 或 clamav")
    if runtime_environment == "production" and backend != "clamav":
        raise ValueError("生产环境必须使用 ClamAV 文件安全扫描")
    return FileScanningSettings(
        backend=backend,
        host=(get("JOB_AGENT_FILE_SCAN_HOST", "127.0.0.1") or "127.0.0.1").strip(),
        port=parse_positive_int(
            get("JOB_AGENT_FILE_SCAN_PORT", "3310"),
            "JOB_AGENT_FILE_SCAN_PORT",
        ),
        timeout_seconds=parse_positive_float(
            get("JOB_AGENT_FILE_SCAN_TIMEOUT_SECONDS", "10"),
            "JOB_AGENT_FILE_SCAN_TIMEOUT_SECONDS",
        ),
    )


def masked_file_scanning_settings(settings: FileScanningSettings) -> dict[str, object]:
    """返回健康检查可展示的扫描配置，不回显网络凭据。"""

    return {
        "backend": settings.backend,
        "host": settings.host,
        "port": settings.port,
        "timeout_seconds": settings.timeout_seconds,
    }


def load_project_visual_analysis_settings(
    env_path: str | Path = DEFAULT_ENV_PATH,
    environ: Mapping[str, str] | None = None,
) -> ProjectVisualAnalysisSettings:
    """读取项目视觉分析开关和成本边界。"""

    file_values = load_dotenv_values(env_path)
    environment = os.environ if environ is None else environ

    def get(key: str, default: str) -> str:
        value = environment.get(key) or file_values.get(key)
        return value if value not in {None, ""} else default

    max_pdf_pages = parse_positive_int(
        get("JOB_AGENT_PROJECT_VISUAL_MAX_PDF_PAGES", "8"),
        "JOB_AGENT_PROJECT_VISUAL_MAX_PDF_PAGES",
    )
    max_images_per_call = parse_positive_int(
        get("JOB_AGENT_PROJECT_VISUAL_MAX_IMAGES_PER_CALL", "4"),
        "JOB_AGENT_PROJECT_VISUAL_MAX_IMAGES_PER_CALL",
    )
    batch_timeout_seconds = parse_positive_float(
        get("JOB_AGENT_PROJECT_VISUAL_BATCH_TIMEOUT_SECONDS", "120"),
        "JOB_AGENT_PROJECT_VISUAL_BATCH_TIMEOUT_SECONDS",
    )
    total_timeout_seconds = parse_positive_float(
        get("JOB_AGENT_PROJECT_VISUAL_TOTAL_TIMEOUT_SECONDS", "300"),
        "JOB_AGENT_PROJECT_VISUAL_TOTAL_TIMEOUT_SECONDS",
    )
    if max_pdf_pages > 20:
        raise ValueError("JOB_AGENT_PROJECT_VISUAL_MAX_PDF_PAGES 不能超过 20")
    if max_images_per_call > 8:
        raise ValueError("JOB_AGENT_PROJECT_VISUAL_MAX_IMAGES_PER_CALL 不能超过 8")
    if batch_timeout_seconds > 600:
        raise ValueError("JOB_AGENT_PROJECT_VISUAL_BATCH_TIMEOUT_SECONDS 不能超过 600")
    if total_timeout_seconds > 1_800:
        raise ValueError("JOB_AGENT_PROJECT_VISUAL_TOTAL_TIMEOUT_SECONDS 不能超过 1800")
    if total_timeout_seconds < batch_timeout_seconds:
        raise ValueError(
            "JOB_AGENT_PROJECT_VISUAL_TOTAL_TIMEOUT_SECONDS 不能小于单批次超时时间"
        )
    return ProjectVisualAnalysisSettings(
        enabled=parse_bool(get("JOB_AGENT_PROJECT_VISUAL_ANALYSIS_ENABLED", "true")),
        max_pdf_pages=max_pdf_pages,
        max_images_per_call=max_images_per_call,
        batch_timeout_seconds=batch_timeout_seconds,
        total_timeout_seconds=total_timeout_seconds,
    )


def masked_project_visual_analysis_settings(
    settings: ProjectVisualAnalysisSettings,
) -> dict[str, object]:
    """返回不含模型密钥的项目视觉分析运行摘要。"""

    return {
        "enabled": settings.enabled,
        "max_pdf_pages": settings.max_pdf_pages,
        "max_images_per_call": settings.max_images_per_call,
        "batch_timeout_seconds": settings.batch_timeout_seconds,
        "total_timeout_seconds": settings.total_timeout_seconds,
    }


def masked_object_storage_settings(settings: ObjectStorageSettings) -> dict[str, object]:
    """返回健康检查可展示的对象存储摘要，不暴露访问密钥。"""

    return {
        "backend": settings.backend,
        "endpoint_url": settings.endpoint_url,
        "bucket": settings.bucket,
        "access_key_set": bool(settings.access_key),
        "secret_key_set": bool(settings.secret_key),
        "region": settings.region,
        "force_path_style": settings.force_path_style,
        "auto_create_bucket": settings.auto_create_bucket,
    }


def load_task_queue_settings(
    env_path: str | Path = DEFAULT_ENV_PATH,
    environ: Mapping[str, str] | None = None,
) -> TaskQueueSettings:
    """从环境变量或 `.env` 读取 Redis/Celery 后台任务配置。

    本地直接运行 Web 时默认关闭，保持教学和单元测试不依赖 Redis；Docker Compose
    会显式开启，并把容器内 Redis 地址注入 Web 与 Worker。
    """

    file_values = load_dotenv_values(env_path)
    environment = os.environ if environ is None else environ

    def get(key: str, default: str | None = None) -> str | None:
        """按系统环境变量优先级读取单个任务队列配置项。"""

        value = environment.get(key) or file_values.get(key)
        return value if value not in {None, ""} else default

    enabled = parse_bool(get("JOB_AGENT_TASK_QUEUE_ENABLED", "false"))
    if not enabled:
        return TaskQueueSettings(enabled=False)

    redis_url = get("JOB_AGENT_REDIS_URL")
    if not redis_url:
        raise ValueError("启用后台任务队列时必须配置 JOB_AGENT_REDIS_URL。")
    parsed_url = urlsplit(redis_url)
    if parsed_url.scheme not in {"redis", "rediss"} or not parsed_url.netloc:
        raise ValueError("JOB_AGENT_REDIS_URL 必须使用 redis:// 或 rediss:// 地址。")

    queue_name = (get("JOB_AGENT_TASK_QUEUE_NAME", "job_agent") or "job_agent").strip()
    if not queue_name:
        raise ValueError("JOB_AGENT_TASK_QUEUE_NAME 不能为空。")
    time_limit = parse_positive_int(
        get("JOB_AGENT_TASK_TIME_LIMIT_SECONDS", "900"),
        "JOB_AGENT_TASK_TIME_LIMIT_SECONDS",
    )
    soft_time_limit = parse_positive_int(
        get("JOB_AGENT_TASK_SOFT_TIME_LIMIT_SECONDS", "840"),
        "JOB_AGENT_TASK_SOFT_TIME_LIMIT_SECONDS",
    )
    if soft_time_limit >= time_limit:
        raise ValueError(
            "JOB_AGENT_TASK_SOFT_TIME_LIMIT_SECONDS 必须小于 JOB_AGENT_TASK_TIME_LIMIT_SECONDS。"
        )
    stale_after = parse_positive_int(
        get("JOB_AGENT_TASK_STALE_AFTER_SECONDS", "1800"),
        "JOB_AGENT_TASK_STALE_AFTER_SECONDS",
    )
    if stale_after <= time_limit:
        raise ValueError(
            "JOB_AGENT_TASK_STALE_AFTER_SECONDS 必须大于 JOB_AGENT_TASK_TIME_LIMIT_SECONDS。"
        )
    return TaskQueueSettings(
        enabled=True,
        redis_url=redis_url,
        queue_name=queue_name,
        task_time_limit_seconds=time_limit,
        task_soft_time_limit_seconds=soft_time_limit,
        task_stale_after_seconds=stale_after,
    )


def masked_task_queue_settings(settings: TaskQueueSettings) -> dict[str, object]:
    """返回可用于健康检查的任务队列摘要，不回显 Redis 密码。"""

    if not settings.enabled or not settings.redis_url:
        return {"enabled": False}
    parsed_url = urlsplit(settings.redis_url)
    hostname = parsed_url.hostname or ""
    port = f":{parsed_url.port}" if parsed_url.port else ""
    return {
        "enabled": True,
        "redis_url": f"{parsed_url.scheme}://{hostname}{port}{parsed_url.path or '/0'}",
        "queue_name": settings.queue_name,
        "task_time_limit_seconds": settings.task_time_limit_seconds,
        "task_soft_time_limit_seconds": settings.task_soft_time_limit_seconds,
        "task_stale_after_seconds": settings.task_stale_after_seconds,
    }


def load_web_security_settings(
    env_path: str | Path = DEFAULT_ENV_PATH,
    environ: Mapping[str, str] | None = None,
) -> WebSecuritySettings:
    """读取 Web 安全、限流和请求观测配置。"""

    file_values = load_dotenv_values(env_path)
    environment = os.environ if environ is None else environ

    def get(*keys: str, default: str | None = None) -> str | None:
        for key in keys:
            if environment.get(key):
                return environment[key]
            if file_values.get(key):
                return file_values[key]
        return default

    runtime_environment = (get("JOB_AGENT_ENVIRONMENT", default="development") or "development").lower()
    if runtime_environment not in {"development", "test", "production"}:
        raise ValueError(
            "JOB_AGENT_ENVIRONMENT 只能是 development、test 或 production"
        )
    rate_limit_backend = (
        get(
            "JOB_AGENT_RATE_LIMIT_BACKEND",
            default="redis" if runtime_environment == "production" else "memory",
        )
        or "memory"
    ).strip().lower()
    if rate_limit_backend not in {"memory", "redis"}:
        raise ValueError("JOB_AGENT_RATE_LIMIT_BACKEND 只能是 memory 或 redis")
    rate_limit_redis_url = get(
        "JOB_AGENT_RATE_LIMIT_REDIS_URL",
        "JOB_AGENT_REDIS_URL",
    )
    if rate_limit_redis_url:
        parsed_rate_limit_url = urlsplit(rate_limit_redis_url)
        if (
            parsed_rate_limit_url.scheme not in {"redis", "rediss"}
            or not parsed_rate_limit_url.netloc
        ):
            raise ValueError(
                "JOB_AGENT_RATE_LIMIT_REDIS_URL 必须使用 redis:// 或 rediss:// 地址。"
            )
    rate_limit_key_prefix = (
        get("JOB_AGENT_RATE_LIMIT_KEY_PREFIX", default="job_agent:rate_limit")
        or "job_agent:rate_limit"
    ).strip(": ")
    if not rate_limit_key_prefix or len(rate_limit_key_prefix) > 80:
        raise ValueError("JOB_AGENT_RATE_LIMIT_KEY_PREFIX 必须为 1 到 80 个字符")

    settings = WebSecuritySettings(
        environment=runtime_environment,
        csrf_enabled=parse_bool(get("JOB_AGENT_CSRF_ENABLED", default="true")),
        security_headers_enabled=parse_bool(
            get("JOB_AGENT_SECURITY_HEADERS_ENABLED", default="true")
        ),
        rate_limit_enabled=parse_bool(get("JOB_AGENT_RATE_LIMIT_ENABLED", default="true")),
        rate_limit_window_seconds=parse_positive_int(
            get("JOB_AGENT_RATE_LIMIT_WINDOW_SECONDS", default="60"),
            "JOB_AGENT_RATE_LIMIT_WINDOW_SECONDS",
        ),
        rate_limit_default_requests=parse_positive_int(
            get("JOB_AGENT_RATE_LIMIT_DEFAULT_REQUESTS", default="240"),
            "JOB_AGENT_RATE_LIMIT_DEFAULT_REQUESTS",
        ),
        rate_limit_auth_requests=parse_positive_int(
            get("JOB_AGENT_RATE_LIMIT_AUTH_REQUESTS", default="20"),
            "JOB_AGENT_RATE_LIMIT_AUTH_REQUESTS",
        ),
        rate_limit_model_requests=parse_positive_int(
            get("JOB_AGENT_RATE_LIMIT_MODEL_REQUESTS", default="60"),
            "JOB_AGENT_RATE_LIMIT_MODEL_REQUESTS",
        ),
        rate_limit_upload_requests=parse_positive_int(
            get("JOB_AGENT_RATE_LIMIT_UPLOAD_REQUESTS", default="20"),
            "JOB_AGENT_RATE_LIMIT_UPLOAD_REQUESTS",
        ),
        rate_limit_admin_requests=parse_positive_int(
            get("JOB_AGENT_RATE_LIMIT_ADMIN_REQUESTS", default="120"),
            "JOB_AGENT_RATE_LIMIT_ADMIN_REQUESTS",
        ),
        rate_limit_write_requests=parse_positive_int(
            get("JOB_AGENT_RATE_LIMIT_WRITE_REQUESTS", default="120"),
            "JOB_AGENT_RATE_LIMIT_WRITE_REQUESTS",
        ),
        rate_limit_backend=rate_limit_backend,
        rate_limit_redis_url=rate_limit_redis_url,
        rate_limit_redis_timeout_seconds=parse_positive_float(
            get("JOB_AGENT_RATE_LIMIT_REDIS_TIMEOUT_SECONDS", default="1"),
            "JOB_AGENT_RATE_LIMIT_REDIS_TIMEOUT_SECONDS",
        ),
        rate_limit_key_prefix=rate_limit_key_prefix,
    )
    if settings.environment == "production":
        disabled_controls = [
            name
            for name, enabled in (
                ("CSRF", settings.csrf_enabled),
                ("安全响应头", settings.security_headers_enabled),
                ("请求限流", settings.rate_limit_enabled),
            )
            if not enabled
        ]
        if disabled_controls:
            raise ValueError(
                "生产环境不能关闭以下 Web 安全控制：" + "、".join(disabled_controls)
            )
        if settings.rate_limit_backend != "redis":
            raise ValueError("生产环境必须使用 Redis 分布式请求限流。")
    if (
        settings.rate_limit_enabled
        and settings.rate_limit_backend == "redis"
        and not settings.rate_limit_redis_url
    ):
        raise ValueError(
            "启用 Redis 请求限流时必须配置 JOB_AGENT_RATE_LIMIT_REDIS_URL。"
        )
    return settings


def load_observability_settings(
    env_path: str | Path = DEFAULT_ENV_PATH,
    environ: Mapping[str, str] | None = None,
) -> ObservabilitySettings:
    """读取日志与 Trace 配置，并为生产环境提供保守默认值。"""

    file_values = load_dotenv_values(env_path)
    environment = os.environ if environ is None else environ

    def get(key: str, default: str | None = None) -> str | None:
        value = environment.get(key) or file_values.get(key)
        return value if value not in {None, ""} else default

    runtime_environment = (
        get("JOB_AGENT_ENVIRONMENT", "development") or "development"
    ).strip().lower()
    if runtime_environment not in {"development", "test", "production"}:
        raise ValueError(
            "JOB_AGENT_ENVIRONMENT 只能是 development、test 或 production"
        )

    log_format = (
        get(
            "JOB_AGENT_LOG_FORMAT",
            "json" if runtime_environment == "production" else "console",
        )
        or "console"
    ).strip().lower()
    if log_format not in {"console", "json"}:
        raise ValueError("JOB_AGENT_LOG_FORMAT 只能是 console 或 json")

    log_level = (get("JOB_AGENT_LOG_LEVEL", "INFO") or "INFO").strip().upper()
    if log_level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
        raise ValueError(
            "JOB_AGENT_LOG_LEVEL 只能是 DEBUG、INFO、WARNING、ERROR 或 CRITICAL"
        )

    sample_ratio = parse_non_negative_float(
        get("JOB_AGENT_OTEL_TRACE_SAMPLE_RATIO", "0.1"),
        "JOB_AGENT_OTEL_TRACE_SAMPLE_RATIO",
    )
    if sample_ratio > 1:
        raise ValueError("JOB_AGENT_OTEL_TRACE_SAMPLE_RATIO 必须在 0 到 1 之间")

    endpoint = (
        get(
            "JOB_AGENT_OTEL_EXPORTER_OTLP_TRACES_ENDPOINT",
            "http://alloy:4318/v1/traces",
        )
        or ""
    ).strip()
    parsed_endpoint = urlsplit(endpoint)
    if parsed_endpoint.scheme not in {"http", "https"} or not parsed_endpoint.netloc:
        raise ValueError(
            "JOB_AGENT_OTEL_EXPORTER_OTLP_TRACES_ENDPOINT 必须是 http(s) URL"
        )
    if parsed_endpoint.username or parsed_endpoint.password:
        raise ValueError("OTLP Trace 地址不能在 URL 中包含账号或密码")

    return ObservabilitySettings(
        environment=runtime_environment,
        log_format=log_format,
        log_level=log_level,
        tracing_enabled=parse_bool(
            get(
                "JOB_AGENT_OTEL_ENABLED",
                "true" if runtime_environment == "production" else "false",
            )
        ),
        otlp_traces_endpoint=endpoint,
        trace_sample_ratio=sample_ratio,
        export_timeout_seconds=parse_positive_float(
            get("JOB_AGENT_OTEL_EXPORT_TIMEOUT_SECONDS", "5"),
            "JOB_AGENT_OTEL_EXPORT_TIMEOUT_SECONDS",
        ),
    )


def masked_web_security_settings(settings: WebSecuritySettings) -> dict[str, object]:
    """返回适合管理员健康检查展示的 Web 安全配置。"""

    return {
        "environment": settings.environment,
        "csrf_enabled": settings.csrf_enabled,
        "security_headers_enabled": settings.security_headers_enabled,
        "rate_limit_enabled": settings.rate_limit_enabled,
        "rate_limit_window_seconds": settings.rate_limit_window_seconds,
        "rate_limit_default_requests": settings.rate_limit_default_requests,
        "rate_limit_auth_requests": settings.rate_limit_auth_requests,
        "rate_limit_model_requests": settings.rate_limit_model_requests,
        "rate_limit_upload_requests": settings.rate_limit_upload_requests,
        "rate_limit_admin_requests": settings.rate_limit_admin_requests,
        "rate_limit_write_requests": settings.rate_limit_write_requests,
        "rate_limit_backend": settings.rate_limit_backend,
        "rate_limit_redis_configured": bool(settings.rate_limit_redis_url),
    }


def load_account_lifecycle_settings(
    env_path: str | Path = DEFAULT_ENV_PATH,
    environ: Mapping[str, str] | None = None,
) -> AccountLifecycleSettings:
    """读取账号注册、邮件和协议版本配置。"""

    file_values = load_dotenv_values(env_path)
    environment = os.environ if environ is None else environ

    def get(key: str, default: str | None = None) -> str | None:
        return environment.get(key) or file_values.get(key) or default

    runtime_environment = (get("JOB_AGENT_ENVIRONMENT", "development") or "development").lower()
    if runtime_environment not in {"development", "test", "production"}:
        raise ValueError("JOB_AGENT_ENVIRONMENT 只能是 development、test 或 production")
    default_required = "true" if runtime_environment == "production" else "false"
    email_backend = (
        get(
            "JOB_AGENT_ACCOUNT_EMAIL_BACKEND",
            "smtp" if runtime_environment == "production" else "console",
        )
        or "console"
    ).strip().lower()
    if email_backend not in {"console", "smtp"}:
        raise ValueError("JOB_AGENT_ACCOUNT_EMAIL_BACKEND 只能是 console 或 smtp")
    settings = AccountLifecycleSettings(
        environment=runtime_environment,
        registration_enabled=parse_bool(get("JOB_AGENT_REGISTRATION_ENABLED", "true")),
        email_verification_required=parse_bool(
            get("JOB_AGENT_EMAIL_VERIFICATION_REQUIRED", default_required)
        ),
        consent_required=parse_bool(get("JOB_AGENT_CONSENT_REQUIRED", default_required)),
        public_base_url=(
            get("JOB_AGENT_PUBLIC_BASE_URL", "http://127.0.0.1:8000")
            or "http://127.0.0.1:8000"
        ).rstrip("/"),
        email_backend=email_backend,
        smtp_host=get("JOB_AGENT_SMTP_HOST"),
        smtp_port=parse_positive_int(get("JOB_AGENT_SMTP_PORT", "587"), "JOB_AGENT_SMTP_PORT"),
        smtp_username=get("JOB_AGENT_SMTP_USERNAME"),
        smtp_password=get("JOB_AGENT_SMTP_PASSWORD"),
        smtp_from_email=get("JOB_AGENT_SMTP_FROM_EMAIL"),
        smtp_use_starttls=parse_bool(get("JOB_AGENT_SMTP_USE_STARTTLS", "true")),
        action_secret=(
            get(
                "JOB_AGENT_ACCOUNT_ACTION_SECRET",
                "development-only-account-action-secret",
            )
            or "development-only-account-action-secret"
        ),
        email_request_cooldown_seconds=parse_positive_int(
            get("JOB_AGENT_ACCOUNT_EMAIL_COOLDOWN_SECONDS", "60"),
            "JOB_AGENT_ACCOUNT_EMAIL_COOLDOWN_SECONDS",
        ),
        email_account_hourly_limit=parse_positive_int(
            get("JOB_AGENT_ACCOUNT_EMAIL_HOURLY_LIMIT", "5"),
            "JOB_AGENT_ACCOUNT_EMAIL_HOURLY_LIMIT",
        ),
        email_source_hourly_limit=parse_positive_int(
            get("JOB_AGENT_ACCOUNT_EMAIL_SOURCE_HOURLY_LIMIT", "20"),
            "JOB_AGENT_ACCOUNT_EMAIL_SOURCE_HOURLY_LIMIT",
        ),
        email_outbox_max_attempts=parse_positive_int(
            get("JOB_AGENT_ACCOUNT_EMAIL_MAX_ATTEMPTS", "5"),
            "JOB_AGENT_ACCOUNT_EMAIL_MAX_ATTEMPTS",
        ),
        email_retry_base_seconds=parse_positive_int(
            get("JOB_AGENT_ACCOUNT_EMAIL_RETRY_BASE_SECONDS", "30"),
            "JOB_AGENT_ACCOUNT_EMAIL_RETRY_BASE_SECONDS",
        ),
        email_claim_timeout_seconds=parse_positive_int(
            get("JOB_AGENT_ACCOUNT_EMAIL_CLAIM_TIMEOUT_SECONDS", "300"),
            "JOB_AGENT_ACCOUNT_EMAIL_CLAIM_TIMEOUT_SECONDS",
        ),
        email_outbox_retention_days=parse_positive_int(
            get("JOB_AGENT_ACCOUNT_EMAIL_RETENTION_DAYS", "14"),
            "JOB_AGENT_ACCOUNT_EMAIL_RETENTION_DAYS",
        ),
        verification_token_ttl_minutes=parse_positive_int(
            get("JOB_AGENT_VERIFICATION_TOKEN_TTL_MINUTES", "1440"),
            "JOB_AGENT_VERIFICATION_TOKEN_TTL_MINUTES",
        ),
        password_reset_token_ttl_minutes=parse_positive_int(
            get("JOB_AGENT_PASSWORD_RESET_TOKEN_TTL_MINUTES", "30"),
            "JOB_AGENT_PASSWORD_RESET_TOKEN_TTL_MINUTES",
        ),
        terms_version=(get("JOB_AGENT_TERMS_VERSION", "development") or "development").strip(),
        privacy_version=(get("JOB_AGENT_PRIVACY_VERSION", "development") or "development").strip(),
    )
    parsed_public_url = urlsplit(settings.public_base_url)
    if parsed_public_url.scheme not in {"http", "https"} or not parsed_public_url.netloc:
        raise ValueError("JOB_AGENT_PUBLIC_BASE_URL 必须是完整的 http:// 或 https:// 地址。")
    if settings.environment == "production":
        if not settings.email_verification_required or not settings.consent_required:
            raise ValueError("生产环境必须启用邮箱验证和协议同意。")
        if parsed_public_url.scheme != "https":
            raise ValueError("生产环境的 JOB_AGENT_PUBLIC_BASE_URL 必须使用 HTTPS。")
        if settings.email_backend != "smtp":
            raise ValueError("生产环境必须使用 SMTP 发送账号邮件。")
        if not settings.smtp_host or not settings.smtp_from_email:
            raise ValueError("生产环境必须配置 JOB_AGENT_SMTP_HOST 和 JOB_AGENT_SMTP_FROM_EMAIL。")
        if (
            len(settings.action_secret) < 32
            or settings.action_secret == "development-only-account-action-secret"
        ):
            raise ValueError("生产环境必须配置至少 32 个字符的 JOB_AGENT_ACCOUNT_ACTION_SECRET。")
        if not parse_bool(get("JOB_AGENT_TASK_QUEUE_ENABLED", "false")):
            raise ValueError("生产环境账号邮件必须启用 Celery 后台任务队列。")
    return settings


def masked_account_lifecycle_settings(settings: AccountLifecycleSettings) -> dict[str, object]:
    """返回不含 SMTP 凭据的账号生命周期配置摘要。"""

    return {
        "registration_enabled": settings.registration_enabled,
        "email_verification_required": settings.email_verification_required,
        "consent_required": settings.consent_required,
        "email_backend": settings.email_backend,
        "smtp_configured": bool(settings.smtp_host and settings.smtp_from_email),
        "email_delivery": "postgresql_outbox",
        "email_request_cooldown_seconds": settings.email_request_cooldown_seconds,
        "email_account_hourly_limit": settings.email_account_hourly_limit,
        "email_source_hourly_limit": settings.email_source_hourly_limit,
        "email_outbox_max_attempts": settings.email_outbox_max_attempts,
        "terms_version": settings.terms_version,
        "privacy_version": settings.privacy_version,
    }


def load_billing_settings(
    env_path: str | Path = DEFAULT_ENV_PATH,
    environ: Mapping[str, str] | None = None,
) -> BillingSettings:
    """读取实时计费和余额配置。"""

    file_values = load_dotenv_values(env_path)
    environment = os.environ if environ is None else environ

    def get(*keys: str, default: str | None = None) -> str | None:
        for key in keys:
            if environment.get(key):
                return environment[key]
            if file_values.get(key):
                return file_values[key]
        return default

    settings = BillingSettings(
        price_per_million_tokens_yuan=parse_positive_float(
            get("JOB_AGENT_BILLING_PRICE_PER_MILLION_TOKENS_YUAN", default="25"),
            "JOB_AGENT_BILLING_PRICE_PER_MILLION_TOKENS_YUAN",
        ),
        starting_balance_yuan=parse_non_negative_float(
            get("JOB_AGENT_BILLING_STARTING_BALANCE_YUAN", default="0"),
            "JOB_AGENT_BILLING_STARTING_BALANCE_YUAN",
        ),
        low_balance_threshold_yuan=parse_positive_float(
            get("JOB_AGENT_BILLING_LOW_BALANCE_THRESHOLD_YUAN", default="10"),
            "JOB_AGENT_BILLING_LOW_BALANCE_THRESHOLD_YUAN",
        ),
    )
    return settings


def masked_billing_settings(settings: BillingSettings) -> dict[str, object]:
    """返回适合健康检查和管理页展示的账单配置。"""

    return {
        "configured": True,
        "price_per_million_tokens_yuan": settings.price_per_million_tokens_yuan,
        "starting_balance_yuan": settings.starting_balance_yuan,
        "low_balance_threshold_yuan": settings.low_balance_threshold_yuan,
    }


def normalize_embedding_api_style(value: str | None) -> str:
    """把 Embedding 协议别名归一化为内部适配器名称。"""

    normalized = (value or "openai_compatible").strip().lower().replace("-", "_")
    aliases = {
        "openai": "openai_compatible",
        "openai_compatible": "openai_compatible",
        "embeddings": "openai_compatible",
        "native": "native_multimodal",
        "native_multimodal": "native_multimodal",
        "multimodal": "native_multimodal",
        "local": "local_hash",
        "local_hash": "local_hash",
    }
    try:
        return aliases[normalized]
    except KeyError as error:
        raise ValueError(
            "不支持的 Embedding API_STYLE："
            f"{value}；可选 openai_compatible、native_multimodal 或 local_hash"
        ) from error


def normalize_rerank_api_style(value: str | None) -> str:
    """把 Rerank 协议别名归一化为内部适配器名称。"""

    normalized = (value or "standard").strip().lower().replace("-", "_")
    aliases = {
        "standard": "standard",
        "standard_rerank": "standard",
        "rerank": "standard",
        "native": "native",
        "provider_native": "native",
    }
    try:
        return aliases[normalized]
    except KeyError as error:
        raise ValueError(
            "不支持的 Rerank API_STYLE："
            f"{value}；可选 standard 或 native"
        ) from error


def load_embedding_settings(
    env_path: str | Path = DEFAULT_ENV_PATH,
    environ: Mapping[str, str] | None = None,
) -> EmbeddingSettings | None:
    """从 `.env` 和系统环境变量加载 embedding 配置。

    如果完全没有提供 embedding 配置，则返回 None，表示回退到本地 hash embedding。
    如果只配了一部分字段，则抛出异常，避免用户误以为已经启用了真实语义向量。
    """

    file_values = load_dotenv_values(env_path)
    environment = os.environ if environ is None else environ

    def get(*keys: str, default: str | None = None) -> str | None:
        """按优先级读取多个候选键名。"""

        for key in keys:
            if environment.get(key):
                return environment[key]
            if file_values.get(key):
                return file_values[key]
        return default

    provider = get("JOB_AGENT_EMBEDDING_PROVIDER")
    model = get("JOB_AGENT_EMBEDDING_MODEL")
    explicit_api_key = get("JOB_AGENT_EMBEDDING_API_KEY")
    explicit_base_url = get("JOB_AGENT_EMBEDDING_BASE_URL")
    api_style = normalize_embedding_api_style(get("JOB_AGENT_EMBEDDING_API_STYLE"))
    timeout = parse_positive_int(
        get("JOB_AGENT_EMBEDDING_TIMEOUT_SECONDS", default="60"),
        "JOB_AGENT_EMBEDDING_TIMEOUT_SECONDS",
    )
    batch_size = parse_positive_int(
        get("JOB_AGENT_EMBEDDING_BATCH_SIZE", default="64"),
        "JOB_AGENT_EMBEDDING_BATCH_SIZE",
    )
    dimensions = get("JOB_AGENT_EMBEDDING_DIMENSIONS")

    if not any(
        [
            provider,
            model,
            explicit_api_key,
            explicit_base_url,
            dimensions,
            get("JOB_AGENT_EMBEDDING_API_STYLE"),
        ]
    ):
        return None
    if not provider:
        raise ValueError("缺少 embedding provider：请在 .env 中配置 JOB_AGENT_EMBEDDING_PROVIDER")
    normalized_provider = provider.strip().lower()
    if normalized_provider in {"local", "local_hash"} or api_style == "local_hash":
        parsed_dimensions = (
            parse_positive_int(dimensions, "JOB_AGENT_EMBEDDING_DIMENSIONS")
            if dimensions
            else None
        )
        return EmbeddingSettings(
            provider=normalized_provider,
            model=model or "local-hash",
            api_key="local",
            base_url="local",
            api_style="local_hash",
            timeout_seconds=timeout,
            batch_size=batch_size,
            dimensions=parsed_dimensions,
        )
    api_key = explicit_api_key or get("OPENAI_API_KEY")
    base_url = explicit_base_url or get("OPENAI_BASE_URL")
    if not model:
        raise ValueError("缺少 embedding 模型名：请在 .env 中配置 JOB_AGENT_EMBEDDING_MODEL")
    if not api_key:
        raise ValueError("缺少 embedding API Key：请在 .env 中配置 JOB_AGENT_EMBEDDING_API_KEY")
    if not base_url:
        raise ValueError("缺少 embedding base URL：请在 .env 中配置 JOB_AGENT_EMBEDDING_BASE_URL")

    return EmbeddingSettings(
        provider=normalized_provider,
        model=model,
        api_key=api_key,
        base_url=base_url,
        api_style=api_style,
        timeout_seconds=timeout,
        batch_size=batch_size,
        dimensions=(
            parse_positive_int(dimensions, "JOB_AGENT_EMBEDDING_DIMENSIONS")
            if dimensions
            else None
        ),
    )


def masked_embedding_settings(settings: EmbeddingSettings | None) -> dict[str, object]:
    """返回适合 Web 管理健康检查展示的 embedding 配置摘要。"""

    if settings is None:
        return {"provider": "local_hash", "mode": "fallback", "configured": False}
    return {
        "provider": settings.provider,
        "model": settings.model,
        "base_url": settings.base_url,
        "api_key_set": bool(settings.api_key and settings.api_key != "local"),
        "timeout_seconds": settings.timeout_seconds,
        "batch_size": settings.batch_size,
        "dimensions": settings.dimensions,
        "api_style": settings.api_style,
        "configured": settings.provider not in {"local", "local_hash"},
    }


def load_rerank_settings(
    env_path: str | Path = DEFAULT_ENV_PATH,
    environ: Mapping[str, str] | None = None,
) -> RerankSettings | None:
    """从 `.env` 读取可选的 Rerank 配置。

    未提供任何 Rerank 字段时返回 ``None``，RAG 保持纯向量召回，保证现有离线场景
    和未开通重排服务的部署不会被配置升级打断。
    """

    file_values = load_dotenv_values(env_path)
    environment = os.environ if environ is None else environ

    def get(*keys: str, default: str | None = None) -> str | None:
        """按系统环境变量优先级读取 Rerank 配置。"""

        for key in keys:
            if environment.get(key):
                return environment[key]
            if file_values.get(key):
                return file_values[key]
        return default

    provider = get("JOB_AGENT_RERANK_PROVIDER")
    model = get("JOB_AGENT_RERANK_MODEL")
    explicit_api_key = get("JOB_AGENT_RERANK_API_KEY")
    explicit_base_url = get("JOB_AGENT_RERANK_BASE_URL")
    api_style = normalize_rerank_api_style(
        get("JOB_AGENT_RERANK_API_STYLE", "JOB_AGENT_RERANK_PROTOCOL")
    )
    timeout = parse_positive_int(
        get("JOB_AGENT_RERANK_TIMEOUT_SECONDS", default="60"),
        "JOB_AGENT_RERANK_TIMEOUT_SECONDS",
    )
    min_relevance_score = parse_non_negative_float(
        get(
            "JOB_AGENT_RERANK_MIN_RELEVANCE_SCORE",
            default=str(DEFAULT_RAG_RERANK_MIN_RELEVANCE_SCORE),
        ),
        "JOB_AGENT_RERANK_MIN_RELEVANCE_SCORE",
    )
    relative_score_threshold = parse_positive_float(
        get(
            "JOB_AGENT_RERANK_RELATIVE_SCORE_THRESHOLD",
            default=str(DEFAULT_RAG_RERANK_RELATIVE_SCORE_THRESHOLD),
        ),
        "JOB_AGENT_RERANK_RELATIVE_SCORE_THRESHOLD",
    )
    if min_relevance_score > 1:
        raise ValueError("JOB_AGENT_RERANK_MIN_RELEVANCE_SCORE 不能超过 1")
    if relative_score_threshold > 1:
        raise ValueError("JOB_AGENT_RERANK_RELATIVE_SCORE_THRESHOLD 不能超过 1")
    raw_retrieval_top_k = get("JOB_AGENT_RAG_RETRIEVAL_TOP_K")
    if raw_retrieval_top_k:
        retrieval_top_k = parse_positive_int(
            raw_retrieval_top_k,
            "JOB_AGENT_RAG_RETRIEVAL_TOP_K",
        )
    else:
        raw_legacy_multiplier = get("JOB_AGENT_RERANK_CANDIDATE_MULTIPLIER")
        if raw_legacy_multiplier:
            legacy_candidate_multiplier = parse_positive_int(
                raw_legacy_multiplier,
                "JOB_AGENT_RERANK_CANDIDATE_MULTIPLIER",
            )
            retrieval_top_k = (
                DEFAULT_RAG_RERANK_TOP_N * legacy_candidate_multiplier
            )
        else:
            retrieval_top_k = DEFAULT_RAG_RETRIEVAL_TOP_K
    if retrieval_top_k > MAX_RAG_RETRIEVAL_TOP_K:
        raise ValueError(
            f"JOB_AGENT_RAG_RETRIEVAL_TOP_K 不能超过 {MAX_RAG_RETRIEVAL_TOP_K}"
        )

    if not any(
        [
            provider,
            model,
            explicit_api_key,
            explicit_base_url,
            get("JOB_AGENT_RERANK_API_STYLE"),
            get("JOB_AGENT_RERANK_PROTOCOL"),
        ]
    ):
        return None
    if not provider:
        raise ValueError("缺少 rerank provider：请在 .env 中配置 JOB_AGENT_RERANK_PROVIDER")
    normalized_provider = provider.strip().lower()
    if normalized_provider in {"disabled", "none", "off"}:
        return None
    if not model:
        raise ValueError("缺少 rerank 模型名：请在 .env 中配置 JOB_AGENT_RERANK_MODEL")
    api_key = explicit_api_key or get("OPENAI_API_KEY")
    if not api_key:
        raise ValueError("缺少 rerank API Key：请在 .env 中配置 JOB_AGENT_RERANK_API_KEY")
    if not explicit_base_url:
        raise ValueError("缺少 rerank base URL：请在 .env 中配置 JOB_AGENT_RERANK_BASE_URL")

    base_url = explicit_base_url
    assert base_url is not None
    return RerankSettings(
        provider=normalized_provider,
        model=model,
        api_key=api_key,
        base_url=base_url,
        api_style=api_style,
        timeout_seconds=timeout,
        retrieval_top_k=retrieval_top_k,
        min_relevance_score=min_relevance_score,
        relative_score_threshold=relative_score_threshold,
    )


def masked_rerank_settings(settings: RerankSettings | None) -> dict[str, object]:
    """返回不含 API Key 的 Rerank 配置摘要，供健康检查展示。"""

    if settings is None:
        return {"provider": "disabled", "configured": False}
    return {
        "provider": settings.provider,
        "api_style": settings.api_style,
        "model": settings.model,
        "base_url": settings.base_url,
        "api_key_set": bool(settings.api_key),
        "timeout_seconds": settings.timeout_seconds,
        "retrieval_top_k": settings.retrieval_top_k,
        "rerank_top_n_default": DEFAULT_RAG_RERANK_TOP_N,
        "min_relevance_score": settings.min_relevance_score,
        "relative_score_threshold": settings.relative_score_threshold,
        "configured": True,
    }


def load_agent_memory_settings(
    env_path: str | Path = DEFAULT_ENV_PATH,
    environ: Mapping[str, str] | None = None,
) -> AgentMemorySettings:
    """从 `.env` 和系统环境变量加载 Agent 记忆配置。

    这些配置不包含敏感信息；提供环境变量只是为了后续按不同模型上下文窗口调整阈值，
    不需要改业务代码。
    """

    file_values = load_dotenv_values(env_path)
    environment = os.environ if environ is None else environ

    def get(*keys: str, default: str | None = None) -> str | None:
        """按优先级读取多个候选键名。"""

        for key in keys:
            if environment.get(key):
                return environment[key]
            if file_values.get(key):
                return file_values[key]
        return default

    enabled = parse_bool(get("JOB_AGENT_MEMORY_ENABLED", default="true"))
    checkpoint_backend = (
        get("JOB_AGENT_MEMORY_CHECKPOINT_BACKEND", default="database") or "database"
    ).strip().lower()
    if checkpoint_backend not in {"database", "memory"}:
        raise ValueError("JOB_AGENT_MEMORY_CHECKPOINT_BACKEND 只能是 database 或 memory")
    return AgentMemorySettings(
        enabled=enabled,
        checkpoint_backend=checkpoint_backend,
        restore_history_limit=parse_positive_int(
            get("JOB_AGENT_MEMORY_RESTORE_HISTORY_LIMIT", default="200"),
            "JOB_AGENT_MEMORY_RESTORE_HISTORY_LIMIT",
        ),
        restore_trigger_tokens=parse_positive_int(
            get("JOB_AGENT_MEMORY_RESTORE_TRIGGER_TOKENS", default="12000"),
            "JOB_AGENT_MEMORY_RESTORE_TRIGGER_TOKENS",
        ),
        restore_keep_messages=parse_positive_int(
            get("JOB_AGENT_MEMORY_RESTORE_KEEP_MESSAGES", default="24"),
            "JOB_AGENT_MEMORY_RESTORE_KEEP_MESSAGES",
        ),
        restore_summary_chars=parse_positive_int(
            get("JOB_AGENT_MEMORY_RESTORE_SUMMARY_CHARS", default="6000"),
            "JOB_AGENT_MEMORY_RESTORE_SUMMARY_CHARS",
        ),
        summary_trigger_tokens=parse_positive_int(
            get("JOB_AGENT_MEMORY_SUMMARY_TRIGGER_TOKENS", default="12000"),
            "JOB_AGENT_MEMORY_SUMMARY_TRIGGER_TOKENS",
        ),
        summary_keep_messages=parse_positive_int(
            get("JOB_AGENT_MEMORY_SUMMARY_KEEP_MESSAGES", default="24"),
            "JOB_AGENT_MEMORY_SUMMARY_KEEP_MESSAGES",
        ),
        summary_trim_tokens=parse_positive_int(
            get("JOB_AGENT_MEMORY_SUMMARY_TRIM_TOKENS", default="6000"),
            "JOB_AGENT_MEMORY_SUMMARY_TRIM_TOKENS",
        ),
    )


def load_cookie_secure(
    env_path: str | Path = DEFAULT_ENV_PATH,
    environ: Mapping[str, str] | None = None,
) -> bool:
    """按系统环境变量优先级读取 Session Cookie 的 Secure 开关。"""

    file_values = load_dotenv_values(env_path)
    environment = os.environ if environ is None else environ
    raw_value = environment.get("JOB_AGENT_COOKIE_SECURE") or file_values.get(
        "JOB_AGENT_COOKIE_SECURE",
        "false",
    )
    secure = parse_bool(raw_value)
    runtime_environment = (
        environment.get("JOB_AGENT_ENVIRONMENT")
        or file_values.get("JOB_AGENT_ENVIRONMENT")
        or "development"
    ).strip().lower()
    if runtime_environment == "production" and not secure:
        raise ValueError("生产环境必须启用 Secure Session Cookie。")
    return secure


def load_semantic_matching_enabled(
    env_path: str | Path = DEFAULT_ENV_PATH,
    environ: Mapping[str, str] | None = None,
) -> bool:
    """读取职位方向语义匹配开关。

    语义匹配会调用 Embedding/Rerank 供应商接口，默认关闭以保证离线测试、
    本地规则模式和没有模型配置的开发环境不会意外发起外部请求。
    """

    file_values = load_dotenv_values(env_path)
    environment = os.environ if environ is None else environ
    raw_value = environment.get("JOB_AGENT_MATCHING_SEMANTIC") or file_values.get(
        "JOB_AGENT_MATCHING_SEMANTIC",
        "false",
    )
    return parse_bool(raw_value)


def masked_agent_memory_settings(settings: AgentMemorySettings) -> dict[str, object]:
    """返回适合 Web 管理健康检查展示的 Agent 记忆配置。"""

    return {
        "enabled": settings.enabled,
        "checkpoint_backend": settings.checkpoint_backend,
        "restore_history_limit": settings.restore_history_limit,
        "restore_trigger_tokens": settings.restore_trigger_tokens,
        "restore_keep_messages": settings.restore_keep_messages,
        "restore_summary_chars": settings.restore_summary_chars,
        "summary_trigger_tokens": settings.summary_trigger_tokens,
        "summary_keep_messages": settings.summary_keep_messages,
        "summary_trim_tokens": settings.summary_trim_tokens,
    }


def parse_bool(value: str | None) -> bool:
    """解析常见布尔配置值。"""

    if value is None:
        return True
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"无法解析布尔配置：{value}")


def parse_positive_int(value: str | None, field_name: str) -> int:
    """解析正整数配置，并给出可读错误。"""

    try:
        parsed = int(value or "")
    except ValueError as error:
        raise ValueError(f"{field_name} 必须是正整数") from error
    if parsed <= 0:
        raise ValueError(f"{field_name} 必须大于 0")
    return parsed

def parse_positive_float(value: str | None, field_name: str) -> float:
    """解析正浮点数配置，并给出可读错误。"""

    try:
        parsed = float(value or "")
    except ValueError as error:
        raise ValueError(f"{field_name} 必须是正数") from error
    if parsed <= 0:
        raise ValueError(f"{field_name} 必须大于 0")
    return parsed


def parse_non_negative_float(value: str | None, field_name: str) -> float:
    """解析允许为 0 的非负浮点数配置。"""

    try:
        parsed = float(value or "")
    except ValueError as error:
        raise ValueError(f"{field_name} 必须是非负数") from error
    if parsed < 0:
        raise ValueError(f"{field_name} 不能小于 0")
    return parsed


def parse_non_negative_int(value: str | None, field_name: str) -> int:
    """解析允许为 0 的非负整数配置。"""

    try:
        parsed = int(value or "")
    except ValueError as error:
        raise ValueError(f"{field_name} 必须是非负整数") from error
    if parsed < 0:
        raise ValueError(f"{field_name} 不能小于 0")
    return parsed
