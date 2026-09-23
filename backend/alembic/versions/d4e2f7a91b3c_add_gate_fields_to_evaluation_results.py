"""add gate fields to evaluation_results

阶段十四（TASK-014）：Evidence Gate 归档。

复用阶段九既有的 EvaluationRun / EvaluationResult，不新增实验表；
仅为 evaluation_results 增加三个可空字段，记录每条用例的 Gate 结果与 retry 信息：
- gate_decision（accept / insufficient / retry）
- gate_version（门控规则版本，便于实验对齐）
- retry_strategy（Gate 判定 retry 时实际使用的策略；未重试为 NULL）

Gate 的完整配置（阈值 / 关系可信度 / retry 映射）写入既有
EvaluationRun.config_snapshot["evidence_gate"]，不新增列。

Revision ID: d4e2f7a91b3c
Revises: c3f1a2b4d5e6
Create Date: 2026-09-23
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d4e2f7a91b3c"
down_revision: str | Sequence[str] | None = "c3f1a2b4d5e6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "evaluation_results",
        sa.Column("gate_decision", sa.String(32), nullable=True),
    )
    op.add_column(
        "evaluation_results",
        sa.Column("gate_version", sa.String(32), nullable=True),
    )
    op.add_column(
        "evaluation_results",
        sa.Column("retry_strategy", sa.String(64), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("evaluation_results", "retry_strategy")
    op.drop_column("evaluation_results", "gate_version")
    op.drop_column("evaluation_results", "gate_decision")
