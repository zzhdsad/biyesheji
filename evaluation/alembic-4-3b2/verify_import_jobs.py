"""4-3B-2 verification: import_jobs schema/data after upgrade on a disposable DB.

Usage:
  PW=<disposable-db-password> python verify_import_jobs.py <database> <empty|legacy>

Checks (read-only except the ORM insert probe in legacy mode):
- alembic_version is the new head
- full ORM/DB table set and nullability consistency
- the 12 previously drifted import_jobs columns are NOT NULL
- created_at / updated_at have DB default now()
- legacy mode: backfilled values follow the documented conservative strategy
- legacy mode: a minimal ORM insert gets non-NULL timestamps from the DB
"""

import asyncio
import json
import os
import sys
from pathlib import Path


def _jsonb(value: object) -> object:
    """asyncpg returns jsonb as text unless a codec is registered."""
    return json.loads(value) if isinstance(value, str) else value

import asyncpg

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "backend"))
from src.domain.models import Base, ImportJob  # noqa: E402

HEAD = "7d1e9c4b2a60"
HOST = "127.0.0.1"
PORT = 55434
USER = "ci_user"

DRIFTED_COLUMNS = (
    "dataset_name",
    "source_file",
    "status",
    "total",
    "processed",
    "succeeded",
    "failed",
    "skipped",
    "failed_items",
    "error_message",
    "created_at",
    "updated_at",
)

failures: list[str] = []


def chk(cond: bool, msg: str) -> None:
    if not cond:
        failures.append(msg)


async def check_schema(conn: asyncpg.Connection) -> dict:
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

    chk(version == HEAD, f"alembic_version={version}, expected {HEAD}")
    chk(version_rows == 1, f"alembic_version row_count={version_rows}")

    orm = Base.metadata.tables
    missing = sorted(set(orm) - tables)
    extra = sorted(tables - set(orm) - {"alembic_version"})
    chk(not missing, f"missing tables: {missing}")
    chk(not extra, f"unexpected tables: {extra}")

    drift = []
    for table, meta in orm.items():
        want = {c.name for c in meta.columns}
        have = {name for (tbl, name) in columns if tbl == table}
        chk(not (want - have), f"{table}: missing columns {sorted(want - have)}")
        chk(not (have - want), f"{table}: unexpected columns {sorted(have - want)}")
        for col in meta.columns:
            row = columns.get((table, col.name))
            if row and (row["is_nullable"] == "YES") != col.nullable:
                drift.append(
                    f"{table}.{col.name}: orm_nullable={col.nullable} "
                    f"db_nullable={row['is_nullable']}"
                )
    chk(not drift, "residual nullability drift: " + "; ".join(drift))

    for name in DRIFTED_COLUMNS:
        row = columns.get(("import_jobs", name))
        chk(row is not None, f"import_jobs.{name}: missing")
        if row is None:
            continue
        chk(
            row["is_nullable"] == "NO",
            f"import_jobs.{name}: is_nullable={row['is_nullable']} expected NO",
        )
        orm_col = ImportJob.__table__.columns[name]
        chk(
            orm_col.nullable is False,
            f"ORM import_jobs.{name}: nullable={orm_col.nullable} expected False",
        )

    for name in ("created_at", "updated_at"):
        row = columns.get(("import_jobs", name))
        default = str(row["column_default"]) if row and row["column_default"] else None
        chk(
            default is not None and "now()" in default,
            f"import_jobs.{name}: default={default} expected now()",
        )
        orm_col = ImportJob.__table__.columns[name]
        chk(
            orm_col.server_default is not None
            and "now()" in str(orm_col.server_default.arg),
            f"ORM import_jobs.{name}: missing server_default now()",
        )

    return {
        "alembic_version": version,
        "table_count": len(tables) - 1,
        "column_count": len(columns),
        "drift": drift,
    }


