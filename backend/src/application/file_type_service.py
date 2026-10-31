"""统一文件类型与编码识别（普通导入 / 关联导入共用）。

背景（本次重构要解决的问题）：
- 现有代码只按**文件后缀**判断类型：document_service 白名单仅 4 种，
  且 dataset_scanner 与 upload_import_service 各有一套判断逻辑；
- 既无法识别错误后缀的文件，也无法处理 GBK/Big5 等中文编码，
  中文 CSV 直接按 utf-8 读会乱码或抛异常。

本模块提供「魔数 + 内容 + 后缀」的规范格式判断与编码探测，
解析阶段不再各自猜测，全部依赖这里输出的 (format, encoding)。

纯函数、无 IO 依赖、无 DB 依赖，便于单元测试。
"""

from __future__ import annotations

from loguru import logger

# ── 规范格式常量（全系统统一使用这些值，不再各处写字符串）────────────────────
FORMAT_CSV = "csv"
FORMAT_TSV = "tsv"
FORMAT_JSON = "json"
FORMAT_JSONL = "jsonl"
FORMAT_XLSX = "xlsx"
FORMAT_PARQUET = "parquet"
FORMAT_PDF = "pdf"
FORMAT_DOCX = "docx"
FORMAT_TXT = "txt"
FORMAT_MD = "md"

# 结构化格式：按「记录」处理，保留原始 Schema（不退化成整块文本）
STRUCTURED_FORMATS: tuple[str, ...] = (
    FORMAT_CSV,
    FORMAT_TSV,
    FORMAT_JSON,
    FORMAT_JSONL,
    FORMAT_XLSX,
    FORMAT_PARQUET,
)
# 文档格式：按「文本 + 切片」处理，进 RAG 检索
DOCUMENT_FORMATS: tuple[str, ...] = (FORMAT_PDF, FORMAT_DOCX, FORMAT_TXT, FORMAT_MD)
SUPPORTED_FORMATS: tuple[str, ...] = STRUCTURED_FORMATS + DOCUMENT_FORMATS

_EXT_MAP: dict[str, str] = {
    ".csv": FORMAT_CSV,
    ".tsv": FORMAT_TSV,
    ".tab": FORMAT_TSV,
    ".json": FORMAT_JSON,
    ".jsonl": FORMAT_JSONL,
    ".ndjson": FORMAT_JSONL,
    ".xlsx": FORMAT_XLSX,
    ".xlsm": FORMAT_XLSX,
    ".parquet": FORMAT_PARQUET,
    ".pq": FORMAT_PARQUET,
    ".pdf": FORMAT_PDF,
    ".docx": FORMAT_DOCX,
    ".txt": FORMAT_TXT,
    ".md": FORMAT_MD,
    ".markdown": FORMAT_MD,
}

# 编码识别失败时的回退试解码顺序（中文场景优先 GB18030）
#
# 注意：**不含 utf-16**。Python 的 utf-16 编解码器在没有 BOM 时会按小端解释字节对，
# 任意偶数长度的 ASCII/UTF-8 文本都能"解码成功"（只是结果是乱码），
# 因此它不能用于探测。实测中规模数据集里就出现过：某文件被判为 utf-16，
# 随后解析以 utf-16 打开时抛 "UTF-16 stream does not start with BOM"，整批导入中断。
# 真正的 UTF-16 靠上面的 BOM 分支识别。
_FALLBACK_ENCODINGS: tuple[str, ...] = ("utf-8", "gb18030", "big5")


def _ext_of(file_name: str) -> str:
    """取小写后缀（含点）；无后缀返回空串。"""
    lower = (file_name or "").lower()
    idx = lower.rfind(".")
    if idx == -1 or idx == len(lower) - 1:
        return ""
    # 处理 .tar.gz 之类的复合后缀：只取最后一段
    return lower[idx:]


def _decodes_after_trim(sample: bytes, enc: str, exc: UnicodeDecodeError) -> bool:
    """错误是否只发生在采样尾部（即字符被截断），截掉后能否正常解码。"""
    if exc.start < len(sample) - 4:
        return False  # 错误在中间，确实是这个编码解不了
    try:
        sample[: exc.start].decode(enc)
        return True
    except UnicodeDecodeError:
        return False


