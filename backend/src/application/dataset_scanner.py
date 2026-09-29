"""真实中医知识数据源扫描器（只读分析，绝不落库 / 不向量化）。

职责（导入中心第一阶段）：
1. 扫描 IMPORT_SOURCE_DIR（配置化，禁止写死路径）下的文件与压缩包；
2. 识别格式、统计大小与记录数量（能精确就精确，超大数据用"抽样估算"并标注）；
3. 抽取字段结构与前若干条**真实**样例；
4. 依据**字段语义 + 实际取值内容**判定目标资源类型（herb/prescription/
   theory/literature/document/relation/unknown），并给出"原始字段 → 系统字段"映射；
5. 无法可靠判断的字段一律标记 pending（待确认），**不编造、不猜测填充**。

约束：
- 压缩包只列目录与样例，不解压、不加载全量；
- 超大语料（如 GB 级 JSON）只做抽样 + 估算，不整文件解析；
- 扫描不会自动导入任何数据。
"""

from __future__ import annotations

import csv
import hashlib
import json
import zipfile
from pathlib import Path
from typing import Any, Iterator

from loguru import logger
from pydantic import BaseModel, Field

from src.core.config import settings

# 文本类文件（按目录聚合为一个"文本集"数据集）
TEXT_EXTS = {".txt", ".md"}
TABULAR_EXTS = {".tsv", ".csv"}
JSON_EXTS = {".json", ".jsonl", ".ndjson"}
PARQUET_EXTS = {".parquet"}
ZIP_EXTS = {".zip"}
# 目录内达到该数量的文本文件才聚合为"文本集"，否则单文件成数据集
TEXT_DIR_MIN_FILES = 10
# 小于该字节的文本文件视为说明文件（README 等），不作为数据集
TEXT_MIN_BYTES = 1024
# 预览中单条文本的最大展示长度（真实值会在导入时使用完整内容）
SAMPLE_TEXT_LIMIT = 400
# 估算记录数时最多读取的字节
ESTIMATE_READ_BYTES = 16 * 1024 * 1024


class FieldProfile(BaseModel):
    """字段画像：名称、推断类型、样例值、非空比例。"""

    name: str
    dtype: str
    sample: str = ""
    non_empty_ratio: float = 0.0


class FieldMapping(BaseModel):
    """原始字段 → 系统字段的映射结论。

    status:
    - mapped：语义明确，可直接导入
    - pending：无法可靠判断（需在导入前人工确认，默认不导入该字段）
    - ignored：结构性字段（ID/排名等），不进入业务字段
    """

    source: str
    target: str | None = None
    status: str = "pending"
    note: str = ""


class DatasetProfile(BaseModel):
    """一个数据集的扫描结论。"""

    dataset_id: str
    name: str
    relative_path: str
    format: str
    size_bytes: int = 0
    record_count: int | None = None
    record_count_method: str = "unknown"  # exact / estimated / file_count / unknown
    fields: list[FieldProfile] = Field(default_factory=list)
    samples: list[dict[str, Any]] = Field(default_factory=list)
    detected_type: str = "unknown"
    detected_reason: str = ""
    candidate_types: list[str] = Field(default_factory=list)
    field_mappings: list[FieldMapping] = Field(default_factory=list)
    pending_fields: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    entries: list[dict[str, Any]] = Field(default_factory=list)  # 压缩包目录清单


class ScanResult(BaseModel):
    source_dir: str
    configured: bool
    datasets: list[DatasetProfile] = Field(default_factory=list)
    skipped: list[str] = Field(default_factory=list)
    message: str = ""


# ── 基础工具 ────────────────────────────────────────────────────────────────


def _stable_id(rel_path: str) -> str:
    return hashlib.sha1(rel_path.encode("utf-8")).hexdigest()[:12]


def _clean_key(key: str) -> str:
    """去除列名中的 BOM 与首尾空白（数据文件常由 Excel/Windows 导出）。"""
    return (key or "").replace("\ufeff", "").strip()


def _truncate(value: Any, limit: int = SAMPLE_TEXT_LIMIT) -> str:
    s = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    return s if len(s) <= limit else s[:limit] + "…"


def _cjk_score(text: str) -> int:
    """粗略统计中文字符数，用于判定哪种解码结果才是正确的。"""
    return sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")


def _decode_text(raw: bytes) -> str:
    """GBK/UTF-8 自动判定。

    注意：GBK 字节序列**可能**被 utf-8 成功解出（解成拉丁扩展字母而非报错），
    因此不能"先解成功就用"，必须比较两种解码的中文字符数量取更可信者。
    """
    # 用 errors="replace" 解码：按字节截断时（读文件头）GBK 尾部的半个汉字不会导致整段失败
    best: tuple[int, str] | None = None
    for enc in ("utf-8", "gb18030"):
        text = raw.decode(enc, errors="replace")
        score = _cjk_score(text[:8000])
        if best is None or score > best[0]:
            best = (score, text)
    return best[1] if best is not None else raw.decode("utf-8", errors="replace")


