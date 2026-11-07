"""结构化数据（space=structured）的字段识别与记录拆分。

只做「读文件 → 识别结构 → 拆成一条一条原始记录」，不碰切片、不碰向量化、不写 Milvus。
所有解析逻辑都是纯函数/纯迭代器（输入 bytes，输出记录批次），便于在路由、后台任务
和小规模验证里复用同一份逻辑。

设计约定：
- **不预设字段**：表头 / JSON 键名是什么就用什么，不同文件可以有完全不同的结构；
  也不要求数据属于中药 / 方剂 / 理论 / 文献任何一类。
- **不改原始数据**：payload 原样保留字段与值（不转型、不翻译、不丢列），
  类型推断与异常提示只写在 fields / anomalies 里。
- **行号可追溯**：CSV/TSV 用 csv 读取器的真实源行号（含被引号跨行的行），
  XLSX 用工作表内绝对行号，JSON 用 ``$[3]`` / ``$.黄芪`` 这样的定位路径。
- **按批产出**：不再有「单文件最多 2 万条」的业务上限，也不会把整份记录列表
  一次性留在内存里。调用方按 ``batch_size`` 消费，写库与解析交替进行。
- 单元格长度与知识文本仍有长度上限（``MAX_FIELD_VALUE_CHARS`` /
  ``MAX_KNOWLEDGE_TEXT_CHARS``），这是单条记录的展示边界，不是行数截断。
"""

from __future__ import annotations

import csv
import io
import json
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Iterator

from src.core.document_formats import delimiter_for
from src.infrastructure.document_extract import ExtractError
from src.utils.text_encoding import decode_text

# 本阶段支持的结构化格式（统一 RAG 接入属后续阶段，这里不扩格式）
STRUCTURED_FORMATS: tuple[str, ...] = ("csv", "tsv", "xlsx", "json")

# 默认每批记录数：调用方可覆盖（服务层用 settings 里的配置）
DEFAULT_PARSE_BATCH_SIZE = 2000
# 单个字段值的最长保留长度（超出截断并标注，防止超长文本塞进 JSONB 与知识文本）
MAX_FIELD_VALUE_CHARS = 2000
# 生成的知识记录文本上限
MAX_KNOWLEDGE_TEXT_CHARS = 4000
# 表头单元格为空 / 重复时用来兜底的列名前缀
_FALLBACK_COLUMN_PREFIX = "第"
_FALLBACK_COLUMN_SUFFIX = "列"
_JSON_WHITESPACE = " \t\r\n"


class StructuredParseError(Exception):
    """文件无法识别为结构化数据（编码、语法、格式或内容为空）。"""


@dataclass
class StructuredRow:
    """一条原始记录：行定位信息 + 原样 payload + 异常提示。"""

    row_no: int
    payload: dict[str, Any]
    anomalies: list[str] = field(default_factory=list)
    sheet_name: str | None = None
    json_path: str | None = None
    record_label: str = ""

    @property
    def knowledge_text(self) -> str:
        return build_knowledge_text(self.payload)


@dataclass
class StructuredFile:
    """一个文件的完整解析结果（兼容旧的「一次拿全量」调用方）。"""

    detected_format: str
    fields: list[dict[str, Any]]
    rows: list[StructuredRow]
    encoding: str = ""
    skipped_count: int = 0
    truncated: bool = False
    read_count: int = 0


@dataclass
class StructuredParseSummary:
    """流式解析结束后的汇总（字段清单 + 实际读取/跳过计数）。"""

    detected_format: str
    fields: list[dict[str, Any]]
    read_count: int
    skipped_count: int
    encoding: str = ""
    # 仅保留字段兼容：当前没有任何业务行数上限，恒为 False
    truncated: bool = False


def is_structured_format(file_type: str | None) -> bool:
    return (file_type or "").lower().lstrip(".") in STRUCTURED_FORMATS


def detect_structured_format(file_type: str | None) -> str:
    fmt = (file_type or "").lower().lstrip(".")
    if fmt not in STRUCTURED_FORMATS:
        raise StructuredParseError(
            f"暂不支持该格式的结构化解析：{file_type or '未知'}（当前支持 CSV / TSV / XLSX / JSON）"
        )
    return fmt


