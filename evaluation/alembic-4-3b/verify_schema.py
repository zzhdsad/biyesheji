"""4-3B-1 schema verification for the disposable isolated PostgreSQL database.

Read-only checks against the disposable DB reached via 127.0.0.1:55433.
Requires PW env var (password is not stored in this file).
"""

import asyncio
import json
import os
import sys
from pathlib import Path

import asyncpg

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "backend"))
from src.domain.models import Base  # noqa: E402

HEAD = "465081816093"
OUT = Path(__file__).with_name("verify.json")

failures: list[str] = []
warnings: list[str] = []


def chk(cond: bool, msg: str) -> None:
    if not cond:
        failures.append(msg)


async def main() -> None:
    conn = await asyncpg.connect(
        host="127.0.0.1",
        port=55433,
        user="ci_user",
        password=os.environ["PW"],
        database="kp_ci_4_3b",
        timeout=10,
    )
    try:
        version = await conn.fetchval("select version_num from alembic_version")
        version_rows = await conn.fetchval("select count(*) from alembic_version")
        tables = {
            r["tablename"]
            for r in await conn.fetch(
                "select tablename from pg_tables where schemaname='public'"
            )
        }
        columns = {
            (r["table_name"], r["column_name"]): dict(r)
            for r in await conn.fetch(
                """select table_name, column_name, data_type, is_nullable, column_default
                   from information_schema.columns where table_schema='public'"""
            )
        }
        indexes = {
            r["indexname"]: r["indexdef"]
            for r in await conn.fetch(
                "select indexname, indexdef from pg_indexes where schemaname='public'"
            )
        }
        constraints = {}
        for r in await conn.fetch(
            """select conrelid::regclass::text table_name, conname, contype,
                      pg_get_constraintdef(oid) definition
               from pg_constraint where connamespace='public'::regnamespace"""
        ):
            row = dict(r)
            # asyncpg maps PostgreSQL internal "char" to bytes
            if isinstance(row["contype"], (bytes, bytearray)):
                row["contype"] = row["contype"].decode()
            if isinstance(row["table_name"], (bytes, bytearray)):
                row["table_name"] = row["table_name"].decode()
            constraints[row["conname"]] = row
    finally:
        await conn.close()

    orm = Base.metadata.tables

    chk(version == HEAD, f"alembic_version={version}, expected {HEAD}")
    chk(version_rows == 1, f"alembic_version row_count={version_rows}")
    missing_tables = sorted(set(orm) - tables)
    extra_tables = sorted(tables - set(orm) - {"alembic_version"})
    chk(not missing_tables, f"missing tables: {missing_tables}")
    chk(not extra_tables, f"unexpected tables: {extra_tables}")

    pk_tables = {
        c["table_name"] for c in constraints.values() if c["contype"] == "p"
    }
    for table, meta in orm.items():
        chk(table in pk_tables, f"{table}: missing primary key")
        want = {col.name for col in meta.columns}
        have = {name for (tbl, name) in columns if tbl == table}
        chk(not (want - have), f"{table}: missing columns {sorted(want - have)}")
        chk(not (have - want), f"{table}: unexpected columns {sorted(have - want)}")

    nullability_drift = []
    for table, meta in orm.items():
        for col in meta.columns:
            row = columns.get((table, col.name))
            if row and (row["is_nullable"] == "YES") != col.nullable:
                nullability_drift.append(
                    {
                        "table": table,
                        "column": col.name,
                        "orm_nullable": col.nullable,
                        "db_nullable": row["is_nullable"] == "YES",
                        "db_type": row["data_type"],
                        "db_default": row["column_default"],
                    }
                )
    if nullability_drift:
        names = [f"{d['table']}.{d['column']}" for d in nullability_drift]
        warnings.append(
            f"ORM/DB nullability drift in {len(nullability_drift)} columns: "
            + ", ".join(names)
        )

    for col in ("created_at", "updated_at"):
        row = columns.get(("import_jobs", col))
        if row and row["column_default"] is None:
            warnings.append(
                f"import_jobs.{col} has no DB default while ORM relies on server_default"
            )

    expected_columns = {
        ("users", "deleted_at"): ("timestamp with time zone", "YES", None),
        ("documents", "progress_percent"): ("integer", "NO", "0"),
        ("documents", "index_status"): ("character varying", "NO", None),
        ("documents", "vector_count"): ("integer", "NO", None),
        ("documents", "indexed_at"): ("timestamp with time zone", "YES", None),
        ("documents", "index_error"): ("text", "NO", None),
        ("documents", "vector_model"): ("character varying", "YES", None),
        ("documents", "source_dataset"): ("character varying", "YES", None),
        ("documents", "import_batch_id"): ("character varying", "YES", None),
        ("evaluation_results", "faithfulness"): ("double precision", "NO", "0"),
        ("evaluation_results", "answer_correctness"): ("double precision", "YES", None),
        ("evaluation_results", "run_id"): ("character varying", "YES", None),
        ("evaluation_results", "gate_decision"): ("character varying", "YES", None),
        ("evaluation_results", "reflection_reason"): ("character varying", "YES", None),
        ("import_jobs", "import_mode"): ("character varying", "NO", None),
        ("kg_nodes", "kb_id"): ("uuid", "YES", None),
        ("kg_nodes", "resource_type"): ("character varying", "YES", None),
        ("kg_nodes", "resource_id"): ("uuid", "YES", None),
        ("kg_nodes", "entity_id"): ("uuid", "YES", None),
        ("kg_nodes", "status"): ("character varying", "NO", None),
        ("kg_nodes", "confidence"): ("double precision", "YES", None),
        ("kg_nodes", "deleted_at"): ("timestamp without time zone", "YES", None),
        ("kg_edges", "kb_id"): ("uuid", "YES", None),
        ("kg_edges", "status"): ("character varying", "NO", None),
        ("kg_edges", "confidence"): ("double precision", "YES", None),
        ("evaluation_runs", "failed_count"): ("integer", "NO", "0"),
        ("evaluation_runs", "idempotency_key"): ("character varying", "YES", None),
        ("feedbacks", "user_id"): ("uuid", "YES", None),
        ("test_cases", "needs_review"): ("boolean", "NO", None),
        ("generic_entities", "record_key"): ("character varying", "YES", None),
        ("generic_relations", "confidence"): ("double precision", "NO", None),
        ("import_records", "kb_id"): ("uuid", "YES", None),
        ("import_files", "job_id"): ("uuid", "NO", None),
        ("revectorize_jobs", "failed_doc_ids"): ("jsonb", "NO", None),
        ("generic_entities", "payload"): ("jsonb", "NO", None),
        ("generic_relations", "payload"): ("jsonb", "NO", None),
        ("import_mapping_templates", "config"): ("jsonb", "NO", None),
    }
    for (table, column), (data_type, nullable, default) in expected_columns.items():
        row = columns.get((table, column))
        chk(row is not None, f"{table}.{column}: missing")
        if row is None:
            continue
        chk(
            row["data_type"] == data_type,
            f"{table}.{column}: data_type={row['data_type']} expected {data_type}",
        )
        chk(
            row["is_nullable"] == nullable,
            f"{table}.{column}: nullable={row['is_nullable']} expected {nullable}",
        )
        if default is not None:
            chk(
                str(row["column_default"]).startswith(default),
                f"{table}.{column}: default={row['column_default']} expected {default}",
            )

    for table in ("herbs", "prescriptions", "theories", "literatures", "categories", "tags"):
        row = columns.get((table, "deleted_at"))
        chk(
            row is not None
            and row["data_type"] == "timestamp with time zone"
            and row["is_nullable"] == "YES",
            f"{table}.deleted_at soft-delete column missing/incorrect",
        )

    index_checks = {
        "uq_generic_entities_kb_type_record_key": ("CREATE UNIQUE INDEX", "record_key IS NOT NULL"),
        "uq_import_records_job_record_key": ("CREATE UNIQUE INDEX", None),
        "uq_import_records_kb_record_key": ("CREATE UNIQUE INDEX", "kb_id IS NOT NULL"),
        "uq_kg_nodes_resource": ("CREATE UNIQUE INDEX", "resource_type IS NOT NULL"),
        "uq_kg_nodes_entity": ("CREATE UNIQUE INDEX", "entity_id IS NOT NULL"),
        "ix_kg_edges_kb_status": ("CREATE INDEX", "(kb_id, status)"),
        "ix_kg_nodes_kb_status": ("CREATE INDEX", "(kb_id, status)"),
        "ix_documents_vector_model": ("CREATE INDEX", "(vector_model)"),
        "ix_documents_index_status": ("CREATE INDEX", "(index_status)"),
        "ix_import_jobs_import_mode": ("CREATE INDEX", "(import_mode)"),
        "ix_evaluation_runs_idempotency_key": ("CREATE UNIQUE INDEX", "(idempotency_key)"),
        "ix_feedbacks_user_id": ("CREATE INDEX", "(user_id)"),
        "ix_knowledge_base_resources_resource_lookup": (
            "CREATE INDEX",
            "(resource_type, resource_id)",
        ),
        "uq_categories_type_parent_name": ("CREATE UNIQUE INDEX", "COALESCE"),
    }
    for name, (prefix, fragment) in index_checks.items():
        definition = indexes.get(name)
        chk(definition is not None, f"index {name}: missing")
        if definition is None:
            continue
        chk(definition.startswith(prefix), f"index {name}: expected {prefix}")
        if fragment:
            haystack = definition.upper() if fragment == "COALESCE" else definition
            chk(fragment in haystack, f"index {name}: missing fragment {fragment}")

    constraint_checks = {
        "uq_import_files_job_path": "UNIQUE",
        "uq_generic_relations_kb_src_dst_type": "UNIQUE",
        "uq_import_mapping_name_fp": "UNIQUE",
        "uq_kbr_kb_type_resource": "UNIQUE",
        "uq_feedbacks_user_message": "UNIQUE",
        "uq_kg_edges_source_target_relation": "UNIQUE",
        "uq_prescription_ingredients_prescription_herb": "UNIQUE",
    }
    for name, fragment in constraint_checks.items():
        row = constraints.get(name)
        chk(row is not None, f"constraint {name}: missing")
        if row:
            chk(
                fragment in row["definition"],
                f"constraint {name}: unexpected definition {row['definition']}",
            )
    for name in ("fk_kg_nodes_kb", "fk_kg_edges_kb", "fk_feedbacks_user_id_users"):
        row = constraints.get(name)
        chk(row is not None, f"FK {name}: missing")
        if row:
            chk(
                row["contype"] == "f" and "ON DELETE CASCADE" in row["definition"],
                f"FK {name}: wrong definition {row['definition']}",
            )

    fk_count = sum(1 for c in constraints.values() if c["contype"] == "f")
    orm_indexes = {idx.name for meta in orm.values() for idx in meta.indexes}
    missing_orm_indexes = sorted(name for name in orm_indexes if name not in indexes)
    if missing_orm_indexes:
        warnings.append(f"ORM indexes absent in DB: {missing_orm_indexes}")

    result = {
        "alembic_version": version,
        "business_table_count": len(tables) - 1,
        "tables": sorted(tables),
        "column_count": len(columns),
        "index_count": len(indexes),
        "constraint_count": len(constraints),
        "fk_count": fk_count,
        "failures": failures,
        "warnings": warnings,
        "nullability_drift": nullability_drift,
    }
    OUT.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

    print(
        "version:", version,
        "business_tables:", len(tables) - 1,
        "columns:", len(columns),
        "indexes:", len(indexes),
        "constraints:", len(constraints),
        "fks:", fk_count,
    )
    print("failures:", len(failures))
    for item in failures:
        print(" FAIL:", item)
    print("warnings:", len(warnings))
    for item in warnings:
        print(" WARN:", item)


asyncio.run(main())
sys.exit(1 if failures else 0)
