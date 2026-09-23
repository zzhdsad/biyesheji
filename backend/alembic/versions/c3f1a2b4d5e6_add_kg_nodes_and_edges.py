"""add kg_nodes and kg_edges

阶段十三（TASK-013）：知识图谱最小模型。

- kg_nodes：图谱节点（现有 Herb/Prescription/Theory/Literature 资源的图谱投影，
  只保存定位与实体匹配所需的 name/aliases，不复制资源正文）
- kg_edges：图谱边（只保存能由现有业务数据可靠推导的关系 + provenance）

Revision ID: c3f1a2b4d5e6
Revises: ec98b0dafd5b
Create Date: 2026-09-23
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "c3f1a2b4d5e6"
down_revision: str | Sequence[str] | None = "ec98b0dafd5b"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "kg_nodes",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("node_type", sa.String(16), nullable=False, server_default="resource"),
        sa.Column("resource_type", sa.String(16), nullable=False),
        sa.Column("resource_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column(
            "aliases",
            postgresql.ARRAY(sa.String(128)),
            nullable=False,
            server_default="{}",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint("resource_type", "resource_id", name="uq_kg_nodes_resource"),
    )
    op.create_index("ix_kg_nodes_node_type", "kg_nodes", ["node_type"])
    op.create_index("ix_kg_nodes_resource_type", "kg_nodes", ["resource_type"])
    op.create_index("ix_kg_nodes_name", "kg_nodes", ["name"])
    op.create_index(
        "ix_kg_nodes_type_resource_type", "kg_nodes", ["node_type", "resource_type"]
    )
    op.create_index(
        "ix_kg_nodes_aliases_gin",
        "kg_nodes",
        ["aliases"],
        postgresql_using="gin",
    )

    op.create_table(
        "kg_edges",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "source_node_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("kg_nodes.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "target_node_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("kg_nodes.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("relation_type", sa.String(32), nullable=False),
        sa.Column("provenance", sa.String(64), nullable=False, server_default=""),
        sa.Column("description", sa.String(255), nullable=False, server_default=""),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint(
            "source_node_id",
            "target_node_id",
            "relation_type",
            name="uq_kg_edges_source_target_relation",
        ),
    )
    op.create_index("ix_kg_edges_source_node_id", "kg_edges", ["source_node_id"])
    op.create_index("ix_kg_edges_target_node_id", "kg_edges", ["target_node_id"])
    op.create_index("ix_kg_edges_relation_type", "kg_edges", ["relation_type"])
    op.create_index(
        "ix_kg_edges_relation_source", "kg_edges", ["relation_type", "source_node_id"]
    )


def downgrade() -> None:
    op.drop_table("kg_edges")
    op.drop_table("kg_nodes")