# ── 值与类型 ────────────────────────────────────────────────────────────────

_INT_RE = re.compile(r"^[+-]?\d+$")
_FLOAT_RE = re.compile(r"^[+-]?(\d+\.\d*|\.\d+|\d+)([eE][+-]?\d+)$")
_PERCENT_RE = re.compile(r"^[+-]?\d+(\.\d+)?%$")
_BOOL_WORDS = frozenset({"true", "false", "yes", "no", "是", "否"})
_DATE_RES = (
    re.compile(r"^\d{4}[-/年]\d{1,2}[-/月]\d{1,2}日?$"),
    re.compile(r"^\d{4}[-/年]\d{1,2}$"),
    re.compile(r"^\d{1,2}[-/]\d{1,2}[-/]\d{4}$"),
)
_NULL_WORDS = frozenset({"null", "none", "nan", "n/a", "na", "-", "—"})


def _blank(value: Any) -> bool:
    """是否算「空值」：None / 空串 / 纯空白 / 空集合 / 常见的占位空写法。"""
    if value is None:
        return True
    if isinstance(value, str):
        stripped = value.strip()
        return not stripped or stripped.lower() in _NULL_WORDS
    return isinstance(value, (list, dict, tuple, set)) and len(value) == 0


def infer_value_type(value: Any) -> str:
    """单个取值的类型：integer / float / bool / date / string（空值由调用方先过滤）。"""
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "float"
    text = str(value).strip()
    if _INT_RE.match(text):
        return "integer"
    if _FLOAT_RE.match(text) or _PERCENT_RE.match(text):
        return "float"
    if text.lower() in _BOOL_WORDS:
        return "bool"
    if any(rx.match(text) for rx in _DATE_RES):
        return "date"
    return "string"


def infer_field_type(values: list[Any]) -> tuple[str, int]:
    """推断字段类型并统计空值数：全空 → null；类型不一致 → mixed。"""
    null_count = sum(1 for v in values if _blank(v))
    kinds = {infer_value_type(v) for v in values if not _blank(v)}
    if not kinds:
        return "null", null_count
    if len(kinds) == 1:
        return kinds.pop(), null_count
    # 整数与浮点混在一起仍属同一数值类型，不算「类型混杂」
    if kinds == {"integer", "float"}:
        return "float", null_count
    return "mixed", null_count


def _merge_field_type(kinds: dict[str, int]) -> str:
    """把逐值类型计数合并成字段类型：全空 → null；整数+浮点 → float。"""
    if not kinds:
        return "null"
    if len(kinds) == 1:
        return next(iter(kinds))
    if set(kinds) == {"integer", "float"}:
        return "float"
    return "mixed"


def value_text(value: Any) -> str:
    """字段值的展示文本：标量原样，容器序列化成 JSON（不改写、不翻译）。"""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        text = value.strip()
    elif isinstance(value, (int, float)):
        text = str(value)
    else:
        text = json.dumps(value, ensure_ascii=False)
    if len(text) > MAX_FIELD_VALUE_CHARS:
        return text[:MAX_FIELD_VALUE_CHARS] + "…"
    return text


def build_knowledge_text(payload: dict[str, Any]) -> str:
    """按**实际字段**动态生成一条独立知识记录文本。

    ``名称：黄芪；性味：甘，微温；归经：肺、脾；功效：补气升阳。``
    空值字段直接跳过，不生成「性味：」这类无意义片段；字段顺序沿用文件里的顺序。
    """
    parts: list[str] = []
    total = 0
    for key, value in payload.items():
        text = value_text(value)
        if not text:
            continue
        segment = f"{key}：{text}"
        if total + len(segment) > MAX_KNOWLEDGE_TEXT_CHARS:
            parts.append("…")
            break
        parts.append(segment)
        total += len(segment) + 1
    if not parts:
        return ""
    return "；".join(parts) + "。"


# ── 字段清单 ────────────────────────────────────────────────────────────────


