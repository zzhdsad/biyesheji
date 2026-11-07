"""站内预览数据构建：文本解码、表格解析、Office 内容抽取、JSON 树与安全 HTML。

返回结构直接供 GET /documents/{id}/preview 序列化；PDF/图片只返回类型，
由前端通过已鉴权的 /content 接口取 Blob 在页面内渲染。
HTML 走服务端白名单清洗 + 前端 sandbox iframe 两道防线，不执行上传文件脚本。
"""

import csv
import io
import json

from src.core.document_formats import (
    HTML_EXTENSIONS,
    IMAGE_EXTENSIONS,
    JSON_EXTENSIONS,
    TABLE_EXTENSIONS,
    TEXT_EXTENSIONS,
    delimiter_for,
)
from src.infrastructure.document_extract import (
    MAX_JSON_NODES,
    ExtractError,
    build_json_tree,
    extract_docx_blocks,
    extract_pptx_slides,
    extract_xlsx_sheets,
    sanitize_html,
)
from src.utils.text_encoding import TextDecodeError, decode_text

# 预览/编辑上限：超出后禁用编辑，避免「只保存了截断内容」造成数据丢失
MAX_EDIT_CHARS = 1_000_000
MAX_TABLE_ROWS = 500
MAX_TABLE_COLS = 50
MAX_DOCX_BLOCKS = 3000
MAX_PPTX_SLIDES = 200


def build_preview(file_name: str, file_type: str, content: bytes) -> dict:
    """按格式构建预览数据；无法解析时返回 kind=unsupported + 明确提示。"""
    file_type = (file_type or "").lower()
    payload: dict = {
        "file_name": file_name,
        "file_type": file_type,
        "kind": "unsupported",
        "editable": False,
        "encoding": None,
        "content": None,
        "rows": None,
        "delimiter": None,
        "blocks": None,
        "sheets": None,
        "slides": None,
        "tree": None,
        "html": None,
        "message": None,
        "truncated": False,
    }

    if file_type in ("pdf", "docx", "xlsx", "pptx"):
        payload["kind"] = file_type
        if file_type == "pdf":
            return payload
        if file_type == "docx":
            return _docx_preview(payload, content)
        if file_type == "xlsx":
            return _xlsx_preview(payload, content)
        return _pptx_preview(payload, content)

    if file_type in IMAGE_EXTENSIONS:
        payload["kind"] = "image"
        return payload

    if file_type in JSON_EXTENSIONS:
        return _json_preview(payload, content)

    if file_type in HTML_EXTENSIONS:
        return _html_preview(payload, content)

    if file_type in TEXT_EXTENSIONS:
        return _text_preview(payload, file_type, content)

    payload["message"] = f"暂不支持预览 .{file_type or '未知'} 格式，请下载后查看"
    return payload


def _source_preview_for_editing(payload: dict, content: bytes) -> bool:
    """JSON/HTML 共用的源码装载：解码、回填 encoding/content，超限时禁用编辑。

    返回 False 表示解码失败（message 已写入），调用方应直接返回。
    """
    try:
        text, encoding = decode_text(content)
    except TextDecodeError as exc:
        payload["message"] = str(exc)
        return False
    payload["encoding"] = encoding
    too_large = len(text) > MAX_EDIT_CHARS
    payload["content"] = text if not too_large else text[:MAX_EDIT_CHARS]
    payload["truncated"] = too_large
    payload["editable"] = not too_large
    if too_large:
        payload["message"] = "文件过大，已截断展示并禁用编辑；请下载后编辑完整文件"
    return True


def _json_preview(payload: dict, content: bytes) -> dict:
    """JSON：树形结构预览 + 可编辑源码；语法非法时给出定位并仍允许修正。"""
    payload["kind"] = "json"
    payload["tree"] = []
    if not _source_preview_for_editing(payload, content):
        return payload
    if payload["truncated"]:
        return payload  # 截断后的片段不是完整 JSON，只提示体积，不报语法错误
    try:
        obj = json.loads(payload["content"] or "")
    except json.JSONDecodeError as exc:
        payload["message"] = (
            f"JSON 格式非法：第 {exc.lineno} 行第 {exc.colno} 列：{exc.msg}"
        )
        return payload
    except RecursionError:
        payload["message"] = "JSON 嵌套层级过深，无法构建结构预览"
        return payload
    tree, truncated = build_json_tree(obj, MAX_JSON_NODES)
    payload["tree"] = tree
    if truncated:
        payload["message"] = f"节点较多，仅展示前 {MAX_JSON_NODES} 个"
    return payload


