"""reconstruct lost revision e6a3b8c1d2f4 (placeholder)

问题：数据库 alembic_version 停在 e6a3b8c1d2f4，但该版本的迁移文件已不在
backend/alembic/versions 目录中，导致 Alembic 报
"Can't locate revision identified by 'e6a3b8c1d2f4'"，任何迁移都无法执行。

核对结论（本文件操作内容的由来）：
- 现有 17 个迁移文件共创建 30 张表，与线上 knowledge_platform 的表清单完全一致，
  说明该版本**没有新建表**；
- 将「全新库（按迁移链从头建）」与「线上库」逐列比对，唯一差异是
  users.deleted_at 的时区类型：线上为 timestamptz，全新库为 naive。
  故该版本唯一的实际 DDL 就是这次类型调整，此处按比对结果复原。

目的：修复迁移链，并使「按迁移链从头构建的库」与线上库结构一致（可验证）。
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e6a3b8c1d2f4"
down_revision: Union[str, Sequence[str], None] = "b3f21c7d9e4a"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 依据「线上库 vs 全新库」结构比对结果复原：users.deleted_at 改为带时区。
    # 其他表/列经比对完全一致，不做任何猜测性操作。
    op.alter_column(
        "users",
        "deleted_at",
        existing_type=sa.DateTime(),
        type_=sa.DateTime(timezone=True),
        existing_nullable=True,
    )


def downgrade() -> None:
    op.alter_column(
        "users",
        "deleted_at",
        existing_type=sa.DateTime(timezone=True),
        type_=sa.DateTime(),
        existing_nullable=True,
    )
