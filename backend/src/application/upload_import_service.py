"""数据导入中心：浏览器上传文件 → 自动识别类型 → 字段映射预览 → 确认导入。

与既有 dataset_scanner / import_service 的分工：
- dataset_scanner：扫描**服务端数据源目录**（TSV/JSON/Parquet 大数据集）；
- 本模块：处理**用户在页面上传的单个文件**（CSV/TSV/XLSX/JSON/JSONL/TXT/MD/
  DOCX/PDF），自动判断数据类型与字段，先给预览（不写库），确认后才写入。

严格约束：
- 不调用 LLM 编造字段：只做**结构化搬运**，原始文件没有的字段保持为空；
- 不做无条件覆盖：已存在资源只**补充当前为空的字段**（enrichment）；
- 导入失败不产生半条数据：逐条 savepoint，单条失败记录原因后继续；
- 不自动启动大批量向量化：只有调用方显式 ``vectorize=true`` 且资源内容足够时
  才走 ResourceVectorService（默认关闭）；
- 每次导入保留 source_dataset / import_batch_id / source_file，可溯源。
"""

from __future__ import annotations

import csv
import io
import json
import os
import re
import uuid
import zipfile
from collections.abc import Iterable
from datetime import datetime
from typing import Any

import xml.etree.ElementTree as ET

from loguru import logger
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from src.domain.models import (
    Herb,
    ImportJob,
    Literature,
    Prescription,
    Theory,
)
from src.utils.timeutil import utcnow

# ── 常量 ────────────────────────────────────────────────────────────────────

TARGET_TYPES: tuple[str, ...] = ("herb", "prescription", "theory", "literature")

UPLOAD_SUBDIR = "imports"
MAX_PREVIEW_ROWS = 20
MAX_COMMIT_ROWS = 5000  # 单次上传导入上限（保护请求时长，非业务限制）

#: 允许上传的扩展名 → 解析方式
SUPPORTED_EXTS: tuple[str, ...] = (
    ".csv", ".tsv", ".txt", ".md", ".json", ".jsonl", ".ndjson", ".xlsx", ".docx", ".pdf",
)

#: ORM 字段（每种类型可写入的目标字段，必须真实存在于 models.py）
FIELDS_BY_TYPE: dict[str, tuple[str, ...]] = {
    "herb": ("name", "aliases", "properties", "channels", "effects", "source", "description"),
    "prescription": (
        "name", "aliases", "efficacy", "indications", "usage_method", "source", "description",
    ),
    "theory": ("name", "aliases", "content", "source"),
    "literature": ("name", "aliases", "author", "dynasty", "summary", "source", "content"),
}

#: 目标字段 → 候选表头（小写比较；中英文都覆盖，按真实数据集的表头整理）
FIELD_SYNONYMS: dict[str, tuple[str, ...]] = {
    "name": (
        "name", "名称", "药名", "中药名", "中药名称", "药材名", "方名", "方剂名", "方剂名称",
        "书名", "文献名", "标题", "术语", "术语名", "中医术语", "词条",
        "chinese_character", "chinese_term", "chinese_patent_medicine", "chinese_herbal_pieces",
        "term", "title",
    ),
    "aliases": (
        "别名", "异名", "又名", "别称", "拼音", "拼音名", "英文", "英文名",
        "aliases", "alias", "synonyms", "chinese_synonyms", "pinyin_term", "pinyin_name",
        "english_term",
    ),
    "properties": (
        "性味", "药性", "性味归经", "四气五味", "properties", "flavors", "nature",
        "properties_and_actions_of_tcm",
    ),
    "channels": ("归经", "经络", "归经经络", "channels", "meridians"),
    "effects": (
        "功效", "功能", "功能主治", "功效与作用", "effects", "efficacy", "function",
        "actions", "properties_and_actions_of_tcm",
    ),
    "efficacy": ("功效", "功能", "功效主治", "efficacy", "function", "actions"),
    "indications": (
        "主治", "适应症", "适用症", "主治病证", "indications", "indication", "treats",
    ),
    "usage_method": (
        "用法", "用法用量", "服用方法", "用法与用量", "usage_method", "usage", "dosage",
        "routes_of_administration", "administration",
    ),
    "author": ("作者", "著者", "编著", "撰者", "author", "writer", "creator"),
    "dynasty": ("朝代", "年代", "成书年代", "时代", "dynasty", "era", "period"),
    "summary": ("摘要", "简介", "内容简介", "summary", "abstract", "brief"),
    "source": (
        "来源", "出处", "来源出处", "书籍", "文献来源", "source", "sources", "origin", "from",
    ),
    "description": (
        "描述", "说明", "备注", "简介描述", "description", "desc", "note", "remark",
    ),
    "content": (
        "内容", "正文", "正文内容", "释义", "定义", "解释", "条文", "content", "text",
        "definition", "english_definition_description", "body",
    ),
}

