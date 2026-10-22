"""add literatures and literature tags

Revision ID: a4d9ebb210fa
Revises: 17b910773a2d
Create Date: 2026-09-21 08:19:33.164481

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = 'a4d9ebb210fa'
down_revision: Union[str, Sequence[str], None] = '17b910773a2d'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # TASK-006: 中医文献资源表 + 关联表（与 theories 同构，不修改任何既有表）
    # 第一步：literatures
    op.create_table('literatures',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('name', sa.String(length=128), nullable=False),
    sa.Column('aliases', postgresql.ARRAY(sa.String(length=128)), nullable=False),
    sa.Column('category_id', sa.UUID(), nullable=True),
    sa.Column('author', sa.String(length=255), nullable=False),
    sa.Column('dynasty', sa.String(length=32), nullable=False),
    sa.Column('summary', sa.String(length=500), nullable=False),
    sa.Column('content', sa.Text(), nullable=False),
    sa.Column('source', sa.String(length=255), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['category_id'], ['categories.id'], ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id')
    )
    # 第二步：索引（name unique / category_id btree / aliases GIN）
    op.create_index(op.f('ix_literatures_name'), 'literatures', ['name'], unique=True)
    op.create_index(op.f('ix_literatures_category_id'), 'literatures', ['category_id'], unique=False)
    op.create_index('ix_literatures_aliases_gin', 'literatures', ['aliases'], unique=False, postgresql_using='gin')
    # 第三步：literature_tags（literature_id CASCADE / tag_id RESTRICT，复合主键）
    op.create_table('literature_tags',
    sa.Column('literature_id', sa.UUID(), nullable=False),
    sa.Column('tag_id', sa.UUID(), nullable=False),
    sa.ForeignKeyConstraint(['literature_id'], ['literatures.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['tag_id'], ['tags.id'], ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('literature_id', 'tag_id')
    )


def downgrade() -> None:
    """Downgrade schema."""
    # 反向：先 drop 关联表，再 drop 索引与主表
    op.drop_table('literature_tags')
    op.drop_index('ix_literatures_aliases_gin', table_name='literatures', postgresql_using='gin')
    op.drop_index(op.f('ix_literatures_category_id'), table_name='literatures')
    op.drop_index(op.f('ix_literatures_name'), table_name='literatures')
    op.drop_table('literatures')
