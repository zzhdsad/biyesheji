"""通用导入：文件类型识别 + 结构化解析的单元测试（小规模数据）。

覆盖 TASK 测试用例「普通导入」中的解析部分：
- 单个 TXT / CSV / TSV / JSON / JSONL / XLSX / Parquet 解析
- 错误编码、空文件、损坏文件
- 行号溯源、原始字段保留、ragged 行不丢数据
"""

from __future__ import annotations

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from openpyxl import Workbook

from src.application.file_type_service import (
    FORMAT_CSV,
    FORMAT_JSON,
    FORMAT_JSONL,
    FORMAT_MD,
    FORMAT_PARQUET,
    FORMAT_PDF,
    FORMAT_TSV,
    FORMAT_TXT,
    FORMAT_XLSX,
    detect_encoding,
    detect_format,
    is_structured,
)
from src.application.tabular_parser import TabularParseError, count_rows, iter_rows

HERB_ROWS = [
    {"名称": "麻黄", "性味": "辛温", "来源": "神农本草经"},
    {"名称": "桂枝", "性味": "辛甘温", "来源": "伤寒论"},
]


# ============================================================================
# 文件类型识别
# ============================================================================


@pytest.mark.parametrize(
    ("file_name", "header", "expected"),
    [
        ("a.pdf", b"%PDF-1.7\n", FORMAT_PDF),
        ("a.parquet", b"PAR1....", FORMAT_PARQUET),
        ("记录.csv", "名称,性味\n".encode("utf-8"), FORMAT_CSV),
        ("记录.tsv", "名称\t性味\n".encode("utf-8"), FORMAT_TSV),
        ("记录.txt", "随便一段正文".encode("utf-8"), FORMAT_TXT),
        ("记录.md", "# 标题".encode("utf-8"), FORMAT_MD),
        ("a.json", b'[{"a": 1}]', FORMAT_JSON),
        ("a.jsonl", b'{"a": 1}\n{"a": 2}\n', FORMAT_JSONL),
        ("未知.bin", b"\x00\x01\x02\x03", ""),
        # 魔数优先于后缀：后缀错了也能识别
        ("被改名.csv", b"%PDF-1.7\n", FORMAT_PDF),
    ],
)
def test_detect_format_by_magic_and_ext(file_name: str, header: bytes, expected: str) -> None:
    assert detect_format(file_name, header) == expected


def test_detect_encoding_of_gbk_content() -> None:
    raw = "名称,性味\n麻黄,辛温\n".encode("gb18030")
    encoding = detect_encoding(raw)
    # 不断言具体名称（识别库可能返回 gb18030/gbk），但必须识别出可用编码
    assert encoding
    assert raw.decode(encoding).startswith("名称")


def test_detect_encoding_of_empty_bytes() -> None:
    assert detect_encoding(b"") == ""


def test_structured_classification() -> None:
    assert is_structured(FORMAT_CSV)
    assert is_structured(FORMAT_XLSX)
    assert not is_structured(FORMAT_PDF)


# ============================================================================
# 解析：正常路径
# ============================================================================


def test_parse_csv_preserves_fields_and_line_numbers(tmp_path) -> None:
    path = tmp_path / "herbs.csv"
    path.write_text("名称,性味,来源\n麻黄,辛温,神农本草经\n桂枝,辛甘温,伤寒论\n", encoding="utf-8")

    rows = list(iter_rows(path, FORMAT_CSV))

    assert [r.row_no for r in rows] == [2, 3]
    assert rows[0].data == {"名称": "麻黄", "性味": "辛温", "来源": "神农本草经"}
    assert rows[1].data == {"名称": "桂枝", "性味": "辛甘温", "来源": "伤寒论"}


def test_parse_tsv(tmp_path) -> None:
    path = tmp_path / "herbs.tsv"
    path.write_text("名称\t性味\n麻黄\t辛温\n", encoding="utf-8")

    rows = list(iter_rows(path, FORMAT_TSV))

    assert rows[0].data == {"名称": "麻黄", "性味": "辛温"}


def test_parse_gbk_csv_with_detected_encoding(tmp_path) -> None:
    path = tmp_path / "gbk.csv"
    path.write_bytes("名称,性味\n麻黄,辛温\n".encode("gb18030"))
    encoding = detect_encoding(path.read_bytes())

    rows = list(iter_rows(path, FORMAT_CSV, encoding))

    assert rows[0].data == {"名称": "麻黄", "性味": "辛温"}


def test_parse_json_array(tmp_path) -> None:
    path = tmp_path / "herbs.json"
    path.write_text('[{"名称":"麻黄","性味":"辛温"},{"名称":"桂枝","性味":"辛甘温"}]', encoding="utf-8")

    rows = list(iter_rows(path, FORMAT_JSON))

    assert [r.row_no for r in rows] == [1, 2]
    assert rows[0].data == {"名称": "麻黄", "性味": "辛温"}


def test_parse_jsonl_uses_physical_line_number(tmp_path) -> None:
    path = tmp_path / "herbs.jsonl"
    path.write_text('{"名称":"麻黄"}\n\n{"名称":"桂枝"}\n', encoding="utf-8")

    rows = list(iter_rows(path, FORMAT_JSONL))

    # 空行被跳过，但行号保留物理行号（1 和 3）
    assert [r.row_no for r in rows] == [1, 3]