#: 类型识别线索：命中即加分（表头 / 文件名）
_TYPE_HINTS: dict[str, tuple[str, ...]] = {
    "herb": (
        "herb", "中药", "药材", "本草", "药性", "性味", "归经", "chinese_character",
        "chinese_herbal_pieces", "aromatcm", "herb_basic",
    ),
    "prescription": (
        "prescription", "方剂", "古方", "方名", "中成药", "chinese_patent_medicine", "cpm",
        "组成", "君臣佐使",
    ),
    "theory": (
        "theory", "理论", "术语", "terminology", "tcmt", "chinese_term", "tcm_terminology",
    ),
    "literature": (
        "literature", "文献", "古籍", "医籍", "书名", "book", "ancient", "canon",
    ),
}

#: 强特征字段：命中一个基本可确定类型（权重更高）
_STRONG_FIELD_HINTS: dict[str, tuple[str, ...]] = {
    "herb": ("性味", "归经", "flavors", "meridians", "properties_and_actions_of_tcm"),
    "prescription": ("组成", "主治", "用法用量", "indications", "routes_of_administration"),
    "theory": ("释义", "definition", "english_definition_description", "chinese_group"),
    "literature": ("作者", "朝代", "author", "dynasty", "成书年代"),
}


# ── 文件落地 ────────────────────────────────────────────────────────────────


def upload_root() -> str:
    """上传文件存放目录（与文档上传共用 uploads 卷，避免引入新挂载）。"""
    base = os.getenv("UPLOAD_DIR") or os.path.join(os.getcwd(), "uploads")
    path = os.path.join(base, UPLOAD_SUBDIR)
    os.makedirs(path, exist_ok=True)
    return path


def _safe_suffix(filename: str) -> str:
    return os.path.splitext(filename or "")[1].lower()


def validate_ext(filename: str) -> None:
    ext = _safe_suffix(filename)
    if ext not in SUPPORTED_EXTS:
        raise ValueError(
            f"不支持的文件类型 {ext or '(无扩展名)'}，支持：{', '.join(SUPPORTED_EXTS)}"
        )


async def save_upload(file: Any) -> tuple[str, str]:
    """保存上传文件，返回 (绝对路径, dataset_id)。"""
    validate_ext(getattr(file, "filename", "") or "")
    dataset_id = f"upload-{uuid.uuid4().hex[:12]}"
    filename = getattr(file, "filename", "") or f"{dataset_id}"
    ext = _safe_suffix(filename)
    target = os.path.join(upload_root(), f"{dataset_id}{ext}")
    data = await file.read()
    with open(target, "wb") as fh:
        fh.write(data)
    return target, dataset_id


# ── 解析 ────────────────────────────────────────────────────────────────────


