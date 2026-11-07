"""add structured parses and structured records

Revision ID: c2e5a7d81f34
Revises: b7f3c19d4e52
Create Date: 2026-11-05 15:10:00.000000

第三阶段「非关联结构化数据管理」的结构变更，全部是**新增表**，不修改、不清空任何
存量数据（documents / chunks / import_* 等表结构一字未动）：

1. ``structured_parses``：一个结构化文件一份解析概览（字段清单、记录数、异常统计）。
   ``doc_id`` 唯一 + FK ``ON DELETE CASCADE``，所以彻底删除文件时概览随之清理，
   不需要额外级联代码，也不可能误删其他文件的解析结果。
2. ``structured_records``：一条原始记录 = 一条独立知识记录，payload 用 JSONB
   原样保留字段与值，row_no / sheet_name / json_path 负责回溯到原始位置。
   同样 doc_id FK CASCADE，与上表同生同灭。

没有新增 documents 列，也没有改 parse_status 的语义：结构化文件仍保持
``parse_status='skipped'``，第二阶段的解析/向量化守卫因此继续生效，
两个空间的 RAG 隔离不受本迁移影响。

downgrade 只删这两张新表。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = 'c2e5a7d81f34'
down_revision: Union[str, Sequence[str], None] = 'b7f3c19d4e52'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'structured_parses',
        sa.Column('id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('doc_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('kb_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('status', sa.String(length=16), nullable=False),
        sa.Column('detected_format', sa.String(length=16), nullable=False),
        sa.Column('encoding', sa.String(length=32), nullable=False),
        sa.Column('fields', postgresql.JSONB(), nullable=False),
        sa.Column('record_count', sa.Integer(), nullable=False),
        sa.Column('skipped_count', sa.Integer(), nullable=False),
        sa.Column('anomaly_count', sa.Integer(), nullable=False),
        sa.Column('truncated', sa.Boolean(), nullable=False),
        sa.Column('error_message', sa.Text(), nullable=False),
        sa.Column('parsed_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['doc_id'], ['documents.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['kb_id'], ['knowledge_bases.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_structured_parses_doc_id'), 'structured_parses', ['doc_id'], unique=True)
    op.create_index(op.f('ix_structured_parses_kb_id'), 'structured_parses', ['kb_id'], unique=False)
    op.create_index(op.f('ix_structured_parses_status'), 'structured_parses', ['status'], unique=False)

    op.create_table(
        'structured_records',
        sa.Column('id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('doc_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('kb_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('seq_no', sa.Integer(), nullable=False),
        sa.Column('row_no', sa.Integer(), nullable=False),
        sa.Column('sheet_name', sa.String(length=128), nullable=True),
        sa.Column('json_path', sa.String(length=512), nullable=True),
        sa.Column('record_label', sa.String(length=255), nullable=False),
        sa.Column('payload', postgresql.JSONB(), nullable=False),
        sa.Column('knowledge_text', sa.Text(), nullable=False),
        sa.Column('anomalies', postgresql.JSONB(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['doc_id'], ['documents.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['kb_id'], ['knowledge_bases.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_structured_records_doc_id'), 'structured_records', ['doc_id'], unique=False)
    op.create_index(op.f('ix_structured_records_kb_id'), 'structured_records', ['kb_id'], unique=False)
    op.create_index(
        'ix_structured_records_doc_seq', 'structured_records', ['doc_id', 'seq_no'], unique=False
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index('ix_structured_records_doc_seq', table_name='structured_records')
    op.drop_index(op.f('ix_structured_records_kb_id'), table_name='structured_records')
    op.drop_index(op.f('ix_structured_records_doc_id'), table_name='structured_records')
    op.drop_table('structured_records')
    op.drop_index(op.f('ix_structured_parses_status'), table_name='structured_parses')
    op.drop_index(op.f('ix_structured_parses_kb_id'), table_name='structured_parses')
    op.drop_index(op.f('ix_structured_parses_doc_id'), table_name='structured_parses')
    op.drop_table('structured_parses')
