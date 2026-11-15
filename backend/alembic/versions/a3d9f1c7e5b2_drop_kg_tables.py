"""drop kg tables: kg_edges, kg_nodes

KG 功能已从前端、后端、RAG、测试与 API 契约中整体下线（阶段 A-D）；
数据库中残留的 kg_edges / kg_nodes 两张表在本迁移中删除。

Revision ID: a3d9f1c7e5b2
Revises: f4b8c2d9e6a1
Create Date: 2026-11-10

upgrade():
  1. DROP TABLE kg_edges（先删：持有指向 kg_nodes 的两个外键）
  2. DROP TABLE kg_nodes
  两表上指向 knowledge_bases 的外键（fk_kg_edges_kb / fk_kg_nodes_kb）
  随表删除自动消失，不删除 knowledge_bases 或任何其他业务表，
  也不使用 CASCADE 清理其他数据库对象。

downgrade():
  按 head 时点（f4b8c2d9e6a1，即 465081816093 之后的真实结构）
  重建两张空表：先 kg_nodes，后 kg_edges（外键依赖顺序）。
  只恢复结构，不恢复数据——删除前的 19,331 个节点与 74,088 条关系
  不在迁移可恢复范围内，只能依赖迁移前的全库备份
  （backups/kp_legacy_6f6fa7f_20261110_145115.dump）。
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "a3d9f1c7e5b2"
down_revision: str | Sequence[str] | None = "f4b8c2d9e6a1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_table("kg_edges")
    op.drop_table("kg_nodes")


def downgrade() -> None:
    # ── kg_nodes：f4b8c2d9e6a1 时点结构 ──────────────────────────────
    op.create_table(
        "kg_nodes",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("node_type", sa.String(16), nullable=False, server_default="resource"),
        # 465081816093：资源节点与实体节点共存 → 这两列由 NOT NULL 改为可空
        sa.Column("resource_type", sa.String(16), nullable=True),
        sa.Column("resource_id", postgresql.UUID(as_uuid=True), nullable=True),
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
        # 465081816093：知识库隔离 + 确认状态
        sa.Column("kb_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("entity_id", postgresql.UUID(as_uuid=True), nullable=True),
        # status 的临时 server_default 已在 465081816093 撤销，此处保持无默认值
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column("deleted_at", sa.DateTime(), nullable=True),
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
    op.create_index("ix_kg_nodes_kb_id", "kg_nodes", ["kb_id"])
    op.create_index("ix_kg_nodes_status", "kg_nodes", ["status"])
    op.create_index("ix_kg_nodes_kb_status", "kg_nodes", ["kb_id", "status"])
    # 465081816093：整体唯一约束 → 两条部分唯一索引
    op.create_index(
        "uq_kg_nodes_resource",
        "kg_nodes",
        ["resource_type", "resource_id"],
        unique=True,
        postgresql_where=sa.text("resource_type IS NOT NULL"),
    )
    op.create_index(
        "uq_kg_nodes_entity",
        "kg_nodes",
        ["entity_id"],
        unique=True,
        postgresql_where=sa.text("entity_id IS NOT NULL"),
    )
    op.create_foreign_key(
        "fk_kg_nodes_kb",
        "kg_nodes",
        "knowledge_bases",
        ["kb_id"],
        ["id"],
        ondelete="CASCADE",
    )

    # ── kg_edges：f4b8c2d9e6a1 时点结构 ──────────────────────────────
    op.create_table(
        "kg_edges",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "source_node_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
        ),
        sa.Column(
            "target_node_id",
            postgresql.UUID(as_uuid=True),
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
        # 465081816093：知识库隔离 + 确认状态
        sa.Column("kb_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.UniqueConstraint(
            "source_node_id",
            "target_node_id",
            "relation_type",
            name="uq_kg_edges_source_target_relation",
        ),
        # 原始迁移使用无显式名的 ForeignKey，PG 按「表_列_fkey」生成名称，
        # 此处显式命名以与库内实际约束名一致
        sa.ForeignKeyConstraint(
            ["source_node_id"],
            ["kg_nodes.id"],
            name="kg_edges_source_node_id_fkey",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["target_node_id"],
            ["kg_nodes.id"],
            name="kg_edges_target_node_id_fkey",
            ondelete="CASCADE",
        ),
    )
    op.create_index("ix_kg_edges_source_node_id", "kg_edges", ["source_node_id"])
    op.create_index("ix_kg_edges_target_node_id", "kg_edges", ["target_node_id"])
    op.create_index("ix_kg_edges_relation_type", "kg_edges", ["relation_type"])
    op.create_index(
        "ix_kg_edges_relation_source", "kg_edges", ["relation_type", "source_node_id"]
    )
    op.create_index("ix_kg_edges_kb_id", "kg_edges", ["kb_id"])
    op.create_index("ix_kg_edges_status", "kg_edges", ["status"])
    op.create_index("ix_kg_edges_kb_status", "kg_edges", ["kb_id", "status"])
    op.create_foreign_key(
        "fk_kg_edges_kb",
        "kg_edges",
        "knowledge_bases",
        ["kb_id"],
        ["id"],
        ondelete="CASCADE",
    )
