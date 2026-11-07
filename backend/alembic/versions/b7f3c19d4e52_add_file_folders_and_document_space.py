"""add file folders and document space/folder columns

Revision ID: b7f3c19d4e52
Revises: 7d1e9c4b2a60
Create Date: 2026-11-05 09:30:00.000000

第一阶段「文件与文件夹管理」的结构变更，全部为**新增**，不改写、不清空任何存量数据：

1. 新表 ``file_folders``：普通文档 / 结构化数据各一棵文件夹树（``space`` 区分），
   ``parent_id`` 自引用 + ``RESTRICT`` 防误删，同 (space, parent) 下目录名唯一。
2. ``documents.space``：VARCHAR(16) NOT NULL，``server_default='document'``。
   存量行因此全部落到「普通文档」空间，行为与改造前完全一致。
3. ``documents.folder_id``：UUID NULL + FK ``RESTRICT``。NULL = 空间根目录。
   物理文件位置仍由 ``storage_path`` 决定，移动文件只改这一列，不搬文件、不重复上传。

downgrade 只做反向 DDL；因为 folder_id 有 RESTRICT 外键，回滚前需由使用方自行确认
没有残留引用（本迁移不提供数据清理）。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b7f3c19d4e52'
down_revision: Union[str, Sequence[str], None] = '7d1e9c4b2a60'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'file_folders',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('space', sa.String(length=16), nullable=False),
        sa.Column('name', sa.String(length=64), nullable=False),
        sa.Column('parent_id', sa.UUID(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['parent_id'], ['file_folders.id'], ondelete='RESTRICT'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_file_folders_space'), 'file_folders', ['space'], unique=False)
    op.create_index(op.f('ix_file_folders_parent_id'), 'file_folders', ['parent_id'], unique=False)
    # 根目录 parent_id IS NULL 不参与唯一比较，用 COALESCE 归一到零 UUID
    op.create_index(
        'uq_file_folders_space_parent_name',
        'file_folders',
        [
            'space',
            sa.literal_column("coalesce(parent_id, '00000000-0000-0000-0000-000000000000')"),
            'name',
        ],
        unique=True,
    )

    op.add_column(
        'documents',
        sa.Column('space', sa.String(length=16), server_default='document', nullable=False),
    )
    op.create_index(op.f('ix_documents_space'), 'documents', ['space'], unique=False)
    op.add_column('documents', sa.Column('folder_id', sa.UUID(), nullable=True))
    op.create_index(op.f('ix_documents_folder_id'), 'documents', ['folder_id'], unique=False)
    op.create_foreign_key(
        'fk_documents_folder_id_file_folders',
        'documents',
        'file_folders',
        ['folder_id'],
        ['id'],
        ondelete='RESTRICT',
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_constraint('fk_documents_folder_id_file_folders', 'documents', type_='foreignkey')
    op.drop_index(op.f('ix_documents_folder_id'), table_name='documents')
    op.drop_column('documents', 'folder_id')
    op.drop_index(op.f('ix_documents_space'), table_name='documents')
    op.drop_column('documents', 'space')
    op.drop_index('uq_file_folders_space_parent_name', table_name='file_folders')
    op.drop_index(op.f('ix_file_folders_parent_id'), table_name='file_folders')
    op.drop_index(op.f('ix_file_folders_space'), table_name='file_folders')
    op.drop_table('file_folders')
