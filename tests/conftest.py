"""PostgreSQL 测试隔离。

所有测试与网页运行时使用同一种 PostgreSQL + pgvector 存储。每次 pytest 运行会创建
一个随机 schema，并在每条测试前后清空业务表，因此不会写入 Docker 中网页使用的 public
schema，也不再需要临时文件数据库或独立向量目录。
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import pytest
import sqlalchemy as sa

from job_hunting_agent.database_migrations import upgrade_database
from job_hunting_agent.database_schema import metadata
from job_hunting_agent.sqlalchemy_store import SQLAlchemyStore

TEST_DATABASE_URL_ENV = "JOB_AGENT_TEST_DATABASE_URL"
# Docker Compose 仅把 PostgreSQL 端口绑定到本机，因此这个默认值不会暴露测试数据库。
DEFAULT_TEST_DATABASE_URL = "postgresql+psycopg://job_agent@127.0.0.1:5432/job_agent"


def _schema_url(base_url: str, schema: str) -> str:
    """在 PostgreSQL URL 上追加 search_path，令每个连接只看到测试 schema。"""

    parsed = urlsplit(base_url)
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    current_options = query.get("options", "").strip()
    # pgvector 扩展类型由 Docker 数据库安装在 public；测试业务表优先落入随机 schema，
    # 同时保留 public 作为类型解析后备，避免 CREATE TABLE 无法找到 vector 类型。
    query["options"] = f"{current_options} -csearch_path={schema},public".strip()
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(query), parsed.fragment))


def _truncate_business_tables(database_url: str) -> None:
    """清空当前测试 schema 的业务表并重置自增 ID，保留 Alembic revision。"""

    table_names = ", ".join(f'"{table.name}"' for table in metadata.sorted_tables)
    engine = sa.create_engine(database_url, pool_pre_ping=True)
    try:
        with engine.begin() as connection:
            connection.execute(sa.text(f"TRUNCATE TABLE {table_names} RESTART IDENTITY CASCADE"))
    finally:
        engine.dispose()


@pytest.fixture(scope="session", autouse=True)
def postgres_test_schema() -> Iterator[str]:
    """创建一次独立 schema，并把连接 URL 注入所有应用层测试。"""

    base_url = os.environ.get(TEST_DATABASE_URL_ENV, DEFAULT_TEST_DATABASE_URL)
    schema = f"job_agent_test_{uuid.uuid4().hex}"
    base_engine = sa.create_engine(base_url, pool_pre_ping=True, connect_args={"connect_timeout": 5})
    previous_runtime_url = os.environ.get("JOB_AGENT_DATABASE_URL")
    previous_object_storage_backend = os.environ.get("JOB_AGENT_OBJECT_STORAGE_BACKEND")
    previous_csrf_enabled = os.environ.get("JOB_AGENT_CSRF_ENABLED")
    previous_test_starting_balance = os.environ.get("JOB_AGENT_BILLING_STARTING_BALANCE_YUAN")
    previous_intent_router_enabled = os.environ.get("JOB_AGENT_INTENT_ROUTER_ENABLED")
    try:
        try:
            with base_engine.begin() as connection:
                connection.execute(sa.text(f'CREATE SCHEMA "{schema}"'))
        except sa.exc.SQLAlchemyError as error:
            pytest.exit(
                "PostgreSQL 测试库不可用。请先执行 docker compose up -d postgres，"
                f"或设置 {TEST_DATABASE_URL_ENV}。原始错误：{error}",
                returncode=2,
            )

        database_url = _schema_url(base_url, schema)
        os.environ["JOB_AGENT_DATABASE_URL"] = database_url
        # 测试使用显式临时目录或内存替身，不要求 CI 额外启动 MinIO。
        os.environ["JOB_AGENT_OBJECT_STORAGE_BACKEND"] = "local"
        # 业务测试不逐条携带浏览器 CSRF header；CSRF 行为由专门 Web 安全测试覆盖。
        os.environ["JOB_AGENT_CSRF_ENABLED"] = "false"
        # 非计费测试需要可调用模型；显式测试资金不改变生产默认的零初始余额。
        os.environ["JOB_AGENT_BILLING_STARTING_BALANCE_YUAN"] = "100"
        # 单元测试通过假模型或显式 IntentRouterSettings 验证路由行为；不能因开发者
        # 本地 `.env` 启用了真实路由模型而产生网络调用、费用或不确定测试结果。
        os.environ["JOB_AGENT_INTENT_ROUTER_ENABLED"] = "false"
        upgrade_database(database_url)
        yield database_url
    finally:
        if previous_runtime_url is None:
            os.environ.pop("JOB_AGENT_DATABASE_URL", None)
        else:
            os.environ["JOB_AGENT_DATABASE_URL"] = previous_runtime_url
        if previous_object_storage_backend is None:
            os.environ.pop("JOB_AGENT_OBJECT_STORAGE_BACKEND", None)
        else:
            os.environ["JOB_AGENT_OBJECT_STORAGE_BACKEND"] = previous_object_storage_backend
        if previous_csrf_enabled is None:
            os.environ.pop("JOB_AGENT_CSRF_ENABLED", None)
        else:
            os.environ["JOB_AGENT_CSRF_ENABLED"] = previous_csrf_enabled
        if previous_test_starting_balance is None:
            os.environ.pop("JOB_AGENT_BILLING_STARTING_BALANCE_YUAN", None)
        else:
            os.environ["JOB_AGENT_BILLING_STARTING_BALANCE_YUAN"] = previous_test_starting_balance
        if previous_intent_router_enabled is None:
            os.environ.pop("JOB_AGENT_INTENT_ROUTER_ENABLED", None)
        else:
            os.environ["JOB_AGENT_INTENT_ROUTER_ENABLED"] = previous_intent_router_enabled
        with base_engine.begin() as connection:
            connection.execute(sa.text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        base_engine.dispose()


@pytest.fixture(autouse=True)
def reset_postgres_test_schema(postgres_test_schema: str) -> Iterator[None]:
    """在每条测试前后清理测试数据，避免同一 pytest 进程中的状态串扰。"""

    _truncate_business_tables(postgres_test_schema)
    yield
    _truncate_business_tables(postgres_test_schema)


@pytest.fixture
def database_url(postgres_test_schema: str) -> str:
    """为需要直接构造 SQLAlchemyStore 的测试提供隔离连接 URL。"""

    return postgres_test_schema


@pytest.fixture
def account_id(postgres_test_schema: str) -> Iterator[int]:
    """创建一条真实账号记录，供领域测试显式验证账号归属约束。"""

    store = SQLAlchemyStore(postgres_test_schema)
    try:
        store.initialize()
        account = store.create_account(
            email=f"test-{uuid.uuid4().hex}@example.com",
            password_hash="test-only-password-hash",
            display_name="测试账号",
        )
        store.create_simulated_recharge_order(
            account.id,
            100,
            idempotency_key=f"test-fixture-recharge:{account.id}",
            description="测试夹具充值",
        )
        yield account.id
    finally:
        store.close()


@pytest.fixture
def temporary_database_url(postgres_test_schema: str) -> Iterator[str]:
    """为迁移升级/回退测试创建独立 schema，避免破坏共享测试 schema。"""

    parsed = urlsplit(postgres_test_schema)
    base_url = urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", parsed.fragment))
    schema = f"job_agent_migration_{uuid.uuid4().hex}"
    base_engine = sa.create_engine(base_url, pool_pre_ping=True)
    try:
        with base_engine.begin() as connection:
            connection.execute(sa.text(f'CREATE SCHEMA "{schema}"'))
        url = _schema_url(base_url, schema)
        yield url
    finally:
        with base_engine.begin() as connection:
            connection.execute(sa.text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        base_engine.dispose()
