"""SQLAlchemy 驱动的生产仓储适配层。

领域读写方法位于 ``storage.py`` 的 ``RepositoryStore``，本模块只负责提供
SQLAlchemy 连接、事务、参数绑定和结果转换。Web、后台任务和测试因此使用同一套
PostgreSQL 行为，不再存在其他数据库运行或测试分支。
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping, Sequence
from datetime import date, datetime, time
from typing import Any, Self

import sqlalchemy as sa
from sqlalchemy.engine import Connection, CursorResult, Engine

from .config import normalize_database_url
from .database_migrations import current_database_revision, latest_database_revision
from .storage import RepositoryConnection, RepositoryStore

INSERT_PATTERN = re.compile(r"^\s*INSERT\s+INTO\s+", re.IGNORECASE)


class SQLAlchemyRow:
    """把 SQLAlchemy 映射行适配成领域仓储需要的列名读取形状。"""

    def __init__(self, values: Mapping[str, Any]):
        self._values = dict(values)

    def __getitem__(self, key: str) -> Any:
        """按列名读取值，并保持旧 JSON/时间字段的字符串契约。"""

        value = self._values[key]
        if isinstance(value, (dict, list)):
            return json.dumps(value, ensure_ascii=False)
        if isinstance(value, (datetime, date, time)):
            return value.isoformat()
        return value

    def __contains__(self, key: str) -> bool:
        """支持仓储层检测兼容性字段是否存在。"""

        return key in self._values

    def get(self, key: str, default: Any = None) -> Any:
        """按映射语义读取可选列，旧迁移行缺少字段时返回默认值。"""

        return self._values.get(key, default)

    def __iter__(self):
        """按列名遍历，兼容映射类型的标准行为。"""

        return iter(self._values)

    def keys(self) -> list[str]:
        """返回列名列表，兼容现有的可选字段检测逻辑。"""

        return list(self._values.keys())


class SQLAlchemyCursor:
    """封装 SQLAlchemy 结果对象，并提供领域仓储需要的最小游标接口。"""

    def __init__(self, result: CursorResult[Any], inserted_row_id: int | None = None):
        self._result = result
        self.lastrowid = inserted_row_id
        self.rowcount = result.rowcount

    def fetchone(self) -> SQLAlchemyRow | None:
        """读取一条结果记录，未命中时返回 ``None``。"""

        row = self._result.mappings().first()
        return SQLAlchemyRow(row) if row is not None else None

    def fetchall(self) -> list[SQLAlchemyRow]:
        """读取全部结果记录。"""

        return [SQLAlchemyRow(row) for row in self._result.mappings().all()]


class SQLAlchemyConnection:
    """让领域仓储的参数化 SQL 在 SQLAlchemy 事务中运行。"""

    def __init__(self, engine: Engine):
        self._engine = engine
        self._connection: Connection | None = None
        self._transaction: Any | None = None

    def __enter__(self) -> Self:
        """打开连接并为每个仓储方法创建一个显式事务。"""

        self._connection = self._engine.connect()
        self._transaction = self._connection.begin()
        return self

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> bool:
        """成功提交、异常回滚，并始终释放数据库连接。"""

        try:
            if self._transaction is not None:
                if exc_type is None:
                    self._transaction.commit()
                else:
                    self._transaction.rollback()
        finally:
            if self._connection is not None:
                self._connection.close()
        return False

    def execute(
        self,
        sql: str,
        parameters: Sequence[object] | Mapping[str, object] | None = None,
    ) -> SQLAlchemyCursor:
        """执行 qmark SQL，并为自增主键写入补充 ``RETURNING id``。"""

        if self._connection is None:
            raise RuntimeError("数据库连接只能在 with store.connect() 内使用。")

        statement, bound_parameters = prepare_statement(sql, parameters)
        is_insert = bool(INSERT_PATTERN.match(statement))
        if is_insert and "RETURNING" not in statement.upper():
            statement = statement.rstrip().rstrip(";") + " RETURNING id"

        result = self._connection.execute(sa.text(statement), bound_parameters)
        inserted_row_id: int | None = None
        if is_insert:
            inserted = result.mappings().first()
            if inserted is not None and inserted.get("id") is not None:
                inserted_row_id = int(inserted["id"])
        return SQLAlchemyCursor(result, inserted_row_id)


class SQLAlchemyStore(RepositoryStore):
    """使用 SQLAlchemy Engine 访问经 Alembic 管理的数据库。

    继承只复用领域读写方法和行转换逻辑。生产启动只检查 Alembic revision，数据库
    结构变更必须由独立迁移命令完成。
    """

    def __init__(self, database_url: str):
        super().__init__()
        self.database_url = normalize_database_url(database_url)
        engine_options: dict[str, Any] = {"pool_pre_ping": True}
        # 全量测试会创建很多短生命周期的 App；测试连接不能让每个 Engine
        # 各自保留一个空闲池，否则长测试会耗尽 PostgreSQL 的连接上限。
        if os.environ.get("JOB_AGENT_TEST_DATABASE_URL"):
            engine_options["poolclass"] = sa.pool.NullPool
        self.engine = sa.create_engine(self.database_url, **engine_options)

    def connect(self) -> RepositoryConnection:
        """返回一次短生命周期的 SQLAlchemy 事务连接。"""

        return SQLAlchemyConnection(self.engine)

    def initialize(self) -> None:
        """确认数据库已经被 Alembic 升级，而不是在应用启动时临时建表。"""

        current_revision = current_database_revision(self.database_url)
        expected_revision = latest_database_revision()
        if current_revision != expected_revision:
            raise RuntimeError(
                "数据库尚未迁移到最新版本；请先等待 Docker migrate 服务完成或执行 alembic upgrade head。"
            )

    def close(self) -> None:
        """释放连接池，供测试和受控进程关闭时调用。"""

        self.engine.dispose()


def prepare_statement(
    sql: str,
    parameters: Sequence[object] | Mapping[str, object] | None,
) -> tuple[str, dict[str, object]]:
    """把仓储层保留的 qmark 参数转换成 SQLAlchemy 命名参数。

    这是仓储 SQL 的兼容适配，不代表运行时支持多种数据库；所有连接仍由
    PostgreSQL SQLAlchemy Engine 提供。
    """

    if parameters is None:
        values: Sequence[object] = ()
    elif isinstance(parameters, Mapping):
        return sql, dict(parameters)
    else:
        values = parameters

    index = 0

    def replace_placeholder(_: re.Match[str]) -> str:
        nonlocal index
        if index >= len(values):
            raise ValueError("SQL 参数数量少于 qmark 占位符数量。")
        placeholder = f":p{index}"
        index += 1
        return placeholder

    statement = re.sub(r"\?", replace_placeholder, sql)
    if index != len(values):
        raise ValueError("SQL 参数数量多于 qmark 占位符数量。")
    return statement, {f"p{item_index}": value for item_index, value in enumerate(values)}