def _decode(raw: bytes) -> str:
    for enc in ("utf-8-sig", "utf-8", "gb18030", "gbk", "latin-1"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def _norm_key(key: str) -> str:
    return re.sub(r"[\s_\-/()（）]+", "", str(key or "")).strip().lower()


def parse_xlsx(path: str) -> tuple[list[str], list[dict[str, Any]]]:
    """最小 XLSX 读取（stdlib zipfile + ElementTree，不新增依赖）。

    只取第一个 worksheet 的单元格文本：sharedStrings 共享字符串 + 内联字符串。
    解析失败时抛出 ValueError，由上层提示"建议另存为 CSV"。
    """
    try:
        with zipfile.ZipFile(path) as zf:
            shared: list[str] = []
            if "xl/sharedStrings.xml" in zf.namelist():
                root = ET.fromstring(zf.read("xl/sharedStrings.xml"))
                ns = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
                for si in root.findall(f"{ns}si"):
                    shared.append("".join(t.text or "" for t in si.iter(f"{ns}t")))
            sheet_name = next(
                (n for n in zf.namelist() if n.startswith("xl/worksheets/sheet1.xml")),
                None,
            )
            if sheet_name is None:
                raise ValueError("未找到 worksheet")
            root = ET.fromstring(zf.read(sheet_name))
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"XLSX 解析失败：{exc}（建议另存为 CSV 后上传）") from exc

    ns = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
    rows: list[list[str]] = []
    for row in root.iter(f"{ns}row"):
        cells: dict[int, str] = {}
        for cell in row.findall(f"{ns}c"):
            ref = cell.get("r") or ""
            col_idx = _col_index(ref)
            ctype = cell.get("t")
            if ctype == "inlineStr":
                value = "".join(t.text or "" for t in cell.iter(f"{ns}t"))
            else:
                v = cell.find(f"{ns}v")
                raw = v.text if v is not None else ""
                if ctype == "s" and raw:
                    value = shared[int(raw)] if int(raw) < len(shared) else ""
                else:
                    value = raw or ""
            cells[col_idx] = value
        if cells:
            width = max(cells) + 1
            rows.append([cells.get(i, "") for i in range(width)])
    if not rows:
        return [], []
    headers = [str(h).strip() for h in rows[0]]
    out: list[dict[str, Any]] = []
    for r in rows[1:]:
        if not any(str(c).strip() for c in r):
            continue
        padded = list(r) + [""] * (len(headers) - len(r))
        out.append({headers[i]: padded[i] for i in range(len(headers))})
    return headers, out


def _col_index(ref: str) -> int:
    idx = 0
    for ch in ref:
        if ch.isalpha():
            idx = idx * 26 + (ord(ch.upper()) - 64)
        else:
            break
    return max(idx - 1, 0)


def parse_delimited(path: str, delimiter: str | None = None) -> tuple[list[str], list[dict]]:
    text = _decode(open(path, "rb").read())
    if delimiter is None:
        first_line = text.split("\n", 1)[0]
        delimiter = "\t" if first_line.count("\t") >= max(first_line.count(","), 1) else ","
    reader = csv.DictReader(io.StringIO(text), delimiter=delimiter)
    headers = [str(h).strip() for h in (reader.fieldnames or [])]
    rows = [{k: (v if v is not None else "") for k, v in row.items()} for row in reader]
    return headers, rows


def parse_json_records(path: str) -> tuple[list[str], list[dict]]:
    text = _decode(open(path, "rb").read()).strip()
    records: list[dict] = []
    if text.startswith("["):
        try:
            data = json.loads(text)
            records = [d for d in data if isinstance(d, dict)]
        except json.JSONDecodeError:
            records = []
    else:
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict):
                records.append(obj)
    headers: list[str] = []
    for rec in records[:50]:
        for key in rec:
            if key not in headers:
                headers.append(str(key))
    return headers, records


def _flatten(obj: Any, prefix: str = "") -> dict[str, Any]:
    """把嵌套 dict/list 摊平成 键路径 → 字符串（list 只取首元素，够用即可）。"""
    out: dict[str, Any] = {}
    if isinstance(obj, dict):
        for key, value in obj.items():
            path = f"{prefix}.{key}" if prefix else str(key)
            if isinstance(value, (dict, list)):
                out.update(_flatten(value, path))
            else:
                out[path] = value
    elif isinstance(obj, list):
        for idx, value in enumerate(obj[:1]):
            out.update(_flatten(value, f"{prefix}.{idx}" if prefix else str(idx)))
    return out