def _dedupe_field_names(raw_names: list[str]) -> list[str]:
    """表头规范化：空列名补「第 N 列」，重名追加序号，保证 payload 的键不冲突。"""
    names: list[str] = []
    seen: dict[str, int] = {}
    for idx, raw in enumerate(raw_names, start=1):
        name = (raw or "").strip()
        if not name:
            name = _column_label(idx)
        count = seen.get(name, 0)
        seen[name] = count + 1
        if count:
            name = f"{name}（{count + 1}）"
        names.append(name)
    return names


class _FieldAccumulator:
    """逐批累加字段清单：保持首次出现顺序，逐字段统计类型与空值数。

    同一字段名在不同工作表里分别统计，所以多 sheet 的 XLSX 不会把两套表头混成一套。
    这里只保留计数与类型分布，不保留字段值本身，内存不随记录数线性增长。
    """

    def __init__(self) -> None:
        # dict 按插入顺序迭代：(sheet, name) → 统计状态
        self._entries: dict[tuple[str | None, str], dict[str, Any]] = {}

    def add(self, payload: dict[str, Any], sheet: str | None = None) -> None:
        for name, value in payload.items():
            key = (sheet, str(name))
            state = self._entries.get(key)
            if state is None:
                state = {
                    "name": str(name),
                    "sheet": sheet,
                    "count": 0,
                    "null_count": 0,
                    "kinds": {},
                }
                self._entries[key] = state
            state["count"] += 1
            if _blank(value):
                state["null_count"] += 1
            else:
                kind = infer_value_type(value)
                kinds = state["kinds"]
                kinds[kind] = kinds.get(kind, 0) + 1

    def finalize(self) -> list[dict[str, Any]]:
        specs: list[dict[str, Any]] = []
        for state in self._entries.values():
            kind = _merge_field_type(state["kinds"])
            spec: dict[str, Any] = {
                "name": state["name"],
                "type": kind,
                "null_count": state["null_count"],
                "count": state["count"],
            }
            if state["sheet"]:
                spec["sheet"] = state["sheet"]
            if kind == "mixed":
                spec["note"] = "同一列出现多种数据类型"
            specs.append(spec)
        return specs


class _ParseState:
    """一次流式解析的可变状态：计数、编码、字段累加器。"""

    def __init__(self, detected_format: str) -> None:
        self.detected_format = detected_format
        self.encoding = ""
        self.read_count = 0
        self.skipped_count = 0
        self.fields = _FieldAccumulator()

    def summary(self) -> StructuredParseSummary:
        return StructuredParseSummary(
            detected_format=self.detected_format,
            fields=self.fields.finalize(),
            read_count=self.read_count,
            skipped_count=self.skipped_count,
            encoding=self.encoding,
            truncated=False,
        )


class StructuredRecordStream:
    """按批迭代记录；迭代结束后通过 ``summary()`` 取字段清单与计数。

    调用方必须保证只在同一个线程里顺序 ``next()``（服务层用 ``asyncio.to_thread``
    串行消费），并在异常路径调用 ``close()`` 释放 XLSX 工作簿等资源。
    """

    def __init__(self, batches: Iterator[list[StructuredRow]], state: _ParseState) -> None:
        self._batches = batches
        self._state = state
        self._closed = False

    def __iter__(self) -> "StructuredRecordStream":
        return self

    def __next__(self) -> list[StructuredRow]:
        return next(self._batches)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        close = getattr(self._batches, "close", None)
        if close is not None:
            close()

    def summary(self) -> StructuredParseSummary:
        return self._state.summary()


# ── 行 → 记录（CSV / TSV / XLSX 共用）────────────────────────────────────────


def _column_label(position: int) -> str:
    return f"{_FALLBACK_COLUMN_PREFIX}{position}{_FALLBACK_COLUMN_SUFFIX}"


def _strip_cell(value: Any) -> str:
    return (value or "").strip()


