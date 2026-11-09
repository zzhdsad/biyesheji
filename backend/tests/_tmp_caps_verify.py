"""20000 条硬上限取消后的分批正确性验证（临时脚本，验证完删除）。

只覆盖纯函数层与只读的数据库分批读取，不写业务数据、不触发解析、不向量化。
"""

from __future__ import annotations

import asyncio
import io
import json
import sys
import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from src.core.config import settings
from src.domain.models import Document, StructuredRecord
from src.application.structured_group_service import StructuredGroupService
from src.utils.structured_data import (
    StructuredParseError,
    open_structured_stream,
    parse_structured_content,
)
from src.utils.structured_relation import (
    FieldProfileProbe,
    GroupFileSchema,
    LinkBuilder,
    build_linked_records,
    key_candidates,
    key_candidates_from_probe,
    relation_id,
    validate_group,
)

FAIL: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"{'PASS' if ok else 'FAIL'} {name} {detail}")
    if not ok:
        FAIL.append(name)


def drain(fmt: str, content: bytes, batch_size: int):
    stream = open_structured_stream(fmt, content, batch_size=batch_size)
    batches: list[list] = []
    try:
        while True:
            try:
                batches.append(next(stream))
            except StopIteration:
                break
    finally:
        summary = stream.summary()
        stream.close()
    return batches, summary