def parse_text_blocks(path: str) -> tuple[list[str], list[dict]]:
    """TXT/MD：优先按"键：值"分块解析，退化为"整篇作为一条文献"。

    - 存在若干"字段名：值"行 → 按空行切块，每块一条记录；
    - 否则 → 单条记录：name = 文件名（去扩展名），content = 全文，适合古籍文本。
    """
    text = _decode(open(path, "rb").read())
    blocks = [b.strip() for b in re.split(r"\n\s*\n", text) if b.strip()]
    records: list[dict] = []
    kv_pattern = re.compile(r"^\s*([^\n:：=]{1,20})\s*[:：=]\s*(.+)$")
    for block in blocks[:MAX_COMMIT_ROWS]:
        rec: dict[str, Any] = {}
        for line in block.splitlines():
            m = kv_pattern.match(line)
            if m:
                rec[m.group(1).strip()] = m.group(2).strip()
            elif rec:
                # 续行并入上一个字段
                last_key = list(rec)[-1]
                rec[last_key] = f"{rec[last_key]}\n{line.strip()}".strip()
        if rec:
            records.append(rec)
    if records:
        headers: list[str] = []
        for rec in records:
            for key in rec:
                if key not in headers:
                    headers.append(key)
        return headers, records

    stem = os.path.splitext(os.path.basename(path))[0]
    return ["name", "content"], [{"name": stem, "content": text}]


def parse_binary_document(path: str, ext: str) -> tuple[list[str], list[dict]]:
    """DOCX / PDF：复用现有文档解析器抽取文本，再按文本块解析。"""
    from src.infrastructure.parser import get_parser  # 延迟导入：避免启动期加载

    with open(path, "rb") as fh:
        raw = fh.read()
    try:
        text = get_parser().parse(raw, ext.lstrip(".")) or ""
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"{ext} 解析失败：{exc}") from exc
    tmp = os.path.join(upload_root(), f"{uuid.uuid4().hex}.txt")
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(text)
    try:
        return parse_text_blocks(tmp)
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass


def parse_file(path: str) -> tuple[list[str], list[dict]]:
    ext = _safe_suffix(path)
    if ext in (".csv", ".tsv"):
        return parse_delimited(path, "\t" if ext == ".tsv" else None)
    if ext in (".json", ".jsonl", ".ndjson"):
        headers, rows = parse_json_records(path)
        return headers, [_flatten(r) for r in rows]
    if ext == ".xlsx":
        return parse_xlsx(path)
    if ext in (".txt", ".md"):
        return parse_text_blocks(path)
    if ext in (".docx", ".pdf"):
        return parse_binary_document(path, ext)
    raise ValueError(f"不支持的文件类型 {ext}")


# ── 类型识别与字段映射 ──────────────────────────────────────────────────────


def detect_target_type(
    *,
    filename: str = "",
    headers: Iterable[str] = (),
    rows: Iterable[dict] | None = None,
) -> tuple[str, dict[str, int]]:
    """根据文件名 / 表头 / 字段内容判断数据类型。

    Returns:
        (target_type, scores)；target_type 可能是 "unknown"（不可靠判断，
        由前端要求用户手动选择，绝不猜测后直接导入）。
    """
    scores: dict[str, int] = {t: 0 for t in TARGET_TYPES}
    header_keys = [_norm_key(h) for h in headers]
    haystack = " ".join(header_keys).lower()
    fname = (filename or "").lower()

    for rtype, hints in _TYPE_HINTS.items():
        for hint in hints:
            key = _norm_key(hint)
            if key and key in haystack:
                scores[rtype] += 2
            if hint.lower() in fname:
                scores[rtype] += 2

    for rtype, hints in _STRONG_FIELD_HINTS.items():
        for hint in hints:
            key = _norm_key(hint)
            if key and key in haystack:
                scores[rtype] += 5

    # 内容特征：含"组成/君臣佐使"多为方剂；含"作者/朝代"多为文献
    sample = list(rows or [])[:5]
    sample_text = " ".join(
        str(v) for row in sample for v in row.values()
    ).lower()
    for token, rtype in (
        ("组成", "prescription"), ("君", "prescription"), ("臣", "prescription"),
        ("朝代", "literature"), ("作者", "literature"),
        ("归经", "herb"), ("性味", "herb"),
        ("释义", "theory"), ("术语", "theory"),
    ):
        if token in sample_text:
            scores[rtype] += 1

    best = max(scores.values()) if scores else 0
    if best < 3:
        return "unknown", scores
    winners = [t for t, s in scores.items() if s == best]
    if len(winners) > 1:
        return "unknown", scores
    return winners[0], scores


