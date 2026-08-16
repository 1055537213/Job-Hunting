"""Alembic 运行环境。

本文件只负责把连接和版本化 schema 交给 Alembic；业务应用不应从这里读取
候选人资料、模型配置或任何敏感正文。
"""

from __future__ import annotations

from sqlalchemy import engine_from_config, pool, text

from alembic import context
from job_hunting_agent.config import (
    load_database_settings,
    normalize_database_url,
    require_postgresql_database_url,
)
from job_hunting_agent.database_schema import metadata

config = context.config
# 应用运行时不能依赖 alembic.ini 中的静态 URL。Docker 和宿主机都从环境
# 配置读取 PostgreSQL；测试辅助函数则通过这个私有 option 显式注入临时数据库 URL。
EXPLICIT_DATABASE_URL_OPTION = "job_agent.runtime_database_url"


def resolve_database_url() -> str:
    """确定本次迁移的数据库 URL，并隔离测试适配器与真实运行时。"""

    injected_url = config.get_main_option(EXPLICIT_DATABASE_URL_OPTION)
    if injected_url:
        return normalize_database_url(injected_url)
    return require_postgresql_database_url(load_database_settings())


# ConfigParser 会将百分号作为插值标记；数据库 URL 的编码查询参数需在写入前转义。
config.set_main_option("sqlalchemy.url", resolve_database_url().replace("%", "%%"))
# 目标元数据只用于 Alembic 的类型比较和未来 autogenerate 辅助；初始 revision 本身
# 已冻结为显式 DDL，不会因以后修改 metadata 而改变历史迁移。
target_metadata = metadata


def run_migrations_offline() -> None:
    """在不建立数据库连接时生成 SQL。"""

    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        compare_server_default=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """建立一次短连接并在事务中执行迁移。"""

    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        # PostgreSQL 的测试 URL 会把业务 schema 放在 public 之前；显式指定
        # 版本表 schema，避免新测试 schema 误读 public.alembic_version。
        version_table_schema = connection.execute(text("SELECT current_schema()")).scalar_one()
        # SQLAlchemy 2.x 会让上面的 SELECT 开启隐式事务；先结束它，确保
        # Alembic 的 context.begin_transaction() 真正拥有后续 DDL 的提交权。
        connection.commit()
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            compare_server_default=True,
            version_table_schema=version_table_schema,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
