"""add import_jobs and batch traceability fields

真实中医知识数据导入中心：
1. 新增 import_jobs 任务表（pending → processing → completed / failed）
2. 为 documents / herbs / prescriptions / theories / literatures 增加
   source_dataset + import_batch_id 两个可空列，用于真实数据批次溯源与后续清理。

说明：全部为可空列 + 纯新增，向下兼容（历史行保持 NULL，代表"手工/测试数据"）。

Revision ID: 2f7c1d9a3b48
Revises: 057cb15ef418
Create Date: 2026-09-27
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "2f7c1d9a3b48"
down_revision: str | None = "057cb15ef418"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# (表名, 是否已存在 import_batch_id 索引)
_TARGET_TABLES = (
    "documents",
    "herbs",
    "prescriptions",
    "theories",
    "literatures",
)


def upgrade() -> None:
    op.create_table(
        "import_jobs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("batch_id", sa.String(64), nullable=False),
        sa.Column("dataset_id", sa.String(128), nullable=False),
        sa.Column("dataset_name", sa.String(255), nullable=True),
        sa.Column("source_file", sa.String(512), nullable=True),
        sa.Column("target_type", sa.String(16), nullable=False),
        sa.Column("kb_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("status", sa.String(16), nullable=True),
        sa.Column("total", sa.Integer(), nullable=True),
        sa.Column("processed", sa.Integer(), nullable=True),
        sa.Column("succeeded", sa.Integer(), nullable=True),
        sa.Column("failed", sa.Integer(), nullable=True),
        sa.Column("skipped", sa.Integer(), nullable=True),
        sa.Column("failed_items", postgresql.JSONB(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("created_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_import_jobs_batch_id", "import_jobs", ["batch_id"])
    op.create_index("ix_import_jobs_dataset_id", "import_jobs", ["dataset_id"])
    op.create_index("ix_import_jobs_target_type", "import_jobs", ["target_type"])
    op.create_index("ix_import_jobs_kb_id", "import_jobs", ["kb_id"])
    op.create_index("ix_import_jobs_status", "import_jobs", ["status"])

    for table in _TARGET_TABLES:
        op.add_column(table, sa.Column("source_dataset", sa.String(128), nullable=True))
        op.add_column(table, sa.Column("import_batch_id", sa.String(64), nullable=True))
        op.create_index(
            f"ix_{table}_import_batch_id", table, ["import_batch_id"]
        )


def downgrade() -> None:
    for table in _TARGET_TABLES:
        op.drop_index(f"ix_{table}_import_batch_id", table_name=table)
        op.drop_column(table, "import_batch_id")
        op.drop_column(table, "source_dataset")

    op.drop_index("ix_import_jobs_status", table_name="import_jobs")
    op.drop_index("ix_import_jobs_kb_id", table_name="import_jobs")
    op.drop_index("ix_import_jobs_target_type", table_name="import_jobs")
    op.drop_index("ix_import_jobs_dataset_id", table_name="import_jobs")
    op.drop_index("ix_import_jobs_batch_id", table_name="import_jobs")
    op.drop_table("import_jobs")
