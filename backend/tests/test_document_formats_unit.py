"""10 种文档格式的单元守卫：格式集合、JSON/HTML 抽取与清洗、内容校验、表格切片、原子替换。

全部不依赖数据库与外部服务（运行 `python -m pytest tests/test_document_formats_unit.py`）。
"""

import re
import uuid

import pytest

from src.core.config import settings
from src.core.document_formats import (
    ALLOWED_EXTENSIONS,
    EDITABLE_EXTENSIONS,
    HTML_EXTENSIONS,
    IMAGE_EXTENSIONS,
    JSON_EXTENSIONS,
    OFFICE_EXTENSIONS,
    TABLE_EXTENSIONS,
    TEXT_EXTENSIONS,
    delimiter_for,
    is_image,
)
from src.infrastructure.document_extract import (
    ExtractError,
    JsonError,
    build_json_tree,
    extract_html_text,
    json_to_text,
    parse_json_bytes,
    sanitize_html,
)
from src.infrastructure.parser import FallbackParser
from src.infrastructure.storage import LocalStorage
from src.utils.chunking import chunk_delimited, chunk_document, estimate_tokens
from src.utils.file_validation import (
    validate_document_content,
    validate_html_text,
    validate_json_text,
    validate_text_document,
)

# 任务要求的 10 种格式
DOCUMENT_TYPES = {
    "docx",
    "pdf",
    "md",
    "txt",
    "csv",
    "tsv",
    "xlsx",
    "json",
    "html",
    "pptx",
}


# ---------- 格式集合 ----------


def test_allowed_extensions_cover_all_document_types():
    assert DOCUMENT_TYPES <= ALLOWED_EXTENSIONS
    assert "exe" not in ALLOWED_EXTENSIONS
    assert ALLOWED_EXTENSIONS == TEXT_EXTENSIONS | IMAGE_EXTENSIONS | OFFICE_EXTENSIONS


def test_only_six_text_types_are_editable():
    assert EDITABLE_EXTENSIONS == {"txt", "md", "csv", "tsv", "json", "html"}
    assert EDITABLE_EXTENSIONS == TEXT_EXTENSIONS
    # Office 与图片只读
    assert not (OFFICE_EXTENSIONS | IMAGE_EXTENSIONS) & EDITABLE_EXTENSIONS
    assert {"pdf", "docx", "xlsx", "pptx"} == OFFICE_EXTENSIONS


def test_json_html_and_table_classification():
    assert JSON_EXTENSIONS == {"json"}
    assert HTML_EXTENSIONS == {"html"}
    assert TABLE_EXTENSIONS == {"csv", "tsv"}
    assert delimiter_for("CSV") == ","
    assert delimiter_for("tsv") == "\t"
    assert delimiter_for("md") is None
    assert delimiter_for("") is None
    assert is_image("PNG") and not is_image("pdf")


# ---------- JSON ----------


def test_parse_json_bytes_roundtrip_and_encoding():
    obj, source = parse_json_bytes('{"方名":"四君子汤"}'.encode("utf-8"))
    assert obj == {"方名": "四君子汤"}
    assert source.startswith("{")


def test_parse_json_bytes_reports_position_on_invalid():
    with pytest.raises(JsonError) as exc:
        parse_json_bytes(b'{\n  "a": 1,\n}\n')
    message = str(exc.value)
    assert "JSON 格式非法" in message
    assert "第 2 行" in message and "列" in message, message


def test_json_to_text_keeps_key_names_and_hierarchy():
    text = json_to_text({"herb": {"name": "黄芪", "effects": ["补气", "固表"]}})
    assert "herb.name = " in text
    assert "herb.effects[0] = " in text
    assert "黄芪" in text and "固表" in text
    # 空容器也要留痕，避免切片丢失结构信息
    assert "a = []" in json_to_text({"a": []})


def test_build_json_tree_shape_and_truncation():
    nodes, truncated = build_json_tree({"a": {"b": [1, 2]}}, max_nodes=2000)
    assert truncated is False
    assert nodes[0]["key"] == "a"
    assert nodes[0]["title"] == "a（1）"  # 容器标题给出子项数量
    child = nodes[0]["children"][0]
    assert child["key"] == "a.b" and child["title"] == "b（2）"
    assert child["children"][0]["key"] == "a.b[0]"
    assert child["children"][1]["key"] == "a.b[1]"

    big = {f"k{i}": i for i in range(50)}
    limited, cut = build_json_tree(big, max_nodes=10)
    assert cut is True
    assert len(limited) == 10


def test_build_json_tree_handles_scalars_and_empty():
    assert build_json_tree([], max_nodes=10)[0][0]["title"] == "（空数据）"
    nodes, _ = build_json_tree("文本", max_nodes=10)
    assert nodes[0]["title"] == '"文本"'


# ---------- HTML ----------


