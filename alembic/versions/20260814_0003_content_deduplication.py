"""add scoped content fingerprints

Revision ID: 20260814_0003
Revises: 20260810_0002
Create Date: 2026-08-14 00:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "20260814_0003"
down_revision = "20260810_0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """为新写入资源增加按业务归属范围生效的精确内容去重约束。"""

    for table_name in (
        "candidate_profiles",
        "jobs",
        "project_experience_cards",
        "resume_artifacts",
    ):
        op.add_column(table_name, sa.Column("content_fingerprint", sa.String(64), nullable=True))

    # 职位来源 URL 只表示用户自己填写的出处；这两列记录实际导入方式和服务端接收
    # 时间，方便追溯截图/文本的来源，而不会访问该链接。
    op.add_column(
        "jobs",
        sa.Column("import_method", sa.String(32), nullable=False, server_default=sa.text("'text'")),
    )
    op.add_column("jobs", sa.Column("captured_at", sa.DateTime(timezone=True), nullable=True))

    op.create_unique_constraint(
        "uq_candidate_profiles_account_content_fingerprint",
        "candidate_profiles",
        ["account_id", "content_fingerprint"],
    )
    op.create_unique_constraint(
        "uq_jobs_account_content_fingerprint",
        "jobs",
        ["account_id", "content_fingerprint"],
    )
    op.create_unique_constraint(
        "uq_project_cards_candidate_content_fingerprint",
        "project_experience_cards",
        ["candidate_id", "content_fingerprint"],
    )
    op.create_unique_constraint(
        "uq_resume_artifacts_candidate_content_fingerprint",
        "resume_artifacts",
        ["candidate_id", "content_fingerprint"],
    )


def downgrade() -> None:
    """移除内容指纹约束，保留既有业务数据。"""

    op.drop_constraint(
        "uq_resume_artifacts_candidate_content_fingerprint",
        "resume_artifacts",
        type_="unique",
    )
    op.drop_constraint(
        "uq_project_cards_candidate_content_fingerprint",
        "project_experience_cards",
        type_="unique",
    )
    op.drop_constraint(
        "uq_jobs_account_content_fingerprint",
        "jobs",
        type_="unique",
    )
    op.drop_constraint(
        "uq_candidate_profiles_account_content_fingerprint",
        "candidate_profiles",
        type_="unique",
    )

    for table_name in (
        "resume_artifacts",
        "project_experience_cards",
        "jobs",
        "candidate_profiles",
    ):
        op.drop_column(table_name, "content_fingerprint")

    op.drop_column("jobs", "captured_at")
    op.drop_column("jobs", "import_method")
