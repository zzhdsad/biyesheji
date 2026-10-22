"""add failed_count and idempotency_key to evaluation_runs

Batch 3（评测可信度）两个 Bug 的最小迁移：

- BUG-011：单条用例因基础设施故障失败时，旧逻辑把它算成 context_relevancy=0
  并计入均值，导致「Milvus 宕机」被记成「检索质量为 0」，污染实验结论。
  新增 failed_count 记录失败用例数，失败用例不进均值。
- BUG-023：评估运行缺少幂等/并发保护，同一实验可无限重复提交产生重复
  run + results。新增 idempotency_key（可空 + 唯一索引）来支持幂等去重，
  由唯一约束在数据库层兜住并发提交。

两列均为可空/带默认值，历史数据不受影响（既有行 failed_count 回填 0）。

Revision ID: c9d1e4f70a2b
Revises: b7c4e8f2a1d9
Create Date: 2026-09-23
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c9d1e4f70a2b"
down_revision: str | Sequence[str] | None = "b7c4e8f2a1d9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "evaluation_runs",
        sa.Column("failed_count", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "evaluation_runs",
        sa.Column("idempotency_key", sa.String(128), nullable=True),
    )
    # 历史行统一回填 0，语义与「无失败用例」一致
    op.execute("UPDATE evaluation_runs SET failed_count = 0 WHERE failed_count IS NULL")
    # 唯一索引：同一幂等键只允许一条 run（并发重复提交由数据库兜底）
    op.create_index(
        "ix_evaluation_runs_idempotency_key",
        "evaluation_runs",
        ["idempotency_key"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("ix_evaluation_runs_idempotency_key", table_name="evaluation_runs")
    op.drop_column("evaluation_runs", "idempotency_key")
    op.drop_column("evaluation_runs", "failed_count")
