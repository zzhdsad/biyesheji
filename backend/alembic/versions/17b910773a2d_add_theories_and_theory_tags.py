"""add theories and theory_tags

Revision ID: 17b910773a2d
Revises: df288e38709a
Create Date: 2026-09-20 16:27:32.826259

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = '17b910773a2d'
down_revision: Union[str, Sequence[str], None] = 'df288e38709a'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # TASK-005: 中医理论资源表 + 关联表
    op.create_table('theories',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('name', sa.String(length=128), nullable=False),
    sa.Column('aliases', postgresql.ARRAY(sa.String(length=128)), nullable=False),
    sa.Column('category_id', sa.UUID(), nullable=True),
    sa.Column('content', sa.Text(), nullable=False),
    sa.Column('source', sa.String(length=255), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['category_id'], ['categories.id'], ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_theories_aliases_gin', 'theories', ['aliases'], unique=False, postgresql_using='gin')
    op.create_index(op.f('ix_theories_category_id'), 'theories', ['category_id'], unique=False)
    op.create_index(op.f('ix_theories_name'), 'theories', ['name'], unique=True)
    op.create_table('theory_tags',
    sa.Column('theory_id', sa.UUID(), nullable=False),
    sa.Column('tag_id', sa.UUID(), nullable=False),
    sa.ForeignKeyConstraint(['tag_id'], ['tags.id'], ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['theory_id'], ['theories.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('theory_id', 'tag_id')
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table('theory_tags')
    op.drop_index(op.f('ix_theories_name'), table_name='theories')
    op.drop_index(op.f('ix_theories_category_id'), table_name='theories')
    op.drop_index('ix_theories_aliases_gin', table_name='theories', postgresql_using='gin')
    op.drop_table('theories')
