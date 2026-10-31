"""generic import: files/records/entities/relations/templates + index status + KG isolation

Revision ID: 465081816093
Revises: e6a3b8c1d2f4

覆盖内容（对应任务决策）：
- 决策 2：新增 import_files / import_records / generic_entities / generic_relations /
  import_mapping_templates 五张表；**复用既有 import_jobs** 作任务机，不另建 sessions 表。
- 决策 3：documents 增加 index_status / vector_count / indexed_at / index_error，
  把「切片是否已就绪」(parse_status) 与「向量是否真的进了 Milvus」拆成两个可独立校验的状态。
- 决策 4/7：generic_relations 必带 kb_id（唯一约束 + 状态索引），杜绝跨知识库关联与权限泄漏；
  关系与节点带 status / confidence，未确认（pending）的不进入 KG 检索与推理。
- kg_nodes 的原整体唯一约束改为两条**部分索引**，使资源节点与通用实体节点可共存。

⚠️ 本文件是**人工整理后的版本**，不是直接采用 autogenerate 的输出：
1. 自动版本混入了与本次无关的 server_default / NOT NULL 漂移操作（涉及
   documents.progress_percent、evaluation_*、import_jobs 多个历史列），一律剔除，
   避免误改既有表语义；
2. 自动版本给已有数据的表（documents 3.5 万行、import_jobs 57 行）加 NOT NULL 列时
   未给默认值，在主库上执行会直接失败，这里改为「先带 server_default 再加列、随后撤销默认值」；
3. 自动版本里 create_foreign_key 名为 None（无法渲染），这里补上显式约束名；
4. 自动版本还包含一条把 users.deleted_at 改回 naive 的操作，会抵消 e6a3b8c1d2f4，已剔除
   （改为同步修正 User 模型，使其与线上 timestamptz 一致）。
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "465081816093"
down_revision: Union[str, Sequence[str], None] = "e6a3b8c1d2f4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ── 1. 通用导入：文件清单（决策 1/2）────────────────────────────────────
    op.create_table(
        "import_files",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("job_id", sa.UUID(), nullable=False),
        sa.Column("relative_path", sa.String(length=512), nullable=False),
        sa.Column("file_name", sa.String(length=255), nullable=False),
        sa.Column("file_ext", sa.String(length=16), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("file_hash", sa.String(length=64), nullable=False),
        sa.Column("encoding", sa.String(length=32), nullable=False),
        sa.Column("detected_format", sa.String(length=16), nullable=False),
        sa.Column("row_count", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("error_message", sa.Text(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.ForeignKeyConstraint(["job_id"], ["import_jobs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("job_id", "relative_path", name="uq_import_files_job_path"),
    )
    op.create_index("ix_import_files_detected_format", "import_files", ["detected_format"], unique=False)
    op.create_index("ix_import_files_file_hash", "import_files", ["file_hash"], unique=False)
    op.create_index("ix_import_files_job_id", "import_files", ["job_id"], unique=False)
    op.create_index("ix_import_files_status", "import_files", ["status"], unique=False)

    # ── 2. 通用实体（任意 Schema，不强制映射四类业务表）─────────────────────
    op.create_table(
        "generic_entities",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("kb_id", sa.UUID(), nullable=False),
        sa.Column("entity_type", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("source_file_id", sa.UUID(), nullable=True),
        sa.Column("source_row_no", sa.Integer(), nullable=True),
        sa.Column("record_key", sa.String(length=64), nullable=True),
        sa.Column("source_pk", sa.String(length=255), nullable=False),
        sa.Column("import_batch_id", sa.String(length=64), nullable=True),
        sa.Column("created_by", sa.UUID(), nullable=True),
        sa.Column("deleted_at", sa.DateTime(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.ForeignKeyConstraint(["kb_id"], ["knowledge_bases.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["source_file_id"], ["import_files.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_generic_entities_entity_type", "generic_entities", ["entity_type"], unique=False)
    op.create_index(
        "ix_generic_entities_import_batch_id", "generic_entities", ["import_batch_id"], unique=False
    )
    op.create_index("ix_generic_entities_kb_id", "generic_entities", ["kb_id"], unique=False)
    op.create_index(
        "ix_generic_entities_kb_pk", "generic_entities", ["kb_id", "entity_type", "source_pk"], unique=False
    )
    op.create_index(
        "ix_generic_entities_kb_type", "generic_entities", ["kb_id", "entity_type"], unique=False
    )
    op.create_index("ix_generic_entities_name", "generic_entities", ["name"], unique=False)
    op.create_index("ix_generic_entities_record_key", "generic_entities", ["record_key"], unique=False)
    op.create_index(
        "ix_generic_entities_source_file_id", "generic_entities", ["source_file_id"], unique=False
    )
    op.create_index("ix_generic_entities_source_pk", "generic_entities", ["source_pk"], unique=False)
    # 幂等：同一知识库内 (类型, 源记录) 唯一
    op.create_index(
        "uq_generic_entities_kb_type_record_key",
        "generic_entities",
        ["kb_id", "entity_type", "record_key"],
        unique=True,
        postgresql_where=sa.text("record_key IS NOT NULL"),
    )

    # ── 3. 原始结构化记录（保留完整 Schema，不退化成文本块）─────────────────
    op.create_table(
        "import_records",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("job_id", sa.UUID(), nullable=False),
        sa.Column("file_id", sa.UUID(), nullable=True),
        sa.Column("kb_id", sa.UUID(), nullable=True),
        sa.Column("row_no", sa.Integer(), nullable=False),
        sa.Column("record_key", sa.String(length=64), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("entity_type", sa.String(length=64), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.ForeignKeyConstraint(["file_id"], ["import_files.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["job_id"], ["import_jobs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["kb_id"], ["knowledge_bases.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_import_records_entity_type", "import_records", ["entity_type"], unique=False)
    op.create_index("ix_import_records_file_id", "import_records", ["file_id"], unique=False)
    op.create_index("ix_import_records_job_id", "import_records", ["job_id"], unique=False)
    op.create_index("ix_import_records_kb_entity", "import_records", ["kb_id", "entity_type"], unique=False)
    op.create_index("ix_import_records_kb_id", "import_records", ["kb_id"], unique=False)
    op.create_index("ix_import_records_record_key", "import_records", ["record_key"], unique=False)
    op.create_index("uq_import_records_job_record_key", "import_records", ["job_id", "record_key"], unique=True)
    op.create_index(
        "uq_import_records_kb_record_key",
        "import_records",
        ["kb_id", "record_key"],
        unique=True,
        postgresql_where=sa.text("kb_id IS NOT NULL"),
    )

    # ── 4. 通用关系（kb_id 强制隔离 + 状态 + 置信度）────────────────────────
    op.create_table(
        "generic_relations",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("kb_id", sa.UUID(), nullable=False),
        sa.Column("source_entity_id", sa.UUID(), nullable=False),
        sa.Column("target_entity_id", sa.UUID(), nullable=False),
        sa.Column("relation_type", sa.String(length=64), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("provenance", sa.String(length=255), nullable=False),
        sa.Column("import_batch_id", sa.String(length=64), nullable=True),
        sa.Column("created_by", sa.UUID(), nullable=True),
        sa.Column("deleted_at", sa.DateTime(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.ForeignKeyConstraint(["kb_id"], ["knowledge_bases.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["source_entity_id"], ["generic_entities.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["target_entity_id"], ["generic_entities.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "kb_id",
            "source_entity_id",
            "target_entity_id",
            "relation_type",
            name="uq_generic_relations_kb_src_dst_type",
        ),
    )
    op.create_index(
        "ix_generic_relations_import_batch_id", "generic_relations", ["import_batch_id"], unique=False
    )
    op.create_index("ix_generic_relations_kb_id", "generic_relations", ["kb_id"], unique=False)
    op.create_index("ix_generic_relations_kb_status", "generic_relations", ["kb_id", "status"], unique=False)
    op.create_index("ix_generic_relations_kb_type", "generic_relations", ["kb_id", "relation_type"], unique=False)
    op.create_index("ix_generic_relations_relation_type", "generic_relations", ["relation_type"], unique=False)
    op.create_index(
        "ix_generic_relations_source_entity_id", "generic_relations", ["source_entity_id"], unique=False
    )
    op.create_index("ix_generic_relations_status", "generic_relations", ["status"], unique=False)
    op.create_index(
        "ix_generic_relations_target_entity_id", "generic_relations", ["target_entity_id"], unique=False
    )

    # ── 5. 映射模板（相同 Schema 复用）──────────────────────────────────────
    op.create_table(
        "import_mapping_templates",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("kb_id", sa.UUID(), nullable=True),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("schema_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("config", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_by", sa.UUID(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.ForeignKeyConstraint(["kb_id"], ["knowledge_bases.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name", "schema_fingerprint", name="uq_import_mapping_name_fp"),
    )
    op.create_index("ix_import_mapping_templates_kb_id", "import_mapping_templates", ["kb_id"], unique=False)
    op.create_index("ix_import_mapping_templates_name", "import_mapping_templates", ["name"], unique=False)
    op.create_index(
        "ix_import_mapping_templates_schema_fingerprint",
        "import_mapping_templates",
        ["schema_fingerprint"],
        unique=False,
    )

    # ── 6. documents：向量索引状态（决策 3）─────────────────────────────────
    # 先带默认值加列，保证已有行可回填；随后撤销默认值，让后续写入必须由代码显式提供。
    op.add_column(
        "documents",
        sa.Column("index_status", sa.String(length=16), nullable=False, server_default="not_indexed"),
    )
    op.add_column(
        "documents", sa.Column("vector_count", sa.Integer(), nullable=False, server_default="0")
    )
    op.add_column("documents", sa.Column("indexed_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column(
        "documents", sa.Column("index_error", sa.Text(), nullable=False, server_default="")
    )
    op.create_index("ix_documents_index_status", "documents", ["index_status"], unique=False)

    # ── 7. import_jobs：导入模式 ────────────────────────────────────────────
    op.add_column(
        "import_jobs",
        sa.Column("import_mode", sa.String(length=16), nullable=False, server_default="normal"),
    )
    op.create_index("ix_import_jobs_import_mode", "import_jobs", ["import_mode"], unique=False)

    # ── 8. kg_edges：知识库隔离 + 确认状态 ──────────────────────────────────
    op.add_column("kg_edges", sa.Column("kb_id", sa.UUID(), nullable=True))
    op.add_column(
        "kg_edges", sa.Column("status", sa.String(length=16), nullable=False, server_default="confirmed")
    )
    op.add_column("kg_edges", sa.Column("confidence", sa.Float(), nullable=True))
    op.create_index("ix_kg_edges_kb_id", "kg_edges", ["kb_id"], unique=False)
    op.create_index("ix_kg_edges_status", "kg_edges", ["status"], unique=False)
    op.create_index("ix_kg_edges_kb_status", "kg_edges", ["kb_id", "status"], unique=False)
    op.create_foreign_key(
        "fk_kg_edges_kb", "kg_edges", "knowledge_bases", ["kb_id"], ["id"], ondelete="CASCADE"
    )

    # ── 9. kg_nodes：知识库隔离 + 实体节点 + 确认状态 ────────────────────────
    op.add_column("kg_nodes", sa.Column("kb_id", sa.UUID(), nullable=True))
    op.add_column("kg_nodes", sa.Column("entity_id", sa.UUID(), nullable=True))
    op.add_column(
        "kg_nodes", sa.Column("status", sa.String(length=16), nullable=False, server_default="confirmed")
    )
    op.add_column("kg_nodes", sa.Column("confidence", sa.Float(), nullable=True))
    op.add_column("kg_nodes", sa.Column("deleted_at", sa.DateTime(), nullable=True))
    # 资源节点与实体节点共存：整体唯一约束 → 两条部分唯一索引
    op.drop_constraint("uq_kg_nodes_resource", "kg_nodes", type_="unique")
    op.alter_column(
        "kg_nodes", "resource_type", existing_type=sa.VARCHAR(length=16), nullable=True
    )
    op.alter_column("kg_nodes", "resource_id", existing_type=sa.UUID(), nullable=True)
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
    op.create_index("ix_kg_nodes_kb_id", "kg_nodes", ["kb_id"], unique=False)
    op.create_index("ix_kg_nodes_status", "kg_nodes", ["status"], unique=False)
    op.create_index("ix_kg_nodes_kb_status", "kg_nodes", ["kb_id", "status"], unique=False)
    op.create_foreign_key(
        "fk_kg_nodes_kb", "kg_nodes", "knowledge_bases", ["kb_id"], ["id"], ondelete="CASCADE"
    )

    # ── 10. 撤销临时默认值（保持模型语义：由代码显式写入）───────────────────
    op.alter_column("documents", "index_status", existing_type=sa.VARCHAR(length=16), server_default=None)
    op.alter_column("documents", "vector_count", existing_type=sa.INTEGER(), server_default=None)
    op.alter_column("documents", "index_error", existing_type=sa.TEXT(), server_default=None)
    op.alter_column("import_jobs", "import_mode", existing_type=sa.VARCHAR(length=16), server_default=None)
    op.alter_column("kg_edges", "status", existing_type=sa.VARCHAR(length=16), server_default=None)
    op.alter_column("kg_nodes", "status", existing_type=sa.VARCHAR(length=16), server_default=None)


def downgrade() -> None:
    op.drop_constraint("fk_kg_nodes_kb", "kg_nodes", type_="foreignkey")
    op.drop_index("ix_kg_nodes_kb_status", table_name="kg_nodes")
    op.drop_index("ix_kg_nodes_status", table_name="kg_nodes")
    op.drop_index("ix_kg_nodes_kb_id", table_name="kg_nodes")
    op.drop_index(
        "uq_kg_nodes_entity", table_name="kg_nodes", postgresql_where=sa.text("entity_id IS NOT NULL")
    )
    op.drop_index(
        "uq_kg_nodes_resource",
        table_name="kg_nodes",
        postgresql_where=sa.text("resource_type IS NOT NULL"),
    )
    op.alter_column("kg_nodes", "resource_id", existing_type=sa.UUID(), nullable=False)
    op.alter_column("kg_nodes", "resource_type", existing_type=sa.VARCHAR(length=16), nullable=False)
    op.create_unique_constraint("uq_kg_nodes_resource", "kg_nodes", ["resource_type", "resource_id"])
    op.drop_column("kg_nodes", "deleted_at")
    op.drop_column("kg_nodes", "confidence")
    op.drop_column("kg_nodes", "status")
    op.drop_column("kg_nodes", "entity_id")
    op.drop_column("kg_nodes", "kb_id")

    op.drop_constraint("fk_kg_edges_kb", "kg_edges", type_="foreignkey")
    op.drop_index("ix_kg_edges_kb_status", table_name="kg_edges")
    op.drop_index("ix_kg_edges_status", table_name="kg_edges")
    op.drop_index("ix_kg_edges_kb_id", table_name="kg_edges")
    op.drop_column("kg_edges", "confidence")
    op.drop_column("kg_edges", "status")
    op.drop_column("kg_edges", "kb_id")

    op.drop_index("ix_import_jobs_import_mode", table_name="import_jobs")
    op.drop_column("import_jobs", "import_mode")

    op.drop_index("ix_documents_index_status", table_name="documents")
    op.drop_column("documents", "index_error")
    op.drop_column("documents", "indexed_at")
    op.drop_column("documents", "vector_count")
    op.drop_column("documents", "index_status")

    op.drop_index("ix_import_mapping_templates_schema_fingerprint", table_name="import_mapping_templates")
    op.drop_index("ix_import_mapping_templates_name", table_name="import_mapping_templates")
    op.drop_index("ix_import_mapping_templates_kb_id", table_name="import_mapping_templates")
    op.drop_table("import_mapping_templates")

    op.drop_index(
        "ix_generic_relations_target_entity_id", table_name="generic_relations"
    )
    op.drop_index("ix_generic_relations_status", table_name="generic_relations")
    op.drop_index("ix_generic_relations_source_entity_id", table_name="generic_relations")
    op.drop_index("ix_generic_relations_relation_type", table_name="generic_relations")
    op.drop_index("ix_generic_relations_kb_type", table_name="generic_relations")
    op.drop_index("ix_generic_relations_kb_status", table_name="generic_relations")
    op.drop_index("ix_generic_relations_kb_id", table_name="generic_relations")
    op.drop_index("ix_generic_relations_import_batch_id", table_name="generic_relations")
    op.drop_table("generic_relations")

    op.drop_index(
        "uq_import_records_kb_record_key",
        table_name="import_records",
        postgresql_where=sa.text("kb_id IS NOT NULL"),
    )
    op.drop_index("uq_import_records_job_record_key", table_name="import_records")
    op.drop_index("ix_import_records_record_key", table_name="import_records")
    op.drop_index("ix_import_records_kb_id", table_name="import_records")
    op.drop_index("ix_import_records_kb_entity", table_name="import_records")
    op.drop_index("ix_import_records_job_id", table_name="import_records")
    op.drop_index("ix_import_records_file_id", table_name="import_records")
    op.drop_index("ix_import_records_entity_type", table_name="import_records")
    op.drop_table("import_records")

    op.drop_index(
        "uq_generic_entities_kb_type_record_key",
        table_name="generic_entities",
        postgresql_where=sa.text("record_key IS NOT NULL"),
    )
    op.drop_index("ix_generic_entities_source_pk", table_name="generic_entities")
    op.drop_index("ix_generic_entities_source_file_id", table_name="generic_entities")
    op.drop_index("ix_generic_entities_record_key", table_name="generic_entities")
    op.drop_index("ix_generic_entities_name", table_name="generic_entities")
    op.drop_index("ix_generic_entities_kb_type", table_name="generic_entities")
    op.drop_index("ix_generic_entities_kb_pk", table_name="generic_entities")
    op.drop_index("ix_generic_entities_kb_id", table_name="generic_entities")
    op.drop_index("ix_generic_entities_import_batch_id", table_name="generic_entities")
    op.drop_index("ix_generic_entities_entity_type", table_name="generic_entities")
    op.drop_table("generic_entities")

    op.drop_index("ix_import_files_status", table_name="import_files")
    op.drop_index("ix_import_files_job_id", table_name="import_files")
    op.drop_index("ix_import_files_file_hash", table_name="import_files")
    op.drop_index("ix_import_files_detected_format", table_name="import_files")
    op.drop_table("import_files")
