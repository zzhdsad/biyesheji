"""add reflection fields to evaluation_results

阶段十五（Self Reflection）归档。

复用阶段九既有的 EvaluationRun / EvaluationResult，不新增实验表；
仅为 evaluation_results 增加四个可空字段，记录每条用例的自反思结果：
- reflection_decision（accept / revise / retry）
- reflection_version（反思规则版本，便于实验对齐）
- reflection_retry_strategy（Reflection retry 实际使用的策略；未重试为 NULL）
- reflection_reason（受控问题码，'+' 连接；供后续按原因归类分析）

Reflection 的完整配置（开关 / LLM 开关 / 阈值 / 上限）写入既有
EvaluationRun.config_snapshot["self_reflection"]，不新增列。

Revision ID: b7c4e8f2a1d9
Revises: d4e2f7a91b3c
Create Date: 2026-09-23
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b7c4e8f2a1d9"
down_revision: str | Sequence[str] | None = "d4e2f7a91b3c"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "evaluation_results",
        sa.Column("reflection_decision", sa.String(32), nullable=True),
    )
    op.add_column(
        "evaluation_results",
        sa.Column("reflection_version", sa.String(32), nullable=True),
    )
    op.add_column(
        "evaluation_results",
        sa.Column("reflection_retry_strategy", sa.String(64), nullable=True),
    )
    op.add_column(
        "evaluation_results",
        sa.Column("reflection_reason", sa.String(255), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("evaluation_results", "reflection_reason")
    op.drop_column("evaluation_results", "reflection_retry_strategy")
    op.drop_column("evaluation_results", "reflection_version")
    op.drop_column("evaluation_results", "reflection_decision")