def build_field_mapping(headers: Iterable[str], target_type: str) -> dict[str, str | None]:
    """目标字段 → 源表头；未识别到的字段为 None（导入时保持为空）。

    精确匹配优先，其次包含匹配（避免"功效主治"被"功效"与"主治"同时抢占的歧义：
    取最长的候选命中，语义更具体）。
    """
    if target_type not in FIELDS_BY_TYPE:
        return {}
    normalized = {_norm_key(h): h for h in headers}
    mapping: dict[str, str | None] = {}
    for field in FIELDS_BY_TYPE[target_type]:
        matched: str | None = None
        best_len = -1
        for syn in FIELD_SYNONYMS.get(field, ()):
            key = _norm_key(syn)
            if not key:
                continue
            if key in normalized:
                candidate = normalized[key]
            else:
                contains = [h for k, h in normalized.items() if key and key in k]
                candidate = contains[0] if contains else None
            if candidate and len(key) > best_len:
                matched, best_len = candidate, len(key)
        mapping[field] = matched
    return mapping


# ── 预览（不写库）────────────────────────────────────────────────────────────


def _cell(row: dict, header: str | None) -> str:
    if not header:
        return ""
    value = row.get(header)
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        return "、".join(str(v) for v in value if v not in (None, ""))
    return str(value).strip()


def _split_list(value: str) -> list[str]:
    parts = re.split(r"[、,，;；/|]+", value or "")
    return [p.strip() for p in parts if p.strip()]


async def build_preview(
    db: AsyncSession,
    *,
    path: str,
    filename: str,
    dataset_id: str,
    target_type: str | None = None,
) -> dict:
    """解析文件并给出导入预览（**不写任何业务数据**）。"""
    headers, rows = parse_file(path)
    rows = rows[:MAX_COMMIT_ROWS]
    detected, scores = detect_target_type(filename=filename, headers=headers, rows=rows)
    final_type = target_type if target_type in TARGET_TYPES else detected
    if final_type not in TARGET_TYPES:
        final_type = detected if detected in TARGET_TYPES else "unknown"
    mapping = build_field_mapping(headers, final_type) if final_type in TARGET_TYPES else {}

    estimated_insert = estimated_update = estimated_skip = estimated_invalid = 0
    if final_type in TARGET_TYPES:
        name_header = (mapping or {}).get("name")
        names: list[str] = []
        for row in rows:
            name = _cell(row, name_header)
            if not name:
                estimated_invalid += 1
            else:
                names.append(name)
        existing = await _existing_names(db, final_type, names)
        for name in names:
            if name in existing:
                estimated_update += 1
            else:
                estimated_insert += 1
        # 无法在预览阶段判断是否真的需要补字段，这里给出保守提示
        estimated_skip = 0

    preview_rows = [
        {k: _cell(row, k) for k in headers[:12]} for row in rows[:MAX_PREVIEW_ROWS]
    ]
    return {
        "dataset_id": dataset_id,
        "file_name": filename,
        "file_type": _safe_suffix(filename),
        "total_rows": len(rows),
        "headers": headers,
        "detected_type": detected,
        "detected_scores": scores,
        "target_type": final_type,
        "field_mapping": mapping,
        "need_user_type": final_type == "unknown",
        "sample_rows": preview_rows,
        "estimate": {
            "insert": estimated_insert,
            "update": estimated_update,
            "skip": estimated_skip,
            "invalid": estimated_invalid,
        },
    }


async def _existing_names(db: AsyncSession, target_type: str, names: list[str]) -> set[str]:
    if not names:
        return set()
    model = {"herb": Herb, "prescription": Prescription,
             "theory": Theory, "literature": Literature}[target_type]
    rows = (
        await db.scalars(select(model.name).where(model.name.in_(names)))
    ).all()
    return set(rows)


# ── 提交导入 ────────────────────────────────────────────────────────────────


