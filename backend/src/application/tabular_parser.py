"""统一结构化文件解析（普通导入 / 关联导入共用）。

要解决的问题：
- 现有代码里 dataset_scanner 与 upload_import_service 各自实现了一套
  CSV/TSV/JSON/JSONL/XLSX/Parquet 读取，行为不一致且 XLSX 依赖缺失；
- 表格曾被无条件拼成整段文本塞进 Document/Chunk，**原始 Schema 与记录全部丢失**；
- 解析失败没有行号信息，前端只能显示「导入失败」而无法定位到具体行。

本模块统一按「记录」解析，输出 (行号, 字段字典)，保证：
1. 字段与值原样保留（不丢字段、不猜类型、不拼接成文本块）；
2. 错误一律带行号，可直接展示给用户在第几行出问题；
3. 流式迭代，大文件不整表载入内存。

依赖：标准库 csv/json + openpyxl（XLSX）+ pyarrow（Parquet）。
"""

from __future__ import annotations

import csv
import json
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from loguru import logger

from src.application.file_type_service import (
    FORMAT_CSV,
    FORMAT_JSON,
    FORMAT_JSONL,
    FORMAT_PARQUET,
    FORMAT_TSV,
    FORMAT_XLSX,
)

# 表头缺失列名时的占位前缀
_COLUMN_PREFIX = "col_"
# 行内超出表头的单元格存放键（避免静默丢数据）
_EXTRA_KEY = "_extra_cells"


