"""项目配置加载。

模型 API Key、base URL、模型名等容易变化且包含敏感信息的内容，都从 `.env`
或系统环境变量读取，不写死在代码里。当前项目不依赖第三方 dotenv 包，
这里实现一个小型 `.env` 解析器，足够覆盖 `KEY=value` 这类常见配置。
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

DEFAULT_ENV_PATH = Path(".env")

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
class TaskQueueSettings:
    """后台任务队列配置。

    Redis 只承担 Celery broker 的短期消息传递；任务状态、归属、进度和错误摘要
    始终写入 PostgreSQL。这样 Redis 重启或过期后，网页仍能从数据库恢复任务状态。
    """

    enabled: bool = False
    redis_url: str | None = None
    queue_name: str = "job_agent"
    task_time_limit_seconds: int = 900
    task_soft_time_limit_seconds: int = 840


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
    candidate_multiplier: int = 4


@dataclass(frozen=True)
class AgentMemorySettings:
    """Agent 对话记忆配置。

    `restore_history_limit` 控制启动恢复时最多读取多少条 PostgreSQL 聊天记录。
    `restore_trigger_tokens` 控制恢复历史过长时何时先压缩再交给 Agent。
    `summary_trigger_tokens` 控制 LangChain 运行中何时触发自动总结。
    `summary_keep_messages` 表示总结后保留最近多少条原文消息。
    """

    enabled: bool = True
    restore_history_limit: int = 200
    restore_trigger_tokens: int = 12000
    restore_keep_messages: int = 24
    restore_summary_chars: int = 6000
    summary_trigger_tokens: int = 12000
    summary_keep_messages: int = 24
    summary_trim_tokens: int = 6000


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
    )


def masked_model_gateway_settings(settings: ModelGatewaySettings) -> dict[str, object]:
    """返回可安全展示的 Gateway 配置摘要。"""

    return {
        "environment": settings.environment,
        "chat_max_retries": settings.chat_max_retries,
        "embedding_max_retries": settings.embedding_max_retries,
        "rerank_max_retries": settings.rerank_max_retries,
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


def masked_database_settings(settings: DatabaseSettings) -> dict[str, object]:
    """返回数据库配置的脱敏摘要。"""

    return {
        "configured": settings.configured,
        "dialect": settings.dialect,
        "url": settings.masked_url,
    }


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
    return TaskQueueSettings(
        enabled=True,
        redis_url=redis_url,
        queue_name=queue_name,
        task_time_limit_seconds=time_limit,
        task_soft_time_limit_seconds=soft_time_limit,
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
    return WebSecuritySettings(
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
    candidate_multiplier = parse_positive_int(
        get("JOB_AGENT_RERANK_CANDIDATE_MULTIPLIER", default="4"),
        "JOB_AGENT_RERANK_CANDIDATE_MULTIPLIER",
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
        candidate_multiplier=candidate_multiplier,
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
        "candidate_multiplier": settings.candidate_multiplier,
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
    return AgentMemorySettings(
        enabled=enabled,
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
    return parse_bool(raw_value)


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


def parse_non_negative_int(value: str | None, field_name: str) -> int:
    """解析允许为 0 的非负整数配置。"""

    try:
        parsed = int(value or "")
    except ValueError as error:
        raise ValueError(f"{field_name} 必须是非负整数") from error
    if parsed < 0:
        raise ValueError(f"{field_name} 不能小于 0")
    return parsed