def _read_text_head(path: Path, limit: int = 4000) -> str:
    """读取文件头部（古籍多为 GBK 系编码，需判定而非试错）。"""
    with open(path, "rb") as f:
        raw = f.read(limit * 4)
    return _decode_text(raw)


def _infer_dtype(value: Any) -> str:
    if value is None or value == "" or value == "NA":
        return "empty"
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int):
        return "int"
    if isinstance(value, float):
        return "float"
    if isinstance(value, (list, tuple)):
        return "list"
    if isinstance(value, dict):
        return "object"
    return "str"


def _profile_fields(rows: list[dict[str, Any]], keys: list[str]) -> list[FieldProfile]:
    """按真实取值统计字段画像（不假设字段名含义）。"""
    profiles: list[FieldProfile] = []
    for key in keys:
        values = [r.get(key) for r in rows]
        non_empty = [v for v in values if v not in (None, "", "NA", [])]
        dtype = _infer_dtype(next((v for v in non_empty), None))
        profiles.append(
            FieldProfile(
                name=key,
                dtype=dtype,
                sample=_truncate(next(iter(non_empty), ""), 120),
                non_empty_ratio=round(len(non_empty) / len(values), 3) if values else 0.0,
            )
        )
    return profiles


# ── 各类格式读取 ────────────────────────────────────────────────────────────


def _read_delimited(path: Path, limit: int) -> tuple[list[str], list[dict[str, Any]], int, str]:
    """TSV/CSV：返回 (字段, 样例行, 记录数, 计数方式)。"""
    delimiter = "\t" if path.suffix.lower() == ".tsv" else ","
    # utf-8-sig：剥离 Windows 导出的 BOM（否则首列名会带 ﻿ 前缀导致规则匹配失败）
    with open(path, "r", encoding="utf-8-sig", errors="replace", newline="") as f:
        reader = csv.DictReader(f, delimiter=delimiter)
        fields = [_clean_key(k) for k in (reader.fieldnames or [])]
        samples: list[dict[str, Any]] = []
        total = 0
        for row in reader:
            total += 1
            if len(samples) < limit:
                samples.append({_clean_key(k): v for k, v in row.items()})
        return fields, samples, total, "exact"


def _iter_json_array(path: Path) -> Iterator[dict[str, Any]]:
    """流式解析 JSON 数组（不整文件加载，兼容紧凑/美化两种排版）。"""
    dec = json.JSONDecoder()
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        buf = ""
        while "[" not in buf:
            chunk = f.read(1 << 16)
            if not chunk:
                return
            buf += chunk
        buf = buf[buf.index("[") + 1 :]
        while True:
            buf = buf.lstrip(" \t\r\n,")
            if buf.startswith("]"):
                return
            if not buf:
                chunk = f.read(1 << 16)
                if not chunk:
                    return
                buf += chunk
                continue
            try:
                obj, idx = dec.raw_decode(buf)
            except ValueError:
                chunk = f.read(1 << 16)
                if not chunk:
                    return
                buf += chunk
                continue
            buf = buf[idx:]
            if isinstance(obj, dict):
                yield obj


def _iter_jsonl(path: Path) -> Iterator[dict[str, Any]]:
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict):
                yield obj


def _read_json(path: Path, limit: int, size_bytes: int) -> tuple[list[str], list[dict], int | None, str]:
    """JSON/JSONL：大文件按已读字节估算记录数，避免全量扫描。"""
    iterator = _iter_jsonl(path) if path.suffix.lower() in (".jsonl", ".ndjson") else _iter_json_array(path)
    samples: list[dict[str, Any]] = []
    keys: list[str] = []
    counted = 0
    est_threshold = settings.IMPORT_ESTIMATE_THRESHOLD_MB * 1024 * 1024
    big = size_bytes > est_threshold

    for obj in iterator:
        counted += 1
        if len(samples) < limit:
            samples.append(obj)
            for k in obj:
                k = _clean_key(k)
                if k not in keys:
                    keys.append(k)
        # 超大文件：抽够样例即停，不做全量扫描（GB 级语料全量计数需数分钟）
        if big and counted >= limit:
            break

    if not big:
        return keys, samples, counted, "exact"

    # 按样例的平均序列化字节数估算总量（仅用于展示量级，接口会标注 estimated）
    if samples:
        avg_bytes = sum(len(json.dumps(s, ensure_ascii=False).encode("utf-8")) for s in samples) / len(samples)
        estimated = int(size_bytes / max(avg_bytes, 1))
    else:
        estimated = 0
    return keys, samples, estimated, "estimated"