# ── 1. CSV：23517 行（比旧的 20000 上限多）───────────────────────────────────
def test_csv() -> None:
    total = 23517
    lines = ["编号,名称,性质"]
    lines += [f"C{i},药材{i},甘" for i in range(1, total + 1)]
    lines.insert(total // 2, ",,")  # 中间一个空行，应计入 skipped 而不是记录
    content = "\r\n".join(lines).encode("utf-8-sig")

    batches, summary = drain("csv", content, 2000)
    rows = [r for b in batches for r in b]
    sizes = [len(b) for b in batches]
    check("csv 批次数", len(batches) == 12, f"batches={len(batches)} sizes={sizes[:2]}...{sizes[-2:]}")
    check("csv 末批不足一批", sizes[-1] == total - 11 * 2000, f"last={sizes[-1]}")
    check("csv 读取计数", summary.read_count == total and len(rows) == total, f"read={summary.read_count}")
    ids = [r.payload["编号"] for r in rows]
    check("csv 不重复", len(set(ids)) == len(ids))
    check("csv 不遗漏", ids == [f"C{i}" for i in range(1, total + 1)])
    check("csv 跳过空行", summary.skipped_count == 1, f"skipped={summary.skipped_count}")
    check("csv 不截断", summary.truncated is False)
    check("csv 字段清单", [f["name"] for f in summary.fields] == ["编号", "名称", "性质"])

    whole = parse_structured_content("csv", content, batch_size=2000)
    check("csv 兼容接口一致", whole.read_count == total and len(whole.rows) == total)


# ── 2. TSV：真实数据量级（D7 约 23516 行）────────────────────────────────────
def test_tsv() -> None:
    total = 23516
    body = "\n".join(f"{i}\t药材{i}\t归经{i}" for i in range(1, total + 1))
    content = f"序号\t名称\t归经\n{body}\n".encode("utf-8")
    batches, summary = drain("tsv", content, 5000)
    rows = [r for b in batches for r in b]
    check("tsv 全部读入", len(rows) == total and summary.read_count == total, f"rows={len(rows)}")
    check("tsv 行号连续", [r.row_no for r in rows[:3]] == [2, 3, 4])
    check("tsv 末批", len(batches[-1]) == total - 4 * 5000, f"last={len(batches[-1])}")


# ── 3. XLSX：多工作表 24000 行 ───────────────────────────────────────────────
def test_xlsx() -> None:
    from openpyxl import Workbook

    wb = Workbook()
    wb.remove(wb.active)
    total = 0
    for sheet_index in range(1, 6):
        ws = wb.create_sheet(f"表{sheet_index}")
        ws.append(["代号", "名称", "剂量"])
        for i in range(4800):
            ws.append([f"S{sheet_index}-{i}", f"药材{i}", i])
            total += 1
    buf = io.BytesIO()
    wb.save(buf)
    content = buf.getvalue()

    batches, summary = drain("xlsx", content, 2000)
    rows = [r for b in batches for r in b]
    check("xlsx 全部读入", len(rows) == total and summary.read_count == total, f"rows={len(rows)}/{total}")
    keys = [(r.sheet_name, r.payload["代号"]) for r in rows]
    check("xlsx 不重复", len(set(keys)) == len(keys))
    check("xlsx 跨表顺序", keys[0][0] == "表1" and keys[-1][0] == "表5" and keys[-1][1] == "S5-4799")
    check("xlsx 末批", len(batches[-1]) == total - 11 * 2000, f"last={len(batches[-1])}")
    sheets = {f.get("sheet") for f in summary.fields}
    check("xlsx 分表字段清单", sheets == {f"表{i}" for i in range(1, 6)}, f"sheets={sorted(s for s in sheets if s)}")


# ── 4. JSON：数组 23517 条 + 对象字典 20001 条 ───────────────────────────────
def test_json() -> None:
    total = 23517
    content = json.dumps(
        [{"编号": f"J{i}", "名称": f"药材{i}", "属性": ["甘", "温"]} for i in range(total)],
        ensure_ascii=False,
    ).encode("utf-8")
    batches, summary = drain("json", content, 2000)
    rows = [r for b in batches for r in b]
    check("json 数组全部读入", len(rows) == total, f"rows={len(rows)}")
    check("json 数组路径", rows[0].json_path == "$[0]" and rows[-1].json_path == f"$[{total - 1}]")
    check("json 数组末批", len(batches[-1]) == total - 11 * 2000, f"last={len(batches[-1])}")
    check("json 嵌套保留", rows[5].payload["属性"] == ["甘", "温"])

    obj = {f"药材{i}": {"编号": i, "性味": "甘"} for i in range(20001)}
    batches, summary = drain("json", json.dumps(obj, ensure_ascii=False).encode("utf-8"), 2000)
    rows = [r for b in batches for r in b]
    check("json 对象全部读入", len(rows) == 20001, f"rows={len(rows)}")
    check("json 对象标签", rows[0].record_label == "药材0" and rows[-1].record_label == "药材20000")
    check("json 对象路径", rows[-1].json_path == "$.药材20000")

    flat = json.dumps({"编号": 1, "名称": "单条"}).encode("utf-8")
    batches, summary = drain("json", flat, 2000)
    check("json 单对象一条记录", summary.read_count == 1 and len(batches) == 1)

    try:
        drain("json", b"{bad json", 2000)
        check("json 语法错误仍报错", False)
    except StructuredParseError:
        check("json 语法错误仍报错", True)


# ── 5. 关系层：画像累积 == 整份记录；生成不再有上限 ──────────────────────────
def _schemas(anchor_rows: int, right_rows: int = 900):
    left = GroupFileSchema(
        doc_id="a" * 32,
        file_name="关联表.tsv",
        alias="关联",
        order_no=0,
        fields=[
            {"name": "药材编号", "type": "integer", "null_count": 0, "count": anchor_rows},
            {"name": "剂量", "type": "integer", "null_count": 0, "count": anchor_rows},
        ],
        records=[
            {
                "record_id": str(uuid.UUID(int=i)),
                "row_no": i + 2,
                "sheet_name": None,
                "json_path": None,
                "payload": {"药材编号": (i % right_rows) + 1, "剂量": i},
            }
            for i in range(anchor_rows)
        ],
        total_rows=anchor_rows,
    )
    right = GroupFileSchema(
        doc_id="b" * 32,
        file_name="药材表.tsv",
        alias="药材",
        order_no=1,
        fields=[
            {"name": "编号", "type": "integer", "null_count": 0, "count": right_rows},
            {"name": "名称", "type": "string", "null_count": 0, "count": right_rows},
        ],
        records=[
            {
                "record_id": str(uuid.UUID(int=10_000 + i)),
                "row_no": i + 2,
                "sheet_name": None,
                "json_path": None,
                "payload": {"编号": i + 1, "名称": f"药材{i}"},
            }
            for i in range(right_rows)
        ],
        total_rows=right_rows,
    )
    return [left, right]


def test_relation_layer() -> None:
    schemas = _schemas(23517)
    anchor = schemas[0]

    probe = FieldProfileProbe(anchor.field_names())
    for start in range(0, len(anchor.records), 2000):
        probe.add(anchor.records[start : start + 2000])
    names = anchor.field_names()
    from_probe = key_candidates_from_probe(anchor, probe)
    whole = key_candidates(anchor)
    check("主键候选：分批 == 整份", from_probe == whole, f"cands={[(c['field'], c['score']) for c in whole]}")
    check("主键候选计数用真实行数", bool(whole) and whole[0]["record_count"] == 23517)

    rel = {
        "id": relation_id(anchor.doc_id, "药材编号", schemas[1].doc_id, "编号"),
        "from_doc_id": anchor.doc_id,
        "from_field": "药材编号",
        "to_doc_id": schemas[1].doc_id,
        "to_field": "编号",
        "kind": "many_to_one",
        "confirmed": True,
    }

    # 流式：锚点按批喂给 LinkBuilder（等价于服务层从数据库逐批读取）
    builder = LinkBuilder(schemas, [rel])
    streamed = []
    for start in range(0, len(anchor.records), 2000):
        for rec in anchor.records[start : start + 2000]:
            streamed.append(builder.build(rec))
    stats = builder.stats()
    rows, mem_stats = build_linked_records(schemas, [rel])
    check("关联生成：流式无 20000 截断", len(streamed) == 23517 and stats["record_count"] == 23517, f"n={len(streamed)}")
    check("关联生成：流式 == 内存结果", [r["knowledge_text"] for r in streamed] == [r["knowledge_text"] for r in rows])
    check("关联生成：统计不含 truncated", "truncated" not in stats and stats["anomaly_count"] == mem_stats["anomaly_count"])
    check("关联生成：锚点行数取真实总数", stats["anchor_rows"] == 23517 and mem_stats["anchor_rows"] == 23517)
    check("关联生成：每条都带锚点坐标", all(r["anchor_record_id"] for r in streamed))
    check("关联生成：seq 顺序即读入顺序", [r["anchor_record_id"] for r in streamed] == [r["anchor_record_id"] for r in rows])

    # 键投影后的锚点 schema（只保留关联字段）不影响校验的行数与匹配统计
    projected = GroupFileSchema(
        doc_id=anchor.doc_id,
        file_name=anchor.file_name,
        alias=anchor.alias,
        order_no=0,
        fields=anchor.fields,
        records=[
            {
                "record_id": r["record_id"],
                "row_no": r["row_no"],
                "payload": {"药材编号": r["payload"]["药材编号"]},
            }
            for r in anchor.records
        ],
        total_rows=23517,
    )
    report = validate_group([projected, schemas[1]], [dict(rel)])
    stat = report["relation_stats"][0]
    check("校验：左侧投影后行数不变", stat["left_rows"] == 23517 and stat["matched"] == 23517, f"matched={stat['matched']}")
    check("校验：总行数取真实总数", report["total_rows"] == 23517 + 900, f"total={report['total_rows']}")


# ── 6. 只读：数据库键集分批与真实记录数一致 ─────────────────────────────────
async def test_db_stream() -> None:
    engine = create_async_engine(settings.DATABASE_URL, pool_pre_ping=True)
    Session = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with Session() as session:
            svc = StructuredGroupService(session)
            doc_id = await session.scalar(
                select(StructuredRecord.doc_id)
                .group_by(StructuredRecord.doc_id)
                .order_by(func.count().desc())
                .limit(1)
            )
            if doc_id is None:
                check("数据库：库内暂无结构化记录，跳过", True)
                return
            expected = int(
                await session.scalar(
                    select(func.count())
                    .select_from(StructuredRecord)
                    .where(StructuredRecord.doc_id == doc_id)
                )
                or 0
            )
            seen_ids: list[str] = []
            batch_sizes: list[int] = []
            async for batch in svc._iter_record_batches(doc_id, batch_size=2000):
                batch_sizes.append(len(batch))
                seen_ids.extend(r["record_id"] for r in batch)
            check("数据库：分批读满全部记录", len(seen_ids) == expected, f"read={len(seen_ids)} expected={expected}")
            check("数据库：无重复记录 id", len(set(seen_ids)) == expected)
            check("数据库：批次数与末批", len(batch_sizes) == (expected + 1999) // 2000, f"batches={len(batch_sizes)} last={batch_sizes[-1]}")
            doc = await session.get(Document, doc_id)
            print(f"     样本文件={doc.file_name if doc else '?'} 记录数={expected} 批次={len(batch_sizes)}")
    finally:
        await engine.dispose()


def main() -> int:
    test_csv()
    test_tsv()
    test_xlsx()
    test_json()
    test_relation_layer()
    asyncio.run(test_db_stream())
    print("\n=== 结果 ===")
    if FAIL:
        print("FAILED: " + "、".join(FAIL))
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
