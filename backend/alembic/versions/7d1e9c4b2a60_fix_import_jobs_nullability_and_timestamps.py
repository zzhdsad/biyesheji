"""fix import_jobs nullability and timestamp defaults

背景：2f7c1d9a3b48 创建 import_jobs 时把 dataset_name / source_file / status /
计数器 / failed_items / error_message / created_at / updated_at 建成了可空列，
且 created_at / updated_at 没有数据库默认值；而 ORM（ImportJob + TimestampMixin）
声明这些列为 NOT NULL、时间列 server_default=now()。空库执行 upgrade head 后
即可复现该结构漂移（4-3B-1 已验证）。

本迁移只修 import_jobs 这一张表，不触碰其它表，也不修改已有迁移文件。
保守回填策略（不伪造业务事实）：

1. 空字符串 / 0 / 空 JSON 数组：与 ORM 默认值、以及 job_to_dict / upload 序列化中
   既有的 `or 0` / `or []` 读取语义完全一致，仅表示"未记录"，不改变任何可见行为；
2. status：属于状态机业务事实，无法由其它列安全推导（pending / completed / failed /
   cancelled 含义不同）。只要存在 NULL status 就抛错中止整个迁移（事务回滚，版本
   停在旧 head），要求人工确认后再执行，绝不猜测状态；
3. created_at / updated_at：优先采用真实记录时间 started_at / finished_at；两者都
   为空的历史行才以迁移执行时间兜底（纯元数据时间，不涉及业务状态），避免使用
   伪造时间改变任务排序或回收逻辑；updated_at 在 created_at 回填后取其值；
4. downgrade 只恢复列可空性并撤销时间默认值，不回退已回填的数据（避免降级丢信息）。

Revision ID: 7d1e9c4b2a60
Revises: 465081816093
Create Date: 2026-11-01
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "7d1e9c4b2a60"
down_revision: str | None = "465081816093"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# (列名, 现有类型)：这些列在 ORM 中均声明为非空
_NOT_NULL_COLUMNS: tuple[tuple[str, sa.types.TypeEngine], ...] = (
    ("dataset_name", sa.String(length=255)),
    ("source_file", sa.String(length=512)),
    ("status", sa.String(length=16)),
    ("total", sa.Integer()),
    ("processed", sa.Integer()),
    ("succeeded", sa.Integer()),
    ("failed", sa.Integer()),
    ("skipped", sa.Integer()),
    ("failed_items", postgresql.JSONB()),
    ("error_message", sa.Text()),
)


def upgrade() -> None:
    # 1. status 无法安全推导：存在 NULL 即中止（整个迁移事务回滚）
    op.execute(
        sa.text(
            """
            DO $$
            DECLARE
                null_status_count bigint;
            BEGIN
                SELECT count(*) INTO null_status_count
                FROM import_jobs WHERE status IS NULL;
                IF null_status_count > 0 THEN
                    RAISE EXCEPTION
                        'import_jobs has % row(s) with NULL status; '
                        'status cannot be derived safely. Resolve these rows '
                        'manually before running this migration.',
                        null_status_count;
                END IF;
            END
            $$;
            """
        )
    )

    # 2. 与 ORM 默认值语义一致的保守回填
    op.execute(
        sa.text(
            """
            UPDATE import_jobs SET
                dataset_name = COALESCE(dataset_name, ''),
                source_file = COALESCE(source_file, ''),
                total = COALESCE(total, 0),
                processed = COALESCE(processed, 0),
                succeeded = COALESCE(succeeded, 0),
                failed = COALESCE(failed, 0),
                skipped = COALESCE(skipped, 0),
                failed_items = COALESCE(failed_items, '[]'::jsonb),
                error_message = COALESCE(error_message, '')
            """
        )
    )

    # 3. 时间列：先 created_at，再让 updated_at 可使用回填后的 created_at
    op.execute(
        sa.text(
            """
            UPDATE import_jobs
            SET created_at = COALESCE(created_at, started_at, finished_at, now())
            WHERE created_at IS NULL
            """
        )
    )
    op.execute(
        sa.text(
            """
            UPDATE import_jobs
            SET updated_at = COALESCE(
                updated_at, finished_at, started_at, created_at, now()
            )
            WHERE updated_at IS NULL
            """
        )
    )

    # 4. 收紧非空约束
    for name, existing_type in _NOT_NULL_COLUMNS:
        op.alter_column(
            "import_jobs",
            name,
            existing_type=existing_type,
            existing_nullable=True,
            nullable=False,
        )

    # 5. 时间列：与 TimestampMixin 的 server_default=now() 对齐
    for name in ("created_at", "updated_at"):
        op.alter_column(
            "import_jobs",
            name,
            existing_type=sa.DateTime(timezone=True),
            existing_nullable=True,
            nullable=False,
            server_default=sa.text("now()"),
        )


def downgrade() -> None:
    # 只回退结构；已回填的数据保留（降级不应销毁信息）
    for name in ("created_at", "updated_at"):
        op.alter_column(
            "import_jobs",
            name,
            existing_type=sa.DateTime(timezone=True),
            existing_nullable=False,
            nullable=True,
            server_default=None,
        )

    for name, existing_type in reversed(_NOT_NULL_COLUMNS):
        op.alter_column(
            "import_jobs",
            name,
            existing_type=existing_type,
            existing_nullable=False,
            nullable=True,
        )
