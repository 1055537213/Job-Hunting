"""Alembic 运行环境。

本文件只负责把连接和版本化 schema 交给 Alembic；业务应用不应从这里读取
候选人资料、模型配置或任何敏感正文。
"""

from __future__ import annotations

from alembic import context
from sqlalchemy import engine_from_config, pool

from job_hunting_agent.database_schema import metadata


config = context.config
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
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            compare_server_default=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