def _empty_value(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip()
    if isinstance(value, (list, tuple)):
        return len(value) == 0
    return False


async def commit_import(
    db: AsyncSession,
    *,
    path: str,
    filename: str,
    dataset_id: str,
    target_type: str,
    user_id: uuid.UUID | None,
    mapping_override: dict[str, str] | None = None,
    vectorize: bool = False,
    kb_id: uuid.UUID | None = None,
) -> dict:
    """真正写入数据库（需先经预览确认）。

    逐条处理：每条独立 savepoint，单条失败记录原因并继续，最终返回
    inserted / updated / skipped / failed 统计与任务信息。
    """
    if target_type not in TARGET_TYPES:
        raise ValueError("必须先确定数据类型（herb/prescription/theory/literature）")

    headers, rows = parse_file(path)
    rows = rows[:MAX_COMMIT_ROWS]
    mapping = dict(build_field_mapping(headers, target_type))
    if mapping_override:
        for field, src in mapping_override.items():
            if field in FIELDS_BY_TYPE[target_type]:
                # src 为 None → 用户显式取消该字段映射
                mapping[field] = src or None
    if not mapping.get("name"):
        raise ValueError("未能识别名称字段，请在预览中手动指定后再导入")

    batch_id = uuid.uuid4().hex[:16]
    job = ImportJob(
        batch_id=batch_id,
        dataset_id=dataset_id,
        dataset_name=filename,
        source_file=path,
        target_type=target_type,
        kb_id=kb_id,
        status="processing",
        total=len(rows),
        processed=0,
        succeeded=0,
        failed=0,
        skipped=0,
        created_by=user_id,
        started_at=utcnow(),
    )
    db.add(job)
    await db.flush()

    model = {"herb": Herb, "prescription": Prescription,
             "theory": Theory, "literature": Literature}[target_type]
    array_fields = {"aliases", "channels"}
    inserted = updated = skipped = failed = 0
    failed_items: list[dict[str, str]] = []

    for index, row in enumerate(rows, start=1):
        name = _cell(row, mapping.get("name"))
        if not name:
            failed += 1
            failed_items.append({"row": str(index), "reason": "名称为空"})
            continue
        values: dict[str, Any] = {}
        for field in FIELDS_BY_TYPE[target_type]:
            if field == "name":
                continue
            raw = _cell(row, mapping.get(field))
            if not raw:
                continue
            values[field] = _split_list(raw) if field in array_fields else raw

        try:
            async with db.begin_nested():
                existing = await db.scalar(
                    select(model).where(model.name == name)
                )
                if existing is None:
                    obj = model(name=name, **values)
                    obj.source_dataset = filename
                    obj.import_batch_id = batch_id
                    db.add(obj)
                    await db.flush()
                    inserted += 1
                else:
                    # enrichment：只填当前为空的字段，不覆盖已维护的真实数据
                    changed = False
                    for field, value in values.items():
                        if _empty_value(getattr(existing, field, None)) and not _empty_value(value):
                            setattr(existing, field, value)
                            changed = True
                    if _empty_value(existing.source_dataset):
                        existing.source_dataset = filename
                        changed = True
                    if changed:
                        await db.flush()
                        updated += 1
                    else:
                        skipped += 1
        except Exception as exc:  # noqa: BLE001 - 单条失败不影响其余记录
            failed += 1
            failed_items.append({"row": str(index), "name": name, "reason": str(exc)[:200]})
            logger.warning(f"导入失败 {target_type} 第 {index} 行 name={name}: {exc}")

        job.processed = index
        if index % 200 == 0:
            await db.flush()

    job.succeeded = inserted + updated
    job.skipped = skipped
    job.failed = failed
    job.failed_items = failed_items[:200]
    job.status = (
        "completed" if failed == 0 and inserted + updated > 0
        else "partial_success" if inserted + updated > 0
        else "failed"
    )
    job.finished_at = utcnow()
    if job.status == "failed":
        job.error_message = "全部记录导入失败，请检查字段映射与文件内容"
    await db.commit()

    result = {
        "job_id": str(job.id),
        "batch_id": batch_id,
        "dataset_id": dataset_id,
        "target_type": target_type,
        "status": job.status,
        "total": len(rows),
        "inserted": inserted,
        "updated": updated,
        "skipped": skipped,
        "failed": failed,
        "failed_items": failed_items[:50],
        "vectorize": vectorize,
    }

    # G：只有显式要求且确实写入了内容时才进入向量流程（默认关闭，避免上传即触发
    # 大批量 BGE-M3 编码）。失败不影响导入结果，仅记录原因。
    if vectorize and (inserted or updated):
        result["vectorize_result"] = await _vectorize_batch(
            db, target_type, batch_id, kb_id
        )
    return result


async def _vectorize_batch(
    db: AsyncSession,
    target_type: str,
    batch_id: str,
    kb_id: uuid.UUID | None,
) -> dict:
    """导入后按需向量化：复用 ResourceVectorService（canonical → chunk → BGE-M3 → Milvus）。

    资源向量必须挂在某个知识库下，因此**只有传了 kb_id 才执行**：
    先确保 knowledge_base_resources 挂载存在，再 vectorize_and_store（幂等）。
    """
    if kb_id is None:
        return {"requested": 0, "vectorized": 0, "errors": ["未指定目标知识库，跳过向量化"]}
    try:
        from src.application.resource_vector_service import ResourceVectorService
        from src.domain.models import KnowledgeBase, KnowledgeBaseResource

        kb = await db.get(KnowledgeBase, kb_id)
        if kb is None or kb.deleted_at is not None:
            return {"requested": 0, "vectorized": 0, "errors": ["目标知识库不存在"]}

        svc = ResourceVectorService()
        model = {"herb": Herb, "prescription": Prescription,
                 "theory": Theory, "literature": Literature}[target_type]
        rows = list(
            (
                await db.scalars(
                    select(model).where(model.import_batch_id == batch_id)
                )
            ).all()
        )
        done = 0
        errors: list[str] = []
        for obj in rows[:200]:  # 单批次上限，避免一次请求占用过久
            try:
                exists = await db.scalar(
                    select(KnowledgeBaseResource).where(
                        KnowledgeBaseResource.knowledge_base_id == kb_id,
                        KnowledgeBaseResource.resource_type == target_type,
                        KnowledgeBaseResource.resource_id == obj.id,
                    )
                )
                if exists is None:
                    db.add(
                        KnowledgeBaseResource(
                            knowledge_base_id=kb_id,
                            resource_type=target_type,
                            resource_id=obj.id,
                        )
                    )
                    await db.flush()
                svc.vectorize_and_store(obj, kb_id=kb_id, resource_type=target_type)
                done += 1
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{obj.id}: {exc}"[:200])
        await db.commit()
        return {"requested": len(rows), "vectorized": done, "errors": errors[:20]}
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"导入后向量化失败 batch={batch_id}: {exc}")
        return {"requested": 0, "vectorized": 0, "errors": [str(exc)[:200]]}


def job_to_dict(job: ImportJob | None) -> dict | None:
    """导入任务 → 前端展示结构（含生命周期统计）。"""
    if job is None:
        return None
    total = job.total or 0
    processed = job.processed or 0
    return {
        "job_id": str(job.id),
        "batch_id": job.batch_id,
        "dataset_id": job.dataset_id,
        "dataset_name": job.dataset_name,
        "source_file": job.source_file,
        "target_type": job.target_type,
        "kb_id": str(job.kb_id) if job.kb_id else None,
        "status": job.status,
        "total": total,
        "processed": processed,
        "succeeded": job.succeeded or 0,
        "skipped": job.skipped or 0,
        "failed": job.failed or 0,
        "percent": int(processed * 100 / total) if total else 0,
        "error_message": job.error_message,
        "failed_items": job.failed_items or [],
        "created_at": job.created_at.isoformat() if job.created_at else None,
        "started_at": job.started_at.isoformat() if job.started_at else None,
        "finished_at": job.finished_at.isoformat() if job.finished_at else None,
        "created_by": str(job.created_by) if job.created_by else None,
    }


def latest_job_sync_payload(job: ImportJob | None) -> dict | None:
    return job_to_dict(job)