def test_extract_html_text_structure_and_noise_removal():
    html = (
        "<html><head><title>标题不算正文</title>"
        "<style>body{color:red}</style></head>"
        "<body><h2>方剂</h2><p>益气健脾</p>"
        "<script>alert(1)</script>"
        "<ul><li>黄芪</li><li>白术</li></ul>"
        "<table><tr><th>药名</th><th>剂量</th></tr>"
        "<tr><td>黄芪</td><td>30g</td></tr></table></body></html>"
    ).encode("utf-8")
    text = extract_html_text(html)
    assert "## 方剂" in text
    assert "- 黄芪" in text
    assert "药名 | 剂量" in text
    assert "黄芪 | 30g" in text
    for noise in ("alert(1)", "color:red", "标题不算正文"):
        assert noise not in text


def test_extract_html_text_raises_when_no_body_text():
    with pytest.raises(ExtractError):
        extract_html_text("<html><head><title>只有标题</title></head><body></body></html>".encode("utf-8"))


def test_sanitize_html_strips_scripts_events_and_dangerous_urls():
    html = (
        '<div onclick="evil()"><script>alert(1)</script>'
        '<a href="javascript:alert(1)">link</a>'
        '<img src="x" onerror="evil()">'
        '<iframe src="https://evil.invalid"></iframe>'
        '<p>正常段落 &amp; 比较 &lt;b&gt;</p>'
        '<a href="https://example.com/ok">safe</a></div>'
    ).encode("utf-8")
    cleaned = sanitize_html(html)
    for danger in ("script", "onclick", "onerror", "javascript:", "iframe", "evil()"):
        assert danger not in cleaned, cleaned
    assert "<iframe" not in cleaned
    # 安全内容保留，文本被转义
    assert 'href="https://example.com/ok"' in cleaned
    assert ">link</a>" in cleaned  # 危险 href 被去掉，链接文字保留
    assert "正常段落" in cleaned
    assert "&lt;b&gt;" in cleaned


def test_sanitize_html_keeps_whitelisted_table_attributes():
    cleaned = sanitize_html(b"<table><tr><td colspan='2'>ab</td></tr></table>")
    assert "<table>" in cleaned and 'colspan="2"' in cleaned and "</td>" in cleaned


def test_sanitize_html_rejects_non_numeric_colspan_and_data_svg():
    cleaned = sanitize_html(b'<td colspan="javascript:1">x</td><img src="data:image/svg+xml,<svg/>">')
    assert "colspan" not in cleaned
    assert "data:image/svg" not in cleaned


# ---------- 内容校验 ----------


def test_validate_json_text():
    assert validate_json_text('{"a": [1, 2]}') is None
    assert "JSON 格式非法" in validate_json_text("{bad}")
    assert validate_json_text("") is not None  # 空源码不是合法 JSON


def test_validate_html_text_balancing():
    assert validate_html_text("<div><p>文本</p></div>") is None
    # 浏览器会隐式闭合的标签不误报
    assert validate_html_text("<ul><li>a<li>b</ul>") is None
    assert "未闭合" in validate_html_text("<div><p>未闭合")
    assert "多余的结束标签" in validate_html_text("<div></span></div>")


def test_validate_text_document_rejects_binary_and_bad_types():
    assert validate_text_document("md", "# 标题") is None
    assert "非法空字符" in validate_text_document("md", "含\x00字符")
    assert "JSON" in validate_text_document("json", "{oops")
    assert validate_text_document("csv", "a,b\n1,2") is None


def test_validate_document_content_magic_bytes():
    assert validate_document_content("pdf", b"%PDF-1.4 xx") is None
    assert "不匹配" in validate_document_content("pdf", b"not a pdf")
    assert validate_document_content("docx", b"PK\x03\x04zipzip") is None
    assert "不匹配" in validate_document_content("docx", b"%PDF-1.4")
    assert validate_document_content("png", b"\x89PNG\r\n\x1a\nxx") is None
    assert validate_document_content("webp", b"RIFF\x00\x00\x00\x00WEBPVP8 ") is None
    assert "不匹配" in validate_document_content("webp", b"RIFF\x00\x00\x00\x00JPEG")


def test_validate_document_content_text_types():
    assert validate_document_content("json", '{"a":1}'.encode()) is None
    assert "JSON" in validate_document_content("json", b'{"a":1,}')
    assert validate_document_content("html", b"<div>ok</div>") is None
    assert "未闭合" in validate_document_content("html", b"<div>oops")
    assert validate_document_content("csv", "姓名,部门".encode("gbk")) is None
    assert "二进制" in validate_document_content("md", b"a\x00b")
    assert "为空" in validate_document_content("md", b"")


# ---------- CSV/TSV 行感知切片 ----------


def _csv_text(rows: int, cell: str = "内容") -> str:
    body = "\n".join(f"{i},{cell * 40}" for i in range(1, rows + 1))
    return f"编号,说明\n{body}"