def _read_parquet(path: Path, limit: int) -> tuple[list[str], list[dict], int, str]:
    import pyarrow.parquet as pq

    pf = pq.ParquetFile(str(path))
    total = pf.metadata.num_rows
    table = next(pf.iter_batches(batch_size=max(limit, 1)))
    rows = table.to_pylist()
    fields = [f.name for f in pf.schema_arrow]
    return fields, rows[:limit], total, "exact"


def _parse_book_meta(file_name: str, head: str) -> dict[str, str]:
    """从古籍文件名与文件头解析书名 / 作者 / 朝代（取不到即留空，不猜测）。"""
    stem = Path(file_name).stem
    # 命名形如 `000-神农本草经` 或 `700.李培生老中医经验集`
    title = stem
    for sep in ("-", ".", "、"):
        if sep in stem:
            head_part = stem.split(sep, 1)[0]
            if head_part.isdigit():
                title = stem.split(sep, 1)[1]
                break
    meta = {"title": title.strip() or stem, "author": "", "dynasty": ""}
    for line in head.splitlines():
        line = line.strip()
        if not meta["author"] and line.startswith("作者："):
            meta["author"] = line.replace("作者：", "").strip()
        elif not meta["dynasty"] and line.startswith("朝代："):
            meta["dynasty"] = line.replace("朝代：", "").strip()
        elif not meta["author"] and line.startswith("作 者："):
            meta["author"] = line.replace("作 者：", "").strip()
    # "不详" 视为未知，不写入
    for key in ("author", "dynasty"):
        if meta[key] in ("不详", "未知", "无", "NA"):
            meta[key] = ""
    return meta


# ── 类型判定与字段映射规则（依据真实字段语义，而非仅字段名）──────────────────


def _rule_mkg_terminology(fields: list[str]) -> tuple[str, str, list[FieldMapping]] | None:
    if not {"TCMT_ID", "Chinese_term", "Chinese_group"} <= set(fields):
        return None
    return (
        "theory",
        "字段含 TCMT_ID + Chinese_term + Chinese_group + English_definition_description："
        "为中医术语/治则条目，语义对应「理论」资源。",
        [
            FieldMapping(source="Chinese_term", target="name", status="mapped", note="术语正名"),
            FieldMapping(source="Chinese_synonyms", target="aliases", status="mapped", note="NA 视为空"),
            FieldMapping(
                source="English_definition_description",
                target="content",
                status="mapped",
                note="原文为英文释义，系统不做翻译，按原样保留",
            ),
            FieldMapping(source="TCMT_ID", target="source", status="mapped", note="拼接为 TCM-MKG:D1:<TCMT_ID> 作为溯源标识"),
            FieldMapping(
                source="Chinese_group",
                target="category_id",
                status="pending",
                note="分组（如「治则」）需对应 categories 分类树节点，当前无现成映射，待人工确认",
            ),
            FieldMapping(source="Pinyin_term", target=None, status="pending", note="系统无拼音字段"),
            FieldMapping(source="English_term", target=None, status="pending", note="系统无英文名/译文字段"),
            FieldMapping(source="Synonyms", target=None, status="pending", note="英文同义词，系统无对应字段"),
            FieldMapping(source="English_group", target=None, status="pending", note="英文分组，系统无对应字段"),
        ],
    )


def _rule_mkg_patent_medicine(fields: list[str]) -> tuple[str, str, list[FieldMapping]] | None:
    if not {"CPM_ID", "Chinese_patent_medicine"} <= set(fields):
        return None
    return (
        "prescription",
        "字段含 CPM_ID + Chinese_patent_medicine + Routes_of_administration："
        "为中成药条目，语义对应「方剂」资源。",
        [
            FieldMapping(source="Chinese_patent_medicine", target="name", status="mapped"),
            FieldMapping(
                source="Routes_of_administration",
                target="usage_method",
                status="mapped",
                note="原文为英文给药途径（如 Oral），系统不翻译，按原样保留",
            ),
            FieldMapping(source="CPM_ID", target="source", status="mapped", note="拼接为 TCM-MKG:D2:<CPM_ID> 作为溯源标识"),
            FieldMapping(source="Pinyin_term", target=None, status="pending", note="系统无拼音字段"),
            FieldMapping(
                target="efficacy",
                status="pending",
                source="（数据集缺失）",
                note="该数据集不含功效/主治/组成字段，导入时留空，**不伪造**",
            ),
        ],
    )


