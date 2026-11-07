"""add structured parse read/failed counts (分批解析计数)

Revision ID: f4b8c2d9e6a1
Revises: b7c2e9d4f1a3
Create Date: 2026-11-06 12:00:00.000000

分批解析需要区分「实际读取」「成功写入」「失败」三类计数：

- record_count 继续表示当前已成功落库的记录数（失败时保留上一份成功结果）；
- read_count 表示最近一次解析实际读取的数据行数；
- failed_count 表示最近一次解析失败时未能写入的行数，成功恒为 0。

只加两个带默认值的整数列，不改既有列、不重建表、不清数据；历史行默认 0。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "f4b8c2d9e6a1"
down_revision: Union[str, Sequence[str], None] = "b7c2e9d4f1a3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "structured_parses",
        sa.Column("read_count", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "structured_parses",
        sa.Column("failed_count", sa.Integer(), nullable=False, server_default="0"),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("structured_parses", "failed_count")
    op.drop_column("structured_parses", "read_count")
