"""add_legacy_drift_columns_progress_percent_faithfulness（BUG-067）

背景：开发库里有两列**只存在于数据库、既不在 ORM 也不在任何迁移里**：

- ``documents.progress_percent``           INTEGER NOT NULL DEFAULT 0
- ``evaluation_results.faithfulness``      DOUBLE PRECISION NOT NULL DEFAULT 0.0

成因是历史上开发曾用 ``create_all`` 按旧模型建表（AGENTS.md §4 / BUG-067 审计），
导致 schema drift：**新环境用 Alembic 从头构建时不会包含这两列**，与开发库结构不同。

处理策略（本迁移刻意保守）：

1. **不删除任何列、不删除任何数据**：这两列在开发库里已有真实数据（documents 2492 行、
   evaluation_results 40 行全部有值），因此 downgrade **不做 DROP**。
2. **幂等**：upgrade 前先 ``inspect`` 目标列是否已存在，存在则跳过。
   这样对"已漂移的开发库"是 no-op，对"新环境"才真正建列，两边都能 upgrade 到 head。
3. **不修改历史迁移**：只追加一个新版本（down_revision = f3a1c7d9b2e4）。

校验方式：``alembic upgrade head`` → ``alembic downgrade -1`` → ``alembic upgrade head``
均可重复执行且结构不变（见 tests/test_alembic_smoke.py 与 test_batch6_quality.py）。

Revision ID: c185a0406aa3
Revises: f3a1c7d9b2e4
"""

from typing import Sequence

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "c185a0406aa3"
down_revision: str | Sequence[str] | None = "f3a1c7d9b2e4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# (表名, 列名, 列定义)——类型与默认值取自开发库实测 information_schema
_LEGACY_COLUMNS: list[tuple[str, str, sa.Column]] = [
    (
        "documents",
        "progress_percent",
        sa.Column("progress_percent", sa.Integer(), nullable=False, server_default=sa.text("0")),
    ),
    (
        "evaluation_results",
        "faithfulness",
        # PostgreSQL FLOAT 无精度即 double precision（float8），与实测 data_type 一致
        sa.Column("faithfulness", sa.Float(), nullable=False, server_default=sa.text("0.0")),
    ),
]


def _column_exists(table: str, column: str) -> bool:
    """目标列是否已存在（幂等关键：已漂移的库不得重复 add_column）。"""
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    return column in {col["name"] for col in inspector.get_columns(table)}


def upgrade() -> None:
    """补齐两列"迁移缺失但数据库中存在"的漂移列；已存在则跳过。"""
    for table, column, definition in _LEGACY_COLUMNS:
        if _column_exists(table, column):
            # 已漂移的开发库：什么都不做，仅保证版本号能推进到 head
            continue
        op.add_column(table, definition)


def downgrade() -> None:
    """刻意不 DROP 这两列（BUG-067 约定：不删除数据）。

    理由：这两列并非由本迁移创建——它们在被引擎初始化时的旧模型里就已存在，
    且开发库中已有真实数据。这里若执行 DROP，会让 ``downgrade`` 删掉本迁移没有
    创建过的列及其数据；一旦有人在开发/预发环境误跑 downgrade，数据无法回滚。

    如果将来确定这两列要下线（ORM 也同步删除字段），应**另写一个显式的删除迁移**，
    并走数据备份流程，而不是让本迁移的 downgrade 隐式删列。
    """
    return
