"""真实数据回填 + 真实标签整理（不编造任何中医知识）。

分三部分，全部基于**数据库里已经存在的真实字段值**或**原始数据源文件**：
1. 标签：从真实字段（herbs.channels / herbs.properties / literatures.dynasty）
   抽取去重值生成标签，并在 tag.description 记录来源字段；同时建立资源关联，
   保证标签可用于筛选查询；
2. 理论：用原始数据源 D1_TCM_terminology.tsv 的释义字段回填 theories.content
   （空值才填），同义词回填 aliases，并按 Chinese_group 生成真实分类；
3. 方剂：用 表1_共享杯版-中医古方数据集.xlsx 的 主治/功效 按方名回填
   prescriptions.indications / efficacy（空值才填，绝不覆盖已有真实数据）。

原则：
- 只填**当前为空**的字段，已有真实数据一律不动；
- 原始数据源没有的内容保持 NULL（不允许 AI 编造）；
- 默认 dry-run，``--apply`` 才写库；
- 每一步都输出"修复前 → 修复后"的空值统计。

用法：
    python scripts/backfill_real_data.py
    python scripts/backfill_real_data.py --apply
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import io
import re
import sys
import uuid
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from loguru import logger  # noqa: E402
from sqlalchemy import func, select, text, update  # noqa: E402
from sqlalchemy.dialects.postgresql import insert as pg_insert  # noqa: E402

from src.application.upload_import_service import parse_xlsx  # noqa: E402
from src.domain.models import (  # noqa: E402
    Category,
    Herb,
    Literature,
    Prescription,
    Tag,
    Theory,
    herb_tags,
    literature_tags,
    prescription_tags,
    theory_tags,
)
from src.infrastructure.database import get_session_factory  # noqa: E402

TCM_MKG_DIR = Path(r"C:\毕设数据源\TCM-MKG")
D1_TERMINOLOGY = TCM_MKG_DIR / "D1_TCM_terminology.tsv"
XLSX_ANCIENT_FORMULA = Path(r"C:\毕设数据源\表1_共享杯版-中医古方数据集.xlsx")

# 性味解析：数据库中该字段的真实值形如「四气：warm；五味：pungent」
# （AromaTCM 原始值为英文），这里只做**固定词典翻译 + 归类**，不做任何推断。
QI_TOKENS = {
    "寒": "寒性", "热": "热性", "温": "温性", "凉": "凉性", "平": "平性",
    "cold": "寒性", "hot": "热性", "warm": "温性", "cool": "凉性", "neutral": "平性",
}
WEI_TOKENS = {
    "酸": "酸味", "苦": "苦味", "甘": "甘味", "辛": "辛味", "咸": "咸味", "淡": "淡味", "涩": "涩味",
    "sour": "酸味", "bitter": "苦味", "sweet": "甘味", "sweetish": "甘味",
    "pungent": "辛味", "acrid": "辛味", "salty": "咸味", "bland": "淡味",
    "astringent": "涩味",
}
# 归经：原始值为英文经络名，翻译为标准中文经络名（无法映射的脏值直接跳过）
CHANNEL_TOKENS = {
    "lung": "肺经", "large intestine": "大肠经", "stomach": "胃经", "spleen": "脾经",
    "heart": "心经", "small intestine": "小肠经", "bladder": "膀胱经", "kidney": "肾经",
    "pericardium": "心包经", "triple energizer": "三焦经", "gallbladder": "胆经",
    "liver": "肝经",
}

MIN_RESOURCE_COUNT = 5  # 只有覆盖 ≥5 个资源的值才生成标签（避免长尾噪声）


def _split_multi(value: str) -> list[str]:
    parts = re.split(r"[、,，;；/]+", (value or "").strip())
    return [p.strip() for p in parts if p.strip()]


# ── 1. 真实标签 ─────────────────────────────────────────────────────────────


async def collect_tag_candidates(db) -> dict[str, dict]:
    """从真实字段抽取标签候选：{tag_name: {"source":..., "members":[(rid, table)]}}。"""
    candidates: dict[str, dict] = {}

    def add(name: str, rtype: str, ids: list[str]) -> None:
        """rtype 同时作为"资源类型"与"来源表"键（herb / literature）。"""
        if not name:
            return
        entry = candidates.setdefault(
            name, {"source": rtype, "members": defaultdict(list)}
        )
        entry["members"][rtype].extend(ids)

    # ① 归经（herbs.channels，ARRAY 字段）
    rows = (
        await db.execute(
            text(
                "SELECT id, unnest(channels) AS ch FROM herbs "
                "WHERE deleted_at IS NULL AND channels IS NOT NULL"
            )
        )
    ).all()
    by_value: dict[str, list[str]] = defaultdict(list)
    skipped_channels: dict[str, int] = defaultdict(int)
    for rid, ch in rows:
        raw = (ch or "").strip().strip("。.,，、").lower()
        value = CHANNEL_TOKENS.get(raw)
        if not value:
            # 原始值存在脏数据（如 "heartheart"、"heartliver"）：无法可靠映射，
            # 一律跳过并计数，不做猜测
            if raw:
                skipped_channels[raw] += 1
            continue
        by_value[value].append(str(rid))
    if skipped_channels:
        print("   归经：以下原始值无法可靠映射到标准经络名，已跳过：",
              dict(list(skipped_channels.items())[:10]))
    for value, ids in by_value.items():
        if len(ids) >= MIN_RESOURCE_COUNT:
            add(value[:32], "herb", ids)

    # ② 性味（herbs.properties，文本字段 → 解析四气五味）
    prop_rows = (
        await db.execute(
            text(
                "SELECT id, properties FROM herbs "
                "WHERE deleted_at IS NULL AND coalesce(btrim(properties),'') <> ''"
            )
        )
    ).all()
    prop_by_token: dict[str, list[str]] = defaultdict(list)
    for rid, props in prop_rows:
        text_value = (props or "")
        # 只取「四气：」「五味：」两段，避免把整句原文拆成噪声
        qi_part = re.search(r"四气[^：:]*[:：]\s*([^；;]+)", text_value)
        wei_part = re.search(r"五味[^：:]*[:：]\s*([^；;]+)", text_value)
        scope = f"{qi_part.group(1) if qi_part else ''}；{wei_part.group(1) if wei_part else ''}"
        if not scope.strip("；"):
            scope = text_value
        for token, name in QI_TOKENS.items():
            if re.search(rf"(?<![a-z]){re.escape(token)}(?![a-z])", scope, re.IGNORECASE):
                prop_by_token[name].append(str(rid))
        for token, name in WEI_TOKENS.items():
            if re.search(rf"(?<![a-z]){re.escape(token)}(?![a-z])", scope, re.IGNORECASE):
                prop_by_token[name].append(str(rid))
    for name, ids in prop_by_token.items():
        if len(ids) >= MIN_RESOURCE_COUNT:
            add(name, "herb", ids)

    # ③ 朝代（literatures.dynasty，结构化字段，基数小）
    lit_rows = (
        await db.execute(
            text(
                "SELECT id, dynasty FROM literatures "
                "WHERE deleted_at IS NULL AND coalesce(btrim(dynasty),'') <> ''"
            )
        )
    ).all()
    dyn_by_value: dict[str, list[str]] = defaultdict(list)
    for rid, dynasty in lit_rows:
        value = (dynasty or "").strip()
        if value:
            dyn_by_value[value].append(str(rid))
    for value, ids in dyn_by_value.items():
        if len(ids) >= MIN_RESOURCE_COUNT:
            add(value[:32], "literature", ids)

    return candidates


_LINK_TABLE = {"herb": herb_tags, "prescription": prescription_tags,
               "theory": theory_tags, "literature": literature_tags}


async def apply_tags(db, candidates: dict[str, dict]) -> dict:
    created, linked, skipped = 0, 0, 0
    for name, info in candidates.items():
        source_field = {
            "herb": "herbs.channels / herbs.properties",
            "literature": "literatures.dynasty",
        }.get(info["source"], info["source"])
        members = info["members"]
        total = sum(len(v) for v in members.values())
        desc = (
            f"来源字段：{source_field}；按数据库真实值自动生成并去重；"
            f"覆盖 {total} 个资源（生成于 2026-09-29）"
        )
        stmt = (
            pg_insert(Tag)
            .values(id=uuid.uuid4(), name=name, color="", description=desc)
            .on_conflict_do_nothing(index_elements=["name"])
        )
        result = await db.execute(stmt)
        if result.rowcount:
            created += 1
        tag_id = (
            await db.scalar(select(Tag.id).where(Tag.name == name))
        )
        if tag_id is None:
            skipped += 1
            continue
        for rtype, ids in members.items():
            table = _LINK_TABLE[rtype]
            # 关联表主键列名随资源类型不同：herb_id / prescription_id / ...
            resource_col = f"{rtype}_id"
            for rid in ids:
                await db.execute(
                    pg_insert(table)
                    .values(**{resource_col: uuid.UUID(rid), "tag_id": tag_id})
                    .on_conflict_do_nothing()
                )
                linked += 1
    await db.commit()
    return {"created": created, "links": linked, "skipped": skipped}


# ── 2. 理论回填（D1 术语表）────────────────────────────────────────────────


def load_terminology() -> dict[str, dict]:
    if not D1_TERMINOLOGY.exists():
        logger.warning(f"未找到术语源文件 {D1_TERMINOLOGY}")
        return {}
    content = D1_TERMINOLOGY.read_text(encoding="utf-8", errors="replace")
    reader = csv.DictReader(io.StringIO(content), delimiter="\t")
    out: dict[str, dict] = {}
    for row in reader:
        term = (row.get("Chinese_term") or "").strip()
        if not term:
            continue
        out[term] = {
            "group": (row.get("Chinese_group") or "").strip(),
            "definition": (row.get("English_definition_description") or "").strip(),
            "synonyms": (row.get("Chinese_synonyms") or "").strip(),
        }
    return out


async def backfill_theories(db, apply: bool) -> dict:
    data = load_terminology()
    if not data:
        return {"matched": 0, "content": 0, "aliases": 0, "category": 0}
    rows = list(
        (
            await db.scalars(
                select(Theory).where(Theory.deleted_at.is_(None))
            )
        ).all()
    )
    matched = content_done = alias_done = 0
    groups: dict[str, list[Theory]] = defaultdict(list)
    for theory in rows:
        src = data.get(theory.name)
        if not src:
            continue
        matched += 1
        if not (theory.content or "").strip() and src["definition"] not in ("", "NA"):
            theory.content = src["definition"]
            content_done += 1
        if (not theory.aliases) and src["synonyms"] not in ("", "NA"):
            theory.aliases = _split_multi(src["synonyms"])[:20]
            alias_done += 1
        group = src["group"]
        if group and group != "NA":
            groups[group].append(theory)

    # 分类：按 D1 的 Chinese_group 生成真实分类（层级归属，与标签职责区分）
    cat_done = 0
    if apply:
        existing = {
            c.name: c
            for c in (
                await db.scalars(
                    select(Category).where(
                        Category.resource_type == "theory", Category.deleted_at.is_(None)
                    )
                )
            ).all()
        }
        for group, theories in groups.items():
            cat = existing.get(group)
            if cat is None:
                cat = Category(
                    id=uuid.uuid4(),
                    name=group[:64],
                    resource_type="theory",
                    parent_id=None,
                    description=(
                        "来源：D1_TCM_terminology.tsv 的 Chinese_group 字段"
                        "（真实分类，自动生成）"
                    ),
                )
                db.add(cat)
                await db.flush()
                existing[group] = cat
            for theory in theories:
                if theory.category_id is None:
                    theory.category_id = cat.id
                    cat_done += 1
    else:
        # dry-run：只统计"哪些理论会被归入真实分类"，绝不写入未持久化的外键
        cat_done = sum(
            1 for theories in groups.values() for t in theories if t.category_id is None
        )

    if apply:
        await db.commit()
    else:
        # dry-run：丢弃本次预演对 ORM 对象的修改，避免后续查询触发 autoflush
        await db.rollback()
    return {
        "matched": matched,
        "content": content_done,
        "aliases": alias_done,
        "category": cat_done,
        "groups": len(groups),
        "total": len(rows),
    }


# ── 3. 方剂回填（共享杯古方数据集）──────────────────────────────────────────


def load_ancient_formulas() -> dict[str, dict]:
    if not XLSX_ANCIENT_FORMULA.exists():
        logger.warning(f"未找到古方数据集 {XLSX_ANCIENT_FORMULA}")
        return {}
    headers, rows = parse_xlsx(str(XLSX_ANCIENT_FORMULA))
    idx = {h: i for i, h in enumerate(headers)}

    def col(*names: str) -> int | None:
        for n in names:
            for h, i in idx.items():
                if h and n in h:
                    return i
        return None

    name_i = col("方剂名称", "方名")
    comp_i = col("方剂药物组成", "组成")
    ind_i = col("主治")
    eff_i = col("功效")
    if name_i is None:
        return {}
    out: dict[str, dict] = {}
    for row in rows:
        values = [row.get(h, "") for h in headers]
        name = (values[name_i] if name_i is not None else "").strip()
        if not name:
            continue
        if name in out:  # 去重：同名只保留第一条（后续行多为同方异卷）
            continue
        out[name] = {
            "composition": (values[comp_i] if comp_i is not None else "").strip(),
            "indications": (values[ind_i] if ind_i is not None else "").strip(),
            "efficacy": (values[eff_i] if eff_i is not None else "").strip(),
        }
    return out


async def backfill_prescriptions(db, apply: bool) -> dict:
    data = load_ancient_formulas()
    if not data:
        return {"matched": 0, "indications": 0, "efficacy": 0}
    rows = list(
        (
            await db.scalars(
                select(Prescription).where(Prescription.deleted_at.is_(None))
            )
        ).all()
    )
    matched = ind_done = eff_done = comp_done = 0
    for pres in rows:
        src = data.get(pres.name)
        if not src:
            continue
        matched += 1
        if not (pres.indications or "").strip() and src["indications"]:
            pres.indications = src["indications"]
            ind_done += 1
        if not (pres.efficacy or "").strip() and src["efficacy"]:
            pres.efficacy = src["efficacy"]
            eff_done += 1
        if not (pres.description or "").strip() and src["composition"]:
            pres.description = f"组成：{src['composition']}"
            comp_done += 1
    if apply:
        await db.commit()
    else:
        await db.rollback()
    return {
        "matched": matched,
        "total": len(rows),
        "indications": ind_done,
        "efficacy": eff_done,
        "composition_into_description": comp_done,
    }


# ── 统计 ────────────────────────────────────────────────────────────────────

_NULL_FIELDS = {
    "prescriptions": ("name", "aliases", "efficacy", "indications", "usage_method",
                      "source", "description"),
    "theories": ("name", "aliases", "source", "content"),
    "literatures": ("name", "aliases", "author", "dynasty", "summary", "source", "content"),
}


async def null_stats(db) -> dict[str, dict[str, tuple[int, int]]]:
    stats: dict[str, dict[str, tuple[int, int]]] = {}
    for table, fields in _NULL_FIELDS.items():
        total = int(
            await db.scalar(text(f"SELECT count(*) FROM {table} WHERE deleted_at IS NULL")) or 0
        )
        entry: dict[str, tuple[int, int]] = {"__total__": (total, total)}
        for field in fields:
            empty = int(
                await db.scalar(
                    text(
                        f"SELECT count(*) FROM {table} WHERE deleted_at IS NULL AND "
                        f"coalesce(btrim(coalesce({field}::text,'')),'') = ''"
                    )
                )
                or 0
            )
            entry[field] = (empty, total)
        stats[table] = entry
    return stats


def print_stats(title: str, stats: dict) -> None:
    print(f"\n### {title}")
    for table, fields in stats.items():
        total = fields["__total__"][0]
        print(f"  {table}（总数 {total}）")
        for field, (empty, _) in fields.items():
            if field == "__total__":
                continue
            print(f"    {field:<14} 空值 {empty:>6}  非空 {total - empty:>6}")


# ── 入口 ────────────────────────────────────────────────────────────────────


async def main(apply: bool) -> None:
    factory = get_session_factory()
    async with factory() as db:
        before = await null_stats(db)
        print_stats("修复前空值统计", before)

        candidates = await collect_tag_candidates(db)
        print("\n### 真实标签候选（来自真实字段，去重后）")
        for name, info in sorted(candidates.items()):
            total = sum(len(v) for v in info["members"].values())
            print(f"   {name:<12} 覆盖 {total:>5} 个资源（来源：{info['source']}）")
        print(f"  合计 {len(candidates)} 个标签候选")

        theory_result = await backfill_theories(db, apply=False)
        print("\n### 理论回填预演（数据源：D1_TCM_terminology.tsv）")
        print(f"   术语表条目 {len(load_terminology())}；可匹配到名称的理论 {theory_result['matched']}/{theory_result['total']}")
        print(f"   可回填 content {theory_result['content']}；可回填 aliases {theory_result['aliases']}；"
              f"可归入真实分类 {theory_result['category']}（分组数 {theory_result['groups']}）")

        pres_result = await backfill_prescriptions(db, apply=False)
        print("\n### 方剂回填预演（数据源：表1_共享杯版-中医古方数据集.xlsx）")
        print(f"   数据集条目 {len(load_ancient_formulas())}；可匹配到名称的方剂 {pres_result['matched']}/{pres_result['total']}")
        print(f"   可回填 主治 {pres_result['indications']}；功效 {pres_result['efficacy']}；"
              f"组成→描述 {pres_result['composition_into_description']}")

        if apply:
            tag_result = await apply_tags(db, candidates)
            # 理论/方剂在预演时已把对象加载进 session 并修改，重新执行并落库
            theory_result = await backfill_theories(db, apply=True)
            pres_result = await backfill_prescriptions(db, apply=True)
            print("\n### 执行结果")
            print(f"   标签：新建 {tag_result['created']}，关联 {tag_result['links']} 条，跳过 {tag_result['skipped']}")
            print(f"   理论：content {theory_result['content']}，aliases {theory_result['aliases']}，分类 {theory_result['category']}")
            print(f"   方剂：主治 {pres_result['indications']}，功效 {pres_result['efficacy']}，组成 {pres_result['composition_into_description']}")

            after = await null_stats(db)
            print_stats("修复后空值统计", after)
        else:
            print("\n（dry-run）未写库。确认后加 --apply 执行。")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    asyncio.run(main(args.apply))
