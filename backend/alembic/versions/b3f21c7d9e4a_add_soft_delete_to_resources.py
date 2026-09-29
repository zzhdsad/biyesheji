"""add deleted_at to resource/taxonomy tables (统一回收站生命周期)

管理中心统一回收站：为中药/方剂/中医理论/文献/分类/标签六张表增加可空
``deleted_at`` 列 + 索引，使其复用与知识库 / 文档 / 用户一致的软删除生命周期
（删除 → 回收站 → 恢复 / 彻底删除），而不是直接物理 DELETE。

设计约束：
- 纯新增可空列，向下兼容：历史行保持 NULL（代表"未删除"），不影响任何现有查询；
- 不删除/重建任何表，不触碰 documents / chunks / Milvus；
- 保留天数统一由 system_configs.trash_retention_days 决定（应用层逻辑）。

Revision ID: b3f21c7d9e4a
Revises: 2f7c1d9a3b48
Create Date: 2026-09-29
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b3f21c7d9e4a"
down_revision: str | None = "2f7c1d9a3b48"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TARGET_TABLES = (
    "herbs",
    "prescriptions",
    "theories",
    "literatures",
    "categories",
    "tags",
)


def upgrade() -> None:
    for table in _TARGET_TABLES:
        op.add_column(
            table,
            sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        )
        op.create_index(f"ix_{table}_deleted_at", table, ["deleted_at"])


def downgrade() -> None:
    for table in reversed(_TARGET_TABLES):
        op.drop_index(f"ix_{table}_deleted_at", table_name=table)
        op.drop_column(table, "deleted_at")