def test_parse_xlsx(tmp_path) -> None:
    path = tmp_path / "herbs.xlsx"
    workbook = Workbook()
    worksheet = workbook.active
    worksheet.append(["名称", "性味", "来源"])
    for row in HERB_ROWS:
        worksheet.append([row["名称"], row["性味"], row["来源"]])
    workbook.save(path)

    rows = list(iter_rows(path, FORMAT_XLSX))

    assert [r.row_no for r in rows] == [2, 3]
    assert rows[0].data == HERB_ROWS[0]


def test_parse_parquet(tmp_path) -> None:
    path = tmp_path / "herbs.parquet"
    table = pa.table(
        {
            "名称": [r["名称"] for r in HERB_ROWS],
            "性味": [r["性味"] for r in HERB_ROWS],
            "来源": [r["来源"] for r in HERB_ROWS],
        }
    )
    pq.write_table(table, path)

    rows = list(iter_rows(path, FORMAT_PARQUET))

    assert [r.row_no for r in rows] == [1, 2]
    assert rows[0].data == HERB_ROWS[0]


# ============================================================================
# 解析：异常路径（必须给出可定位的错误）
# ============================================================================


def test_empty_file_reports_error(tmp_path) -> None:
    path = tmp_path / "empty.csv"
    path.write_bytes(b"")

    with pytest.raises(TabularParseError) as exc:
        list(iter_rows(path, FORMAT_CSV))

    assert "空" in exc.value.message


def test_broken_json_reports_line_number(tmp_path) -> None:
    path = tmp_path / "broken.json"
    path.write_text('[{"a": 1}, {"a": 2}', encoding="utf-8")

    with pytest.raises(TabularParseError) as exc:
        list(iter_rows(path, FORMAT_JSON))

    assert "JSON 解析失败" in exc.value.message


def test_broken_jsonl_reports_line_number(tmp_path) -> None:
    path = tmp_path / "broken.jsonl"
    path.write_text('{"a": 1}\n{"a": 2\n', encoding="utf-8")

    with pytest.raises(TabularParseError) as exc:
        list(iter_rows(path, FORMAT_JSONL))

    assert exc.value.row_no == 2


def test_corrupt_parquet_reports_error(tmp_path) -> None:
    path = tmp_path / "broken.parquet"
    path.write_bytes(b"PAR1" + b"\x00" * 32)

    with pytest.raises(TabularParseError) as exc:
        list(iter_rows(path, FORMAT_PARQUET))

    assert "损坏" in exc.value.message


def test_non_structured_format_refused(tmp_path) -> None:
    path = tmp_path / "a.txt"
    path.write_text("正文", encoding="utf-8")

    with pytest.raises(TabularParseError) as exc:
        list(iter_rows(path, FORMAT_TXT))

    assert "不是结构化格式" in exc.value.message


# ============================================================================
# 数据完整性：不丢字段、不静默丢弃数据
# ============================================================================


def test_ragged_row_keeps_extra_cells(tmp_path) -> None:
    path = tmp_path / "ragged.csv"
    path.write_text("a,b\n1,2,3\n", encoding="utf-8")

    rows = list(iter_rows(path, FORMAT_CSV))

    assert rows[0].data["_extra_cells"] == ["3"]
    assert rows[0].data["a"] == "1"


def test_blank_and_duplicate_headers_are_normalized(tmp_path) -> None:
    path = tmp_path / "dup.csv"
    path.write_text("名称,,名称\n麻黄,x,别名\n", encoding="utf-8")

    rows = list(iter_rows(path, FORMAT_CSV))

    assert rows[0].data == {"名称": "麻黄", "col_2": "x", "名称_1": "别名"}


def test_blank_lines_are_skipped_without_shifting_line_numbers(tmp_path) -> None:
    path = tmp_path / "blank.csv"
    path.write_text("a\n1\n\n2\n", encoding="utf-8")

    rows = list(iter_rows(path, FORMAT_CSV))

    assert [r.row_no for r in rows] == [2, 4]


def test_count_rows_returns_zero_and_error_on_bad_file(tmp_path) -> None:
    path = tmp_path / "bad.csv"
    path.write_bytes(b"")

    total, error = count_rows(path, FORMAT_CSV)

    assert total == 0
    assert error


def test_count_rows_normal(tmp_path) -> None:
    path = tmp_path / "herbs.csv"
    path.write_text("a\n1\n2\n3\n", encoding="utf-8")

    total, error = count_rows(path, FORMAT_CSV)

    assert (total, error) == (3, None)


def test_max_rows_limits_output(tmp_path) -> None:
    path = tmp_path / "many.csv"
    path.write_text("a\n" + "\n".join(str(i) for i in range(1, 101)) + "\n", encoding="utf-8")

    rows = list(iter_rows(path, FORMAT_CSV, max_rows=5))

    assert len(rows) == 5
    assert [r.row_no for r in rows] == [2, 3, 4, 5, 6]