def _html_preview(payload: dict, content: bytes) -> dict:
    """HTML：清洗后的渲染内容 + 原始源码编辑（两者内容对应）。"""
    payload["kind"] = "html"
    if not _source_preview_for_editing(payload, content):
        return payload
    try:
        payload["html"] = sanitize_html(content)
    except ExtractError as exc:
        payload["message"] = str(exc)
    return payload


def _text_preview(payload: dict, file_type: str, content: bytes) -> dict:
    # 扩展名是文本但内容为二进制（如改名的 Office 文件）时明确拒绝，避免乱码
    if b"\x00" in content:
        payload["message"] = "文件包含二进制内容，无法按文本预览，请下载后查看"
        return payload
    try:
        text, encoding = decode_text(content)
    except TextDecodeError as exc:
        payload["message"] = str(exc)
        return payload

    payload["encoding"] = encoding
    if file_type in TABLE_EXTENSIONS:
        delimiter = delimiter_for(file_type) or ","
        payload["delimiter"] = delimiter
        payload["kind"] = "table"
        try:
            rows, truncated = _parse_table(text, delimiter)
        except csv.Error as exc:
            payload["kind"] = "text"
            payload["message"] = f"表格解析失败，已切换为文本预览：{exc}"
            rows, truncated = [], False
        payload["rows"] = rows
        too_large = len(text) > MAX_EDIT_CHARS
        payload["truncated"] = truncated or too_large
        payload["content"] = text if not too_large else text[:MAX_EDIT_CHARS]
        payload["editable"] = not payload["truncated"]
        if payload["truncated"] and not payload["message"]:
            payload["message"] = "文件过大，已截断展示并禁用编辑；请下载后编辑完整文件"
        return payload

    payload["kind"] = "text"
    too_large = len(text) > MAX_EDIT_CHARS
    payload["content"] = text if not too_large else text[:MAX_EDIT_CHARS]
    payload["truncated"] = too_large
    payload["editable"] = not too_large
    if too_large:
        payload["message"] = "文件过大，已截断展示并禁用编辑；请下载后编辑完整文件"
    return payload


def _parse_table(text: str, delimiter: str) -> tuple[list[list[str]], bool]:
    rows: list[list[str]] = []
    truncated = False
    reader = csv.reader(io.StringIO(text, newline=""), delimiter=delimiter)
    for row in reader:
        if len(rows) >= MAX_TABLE_ROWS:
            truncated = True
            break
        if not row or not any(cell.strip() for cell in row):
            continue
        if len(row) > MAX_TABLE_COLS:
            row = row[:MAX_TABLE_COLS]
            truncated = True
        rows.append(row)
    return rows, truncated


def _docx_preview(payload: dict, content: bytes) -> dict:
    try:
        blocks, truncated = extract_docx_blocks(content, MAX_DOCX_BLOCKS)
    except ExtractError as exc:
        payload["kind"] = "unsupported"
        payload["message"] = str(exc)
        return payload
    payload["blocks"] = blocks
    payload["truncated"] = truncated
    if truncated:
        payload["message"] = f"内容较长，仅展示前 {MAX_DOCX_BLOCKS} 段"
    elif not blocks:
        payload["message"] = "文档中未提取到文本内容"
    return payload


def _xlsx_preview(payload: dict, content: bytes) -> dict:
    try:
        sheets, truncated = extract_xlsx_sheets(
            content, MAX_TABLE_ROWS, MAX_TABLE_COLS
        )
    except ExtractError as exc:
        payload["kind"] = "unsupported"
        payload["message"] = str(exc)
        return payload
    payload["sheets"] = sheets
    payload["truncated"] = truncated
    if truncated:
        payload["message"] = (
            f"内容较长，每个工作表仅展示前 {MAX_TABLE_ROWS} 行 / "
            f"{MAX_TABLE_COLS} 列"
        )
    return payload


def _pptx_preview(payload: dict, content: bytes) -> dict:
    try:
        slides, truncated = extract_pptx_slides(content, MAX_PPTX_SLIDES)
    except ExtractError as exc:
        payload["kind"] = "unsupported"
        payload["message"] = str(exc)
        return payload
    payload["slides"] = slides
    payload["truncated"] = truncated
    if truncated:
        payload["message"] = f"演示文稿较长，仅展示前 {MAX_PPTX_SLIDES} 页"
    elif not any(slide["lines"] for slide in slides):
        payload["message"] = "演示文稿中未提取到可显示的文本"
    return payload
