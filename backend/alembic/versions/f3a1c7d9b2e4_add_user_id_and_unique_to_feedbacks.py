"""add user_id and unique constraint to feedbacks

BUG-038：feedbacks 表缺归属维度（无 user_id）且没有任何唯一约束，
并发重复提交会插入多行 Feedback（应用层"先查后写"存在并发窗口），
同一条消息的反馈计数虚高、覆盖式更新语义失效。

- feedbacks.user_id：可空 UUID，外键 users.id（ON DELETE CASCADE）+ 索引。
  迁移前既有行保持 NULL（历史数据无法可靠推断归属，不回填）。
- uq_feedbacks_user_message：(user_id, message_id) 唯一约束。
  PostgreSQL 唯一约束中 NULL 互不相等 → 历史行（user_id IS NULL）不会
  触发冲突，故本迁移无需清理既有重复行。

Revision ID: f3a1c7d9b2e4
Revises: c9d1e4f70a2b
Create Date: 2026-09-24
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f3a1c7d9b2e4"
down_revision: str | Sequence[str] | None = "c9d1e4f70a2b"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("feedbacks", sa.Column("user_id", sa.UUID(), nullable=True))
    op.create_index("ix_feedbacks_user_id", "feedbacks", ["user_id"])
    op.create_foreign_key(
        "fk_feedbacks_user_id_users",
        "feedbacks",
        "users",
        ["user_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_unique_constraint(
        "uq_feedbacks_user_message", "feedbacks", ["user_id", "message_id"]
    )


def downgrade() -> None:
    op.drop_constraint("uq_feedbacks_user_message", "feedbacks", type_="unique")
    op.drop_constraint("fk_feedbacks_user_id_users", "feedbacks", type_="foreignkey")
    op.drop_index("ix_feedbacks_user_id", table_name="feedbacks")
    op.drop_column("feedbacks", "user_id")
