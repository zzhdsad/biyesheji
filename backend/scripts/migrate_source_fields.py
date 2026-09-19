"""来源可信度字段迁移脚本（幂等，可重复执行）。

作用：
1. PostgreSQL：documents 增加 source_type/era/credibility_level，
   chunks 增加 source_type/credibility_level（ADD COLUMN IF NOT EXISTS）。
   已存在的列不动，历史数据三字段为 NULL，由补标接口/脚本回填。
2. Milvus：document_chunks 新集合以动态字段方式携带 source_type /
   credibility_level，无需在 schema 中显式加列。
   - 若集合不存在：应用下次写入时自动以 enable_dynamic_field=True 创建，无需操作。
   - 若集合已存在且创建时未开启动态字段（老集合）：Milvus 2.4 不支持事后改 schema，
     需加 --rebuild-milvus 显式删除并重建集合（向量数据清空），
     重建后对 success/completed 文档执行重新向量化（可用 --reindex 一并完成）。

用法：
    python scripts/migrate_source_fields.py              # 仅迁移 PG
    python scripts/migrate_source_fields.py --rebuild-milvus   # 同时重建 Milvus 集合
    python scripts/migrate_source_fields.py --rebuild-milvus --reindex  # 重建并重向量化
"""

import argparse
import asyncio
import sys
from pathlib import Path

# 允许从 backend/ 目录直接运行
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import text  # noqa: E402
from sqlalchemy.ext.asyncio import create_async_engine  # noqa: E402

from src.core.config import settings  # noqa: E402

PG_COLUMNS: dict[str, list[str]] = {
    "documents": [
        "source_type VARCHAR(16)",
        "era VARCHAR(8)",
        "credibility_level SMALLINT",
    ],
    "chunks": [
        "source_type VARCHAR(16)",
        "credibility_level SMALLINT",
    ],
}


async def migrate_pg() -> None:
    engine = create_async_engine(settings.DATABASE_URL)
    try:
        async with engine.begin() as conn:
            for table, columns in PG_COLUMNS.items():
                for ddl in columns:
                    name = ddl.split()[0]
                    await conn.execute(
                        text(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {ddl}")
                    )
                    print(f"[PG] {table}.{name} 已就绪")
    finally:
        await engine.dispose()


def milvus_dynamic_enabled() -> bool | None:
    """集合是否开启动态字段；集合不存在返回 None，无法连接抛异常。"""
    from pymilvus import MilvusClient

    client = MilvusClient(uri=settings.MILVUS_URI)
    collection = settings.MILVUS_COLLECTION
    if not client.has_collection(collection):
        return None
    desc = client.describe_collection(collection)
    return bool(desc.get("enable_dynamic_field", False))


def rebuild_milvus() -> None:
    """删除旧集合并以动态字段重建（显式操作，不在业务请求路径自动执行）。"""
    from src.infrastructure.milvus_store import MilvusStore

    store = MilvusStore()
    client = store._get_client()  # noqa: SLF001
    client.drop_collection(settings.MILVUS_COLLECTION)
    print(f"[Milvus] 旧集合 {settings.MILVUS_COLLECTION} 已删除")
    store.ensure_collection()
    print(f"[Milvus] 新集合已创建（enable_dynamic_field=True）")


async def reindex_all_completed() -> None:
    """对所有 success/completed 文档重新向量化（重建集合后的回填）。"""
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from src.application.indexing_service import IndexingService
    from src.domain.models import Document
    from src.infrastructure.database import get_engine
    from sqlalchemy import select

    engine = get_engine()
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as db:
        docs = (
            await db.scalars(
                select(Document).where(
                    Document.deleted_at.is_(None),
                    Document.parse_status.in_(["success", "completed"]),
                )
            )
        ).all()
        ids = [d.id for d in docs]
    print(f"[Reindex] 待重新向量化文档 {len(ids)} 个")
    for doc_id in ids:
        async with factory() as db:
            try:
                await IndexingService(db).run(doc_id)
                print(f"[Reindex] 完成 {doc_id}")
            except Exception as exc:  # noqa: BLE001
                print(f"[Reindex] 失败 {doc_id}: {exc}")
    await engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description="来源可信度字段迁移")
    parser.add_argument(
        "--rebuild-milvus",
        action="store_true",
        help="删除并重建 Milvus 集合（清空向量，需随后重新向量化）",
    )
    parser.add_argument(
        "--reindex",
        action="store_true",
        help="重建集合后对 success/completed 文档重新向量化",
    )
    args = parser.parse_args()

    asyncio.run(migrate_pg())

    state = milvus_dynamic_enabled()
    if state is None:
        print("[Milvus] 集合不存在，将在首次写入时自动创建（已开启动态字段）")
    elif state is True:
        print("[Milvus] 集合已开启动态字段，source_type/credibility_level 直接写入，无需迁移")
    elif args.rebuild_milvus:
        rebuild_milvus()
        if args.reindex:
            asyncio.run(reindex_all_completed())
    else:
        print(
            "[Milvus] 警告：现有集合未开启动态字段，写入来源字段会被忽略。\n"
            "确认可清空向量数据后，执行：python scripts/migrate_source_fields.py "
            "--rebuild-milvus --reindex"
        )


if __name__ == "__main__":
    main()
