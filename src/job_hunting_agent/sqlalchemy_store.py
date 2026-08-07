"""SQLAlchemy 驱动的生产仓储适配层。

现有业务服务已经通过 ``SQLiteStore`` 集中处理候选人、会话、简历和用量逻辑。
为了在一次可回滚的改造中切换到 PostgreSQL，本模块复用这组成熟业务方法，提供
SQLAlchemy 连接、事务、参数绑定和结果兼容层。后续阶段可以把方法内部逐步改为
SQLAlchemy Core，而不改变上层 Agent、FastAPI 或认证服务的接口。
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from datetime import date, datetime, time
from typing import Any

import sqlalchemy as sa
from sqlalchemy.engine import Connection, CursorResult, Engine

from .config import normalize_database_url
from .database_migrations import current_database_revision, latest_database_revision
from .storage import SQLiteStore


INSERT_PATTERN = re.compile(r"^\s*INSERT\s+INTO\s+", re.IGNORECASE)


class SQLAlchemyRow:
    """把 SQLAlchemy 映射行适配成现有仓储代码使用的 sqlite3.Row 形状。"""

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

    def keys(self) -> list[str]:
        """返回列名列表，兼容现有的可选字段检测逻辑。"""

        return list(self._values.keys())


class SQLAlchemyCursor:
    """封装 SQLAlchemy 结果对象，并提供 sqlite3 风格的最小游标接口。"""

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
    """让既有 qmark SQL 在 SQLAlchemy 事务中运行的连接适配器。"""

    def __init__(self, engine: Engine):
        self._engine = engine
        self._connection: Connection | None = None
        self._transaction: Any | None = None

    def __enter__(self) -> "SQLAlchemyConnection":
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


class SQLAlchemyStore(SQLiteStore):
    """使用 SQLAlchemy Engine 访问经 Alembic 管理的数据库。

    继承只复用领域读写方法和行转换逻辑，不调用 SQLite 的连接或建表逻辑。生产启动
    只检查 Alembic revision，数据库结构变更必须由独立迁移命令完成。
    """

    def __init__(self, database_url: str):
        self.database_url = normalize_database_url(database_url)
        self.engine = sa.create_engine(self.database_url, pool_pre_ping=True)

    def connect(self) -> SQLAlchemyConnection:
        """返回一次短生命周期的 SQLAlchemy 事务连接。"""

        return SQLAlchemyConnection(self.engine)

    def initialize(self) -> None:
        """确认数据库已经被 Alembic 升级，而不是在应用启动时临时建表。"""

        current_revision = current_database_revision(self.database_url)
        expected_revision = latest_database_revision()
        if current_revision != expected_revision:
            raise RuntimeError(
                "数据库尚未迁移到最新版本；请先执行 job-agent database-upgrade。"
            )

    def close(self) -> None:
        """释放连接池，供测试和受控进程关闭时调用。"""

        self.engine.dispose()


def prepare_statement(
    sql: str,
    parameters: Sequence[object] | Mapping[str, object] | None,
) -> tuple[str, dict[str, object]]:
    """把既有 sqlite qmark 参数转换成 SQLAlchemy 命名参数。

    当前仓储 SQL 不在字符串字面量中使用问号，因此逐个替换可保持查询可读，同时支持
    ``IN (?, ?, ?)`` 这类动态占位符。新代码应优先直接使用 SQLAlchemy Core。
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
