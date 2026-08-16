"""repair job import provenance columns

Revision ID: 20260816_0004
Revises: 20260814_0003
Create Date: 2026-08-16 00:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "20260816_0004"
down_revision = "20260814_0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """补齐曾在已执行迁移后新增的职位来源追溯列。

    早期本地数据库可能已经记录为 ``20260814_0003``，但当时的 ``jobs`` 表只
    包含内容指纹列。新安装会在 ``0003`` 中已拥有这些列，因此这里必须先检查，
    使同一迁移链同时兼容两种数据库状态。
    """

    existing_columns = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("jobs")}
    if "import_method" not in existing_columns:
        op.add_column(
            "jobs",
            sa.Column("import_method", sa.String(32), nullable=False, server_default=sa.text("'text'")),
        )
    if "captured_at" not in existing_columns:
        op.add_column("jobs", sa.Column("captured_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    """保留列，避免全新安装回退到 ``0003`` 时产生迁移历史与表结构分叉。

    ``0003`` 的当前定义已经拥有这两列；继续回退到更早版本时，``0003`` 的
    downgrade 会统一删除它们。这里不单独删除，能同时兼容历史数据库和新数据库。
    """

    return