def _rule_mkg_herbal_pieces(fields: list[str]) -> tuple[str, str, list[FieldMapping]] | None:
    if not {"CHP_ID", "Chinese_herbal_pieces"} <= set(fields):
        return None
    return (
        "herb",
        "字段含 CHP_ID + Chinese_herbal_pieces + Chinese_synonyms："
        "为中药饮片条目，语义对应「中药」资源。",
        [
            FieldMapping(source="Chinese_herbal_pieces", target="name", status="mapped"),
            FieldMapping(source="Chinese_synonyms", target="aliases", status="mapped", note="空值视为无别名"),
            FieldMapping(source="CHP_ID", target="source", status="mapped", note="拼接为 TCM-MKG:D6:<CHP_ID> 作为溯源标识"),
            FieldMapping(
                source="Sources",
                target="source",
                status="pending",
                note="取值如 Viridiplantae（生物界拉丁名），与系统 herb.source「出处/基原」语义接近但需人工确认",
            ),
            FieldMapping(source="Pinyin_term", target=None, status="pending", note="系统无拼音字段"),
            FieldMapping(source="English_term", target=None, status="pending", note="系统无英文名/译文字段"),
            FieldMapping(
                target="properties",
                status="pending",
                source="（见 D7 性味表）",
                note="本数据集不含性味/归经；D7_CHP_Medicinal_properties 可按 CHP_ID 关联补充",
            ),
        ],
    )


def _rule_mkg_properties(fields: list[str]) -> tuple[str, str, list[FieldMapping]] | None:
    if not {"CHP_ID", "Medicinal_properties", "Class"} <= set(fields):
        return None
    return (
        "relation",
        "字段为 CHP_ID + Medicinal_properties + Class + 排名：属「饮片属性」关联表，"
        "本身不是独立资源，只能并入已导入的 herbs（性味/归经）。",
        [
            FieldMapping(source="CHP_ID", target="（关联键）", status="ignored", note="关联到 herbs.import_batch 内的 CHP_ID 溯源值"),
            FieldMapping(
                source="Medicinal_properties",
                target="herb.properties",
                status="pending",
                note="取值如 Sweet medicinal（英文），需人工确认性味中文映射规则后才可写入",
            ),
            FieldMapping(source="Class", target=None, status="pending", note="属性类别（Medicinal flavor 等），系统无对应字段"),
            FieldMapping(source="x_rank", target=None, status="ignored", note="排序/坐标，无业务语义"),
            FieldMapping(source="y_rank", target=None, status="ignored", note="排序/坐标，无业务语义"),
        ],
    )


def _rule_mkg_cpm_tcmt(fields: list[str]) -> tuple[str, str, list[FieldMapping]] | None:
    # 必须排在术语规则前：D3 带有 Chinese_term 等载荷字段，但它本质是 CPM↔TCMT 关联表
    if not {"CPM_ID", "TCMT_ID"} <= set(fields):
        return None
    return (
        "relation",
        "字段为 CPM_ID + TCMT_ID（并冗余携带术语文本）：属「中成药—术语」关联表，"
        "系统无对应关联结构，术语本体应取自 D1。",
        [
            FieldMapping(source="CPM_ID", target="（关联键）", status="ignored", note="关联到 prescriptions 溯源值"),
            FieldMapping(source="TCMT_ID", target="（关联键）", status="ignored", note="关联到 theories 溯源值"),
            FieldMapping(
                source="Chinese_term",
                target=None,
                status="pending",
                note="该表为关联表，术语文本重复出现；本体请以 D1 为准，避免重复导入",
            ),
        ],
    )


def _rule_mkg_cpm_chp(fields: list[str]) -> tuple[str, str, list[FieldMapping]] | None:
    if not {"CPM_ID", "CHP_ID"} <= set(fields):
        return None
    if "Dosage_ratio" in fields:
        return (
            "relation",
            "字段为 CPM_ID + CHP_ID + Dosage_ratio：属「方剂—饮片组成」关联表，"
            "可映射到 prescription_ingredients，但需方剂与中药先导入完成。",
            [
                FieldMapping(source="CPM_ID", target="（关联键）", status="ignored", note="关联到 prescriptions 溯源值"),
                FieldMapping(source="CHP_ID", target="（关联键）", status="ignored", note="关联到 herbs 溯源值"),
                FieldMapping(
                    source="Dosage_ratio",
                    target="prescription_ingredients.amount",
                    status="pending",
                    note="多数行为空；且为「比例」无单位，与 amount(数值)+unit 结构不直接对应，待确认",
                ),
            ],
        )
    return (
        "relation",
        "字段为 CPM_ID + TCMT_ID：属「中成药—术语」关联表，系统无对应关联结构。",
        [
            FieldMapping(source="CPM_ID", target="（关联键）", status="ignored", note="关联到 prescriptions 溯源值"),
            FieldMapping(source="TCMT_ID", target="（关联键）", status="ignored", note="关联到 theories 溯源值"),
            FieldMapping(
                source="（无载荷字段）",
                target=None,
                status="pending",
                note="该关联在当前数据模型中没有落点，建议第二阶段以标签或知识图谱方式接入",
            ),
        ],
    )


