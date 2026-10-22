"""add_document_vector_model_and_revectorize_jobs

BGE-M3 切换体验优化（增量）：

1. documents.vector_model：记录"当前 Milvus 中该文档向量由哪个 embedding 模型生成"。
   可空列，历史数据保持 NULL（legacy，检索行为不变）。
2. revectorize_jobs：批量重新向量化任务表（进度 / 失败明细 / 重试）。

两项均为**纯新增**，不删除、不修改既有列，已有数据安全。
downgrade 只移除新增对象，且不触碰 documents 的业务数据（仅删本列）。

Revision ID: 057cb15ef418
Revises: c185a0406aa3
Create Date: 2026-09-25
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "057cb15ef418"
down_revision: str | None = "c185a0406aa3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # ── 1. documents.vector_model（幂等：列已存在则跳过）──────────────────────
    conn = op.get_bind()
    exists = conn.execute(
        sa.text(
            "SELECT 1 FROM information_schema.columns "
            "WHERE table_schema = 'public' AND table_name = 'documents' "
            "AND column_name = 'vector_model'"
        )
    ).scalar()
    if not exists:
        op.add_column(
            "documents",
            sa.Column("vector_model", sa.String(length=128), nullable=True),
        )
        op.create_index("ix_documents_vector_model", "documents", ["vector_model"])

    # ── 2. revectorize_jobs（幂等：表已存在则跳过）────────────────────────────
    table_exists = conn.execute(
        sa.text(
            "SELECT 1 FROM information_schema.tables "
            "WHERE table_schema = 'public' AND table_name = 'revectorize_jobs'"
        )
    ).scalar()
    if not table_exists:
        op.create_table(
            "revectorize_jobs",
            sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("target_model", sa.String(length=128), nullable=False),
            sa.Column("previous_model", sa.String(length=128), nullable=True),
            sa.Column("kb_id", postgresql.UUID(as_uuid=True), nullable=True),
            sa.Column("status", sa.String(length=16), nullable=False),
            sa.Column("total", sa.Integer(), nullable=False),
            sa.Column("processed", sa.Integer(), nullable=False),
            sa.Column("succeeded", sa.Integer(), nullable=False),
            sa.Column("failed", sa.Integer(), nullable=False),
            sa.Column("failed_doc_ids", postgresql.JSONB(), nullable=False),
            sa.Column("error_message", sa.Text(), nullable=False),
            sa.Column("created_by", postgresql.UUID(as_uuid=True), nullable=True),
            sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                server_default=sa.text("now()"),
                nullable=False,
            ),
            sa.Column(
                "updated_at",
                sa.DateTime(timezone=True),
                server_default=sa.text("now()"),
                nullable=False,
            ),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index("ix_revectorize_jobs_kb_id", "revectorize_jobs", ["kb_id"])
        op.create_index("ix_revectorize_jobs_status", "revectorize_jobs", ["status"])


def downgrade() -> None:
    """仅移除本迁移新增的对象。

    说明：documents.vector_model 是可空的"标注列"，downgrade 删除它不会丢失
    文档 / 切片 / 资源等业务数据，仅丢失"向量由哪个模型生成"的标注信息；
    重新执行 upgrade 后可通过"批量重新向量化"重新标注。
    """
    conn = op.get_bind()
    table_exists = conn.execute(
        sa.text(
            "SELECT 1 FROM information_schema.tables "
            "WHERE table_schema = 'public' AND table_name = 'revectorize_jobs'"
        )
    ).scalar()
    if table_exists:
        op.drop_index("ix_revectorize_jobs_status", table_name="revectorize_jobs")
        op.drop_index("ix_revectorize_jobs_kb_id", table_name="revectorize_jobs")
        op.drop_table("revectorize_jobs")

    exists = conn.execute(
        sa.text(
            "SELECT 1 FROM information_schema.columns "
            "WHERE table_schema = 'public' AND table_name = 'documents' "
            "AND column_name = 'vector_model'"
        )
    ).scalar()
    if exists:
        op.drop_index("ix_documents_vector_model", table_name="documents")
        op.drop_column("documents", "vector_model")