def _trim_trailing_blanks(values: list[Any]) -> list[Any]:
    """去掉行尾空格子。

    openpyxl 会把每行补齐到工作表最大列数，直接按表头映射会把「这行本来就只写了
    3 列」误报成「多出 2 列且为空」。行尾空白没有信息，先裁掉再比对列数。
    """
    end = len(values)
    while end > 0 and not value_text(values[end - 1]):
        end -= 1
    return values[:end]


def _row_to_payload(
    names: list[str], values: list[Any], stringify: Callable[[Any], str]
) -> tuple[dict[str, Any], list[str]]:
    """按表头把一行映射成 payload，并如实记录列数不符与空值提示。

    列多于表头时追加「第 N 列」键，列少于表头时补空串：两种情况都不丢数据，
    同时把偏差写进 anomalies，供预览页提示。
    """
    anomalies: list[str] = []
    payload: dict[str, Any] = {}
    for i, name in enumerate(names):
        if i < len(values):
            value = stringify(values[i])
        else:
            value = ""
            anomalies.append(f"字段「{name}」缺失（本行仅 {len(values)} 列，表头 {len(names)} 列）")
        payload[name] = value
    if len(values) > len(names):
        extra = len(values) - len(names)
        anomalies.append(f"列数多于表头（多出 {extra} 列）")
        for j in range(extra):
            # 兜底列名用 1 基的实际列位，别写成「第5列」其实是第 6 个单元格
            payload[_column_label(len(names) + j + 1)] = stringify(values[len(names) + j])
    for name, value in payload.items():
        if _blank(value):
            anomalies.append(f"字段「{name}」为空")
    return payload, anomalies


def _flush_batch(state: _ParseState, batch: list[StructuredRow]) -> list[StructuredRow]:
    state.read_count += len(batch)
    return batch


# ── CSV / TSV ───────────────────────────────────────────────────────────────


def _iter_delimited(
    fmt: str, content: bytes, batch_size: int, state: _ParseState
) -> Iterator[list[StructuredRow]]:
    delimiter = delimiter_for(fmt)
    if delimiter is None:  # pragma: no cover - detect_structured_format 已拦住格式
        raise StructuredParseError(f"{fmt} 不是可解析的分隔文本格式")
    try:
        text, encoding = decode_text(content)
    except Exception as exc:  # noqa: BLE001 - 编码识别失败属解析失败
        raise StructuredParseError(f"文本编码无法识别：{exc}") from exc
    if not text.strip():
        raise StructuredParseError("文件内容为空，未识别到表头与数据行")
    state.encoding = encoding

    names: list[str] | None = None
    batch: list[StructuredRow] = []
    # csv 读取器按源行号计数（line_num 会被引号跨行推进），行号因此能回到原始文件位置
    with io.StringIO(text, newline="") as buffer:
        reader = csv.reader(buffer, delimiter=delimiter)
        for cells in reader:
            if not any(cell.strip() for cell in cells):
                state.skipped_count += 1
                continue
            if names is None:
                names = _dedupe_field_names(cells)
                continue
            payload, anomalies = _row_to_payload(names, cells, _strip_cell)
            state.fields.add(payload)
            batch.append(
                StructuredRow(row_no=reader.line_num, payload=payload, anomalies=anomalies)
            )
            if len(batch) >= batch_size:
                yield _flush_batch(state, batch)
                batch = []

    if names is None:
        raise StructuredParseError("首行没有可用内容，未识别到表头")
    if batch:
        yield _flush_batch(state, batch)
    if state.read_count == 0:
        raise StructuredParseError("未识别到数据记录（只有表头，或表头以下全是空行）")


# ── XLSX ────────────────────────────────────────────────────────────────────


