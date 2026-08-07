"""Alembic 数据库迁移的应用入口。

业务代码不应直接调用 ``metadata.create_all()`` 初始化生产数据库。这里把 Alembic
配置收束为一个小接口，CLI、容器迁移任务和测试可以共用它，同时保证数据库密码
只从配置读取且不会被打印到输出中。
"""

from __future__ import annotations

from pathlib import Path

import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory

from .config import normalize_database_url


def find_project_root() -> Path:
    """定位包含 Alembic 文件的项目根目录，兼容源码和 Docker 安装两种形态。"""

    source_root = Path(__file__).resolve().parents[2]
    for candidate in (Path.cwd(), source_root, Path("/app")):
        if (candidate / "alembic.ini").is_file() and (candidate / "alembic").is_dir():
            return candidate
    raise FileNotFoundError("找不到包含 alembic.ini 和 alembic/ 的项目根目录。")


# 本地 editable 安装从 src/ 回溯，Docker wheel 安装则从 /app 工作目录定位迁移脚本。
PROJECT_ROOT = find_project_root()
ALEMBIC_INI_PATH = PROJECT_ROOT / "alembic.ini"
ALEMBIC_SCRIPT_PATH = PROJECT_ROOT / "alembic"


def build_alembic_config(database_url: str) -> Config:
    """构造一次迁移运行所需的 Alembic 配置。

    这里显式写入绝对脚本路径，避免 Docker、pytest 或从任意当前目录执行 CLI 时
    因相对路径不同而找不到迁移文件。
    """

    if not ALEMBIC_INI_PATH.is_file():
        raise FileNotFoundError(f"找不到 Alembic 配置文件：{ALEMBIC_INI_PATH}")
    if not ALEMBIC_SCRIPT_PATH.is_dir():
        raise FileNotFoundError(f"找不到 Alembic 脚本目录：{ALEMBIC_SCRIPT_PATH}")

    config = Config(str(ALEMBIC_INI_PATH))
    config.set_main_option("script_location", str(ALEMBIC_SCRIPT_PATH))
    config.set_main_option("sqlalchemy.url", normalize_database_url(database_url))
    return config


def upgrade_database(database_url: str, revision: str = "head") -> str | None:
    """把数据库升级到指定 Alembic revision，默认升级到最新版本。"""

    normalized_url = normalize_database_url(database_url)
    command.upgrade(build_alembic_config(normalized_url), revision)
    return current_database_revision(normalized_url)


def downgrade_database(database_url: str, revision: str = "-1") -> str | None:
    """把数据库回退到较早 revision，供演练和受控故障恢复使用。

    这个函数只提供迁移能力，不会替调用方删除备份或绕过上线审批；生产回退前仍应
    先完成备份和影响确认。
    """

    normalized_url = normalize_database_url(database_url)
    command.downgrade(build_alembic_config(normalized_url), revision)
    return current_database_revision(normalized_url)


def latest_database_revision() -> str:
    """读取仓库中迁移链的最新 revision，不连接业务数据库。"""

    if not ALEMBIC_INI_PATH.is_file():
        raise FileNotFoundError(f"找不到 Alembic 配置文件：{ALEMBIC_INI_PATH}")
    config = Config(str(ALEMBIC_INI_PATH))
    config.set_main_option("script_location", str(ALEMBIC_SCRIPT_PATH))
    revision = ScriptDirectory.from_config(config).get_current_head()
    if revision is None:  # pragma: no cover - 迁移目录至少应保留初始版本。
        raise RuntimeError("Alembic 迁移目录中不存在可用 revision。")
    return revision


def current_database_revision(database_url: str) -> str | None:
    """读取当前数据库 revision；尚未初始化的空库返回 ``None``。"""

    engine = sa.create_engine(normalize_database_url(database_url), pool_pre_ping=True)
    try:
        with engine.connect() as connection:
            return MigrationContext.configure(connection).get_current_revision()
    finally:
        engine.dispose()
