"""add documents.content_hash (内容指纹，用于重复文件提示)

Revision ID: e9a4c1b7d3f2
Revises: d8b4f2a6c915
Create Date: 2026-11-05 23:10:00.000000

第五阶段「统一解析与处理」的唯一结构变更：**documents 增加一个可空列 + 普通索引**。

- 不改动任何既有列、不重建表、不清数据；
- 历史行的 content_hash 保持 NULL，语义是「未检测」，不做批量回填
  （回填要逐个读取物理文件，属于长任务，本阶段不启动）；
- 该列只服务于一件事：同一知识库内提示「内容完全相同的文件已存在」，
  既不做模糊匹配，也不构成删除或覆盖任何数据的依据。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'e9a4c1b7d3f2'
down_revision: Union[str, Sequence[str], None] = 'd8b4f2a6c915'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "documents",
        sa.Column("content_hash", sa.String(length=64), nullable=True),
    )
    op.create_index(
        op.f("ix_documents_content_hash"),
        "documents",
        ["content_hash"],
        unique=False,
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f("ix_documents_content_hash"), table_name="documents")
    op.drop_column("documents", "content_hash")