def _rule_canon_parquet(fields: list[str]) -> tuple[str, str, list[FieldMapping]] | None:
    if not {"title", "author", "dynasty", "text"} <= set(fields):
        return None
    return (
        "literature",
        "字段含 title/author/dynasty/text：为中医经典著作条目（含完整书目著录与正文），"
        "语义对应「文献」资源；其 text 同时可作为 document 进入 RAG 链路。",
        [
            FieldMapping(source="title", target="name", status="mapped"),
            FieldMapping(source="author", target="author", status="mapped"),
            FieldMapping(source="dynasty", target="dynasty", status="mapped", note="原文如「清」「明」"),
            FieldMapping(source="text", target="content", status="mapped", note="正文较长，导入 document 时作为 RAG 文本"),
            FieldMapping(source="work_family", target="aliases", status="mapped", note="同一著作族可作为别名/丛书名"),
            FieldMapping(source="id", target="source", status="mapped", note="如 canon-伤寒论-0075，作为溯源标识"),
            FieldMapping(source="source_format", target=None, status="pending", note="原始来源格式，系统无对应字段"),
            FieldMapping(source="edition_type", target=None, status="pending", note="版本类型，系统无对应字段"),
            FieldMapping(source="extraction_method", target=None, status="ignored", note="抽取方式，技术元信息"),
            FieldMapping(source="rights_status", target=None, status="pending", note="版权状态，系统无对应字段"),
            FieldMapping(source="rights_basis", target=None, status="pending", note="版权依据，系统无对应字段"),
            FieldMapping(source="validation_status", target=None, status="pending", note="校验状态，系统无对应字段"),
            FieldMapping(source="validation_overlap", target=None, status="ignored", note="校验重叠率，技术元信息"),
            FieldMapping(source="char_count", target=None, status="ignored", note="字数统计，技术元信息"),
            FieldMapping(source="cjk_ratio", target=None, status="ignored", note="中文占比，技术元信息"),
            FieldMapping(source="ship_tier", target=None, status="pending", note="质量分层，系统无对应字段"),
        ],
    )


def _rule_pretrain_corpus(fields: list[str]) -> tuple[str, str, list[FieldMapping]] | None:
    # 形如 {"type": "tcm_pretrain_web_text", "content": [{"type": "text", "text": "..."}]}
    if "content" in fields and "type" in fields and len(fields) <= 3:
        return (
            "document",
            "记录形如 {type, content:[{type,text}]}：为大规模中医预训练语料，"
            "**不含书名/作者/朝代等著录信息**，无法构成 literature，只能作为 document 进入 RAG。",
            [
                FieldMapping(source="content[0].text", target="content", status="mapped", note="语料正文"),
                FieldMapping(source="type", target=None, status="pending", note="语料类型标记，系统无对应字段（可作为标签候选）"),
                FieldMapping(
                    source="（缺失）",
                    target="literature.name/author/dynasty",
                    status="pending",
                    note="该数据集无书目著录字段，导入 document 时标题只能用「数据集名+序号」，**不伪造作者/朝代**",
                ),
            ],
        )
    return None


