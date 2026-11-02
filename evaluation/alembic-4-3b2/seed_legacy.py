"""Seed synthetic legacy rows at old head (465081816093) on a disposable DB.

Rows mimic the real drift: app always writes status, but timestamps/counters/
optional text columns may be NULL. No NULL-status row is seeded here; that case
is exercised separately in kp_ci_4_3b2_abort.
"""

import asyncio
import os
import sys

import asyncpg

HOST = "127.0.0.1"
PORT = 55434
USER = "ci_user"

SQL = """
INSERT INTO import_jobs
    (id, batch_id, dataset_id, dataset_name, source_file, target_type, import_mode,
     status, total, processed, succeeded, failed, skipped, failed_items,
     error_message, created_by, started_at, finished_at, created_at, updated_at)
VALUES
    (gen_random_uuid(), 'legacy-a', 'ds-a', NULL, NULL, 'herb', 'normal',
     'pending', NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL),
    (gen_random_uuid(), 'legacy-b', 'ds-b', NULL, NULL, 'herb', 'normal',
     'processing', NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL,
     TIMESTAMPTZ '2026-01-02 03:04:05+00', NULL, NULL, NULL),
    (gen_random_uuid(), 'legacy-c', 'ds-c', NULL, NULL, 'herb', 'normal',
     'completed', NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL,
     NULL, TIMESTAMPTZ '2026-02-03 04:05:06+00', NULL, NULL);
"""


async def main() -> None:
    conn = await asyncpg.connect(
        host=HOST, port=PORT, user=USER, password=os.environ["PW"],
        database=sys.argv[1], timeout=10,
    )
    try:
        version = await conn.fetchval("select version_num from alembic_version")
        print("version before seed:", version)
        if version != "465081816093":
            raise SystemExit(f"expected old head 465081816093, got {version}")
        await conn.execute(SQL)
        count = await conn.fetchval("select count(*) from import_jobs")
        nulls = await conn.fetchrow(
            """select
                 count(*) filter (where status is null) as null_status,
                 count(*) filter (where created_at is null) as null_created,
                 count(*) filter (where updated_at is null) as null_updated
               from import_jobs"""
        )
        print("seeded rows:", count, "pre-upgrade nulls:", dict(nulls))
    finally:
        await conn.close()


asyncio.run(main())
