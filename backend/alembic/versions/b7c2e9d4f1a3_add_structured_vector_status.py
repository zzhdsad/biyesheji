"""add structured vector status columns (统一向量索引接入状态)

Revision ID: b7c2e9d4f1a3
Revises: e9a4c1b7d3f2
Create Date: 2026-11-06 10:00:00.000000

第六阶段「统一 RAG 知识库接入」的唯一结构变更：**两张已有表各加 5 个可空/带默认值的列**。

- structured_parses：记录单个结构化文件的知识记录是否已写入统一向量索引；
- structured_groups：记录关联文件组（仅 confirmed 状态）是否已写入统一向量索引。

不改任何既有列、不重建表、不清数据；历史行按 server_default 得到 pending/0，
语义是「尚未接入向量索引」，与页面上的「待向量化」一致，不做批量回填
（回填需要加载 Embedding 模型，属于长任务，由用户在页面上按需触发）。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b7c2e9d4f1a3'
down_revision: Union[str, Sequence[str], None] = 'e9a4c1b7d3f2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_VECTOR_COLUMNS = (
    sa.Column(
        "vector_status",
        sa.String(length=16),
        nullable=False,
        server_default="pending",
    ),
    sa.Column(
        "vector_count",
        sa.Integer(),
        nullable=False,
        server_default="0",
    ),
    sa.Column("vector_model", sa.String(length=128), nullable=True),
    sa.Column("vector_error", sa.Text(), nullable=False, server_default=""),
    sa.Column("vectorized_at", sa.DateTime(timezone=True), nullable=True),
)


def upgrade() -> None:
    """Upgrade schema."""
    for table in ("structured_parses", "structured_groups"):
        for column in _VECTOR_COLUMNS:
            op.add_column(table, column.copy())


def downgrade() -> None:
    """Downgrade schema."""
    for table in ("structured_parses", "structured_groups"):
        for name in ("vectorized_at", "vector_error", "vector_model", "vector_count", "vector_status"):
            op.drop_column(table, name)