def test_chunk_delimited_repeats_header_and_keeps_rows_whole():
    text = _csv_text(60)
    chunks = chunk_delimited(text, ",")
    assert len(chunks) > 1, "内容足够长，应切成多块"
    for rc in chunks:
        assert rc.content.startswith("编号,说明\n"), rc.content[:40]
        assert rc.title_path and rc.title_path.startswith("表格 第")
        body_lines = rc.content.split("\n")[1:]
        assert body_lines, rc.content
        for line in body_lines:
            # 每行都以「编号,」开头 → 说明没有把一行从中间拆开
            assert line.split(",")[0].isdigit(), line[:40]
    # 行号连续且不重复
    covered = []
    for rc in chunks:
        numbers = re.findall(r"\d+", rc.title_path)
        assert numbers, rc.title_path
        covered.append((int(numbers[0]), int(numbers[-1])))
    assert covered[0][0] == 1
    for prev, nxt in zip(covered, covered[1:]):
        assert nxt[0] == prev[1] + 1
    assert covered[-1][1] == 60


def test_chunk_delimited_oversized_single_row_not_split():
    huge = "1," + "很长" * 4000
    chunks = chunk_delimited(f"编号,说明\n{huge}", ",")
    assert len(chunks) == 1
    assert chunks[0].content.endswith(huge)
    # 行号按数据行（不含表头）计数
    assert chunks[0].title_path == "表格 第 1 行"


def test_chunk_delimited_header_only():
    chunks = chunk_delimited("编号,说明\n", ",")
    assert len(chunks) == 1
    assert chunks[0].content == "编号,说明"
    assert chunks[0].title_path == "表格 表头"


def test_chunk_delimited_quoted_fields_keep_quotes():
    text = '姓名,备注\n张三,"含,逗号和""引号"""\n'
    chunks = chunk_delimited(text, ",")
    assert '"含,逗号和""引号"""' in chunks[0].content
    assert chunks[0].content.split("\n")[1].startswith("张三,")


def test_chunk_delimited_malformed_falls_back_to_generic_chunking():
    # 引号未闭合 → csv.Error → 回退通用切片，内容不丢
    broken = 'a,b\n"未闭合的引号,内容\n' + "补充" * 500
    chunks = chunk_delimited(broken, ",")
    assert chunks
    joined = "".join(rc.content for rc in chunks)
    assert "未闭合的引号" in joined
    for rc in chunks:
        assert rc.content.strip()


def test_chunk_delimited_index_is_sequential():
    chunks = chunk_delimited(_csv_text(30), ",")
    assert [rc.chunk_index for rc in chunks] == list(range(len(chunks)))


def test_chunk_document_still_handles_markdown():
    md = "# 一级\n\n## 二级\n\n" + ("正文内容。" * 300 + "\n\n") * 4
    chunks = chunk_document(md)
    assert chunks
    assert any("一级 > 二级" == rc.title_path for rc in chunks)
    assert estimate_tokens(chunks[0].content) > 0


# ---------- 解析分派 ----------


def test_fallback_parser_handles_json_and_html():
    parser = FallbackParser()
    assert "a.b[0] = 1" in parser.parse(b'{"a":{"b":[1,2]}}', "json")
    text = parser.parse("<h1>标题</h1><p>正文</p>".encode("utf-8"), "html")
    assert "# 标题" in text and "正文" in text
    pages = parser.parse_pages(b'{"k":"v"}', "json")
    assert pages == [(None, parser.parse(b'{"k":"v"}', "json"))]


def test_fallback_parser_rejects_invalid_json_and_empty_html():
    from src.infrastructure.parser import ParseError

    with pytest.raises(ParseError):
        parser = FallbackParser()
        parser.parse(b"{broken", "json")
    with pytest.raises(ParseError):
        FallbackParser().parse(b"<p></p>", "html")


# ---------- 原子替换 ----------


def test_local_storage_replace_writes_and_cleans_tmp(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "UPLOAD_DIR", str(tmp_path))
    storage = LocalStorage()
    key = f"kb-{uuid.uuid4().hex[:8]}/doc.md"
    storage.save(key, "# 旧内容".encode("utf-8"))
    storage.replace(key, "# 新内容很长".encode("utf-8") * 100)

    assert storage.load(key).startswith("# 新内容很长".encode("utf-8"))
    target = tmp_path / key
    assert target.exists()
    # 临时文件已清理，同目录只剩目标文件
    leftovers = [p.name for p in target.parent.iterdir() if ".tmp-" in p.name]
    assert leftovers == []


def test_local_storage_replace_keeps_original_on_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "UPLOAD_DIR", str(tmp_path))
    storage = LocalStorage()
    key = f"kb-{uuid.uuid4().hex[:8]}/doc.md"
    storage.save(key, "# 原内容".encode("utf-8"))

    import src.infrastructure.storage as storage_mod

    def boom(*args, **kwargs):
        raise OSError("模拟磁盘写入失败")

    monkeypatch.setattr(storage_mod.os, "replace", boom)
    with pytest.raises(OSError):
        storage.replace(key, "# 覆盖内容".encode("utf-8"))

    assert storage.load(key) == "# 原内容".encode("utf-8")
    target = tmp_path / key
    assert [p.name for p in target.parent.iterdir() if ".tmp-" in p.name] == []
