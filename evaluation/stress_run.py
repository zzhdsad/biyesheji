"""分阶段压力测试：小规模 → 中规模 → 全量（隔离库，绝不碰主库）。

数据集形态（D1～D7 + 方剂组成，含「中药化学成分关联数据」）：
  D1 术语          PK 术语id
  D2 中成药        PK 中成药id
  D3 中成药-术语    M:N 关联
  D4 中药          PK 中药id
  D5 化学成分      PK 成分id
  D6 中药-化学成分  M:N 关联（带含量）  ← 中药化学成分关联数据
  D7 方剂          PK 方剂id
  D8 方剂-中药组成  M:N 关联（带用量）

指标：解析/发现/导入耗时、峰值内存、成功率、实体数、关系数、图谱索引数、
失败明细，以及**部分失败后的恢复情况**（坏行不影响其他行）。
每次运行都注入 3 条主键为空的坏行，验证「部分失败可恢复」。

用法：python stress_run.py small|medium|full
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import random
import resource
import sys
import time
import uuid

sys.path.insert(0, "/app")

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from src.application import file_type_service as fts
from src.application import relation_discovery, schema_analyzer, tabular_parser
from src.application.entity_import_service import EntityImportService
from src.application.index_state_service import IndexStateService
from src.core.config import settings
from src.domain.models import (
    GenericEntity,
    GenericRelation,
    ImportFile,
    ImportJob,
    ImportRecord,
    KgEdge,
    KgNode,
    KnowledgeBase,
    User,
)

SCALES = {
    #            D1术语 D2中成药 D3关联 D4中药 D5成分 D6成分关联 D7方剂 D8组成
    #            D1术语 D2中成药 D3关联 D4中药 D5成分 D6成分关联 D7方剂 D8组成 D9文献
    "small":  dict(d1=100,  d2=50,   d3=200,  d4=100,  d5=80,   d6=300,  d7=60,   d8=250,  d9=50),
    "medium": dict(d1=2000, d2=1000, d3=4000, d4=2000, d5=1600, d6=6000, d7=1200, d8=5000, d9=1000),
    "full":   dict(d1=5000, d2=3000, d3=20000, d4=7000, d5=4000, d6=30000, d7=9000, d8=45000, d9=5000),
}

BAD_ROWS = 3  # 注入的主键为空的坏行数
OUT_ROOT = "/tmp/stress"


def _mb() -> float:
    """当前进程峰值驻留内存（MB）。"""
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0


def gen_files(scale: dict, out_dir: str) -> dict[str, str]:
    os.makedirs(out_dir, exist_ok=True)
    random.seed(20261031)
    paths: dict[str, str] = {}

    def write(name: str, header: list[str], rows: list[list[str]]) -> None:
        path = os.path.join(out_dir, f"{name}.csv")
        with open(path, "w", encoding="utf-8", newline="") as fh:
            fh.write(",".join(header) + "\n")
            for row in rows:
                fh.write(",".join(row) + "\n")
        paths[name] = path

    n = scale
    write("D1术语", ["术语id", "术语名称", "解释"], [
        [f"T{i}", f"术语{i}", f"中医术语释义{i}"] for i in range(1, n["d1"] + 1)
    ])
    write("D2中成药", ["中成药id", "名称", "功效"], [
        [f"M{i}", f"成药{i}", f"功效说明{i}"] for i in range(1, n["d2"] + 1)
    ])
    write("D3中成药术语", ["中成药id", "术语id"], [
        [f"M{random.randint(1, n['d2'])}", f"T{random.randint(1, n['d1'])}"]
        for _ in range(n["d3"])
    ])
    write("D4中药", ["中药id", "中药名称", "性味", "归经"], [
        [f"H{i}", f"药材{i}", "甘温", "脾经"] for i in range(1, n["d4"] + 1)
    ])
    write("D5化学成分", ["成分id", "成分名称", "分子式", "分子量"], [
        [f"C{i}", f"成分{i}", f"C{i}H{i}O", str(100 + i)] for i in range(1, n["d5"] + 1)
    ])
    write("D6中药成分关联", ["中药id", "成分id", "含量"], [
        [f"H{random.randint(1, n['d4'])}", f"C{random.randint(1, n['d5'])}",
         f"{random.uniform(0.1, 9.9):.2f}"]
        for _ in range(n["d6"])
    ])
    write("D7方剂", ["方剂id", "方剂名称", "主治"], [
        [f"P{i}", f"方剂{i}", f"主治{i}"] for i in range(1, n["d7"] + 1)
    ])
    write("D8方剂组成", ["方剂id", "中药id", "用量"], [
        [f"P{random.randint(1, n['d7'])}", f"H{random.randint(1, n['d4'])}", f"{random.randint(1, 30)}g"]
        for _ in range(n["d8"])
    ])
    return paths


def inject_bad_rows(paths: dict[str, str], scale: dict) -> dict:
    """注入两类缺陷，验证「部分失败可恢复」：
    1. D1 末尾 3 行主键为空（脏数据）
    2. 新增 D9文献.jsonl，含 3 行畸形 JSON（行级解析失败，必须带行号报出，
       且不影响同文件其余行、不影响其他表）
    """
    path = paths["D1术语"]
    with open(path, "a", encoding="utf-8", newline="") as fh:
        for i in range(BAD_ROWS):
            fh.write(f",坏行{i + 1},主键为空\n")

    jsonl_path = os.path.join(os.path.dirname(path), "D9文献.jsonl")
    with open(jsonl_path, "w", encoding="utf-8") as fh:
        for i in range(1, scale["d9"] + 1):
            fh.write(json.dumps({"文献id": f"L{i}", "标题": f"文献{i}"}, ensure_ascii=False) + "\n")
        for i in range(BAD_ROWS):
            fh.write(f'{{"文献id": "BAD{i}", "标题": 缺引号\n')  # 畸形行
    paths["D9文献"] = jsonl_path
    return {"empty_pk_rows": BAD_ROWS, "malformed_jsonl_rows": BAD_ROWS}


async def run(scale_name: str) -> dict:
    scale = SCALES[scale_name]
    out_dir = os.path.join(OUT_ROOT, scale_name)
    paths = gen_files(scale, out_dir)
    injected = inject_bad_rows(paths, scale)

    engine = create_async_engine(settings.DATABASE_URL)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    report: dict = {"scale": scale_name, "injected_bad_rows": injected}
    t_all = time.perf_counter()
    base_mem = _mb()

    async with factory() as session:
        tag = f"stress-{scale_name}-{uuid.uuid4().hex[:6]}"
        user = User(
            email=f"{tag}@biyesheji.dev",
            username=tag[:40],
            hashed_password="x",
            role="admin",
            name="压力测试",
            department="测试部",
            must_change_password=False,
        )
        session.add(user)
        await session.flush()
        kb = KnowledgeBase(name=f"压力库-{tag}", owner_id=user.id, visibility="private")
        session.add(kb)
        await session.flush()
        job = ImportJob(
            batch_id=f"stress-{scale_name}",
            dataset_id=tag,
            dataset_name=f"压力测试数据集（{scale_name}）",
            target_type="generic",
            import_mode="relational",
            kb_id=kb.id,
            status="processing",
            created_by=user.id,
        )
        session.add(job)
        await session.flush()

        # ── 解析 ────────────────────────────────────────────────────────
        t0 = time.perf_counter()
        rows_by_table: dict[str, list] = {}
        files_by_table: dict[str, ImportFile] = {}
        parse_errors: list[str] = []
        row_errors: list[dict] = []
        for name, path in paths.items():
            data = open(path, "rb").read()
            fmt = fts.detect_format(os.path.basename(path), data[:64])
            enc = fts.detect_encoding(data)
            try:
                parsed = list(
                    tabular_parser.iter_rows(
                        path,
                        fmt,
                        enc,
                        on_error=lambda row_no, msg, _n=name: row_errors.append(
                            {"file": _n, "row_no": row_no, "message": msg}
                        ),
                    )
                )
            except tabular_parser.TabularParseError as exc:
                parse_errors.append(f"{name}: {exc.message}")
                continue
            rows_by_table[name] = parsed
            import_file = ImportFile(
                job_id=job.id,
                relative_path=f"{name}.csv",
                file_name=f"{name}.csv",
                file_ext=".csv",
                size_bytes=len(data),
                file_hash=hashlib.sha256(name.encode("utf-8")).hexdigest(),
                encoding=enc,
                detected_format=fmt,
                row_count=len(parsed),
            )
            session.add(import_file)
            await session.flush()
            files_by_table[name] = import_file
        await session.commit()
        report["parse_seconds"] = round(time.perf_counter() - t0, 2)
        report["rows"] = {k: len(v) for k, v in rows_by_table.items()}
        report["total_rows"] = sum(len(v) for v in rows_by_table.values())
        report["parse_errors"] = parse_errors
        report["row_errors"] = row_errors
        report["row_error_count"] = len(row_errors)

        # ── Schema 分析 + 关系发现 ──────────────────────────────────────
        t0 = time.perf_counter()
        profiles = [
            schema_analyzer.analyze_rows(rows, table_name=name)
            for name, rows in rows_by_table.items()
        ]
        plan = relation_discovery.discover_relations(profiles)
        report["discover_seconds"] = round(time.perf_counter() - t0, 2)
        report["primary_keys"] = {t: list(v) for t, v in plan.primary_keys.items()}
        report["foreign_keys"] = len(plan.foreign_keys)
        report["junctions"] = [
            f"{j.table}:{j.left_table}->{j.right_table}" for j in plan.junctions
        ]
        report["peak_mem_after_parse_mb"] = round(_mb(), 1)

        # ── 导入 ────────────────────────────────────────────────────────
        svc = EntityImportService(session)
        t0 = time.perf_counter()
        stats = await svc.import_relational(
            kb_id=kb.id,
            user=user,
            job_id=job.id,
            rows_by_table=rows_by_table,
            files_by_table=files_by_table,
            primary_keys={t: plan.primary_keys[t][0] for t in plan.primary_keys},
            foreign_keys=plan.foreign_keys,
            junctions=plan.junctions,
            batch_id=job.batch_id,
            auto_confirm=True,
        )
        await session.commit()
        report["import_seconds"] = round(time.perf_counter() - t0, 2)
        report["stats"] = stats.to_dict()

        # ── 落库核对（不只看服务返回值）─────────────────────────────────
        async def count(model, *filters) -> int:
            stmt = select(func.count()).select_from(model)
            for f in filters:
                stmt = stmt.where(f)
            return int(await session.scalar(stmt) or 0)

        report["db_entities"] = await count(GenericEntity, GenericEntity.kb_id == kb.id)
        report["db_records"] = await count(ImportRecord, ImportRecord.kb_id == kb.id)
        report["db_relations"] = await count(GenericRelation, GenericRelation.kb_id == kb.id)
        report["db_kg_nodes"] = await count(KgNode, KgNode.kb_id == kb.id)
        report["db_kg_edges"] = await count(KgEdge, KgEdge.kb_id == kb.id)

        # ── 索引一致性校验耗时（同知识库规模下的可观测成本）────────────
        t0 = time.perf_counter()
        diff = await IndexStateService(session).diff(kb_id=kb.id)
        report["index_diff_seconds"] = round(time.perf_counter() - t0, 2)
        report["index_diff_documents"] = diff.scanned_documents
        report["index_clean"] = diff.clean

        # ── 恢复验证：重跑同一批数据，确认幂等且无新增失败 ──────────────
        t0 = time.perf_counter()
        stats2 = await svc.import_relational(
            kb_id=kb.id,
            user=user,
            job_id=job.id,
            rows_by_table=rows_by_table,
            files_by_table=files_by_table,
            primary_keys={t: plan.primary_keys[t][0] for t in plan.primary_keys},
            foreign_keys=plan.foreign_keys,
            junctions=plan.junctions,
            batch_id=job.batch_id,
            auto_confirm=True,
        )
        await session.commit()
        report["reimport_seconds"] = round(time.perf_counter() - t0, 2)
        report["reimport_entities_added"] = stats2.entities
        report["reimport_relations_added"] = stats2.relations
        report["reimport_failed"] = stats2.failed_rows
        report["db_entities_after_reimport"] = await count(
            GenericEntity, GenericEntity.kb_id == kb.id
        )

        await session.delete(kb)
        await session.commit()

    await engine.dispose()
    report["total_seconds"] = round(time.perf_counter() - t_all, 2)
    report["peak_mem_mb"] = round(_mb(), 1)
    report["mem_growth_mb"] = round(_mb() - base_mem, 1)
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("scale", choices=list(SCALES))
    args = parser.parse_args()
    result = asyncio.run(run(args.scale))
    print(json.dumps(result, ensure_ascii=False, indent=2))