def detect_encoding(data: bytes, sample_size: int = 65536) -> str:
    """识别字节内容的文本编码。

    顺序：BOM → 常见编码确定性试解码 → charset_normalizer 兜底识别。
    全部失败返回空串（由调用方给出明确错误，不猜测）。
    """
    if not data:
        return ""
    if data.startswith(b"\xef\xbb\xbf"):
        return "utf-8-sig"
    if data.startswith(b"\xff\xfe") or data.startswith(b"\xfe\xff"):
        return "utf-16"

    # 先按常见编码**确定性**试解码。
    # 顺序很关键：utf-8 在最前可保证 UTF-8 文件不会被误判为 GB 系；
    # gb18030 是 gb2312/gbk 的超集，能覆盖中文场景。
    #
    # 不能反过来先问识别库：实测对几十字节的短样本（表头+两行数据），
    # charset_normalizer 会把 GB18030 内容误判成 utf-16 等「也能解码但结果是乱码」的
    # 编码，导致中文 CSV 解析出乱码甚至 0 行。确定性试解码可完全避免该问题。
    sample = data[:sample_size]
    for enc in _FALLBACK_ENCODINGS:
        try:
            sample.decode(enc)
            return enc
        except UnicodeDecodeError as exc:
            # 采样边界可能正好切在某个多字节字符中间，此时"解码失败"是假象。
            # 必须截掉尾部残缺字节再试一次，否则 UTF-8 文件会被误判成 GB18030：
            # 全量压力测试实测，D4 表的主键被解成「涓嵂id」乱码，
            # 连带外键与关联表识别全部失效（关系数 0）。
            if _decodes_after_trim(sample, enc, exc):
                return enc
            continue
        except LookupError:
            continue

    # 常见编码都不行，再交给识别库兜底（如 utf-8 之外的其他语种编码）
    try:
        from charset_normalizer import from_bytes

        best = from_bytes(sample).best()
        if best is not None:
            return best.encoding
    except Exception as exc:  # 识别库异常不应阻断导入
        logger.debug(f"[file_type] 编码识别异常: {exc}")
    return ""


def detect_format(file_name: str, header: bytes) -> str:
    """按「魔数 → 内容 → 后缀」判断规范格式。

    无法判断时返回空串，由调用方给出明确错误（不猜测、不默认当文本处理）。
    """
    head = header[:8]
    ext = _ext_of(file_name)

    if head.startswith(b"%PDF"):
        return FORMAT_PDF
    if head.startswith(b"PAR1"):
        return FORMAT_PARQUET

    if head.startswith(b"PK\x03\x04"):
        # OOXML 容器（docx / xlsx）无法仅凭魔数区分，按扩展名判定
        if ext in (".xlsx", ".xlsm"):
            return FORMAT_XLSX
        if ext == ".docx":
            return FORMAT_DOCX
        return ""

    # 文本类：首字符是 [ 或 { 时属于 JSON 家族
    stripped = (header or b"").lstrip()
    if stripped[:1] in (b"[", b"{"):
        if ext in (".jsonl", ".ndjson"):
            return FORMAT_JSONL
        # 单行即完整对象 → JSONL；否则按 JSON 数组/对象处理
        first_line = stripped.split(b"\n", 1)[0].strip()
        if first_line.startswith(b"{") and first_line.endswith(b"}"):
            return FORMAT_JSONL
        return FORMAT_JSON

    return _EXT_MAP.get(ext, "")


def is_supported(fmt: str) -> bool:
    return fmt in SUPPORTED_FORMATS


def is_structured(fmt: str) -> bool:
    """是否为结构化格式（需要按记录处理而非整块文本）。"""
    return fmt in STRUCTURED_FORMATS


def is_document(fmt: str) -> bool:
    return fmt in DOCUMENT_FORMATS


def describe_unsupported(file_name: str, fmt: str) -> str:
    """生成人类可读的不支持原因（用于前端错误展示）。"""
    ext = _ext_of(file_name) or "(无后缀)"
    if not fmt:
        return f"无法识别文件类型：{ext}（不在支持范围：{', '.join(SUPPORTED_FORMATS)}）"
    return f"暂不支持的文件类型：{fmt}"