def _iter_xlsx(
    content: bytes, batch_size: int, state: _ParseState
) -> Iterator[list[StructuredRow]]:
    from openpyxl import load_workbook

    try:
        workbook = load_workbook(io.BytesIO(content), read_only=True, data_only=True)
    except Exception as exc:  # noqa: BLE001
        raise StructuredParseError(f"XLSX 内容读取失败：{exc}") from exc

    try:
        batch: list[StructuredRow] = []
        try:
            # 不再限制工作表数量：每个有数据的工作表都完整读取
            for sheet in workbook.worksheets:
                names: list[str] | None = None
                for index, values in enumerate(sheet.iter_rows(values_only=True), start=1):
                    raw = list(values)
                    if not any(value_text(value) for value in raw):
                        state.skipped_count += 1
                        continue
                    trimmed = _trim_trailing_blanks(raw)
                    if names is None:
                        names = _dedupe_field_names(
                            [value_text(value) for value in trimmed]
                        )
                        continue
                    payload, anomalies = _row_to_payload(names, trimmed, value_text)
                    state.fields.add(payload, sheet.title)
                    batch.append(
                        StructuredRow(
                            row_no=index,
                            payload=payload,
                            anomalies=anomalies,
                            sheet_name=sheet.title,
                        )
                    )
                    if len(batch) >= batch_size:
                        yield _flush_batch(state, batch)
                        batch = []
                # 只有表头或整表为空的工作表：不产出记录（与旧行为一致）
        except StructuredParseError:
            raise
        except Exception as exc:  # noqa: BLE001 - 单个坏工作表不能让整份解析变成脏数据
            raise StructuredParseError(f"XLSX 工作表读取失败：{exc}") from exc

        if batch:
            yield _flush_batch(state, batch)
        if state.read_count == 0:
            raise StructuredParseError("未识别到数据记录（每个工作表只有表头或整表为空）")
    finally:
        workbook.close()


# ── JSON ────────────────────────────────────────────────────────────────────


def _json_path(parent: str, key: Any) -> str:
    if isinstance(key, int):
        return f"{parent}[{key}]"
    return f"{parent}.{key}"


def _scalarize_json(payload: dict[str, Any]) -> dict[str, Any]:
    """记录内的嵌套值保留原样（JSONB 能存），只把 None 与超长字符串规范化。"""
    out: dict[str, Any] = {}
    for key, value in payload.items():
        if isinstance(value, str) and len(value) > MAX_FIELD_VALUE_CHARS:
            out[key] = value[:MAX_FIELD_VALUE_CHARS] + "…"
        else:
            out[key] = value
    return out


def _anomalies_for(payload: dict[str, Any]) -> list[str]:
    anomalies: list[str] = []
    for name, value in payload.items():
        if _blank(value):
            anomalies.append(f"字段「{name}」为空")
        elif isinstance(value, (list, dict)):
            anomalies.append(f"字段「{name}」是嵌套结构，已原样保留")
    return anomalies


def _skip_json_ws(text: str, index: int) -> int:
    while index < len(text) and text[index] in _JSON_WHITESPACE:
        index += 1
    return index


def _json_error(exc: json.JSONDecodeError, text: str) -> StructuredParseError:
    """把 raw_decode 的字符位置换成行列号，保持与 json.loads 的错误可读性。"""
    pos = max(0, min(exc.pos, len(text)))
    line = text.count("\n", 0, pos) + 1
    last_break = text.rfind("\n", 0, pos)
    column = pos - last_break
    return StructuredParseError(f"JSON 格式非法：第 {line} 行第 {column} 列：{exc.msg}")


def _iter_json_object_entries(
    text: str, decoder: json.JSONDecoder
) -> Iterator[tuple[str, Any]]:
    """逐个产出顶层对象的键值；每个值解码后立即交回调用方，不保留整对象。"""
    index = _skip_json_ws(text, 0)
    if index >= len(text) or text[index] != "{":
        raise StructuredParseError("JSON 顶层不是对象")
    index = _skip_json_ws(text, index + 1)
    if index < len(text) and text[index] == "}":
        index = _skip_json_ws(text, index + 1)
        if index != len(text):
            raise StructuredParseError("JSON 顶层对象后存在多余内容")
        return
    while True:
        if index >= len(text):
            raise StructuredParseError("JSON 对象未闭合")
        try:
            key, index = decoder.raw_decode(text, index)
        except json.JSONDecodeError as exc:
            raise _json_error(exc, text) from exc
        if not isinstance(key, str):
            raise StructuredParseError("JSON 对象的键必须是字符串")
        index = _skip_json_ws(text, index)
        if index >= len(text) or text[index] != ":":
            raise StructuredParseError(f"JSON 对象字段「{key}」后缺少冒号")
        index = _skip_json_ws(text, index + 1)
        try:
            value, index = decoder.raw_decode(text, index)
        except json.JSONDecodeError as exc:
            raise _json_error(exc, text) from exc
        yield key, value
        index = _skip_json_ws(text, index)
        if index >= len(text):
            raise StructuredParseError("JSON 对象未闭合")
        if text[index] == ",":
            index = _skip_json_ws(text, index + 1)
            continue
        if text[index] == "}":
            index = _skip_json_ws(text, index + 1)
            if index != len(text):
                raise StructuredParseError("JSON 顶层对象后存在多余内容")
            return
        raise StructuredParseError("JSON 对象字段之间缺少逗号或对象未闭合")


