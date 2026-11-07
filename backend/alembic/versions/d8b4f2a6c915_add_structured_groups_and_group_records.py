"""add structured groups and group records (关联结构化数据)

Revision ID: d8b4f2a6c915
Revises: c2e5a7d81f34
Create Date: 2026-11-05 21:30:00.000000

第四阶段「关联结构化数据管理」的结构变更，**全部是新增表**：documents、
structured_parses、structured_records、chunks 等第三阶段与第二阶段的表一字未动，
不做任何数据清理或重建。

1. ``structured_groups``：一个关联数据文件组（= 一个独立关联导入任务）。
   关系配置与校验报告用 JSONB 存快照，推测出的关系 ``confirmed=false``，
   未经用户确认不会进入正式生成流程。
2. ``structured_group_files``：文件组成员（group_id + doc_id 唯一）。
   两个外键都是 ``ON DELETE CASCADE``：彻底删除文件时成员行自动消失，
   路由层随即把该组标记为 stale，因此不会残留指向已删除文件的关系。
3. ``structured_group_records``：确认后生成的关联知识记录 + 来源追溯坐标，
   group_id FK CASCADE，删除文件组时关联记录随之清理。

downgrade 只删这三张新表。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = 'd8b4f2a6c915'
down_revision: Union[str, Sequence[str], None] = 'c2e5a7d81f34'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'structured_groups',
        sa.Column('id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('kb_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('name', sa.String(length=255), nullable=False),
        sa.Column('status', sa.String(length=16), nullable=False),
        sa.Column('relations', postgresql.JSONB(), nullable=False),
        sa.Column('validation', postgresql.JSONB(), nullable=False),
        sa.Column('record_count', sa.Integer(), nullable=False),
        sa.Column('error_message', sa.Text(), nullable=False),
        sa.Column('validated_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('confirmed_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['kb_id'], ['knowledge_bases.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_structured_groups_kb_id'), 'structured_groups', ['kb_id'], unique=False)
    op.create_index(op.f('ix_structured_groups_status'), 'structured_groups', ['status'], unique=False)

    op.create_table(
        'structured_group_files',
        sa.Column('id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('group_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('doc_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('kb_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('order_no', sa.Integer(), nullable=False),
        sa.Column('alias', sa.String(length=64), nullable=False),
        sa.Column('file_name', sa.String(length=255), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['doc_id'], ['documents.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['group_id'], ['structured_groups.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['kb_id'], ['knowledge_bases.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('group_id', 'doc_id', name='uq_structured_group_files_group_doc'),
    )
    op.create_index(op.f('ix_structured_group_files_group_id'), 'structured_group_files', ['group_id'], unique=False)
    op.create_index(op.f('ix_structured_group_files_doc_id'), 'structured_group_files', ['doc_id'], unique=False)
    op.create_index(op.f('ix_structured_group_files_kb_id'), 'structured_group_files', ['kb_id'], unique=False)

    op.create_table(
        'structured_group_records',
        sa.Column('id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('group_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('kb_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('seq_no', sa.Integer(), nullable=False),
        sa.Column('anchor_doc_id', postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column('anchor_record_id', postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column('knowledge_text', sa.Text(), nullable=False),
        sa.Column('parts', postgresql.JSONB(), nullable=False),
        sa.Column('sources', postgresql.JSONB(), nullable=False),
        sa.Column('relation_ids', postgresql.JSONB(), nullable=False),
        sa.Column('anomalies', postgresql.JSONB(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['group_id'], ['structured_groups.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['kb_id'], ['knowledge_bases.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_structured_group_records_group_id'), 'structured_group_records', ['group_id'], unique=False)
    op.create_index(op.f('ix_structured_group_records_kb_id'), 'structured_group_records', ['kb_id'], unique=False)
    op.create_index(
        'ix_structured_group_records_group_seq',
        'structured_group_records',
        ['group_id', 'seq_no'],
        unique=False,
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index('ix_structured_group_records_group_seq', table_name='structured_group_records')
    op.drop_index(op.f('ix_structured_group_records_kb_id'), table_name='structured_group_records')
    op.drop_index(op.f('ix_structured_group_records_group_id'), table_name='structured_group_records')
    op.drop_table('structured_group_records')
    op.drop_index(op.f('ix_structured_group_files_kb_id'), table_name='structured_group_files')
    op.drop_index(op.f('ix_structured_group_files_doc_id'), table_name='structured_group_files')
    op.drop_index(op.f('ix_structured_group_files_group_id'), table_name='structured_group_files')
    op.drop_table('structured_group_files')
    op.drop_index(op.f('ix_structured_groups_status'), table_name='structured_groups')
    op.drop_index(op.f('ix_structured_groups_kb_id'), table_name='structured_groups')
    op.drop_table('structured_groups')