class TabularParseError(Exception):
    """结构化文件解析失败。

    row_no 为源文件中的行号（1 基），用于前端错误定位。
    """

    def __init__(self, message: str, row_no: int | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.row_no = row_no


@dataclass(frozen=True)
class ParsedRow:
    """一行解析结果：源行号 + 字段字典。"""

    row_no: int
    data: dict[str, Any]


def _normalize(value: Any) -> Any:
    """把各种来源的单元格值规范化为 JSON 可序列化类型。

    - 时间类型统一转 ISO 字符串（保留可读性与可比较性）
    - Decimal 转 float
    - list/dict 递归处理（JSON 字段里的嵌套结构原样保留）
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return value
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, (list, tuple)):
        return [_normalize(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _normalize(v) for k, v in value.items()}
    return str(value)


def _make_header(cells: list[Any]) -> list[str]:
    """生成规范字段名：去 BOM/空白，空列名占位，重名自动加序号。"""
    fields: list[str] = []
    seen: dict[str, int] = {}
    for idx, raw in enumerate(cells, start=1):
        name = str(raw or "").strip().lstrip("\ufeff")
        if not name:
            name = f"{_COLUMN_PREFIX}{idx}"
        if name in seen:
            seen[name] += 1
            name = f"{name}_{seen[name]}"
        else:
            seen[name] = 0
        fields.append(name)
    return fields


def _zip_row(fields: list[str], values: list[Any]) -> dict[str, Any]:
    """按表头组装一行：缺值补空串，多余单元格存入 _extra_cells（不静默丢弃）。"""
    data: dict[str, Any] = {}
    for idx, name in enumerate(fields):
        data[name] = _normalize(values[idx]) if idx < len(values) else ""
    extras = [_normalize(v) for v in values[len(fields) :]]
    if extras:
        data[_EXTRA_KEY] = extras
    return data


def _is_blank(values: list[Any]) -> bool:
    return all(v is None or (isinstance(v, str) and not v.strip()) for v in values)


def _iter_delimited(
    path: Path,
    fmt: str,
    encoding: str,
    max_rows: int | None,
    on_error: Callable[[int, str], None] | None = None,
) -> Iterator[ParsedRow]:
    delimiter = "\t" if fmt == FORMAT_TSV else ","
    try:
        handle = path.open("r", encoding=encoding, newline="")
    except LookupError as exc:
        raise TabularParseError(f"不支持的编码：{encoding}（{exc}") from exc

    with handle:
        reader = csv.reader(handle, delimiter=delimiter)
        try:
            header = next(reader)
        except StopIteration:
            raise TabularParseError("文件为空：未找到表头行") from None
        except UnicodeDecodeError as exc:
            raise TabularParseError(
                f"编码 {encoding} 无法解码文件（第 {exc.start + 1} 字节处）", row_no=1
            ) from exc
        except UnicodeError as exc:
            # 例如按 utf-16 打开无 BOM 的文件会抛 UnicodeError；
            # 必须转成可展示的解析错误，否则整批导入会因单个文件中断。
            raise TabularParseError(
                f"编码 {encoding} 打开文件失败：{exc}（建议确认文件实际编码）", row_no=1
            ) from exc
        except csv.Error as exc:
            raise TabularParseError(f"表头解析失败：{exc}", row_no=1) from exc

        fields = _make_header(header)
        emitted = 0
        for line_no, values in enumerate(reader, start=2):
            if max_rows is not None and emitted >= max_rows:
                break
            try:
                if _is_blank(values):
                    continue
                yield ParsedRow(row_no=line_no, data=_zip_row(fields, values))
                emitted += 1
            except UnicodeDecodeError as exc:
                message = f"第 {line_no} 行编码错误（{encoding} 无法解码，位置 {exc.start}）"
                if on_error is not None:
                    on_error(line_no, message)
                    continue
                raise TabularParseError(message, row_no=line_no) from exc
            except csv.Error as exc:
                message = f"第 {line_no} 行解析失败：{exc}"
                if on_error is not None:
                    on_error(line_no, message)
                    continue
                raise TabularParseError(message, row_no=line_no) from exc


def _iter_json(path: Path, encoding: str, max_rows: int | None) -> Iterator[ParsedRow]:
    try:
        raw = path.read_text(encoding=encoding)
    except UnicodeDecodeError as exc:
        raise TabularParseError(
            f"编码 {encoding} 无法解码文件（位置 {exc.start}）"
        ) from exc
    except LookupError as exc:
        raise TabularParseError(f"不支持的编码：{encoding}（{exc}") from exc

    if not raw.strip():
        raise TabularParseError("文件为空：JSON 内容为空")

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise TabularParseError(
            f"JSON 解析失败（第 {exc.lineno} 行第 {exc.colno} 列）：{exc.msg}",
            row_no=exc.lineno,
        ) from exc

    if isinstance(payload, dict):
        items: list[Any] = [payload]
    elif isinstance(payload, list):
        items = payload
    else:
        raise TabularParseError(
            f"JSON 顶层结构不支持：{type(payload).__name__}（应为数组或对象）"
        )

    for idx, item in enumerate(items, start=1):
        if max_rows is not None and idx > max_rows:
            break
        if not isinstance(item, dict):
            raise TabularParseError(
                f"第 {idx} 条记录不是对象：{type(item).__name__}", row_no=idx
            )
        yield ParsedRow(row_no=idx, data={str(k): _normalize(v) for k, v in item.items()})


def _iter_jsonl(
    path: Path,
    encoding: str,
    max_rows: int | None,
    on_error: Callable[[int, str], None] | None = None,
) -> Iterator[ParsedRow]:
    try:
        handle = path.open("r", encoding=encoding)
    except LookupError as exc:
        raise TabularParseError(f"不支持的编码：{encoding}（{exc}") from exc

    with handle:
        emitted = 0
        for line_no, line in enumerate(handle, start=1):
            if max_rows is not None and emitted >= max_rows:
                break
            stripped = line.strip()
            if not stripped:
                continue
            try:
                obj = json.loads(stripped)
            except UnicodeDecodeError as exc:
                raise TabularParseError(
                    f"第 {line_no} 行编码错误（{encoding} 无法解码，位置 {exc.start}）", row_no=line_no
                ) from exc
            except json.JSONDecodeError as exc:
                message = f"第 {line_no} 行 JSON 解析失败：{exc.msg}"
                # 给了 on_error 就跳过坏行继续：一个坏行不该让整个文件几千条数据全部丢失
                if on_error is not None:
                    on_error(line_no, message)
                    continue
                raise TabularParseError(message, row_no=line_no) from exc
            if not isinstance(obj, dict):
                message = f"第 {line_no} 行不是对象：{type(obj).__name__}"
                if on_error is not None:
                    on_error(line_no, message)
                    continue
                raise TabularParseError(message, row_no=line_no) from None
            yield ParsedRow(
                row_no=line_no, data={str(k): _normalize(v) for k, v in obj.items()}
            )
            emitted += 1


def _iter_xlsx(path: Path, max_rows: int | None, sheet: str | None) -> Iterator[ParsedRow]:
    try:
        from openpyxl import load_workbook
    except ImportError as exc:  # pragma: no cover - 依赖缺失时才会触发
        raise TabularParseError("缺少 openpyxl 依赖，无法解析 XLSX 文件") from exc

    try:
        workbook = load_workbook(filename=str(path), read_only=True, data_only=True)
    except Exception as exc:
        raise TabularParseError(f"XLSX 文件损坏或不是有效工作簿：{exc}") from exc

    try:
        worksheet = workbook[sheet] if sheet else workbook.worksheets[0]
        rows = worksheet.iter_rows(values_only=True)
        try:
            header = next(rows)
        except StopIteration:
            raise TabularParseError("工作表为空：未找到表头行") from None

        fields = _make_header(list(header))
        emitted = 0
        for row_no, values in enumerate(rows, start=2):
            if max_rows is not None and emitted >= max_rows:
                break
            if values is None or _is_blank(list(values)):
                continue
            yield ParsedRow(row_no=row_no, data=_zip_row(fields, list(values)))
            emitted += 1
    finally:
        workbook.close()


def _iter_parquet(path: Path, max_rows: int | None) -> Iterator[ParsedRow]:
    try:
        import pyarrow.parquet as pq
    except ImportError as exc:  # pragma: no cover - 依赖缺失时才会触发
        raise TabularParseError("缺少 pyarrow 依赖，无法解析 Parquet 文件") from exc

    try:
        parquet_file = pq.ParquetFile(str(path))
    except Exception as exc:
        raise TabularParseError(f"Parquet 文件损坏或不是有效文件：{exc}") from exc

    fields = [name for name in parquet_file.schema_arrow.names]
    row_no = 0
    try:
        for batch in parquet_file.iter_batches(batch_size=2048):
            for record in batch.to_pylist():
                row_no += 1
                if max_rows is not None and row_no > max_rows:
                    return
                yield ParsedRow(
                    row_no=row_no,
                    data={name: _normalize(record.get(name)) for name in fields},
                )
    except Exception as exc:
        raise TabularParseError(f"Parquet 第 {row_no + 1} 行读取失败：{exc}", row_no=row_no + 1) from exc


def iter_rows(
    path: str | Path,
    fmt: str,
    encoding: str = "",
    *,
    sheet: str | None = None,
    max_rows: int | None = None,
    on_error: Callable[[int, str], None] | None = None,
) -> Iterator[ParsedRow]:
    """按规范格式逐行解析结构化文件。

    行号语义：文本类为文件物理行号；XLSX 为工作表行号；Parquet 为记录序号（均 1 基）。

    on_error：给了回调时，单行级的解析错误（JSONL 畸形行、CSV 行的编码/格式错误）
    会报告给回调并**跳过继续**，而不是中断整个文件——真实数据集里几行脏数据
    不该让同文件其余几千条记录一起丢失。表头级错误仍然抛出（没有表头无法继续）。
    """
    target = Path(path)
    if not target.exists():
        raise TabularParseError(f"文件不存在：{target}")
    if target.stat().st_size == 0:
        raise TabularParseError("文件为空（0 字节）")

    encoding = encoding or "utf-8"
    if fmt in (FORMAT_CSV, FORMAT_TSV):
        yield from _iter_delimited(target, fmt, encoding, max_rows, on_error)
    elif fmt == FORMAT_JSON:
        yield from _iter_json(target, encoding, max_rows)
    elif fmt == FORMAT_JSONL:
        yield from _iter_jsonl(target, encoding, max_rows, on_error)
    elif fmt == FORMAT_XLSX:
        yield from _iter_xlsx(target, max_rows, sheet)
    elif fmt == FORMAT_PARQUET:
        yield from _iter_parquet(target, max_rows)
    else:
        raise TabularParseError(f"格式 {fmt} 不是结构化格式，无法按记录解析")


def count_rows(path: str | Path, fmt: str, encoding: str = "") -> tuple[int, str | None]:
    """统计记录数，返回 (行数, 错误信息)。不抛异常，便于扫描阶段批量处理。"""
    try:
        total = sum(1 for _ in iter_rows(path, fmt, encoding))
    except TabularParseError as exc:
        logger.warning(f"[tabular_parser] 统计行数失败：{exc.message}")
        return 0, exc.message
    return total, None