async def check_legacy_data(conn: asyncpg.Connection) -> dict:
    rows = {
        r["batch_id"]: dict(r)
        for r in await conn.fetch(
            """select batch_id, dataset_name, source_file, status, total, processed,
                      succeeded, failed, skipped, failed_items, error_message,
                      started_at, finished_at, created_at, updated_at
               from import_jobs where batch_id like 'legacy-%'"""
        )
    }
    chk(set(rows) == {"legacy-a", "legacy-b", "legacy-c"}, f"legacy rows missing: {rows.keys()}")

    a = rows.get("legacy-a", {})
    chk(a.get("status") == "pending", "legacy-a status changed")
    chk(a.get("dataset_name") == "", f"legacy-a dataset_name={a.get('dataset_name')!r}")
    chk(a.get("source_file") == "", f"legacy-a source_file={a.get('source_file')!r}")
    for name in ("total", "processed", "succeeded", "failed", "skipped"):
        chk(a.get(name) == 0, f"legacy-a {name}={a.get(name)!r} expected 0")
    chk(
        list(_jsonb(a.get("failed_items")) or []) == [],
        f"legacy-a failed_items={a.get('failed_items')!r}",
    )
    chk(a.get("error_message") == "", f"legacy-a error_message={a.get('error_message')!r}")
    chk(a.get("started_at") is None and a.get("finished_at") is None, "legacy-a timestamps changed")
    chk(a.get("created_at") is not None, "legacy-a created_at is NULL")
    chk(a.get("updated_at") is not None, "legacy-a updated_at is NULL")
    chk(a.get("created_at") == a.get("updated_at"), "legacy-a fallback timestamps differ")

    b = rows.get("legacy-b", {})
    chk(b.get("status") == "processing", "legacy-b status changed")
    chk(b.get("created_at") == b.get("started_at"), "legacy-b created_at != started_at")
    chk(b.get("updated_at") == b.get("started_at"), "legacy-b updated_at != started_at")

    c = rows.get("legacy-c", {})
    chk(c.get("status") == "completed", "legacy-c status changed")
    chk(c.get("created_at") == c.get("finished_at"), "legacy-c created_at != finished_at")
    chk(c.get("updated_at") == c.get("finished_at"), "legacy-c updated_at != finished_at")

    for batch_id, row in rows.items():
        for name in DRIFTED_COLUMNS:
            chk(row.get(name) is not None, f"{batch_id}.{name} is NULL after upgrade")

    return {
        "rows": {
            k: {kk: str(vv) for kk, vv in v.items()} for k, v in rows.items()
        }
    }


async def check_orm_insert() -> dict:
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    url = os.environ["DATABASE_URL"]
    engine = create_async_engine(url, pool_pre_ping=True)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            job = ImportJob(
                batch_id="post-migration-orm",
                dataset_id="post-migration-dataset",
                target_type="herb",
                status="pending",
            )
            session.add(job)
            await session.commit()
            await session.refresh(job)
            result = {
                "job_id": str(job.id),
                "dataset_name": job.dataset_name,
                "source_file": job.source_file,
                "total": job.total,
                "processed": job.processed,
                "succeeded": job.succeeded,
                "failed": job.failed,
                "skipped": job.skipped,
                "failed_items": job.failed_items,
                "error_message": job.error_message,
                "created_at": str(job.created_at) if job.created_at else None,
                "updated_at": str(job.updated_at) if job.updated_at else None,
            }
    finally:
        await engine.dispose()

    chk(result["created_at"] is not None, "ORM insert: created_at is NULL")
    chk(result["updated_at"] is not None, "ORM insert: updated_at is NULL")
    chk(result["dataset_name"] == "", f"ORM insert: dataset_name={result['dataset_name']!r}")
    chk(result["source_file"] == "", f"ORM insert: source_file={result['source_file']!r}")
    chk(result["failed_items"] == [], f"ORM insert: failed_items={result['failed_items']!r}")
    for name in ("total", "processed", "succeeded", "failed", "skipped"):
        chk(result[name] == 0, f"ORM insert: {name}={result[name]!r}")
    return result


async def main() -> None:
    database = sys.argv[1]
    mode = sys.argv[2]
    conn = await asyncpg.connect(
        host=HOST, port=PORT, user=USER, password=os.environ["PW"],
        database=database, timeout=10,
    )
    try:
        result = {"database": database, "mode": mode}
        result.update(await check_schema(conn))
        if mode == "legacy":
            result.update(await check_legacy_data(conn))
    finally:
        await conn.close()

    if mode == "legacy":
        result["orm_insert"] = await check_orm_insert()

    out = Path(__file__).with_name(f"verify_{mode}.json")
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str), encoding="utf-8")

    print(f"database={database} mode={mode} version={result['alembic_version']}")
    print(f"tables={result['table_count']} columns={result['column_count']}")
    print(f"failures: {len(failures)}")
    for item in failures:
        print(" FAIL:", item)


asyncio.run(main())
sys.exit(1 if failures else 0)