def _json_object_all_nested(text: str, decoder: json.JSONDecoder) -> bool:
    """判断顶层对象是否「每个键下都是一条独立记录」。

    混合类型时按旧语义把整个对象当作一条记录；这里只做一次只读扫描，
    值本身用完即丢，不会把整份对象留在内存里。
    """
    has_value = False
    for _, value in _iter_json_object_entries(text, decoder):
        has_value = True
        if not isinstance(value, dict):
            return False
    return has_value


def _iter_json_array(
    text: str, decoder: json.JSONDecoder, batch_size: int, state: _ParseState
) -> Iterator[list[StructuredRow]]:
    index = _skip_json_ws(text, 0)
    if index >= len(text) or text[index] != "[":
        raise StructuredParseError("JSON 顶层不是数组")
    index += 1
    batch: list[StructuredRow] = []
    item_index = 0
    while True:
        index = _skip_json_ws(text, index)
        if index >= len(text):
            raise StructuredParseError("JSON 数组未闭合")
        if text[index] == "]":
            index = _skip_json_ws(text, index + 1)
            if index != len(text):
                raise StructuredParseError("JSON 顶层数组后存在多余内容")
            break
        try:
            item, index = decoder.raw_decode(text, index)
        except json.JSONDecodeError as exc:
            raise _json_error(exc, text) from exc
        if item is None:
            state.skipped_count += 1
        else:
            payload = _scalarize_json(item) if isinstance(item, dict) else {"值": item}
            state.fields.add(payload)
            batch.append(
                StructuredRow(
                    row_no=item_index + 1,
                    payload=payload,
                    anomalies=_anomalies_for(payload),
                    json_path=_json_path("$", item_index),
                )
            )
            if len(batch) >= batch_size:
                yield _flush_batch(state, batch)
                batch = []
        item_index += 1
        index = _skip_json_ws(text, index)
        if index < len(text) and text[index] == ",":
            index += 1
            continue
        if index < len(text) and text[index] == "]":
            index = _skip_json_ws(text, index + 1)
            if index != len(text):
                raise StructuredParseError("JSON 顶层数组后存在多余内容")
            break
        raise StructuredParseError("JSON 数组元素之间缺少逗号或数组未闭合")

    if batch:
        yield _flush_batch(state, batch)
    if state.read_count == 0:
        raise StructuredParseError("JSON 中没有可解析的数据记录")