def _rule_aromatcm_herb_basic(fields: list[str]) -> tuple[str, str, list[FieldMapping]] | None:
    """AromaTCM「中医基本表」herb_basic：中药名称 + 功效/四气/五味/归经/毒性。

    与 TCM-MKG 饮片表的差别是它自带**业务功效内容**（Efficacy / Flavors /
    Properties_and_actions_of_TCM / Meridians），因此除名称外还能落到
    herb.effects / properties / channels，补上当前中药资源只有"名称+出处"的缺口。
    取值一律为数据集英文原文，系统不翻译。
    """
    required = {"Chinese_Character", "Efficacy", "Flavors", "Meridians"}
    if not required <= set(fields):
        return None
    return (
        "herb",
        "字段含 Chinese_Character + Efficacy + Flavors + Properties_and_actions_of_TCM "
        "+ Meridians：为 AromaTCM 中药基本表（名称带功效/五味/四气/归经），"
        "语义对应「中药」资源。",
        [
            FieldMapping(source="Chinese_Character", target="name", status="mapped"),
            FieldMapping(
                source="Efficacy",
                target="effects",
                status="mapped",
                note="英文功效分类（如 curing rheumatism），原文保留，不翻译",
            ),
            FieldMapping(
                source="Properties_and_actions_of_TCM",
                target="properties",
                status="mapped",
                note="四气（如 warm），与五味合并写入 herb.properties（性味）",
            ),
            FieldMapping(
                source="Flavors",
                target="properties",
                status="mapped",
                note="五味（如 bitter、pungent），英文原文保留",
            ),
            FieldMapping(
                source="Meridians",
                target="channels",
                status="mapped",
                note="归经（如 liver、kidney），按顿号拆分为数组，英文原文保留",
            ),
            FieldMapping(
                source="Toxicity",
                target="description",
                status="mapped",
                note="毒性（多数为空），有值才写入 description",
            ),
            FieldMapping(
                source="Latin_Name",
                target="description",
                status="mapped",
                note="拉丁药材名，写入 description（非别名字段）",
            ),
            FieldMapping(
                source="Pinyin_Name",
                target="description",
                status="mapped",
                note="拼音，写入 description（非别名字段）",
            ),
            FieldMapping(
                source="Source_Plant_Latin_Names",
                target="description",
                status="mapped",
                note="原植物拉丁名（基原），写入 description",
            ),
            FieldMapping(
                source="Herb_ID",
                target="source",
                status="mapped",
                note="拼接为 <数据集名>:<Herb_ID> 作为溯源标识",
            ),
        ],
    )


# 顺序即优先级：关联表规则必须排在术语规则之前，
# 否则 CPM_ID+TCMT_ID+Chinese_term 这类"带载荷字段的关联表"会被误判为术语本体。
_RULES = (
    _rule_aromatcm_herb_basic,
    _rule_mkg_cpm_tcmt,
    _rule_mkg_cpm_chp,
    _rule_mkg_terminology,
    _rule_mkg_patent_medicine,
    _rule_mkg_herbal_pieces,
    _rule_mkg_properties,
    _rule_canon_parquet,
    _rule_pretrain_corpus,
)


def _detect(fields: list[str]) -> tuple[str, str, list[FieldMapping], list[str]]:
    for rule in _RULES:
        hit = rule(fields)
        if hit:
            kind, reason, mappings = hit
            pending = [m.source for m in mappings if m.status == "pending"]
            return kind, reason, mappings, pending
    return (
        "unknown",
        "字段结构未匹配到任何已知规则，需人工确认目标类型与字段映射。",
        [FieldMapping(source=f, target=None, status="pending", note="未识别字段") for f in fields],
        list(fields),
    )


# ── 数据集构建 ──────────────────────────────────────────────────────────────


def _build_tabular_dataset(path: Path, rel: str, *, limit: int) -> DatasetProfile:
    suffix = path.suffix.lower()
    size = path.stat().st_size
    if suffix in PARQUET_EXTS:
        fields, samples, count, method = _read_parquet(path, limit)
        fmt = "parquet"
    elif suffix in TABULAR_EXTS:
        fields, samples, count, method = _read_delimited(path, limit)
        fmt = suffix.lstrip(".")
    else:  # json / jsonl
        fields, samples, count, method = _read_json(path, limit, size)
        fmt = suffix.lstrip(".")

    # 样例里的超长字段要截断，避免把整本书正文塞进扫描响应
    trimmed = [{k: _truncate(v) for k, v in row.items()} for row in samples]
    kind, reason, mappings, pending = _detect(fields)
    warnings: list[str] = []
    if method == "estimated":
        warnings.append("文件过大，记录数为按抽样估算的量级值，非精确值")
    if kind == "relation":
        warnings.append("该数据集为关联/属性表，本身不构成独立资源，需依赖主表已导入")
    return DatasetProfile(
        dataset_id=_stable_id(rel),
        name=path.name,
        relative_path=rel,
        format=fmt,
        size_bytes=size,
        record_count=count,
        record_count_method=method,
        fields=_profile_fields(samples, fields),
        samples=trimmed,
        detected_type=kind,
        detected_reason=reason,
        candidate_types=(["document"] if kind == "literature" else []),
        field_mappings=mappings,
        pending_fields=pending,
        warnings=warnings,
    )


