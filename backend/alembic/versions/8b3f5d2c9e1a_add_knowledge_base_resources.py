"""add knowledge_base_resources

Revision ID: 8b3f5d2c9e1a
Revises: a4d9ebb210fa
Create Date: 2026-09-21 18:30:00.000000

TASK-008 Stage 2: 新增 knowledge_base_resources 表，用于把传统资源
（herb/prescription/theory/literature）多态挂载到 KnowledgeBase，作为后续
Resource 进入 RAG 检索的数据基础。本迁移仅建立结构与约束，不修改任何
既有表、不写入业务数据；resource_id 不建 FK（多态关联，存在性由应用层校验）。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '8b3f5d2c9e1a'
down_revision: Union[str, Sequence[str], None] = 'a4d9ebb210fa'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # TASK-008 Stage 2：KB ↔ 传统资源多态挂载关联表
    # - knowledge_base_id FK CASCADE：KB 删除时关联自动清理
    # - resource_id 无 FK：多态关联，PostgreSQL 不支持单一 FK 指向多表，
    #   资源存在性/类型-UUID 匹配由应用层在 Stage 3 挂载接口校验
    # - 唯一约束 (kb_id, resource_type, resource_id)：同一 KB 不重复挂载同一资源
    #   复合唯一约束自带以 knowledge_base_id 为最左前缀的索引，覆盖
    #   "KB 内全部挂载"查询，无需单独为 knowledge_base_id 建索引
    # - 反查索引 (resource_type, resource_id)：供删除 Resource 时按类型+ID 清理挂载
    op.create_table('knowledge_base_resources',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('knowledge_base_id', sa.UUID(), nullable=False),
        sa.Column('resource_type', sa.String(length=16), nullable=False),
        sa.Column('resource_id', sa.UUID(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['knowledge_base_id'], ['knowledge_bases.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('knowledge_base_id', 'resource_type', 'resource_id', name='uq_kbr_kb_type_resource'),
    )
    # 反查索引：删除 Resource 时需按 (resource_type, resource_id) 清理所有挂载。
    # 唯一复合约束最左前缀是 knowledge_base_id，不覆盖此查询路径，故单独建索引。
    op.create_index(
        'ix_knowledge_base_resources_resource_lookup',
        'knowledge_base_resources',
        ['resource_type', 'resource_id'],
        unique=False,
    )


def downgrade() -> None:
    """Downgrade schema."""
    # 反向：先 drop 反查索引，再 drop 表（FK/Unique/PK 随表一并删除）
    op.drop_index(
        'ix_knowledge_base_resources_resource_lookup',
        table_name='knowledge_base_resources',
    )
    op.drop_table('knowledge_base_resources')