def _iter_json(
    content: bytes, batch_size: int, state: _ParseState
) -> Iterator[list[StructuredRow]]:
    try:
        text, encoding = decode_text(content)
    except Exception as exc:  # noqa: BLE001
        raise StructuredParseError(f"JSON 文件解码失败：{exc}") from exc
    state.encoding = encoding
    decoder = json.JSONDecoder()
    index = _skip_json_ws(text, 0)
    if index >= len(text):
        raise StructuredParseError("文件内容为空")
    head = text[index]

    if head == "[":
        yield from _iter_json_array(text, decoder, batch_size, state)
        return
    if head != "{":
        raise StructuredParseError("JSON 顶层不是对象或数组，无法按记录表解析")

    if _json_object_all_nested(text, decoder):
        # {"黄芪": {...}, "党参": {...}}：每个键下是一条独立记录
        batch: list[StructuredRow] = []
        for item_index, (key, value) in enumerate(
            _iter_json_object_entries(text, decoder), start=1
        ):
            payload = _scalarize_json(value)
            state.fields.add(payload)
            batch.append(
                StructuredRow(
                    row_no=item_index,
                    payload=payload,
                    anomalies=_anomalies_for(payload),
                    json_path=_json_path("$", key),
                    record_label=str(key),
                )
            )
            if len(batch) >= batch_size:
                yield _flush_batch(state, batch)
                batch = []
        if batch:
            yield _flush_batch(state, batch)
        if state.read_count == 0:
            raise StructuredParseError("JSON 中没有可解析的数据记录")
        return

    # {"a": 1, "b": "x"} 或值类型混杂：整个对象就是一条记录
    try:
        obj = json.loads(text)
    except json.JSONDecodeError as exc:
        raise StructuredParseError(
            f"JSON 格式非法：第 {exc.lineno} 行第 {exc.colno} 列：{exc.msg}"
        ) from exc
    except RecursionError as exc:
        raise StructuredParseError("JSON 嵌套层级过深，无法解析") from exc
    payload = _scalarize_json(dict(obj))
    state.fields.add(payload)
    yield _flush_batch(
        state,
        [
            StructuredRow(
                row_no=1,
                payload=payload,
                anomalies=_anomalies_for(payload),
                json_path="$",
            )
        ],
    )


# ── 入口 ────────────────────────────────────────────────────────────────────


def open_structured_stream(
    file_type: str, content: bytes, batch_size: int = DEFAULT_PARSE_BATCH_SIZE
) -> StructuredRecordStream:
    """按格式打开记录流；每次 ``next()`` 返回一批 ``StructuredRow``。

    失败抛 ``StructuredParseError``（带可读原因）。调用方在异常路径必须
    ``close()``，以释放 XLSX 工作簿；CSV/JSON 的迭代器关闭是幂等的。
    """
    if not content:
        raise StructuredParseError("文件内容为空")
    if batch_size < 1:
        raise ValueError("batch_size 必须大于 0")
    # ExtractError 也在这里统一转成本模块的异常类型，调用方只处理一种失败
    fmt = detect_structured_format(file_type)
    state = _ParseState(fmt)
    try:
        if fmt in ("csv", "tsv"):
            batches = _iter_delimited(fmt, content, batch_size, state)
        elif fmt == "xlsx":
            batches = _iter_xlsx(content, batch_size, state)
        else:
            batches = _iter_json(content, batch_size, state)
    except ExtractError as exc:
        raise StructuredParseError(str(exc)) from exc
    return StructuredRecordStream(batches, state)


def parse_structured_content(
    file_type: str, content: bytes, batch_size: int = DEFAULT_PARSE_BATCH_SIZE
) -> StructuredFile:
    """兼容旧调用方：一次性收集全部记录（大文件请改用 ``open_structured_stream``）。"""
    stream = open_structured_stream(file_type, content, batch_size)
    rows: list[StructuredRow] = []
    try:
        for batch in stream:
            rows.extend(batch)
        summary = stream.summary()
    finally:
        stream.close()
    return StructuredFile(
        detected_format=summary.detected_format,
        fields=summary.fields,
        rows=rows,
        encoding=summary.encoding,
        skipped_count=summary.skipped_count,
        truncated=summary.truncated,
        read_count=summary.read_count,
    )


__all__ = [
    "DEFAULT_PARSE_BATCH_SIZE",
    "MAX_KNOWLEDGE_TEXT_CHARS",
    "MAX_FIELD_VALUE_CHARS",
    "StructuredFile",
    "StructuredParseError",
    "StructuredParseSummary",
    "StructuredRecordStream",
    "StructuredRow",
    "STRUCTURED_FORMATS",
    "build_knowledge_text",
    "detect_structured_format",
    "infer_field_type",
    "is_structured_format",
    "open_structured_stream",
    "parse_structured_content",
]