def _build_text_dir_dataset(dir_path: Path, rel: str, files: list[Path], *, limit: int) -> DatasetProfile:
    """古籍目录：一个目录聚合为一个"文本集"数据集。"""
    size = sum(p.stat().st_size for p in files)
    samples: list[dict[str, Any]] = []
    for p in files[:limit]:
        head = _read_text_head(p, 4000)
        meta = _parse_book_meta(p.name, head)
        body = _read_text_head(p, 20000)
        samples.append(
            {
                "file_name": p.name,
                "title": meta["title"],
                "author": meta["author"],
                "dynasty": meta["dynasty"],
                "size_kb": round(p.stat().st_size / 1024, 1),
                "content_preview": _truncate(body, SAMPLE_TEXT_LIMIT),
            }
        )
    fields = ["file_name", "title", "author", "dynasty", "size_kb", "content_preview"]
    return DatasetProfile(
        dataset_id=_stable_id(rel),
        name=f"{dir_path.name}（{len(files)} 个文本文件）",
        relative_path=rel,
        format="text_dir",
        size_bytes=size,
        record_count=len(files),
        record_count_method="file_count",
        fields=_profile_fields(samples, fields),
        samples=samples,
        detected_type="literature",
        detected_reason=(
            "目录下为逐本古籍的纯文本（文件名含书名，文件头含「作者：」「朝代：」等著录行），"
            "语义对应「文献」资源；正文同时可作为 document 进入 RAG 链路。"
        ),
        candidate_types=["document"],
        field_mappings=[
            FieldMapping(source="file_name", target="name", status="mapped", note="形如 `000-神农本草经.txt`，去序号后取书名"),
            FieldMapping(source="title", target="name", status="mapped", note="优先使用文件头 <篇名> 解析出的书名"),
            FieldMapping(source="author", target="author", status="mapped", note="文件头「作者：」；值为「不详」时留空"),
            FieldMapping(source="dynasty", target="dynasty", status="mapped", note="文件头「朝代：」；值为「不详」时留空"),
            FieldMapping(source="content", target="content", status="mapped", note="全文正文（literature.content / document 文本）"),
            FieldMapping(
                source="（文件名/文件头）",
                target="source",
                status="mapped",
                note="记录数据集名 + 相对文件路径作为溯源",
            ),
        ],
        pending_fields=[],
        warnings=[
            "文件为 GBK/UTF-8 混合编码，扫描按 utf-8 → gb18030 顺序解码",
            "部分文件头著录缺失（作者/朝代为空），导入时留空，不猜测填充",
        ],
    )


def _build_zip_dataset(path: Path, rel: str) -> DatasetProfile:
    """压缩包：只读目录清单与少量样例，不解压、不加载全量。"""
    with zipfile.ZipFile(path) as zf:
        infos = zf.infolist()
        entries = [
            {"name": i.filename, "size_kb": round(i.file_size / 1024, 1)}
            for i in infos[:20]
        ]
        inner_exts: dict[str, int] = {}
        for i in infos:
            ext = Path(i.filename).suffix.lower() or "(无扩展名)"
            inner_exts[ext] = inner_exts.get(ext, 0) + 1
    return DatasetProfile(
        dataset_id=_stable_id(rel),
        name=path.name,
        relative_path=rel,
        format="zip",
        size_bytes=path.stat().st_size,
        record_count=len(infos),
        record_count_method="file_count",
        fields=[FieldProfile(name=ext, dtype="file", sample=f"{cnt} 个") for ext, cnt in inner_exts.items()],
        detected_type="unknown",
        detected_reason="压缩包未解压，仅读取内部目录结构，无法判定字段语义。",
        entries=entries,
        warnings=[
            "压缩包只做目录分析，未解压、未加载数据（避免数十 GB 级解压）",
            "如需导入请先解压到数据源目录后再扫描，或人工确认内部文件结构",
        ],
    )


def scan_source_dir(limit: int | None = None) -> ScanResult:
    """扫描 IMPORT_SOURCE_DIR，返回各数据集画像（只读，不导入）。

    Raises:
        ValueError: 未配置 IMPORT_SOURCE_DIR 或目录不存在。
    """
    root_str = (settings.IMPORT_SOURCE_DIR or "").strip()
    if not root_str:
        return ScanResult(
            source_dir="",
            configured=False,
            message="未配置 IMPORT_SOURCE_DIR，请在 .env 中设置数据源根目录",
        )
    root = Path(root_str)
    if not root.exists() or not root.is_dir():
        return ScanResult(
            source_dir=root_str,
            configured=True,
            message=f"IMPORT_SOURCE_DIR 不存在或不是目录：{root_str}",
        )

    sample_limit = limit or settings.IMPORT_SCAN_SAMPLE_ROWS
    datasets: list[DatasetProfile] = []
    skipped: list[str] = []

    # 1) 非文本文件：逐文件成数据集
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        rel = str(path.relative_to(root)).replace("\\", "/")
        suffix = path.suffix.lower()
        if suffix in TABULAR_EXTS | JSON_EXTS | PARQUET_EXTS:
            try:
                datasets.append(_build_tabular_dataset(path, rel, limit=sample_limit))
            except Exception as exc:  # noqa: BLE001 - 单个文件失败不影响整体扫描
                logger.warning(f"扫描数据集失败 {rel}: {exc}")
                skipped.append(f"{rel}（读取失败：{exc}）")
        elif suffix in ZIP_EXTS:
            try:
                datasets.append(_build_zip_dataset(path, rel))
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"读取压缩包失败 {rel}: {exc}")
                skipped.append(f"{rel}（压缩包读取失败：{exc}）")
        elif suffix in TEXT_EXTS:
            continue  # 文本类交给目录聚合逻辑
        else:
            skipped.append(f"{rel}（不支持的格式 {suffix or '无扩展名'}）")

    # 2) 文本文件：目录内达到阈值则聚合为一个数据集，否则单独成数据集
    text_by_dir: dict[Path, list[Path]] = {}
    for path in sorted(root.rglob("*")):
        if path.is_file() and path.suffix.lower() in TEXT_EXTS:
            if path.stat().st_size < TEXT_MIN_BYTES:
                skipped.append(f"{path.name}（小于 1KB，视为说明文件）")
                continue
            text_by_dir.setdefault(path.parent, []).append(path)

    for dir_path, files in text_by_dir.items():
        rel = str(dir_path.relative_to(root)).replace("\\", "/")
        if len(files) >= TEXT_DIR_MIN_FILES:
            datasets.append(_build_text_dir_dataset(dir_path, rel, files, limit=sample_limit))
        else:
            for p in files:
                datasets.append(
                    _build_text_dir_dataset(p, f"{rel}/{p.name}" if rel != "." else p.name, [p], limit=1)
                )

    datasets.sort(key=lambda d: d.relative_path)
    logger.info(f"数据源扫描完成：{len(datasets)} 个数据集，跳过 {len(skipped)} 项")
    return ScanResult(source_dir=root_str, configured=True, datasets=datasets, skipped=skipped)


def read_records(dataset_id: str, max_records: int | None = None) -> tuple[DatasetProfile, list[dict[str, Any]]]:
    """按 dataset_id 重新读取记录（导入时使用；只读取需要的条数）。

    Returns:
        (数据集画像, 原始记录列表)

    Raises:
        KeyError: dataset_id 不存在
    """
    result = scan_source_dir()
    profile = next((d for d in result.datasets if d.dataset_id == dataset_id), None)
    if profile is None:
        raise KeyError(f"数据集不存在：{dataset_id}")

    root = Path(result.source_dir)
    path = root / profile.relative_path
    cap = max_records or settings.IMPORT_MAX_RECORDS_PER_JOB
    records: list[dict[str, Any]] = []

    if profile.format == "text_dir":
        files = sorted(path.glob("*")) if path.is_dir() else [path]
        files = [f for f in files if f.is_file() and f.suffix.lower() in TEXT_EXTS]
        for f in files[:cap]:
            records.append(
                {
                    "file_name": f.name,
                    **_parse_book_meta(f.name, _read_text_head(f, 4000)),
                    "content": _read_text_file_full(f),
                    "_source_file": str(f.relative_to(root)).replace("\\", "/"),
                }
            )
    elif profile.format in ("tsv", "csv"):
        delimiter = "\t" if profile.format == "tsv" else ","
        with open(path, "r", encoding="utf-8", errors="replace", newline="") as fh:
            for i, row in enumerate(csv.DictReader(fh, delimiter=delimiter)):
                if i >= cap:
                    break
                # 列名清洗必须与扫描阶段一致：数据文件常带 UTF-8 BOM 与首尾空白，
                # 否则列名字面量（如 Herb_ID）在导入阶段取不到值，溯源信息丢失。
                records.append({_clean_key(k): v for k, v in row.items()})
    elif profile.format == "parquet":
        import pyarrow.parquet as pq

        pf = pq.ParquetFile(str(path))
        for batch in pf.iter_batches(batch_size=max(cap, 1)):
            records.extend(batch.to_pylist())
            if len(records) >= cap:
                break
        records = records[:cap]
    elif profile.format in ("json", "jsonl", "ndjson"):
        iterator = (
            _iter_jsonl(path)
            if profile.format in ("jsonl", "ndjson")
            else _iter_json_array(path)
        )
        for i, obj in enumerate(iterator):
            if i >= cap:
                break
            records.append(obj)
    else:
        raise ValueError(f"数据集格式暂不支持导入：{profile.format}")

    return profile, records


def _read_text_file_full(path: Path) -> str:
    return _decode_text(path.read_bytes())
