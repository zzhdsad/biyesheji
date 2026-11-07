"""文档内容抽取（DOCX / XLSX / PPTX / JSON / HTML），供解析入库与站内预览共用。

不引入重量级 Office 渲染依赖：
- DOCX：python-docx
- XLSX：openpyxl（read_only 流式读取）
- PPTX / HTML / JSON：标准库（zipfile + XML / html.parser / json）

HTML 预览用 sanitize_html 做白名单清洗（去脚本、事件属性与危险协议），
前端再套一层 sandbox iframe；两道防线都不依赖第三方库。
"""

import html
import io
import json
import re
import zipfile
from html.parser import HTMLParser
from xml.etree import ElementTree

from src.utils.text_encoding import TextDecodeError, decode_text

_DRAWING_NS = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
_SLIDE_RE = re.compile(r"^ppt/slides/slide(\d+)\.xml$")
# 限制单页/整包解压体积，避免压缩炸弹在解析阶段耗尽内存
_MAX_SLIDE_XML_BYTES = 10 * 1024 * 1024
_MAX_TOTAL_XML_BYTES = 100 * 1024 * 1024


class ExtractError(Exception):
    """Office 文档内容抽取失败。"""


def _heading_level(style_name: str) -> int | None:
    style = (style_name or "").strip().lower()
    if not style.startswith("heading"):
        return None
    try:
        return min(max(int(style.split()[-1]), 1), 6)
    except (ValueError, IndexError):
        return 2


def extract_docx_blocks(
    content: bytes, max_blocks: int | None = None
) -> tuple[list[dict], bool]:
    """抽取 DOCX 段落与表格，返回 (blocks, truncated)。

    block: {"type": "heading"|"paragraph", "level": int|None, "text": str}
    """
    from docx import Document as DocxDocument

    document = DocxDocument(io.BytesIO(content))
    blocks: list[dict] = []
    truncated = False

    def append(block: dict) -> None:
        nonlocal truncated
        if max_blocks is not None and len(blocks) >= max_blocks:
            truncated = True
            return
        blocks.append(block)

    try:
        for para in document.paragraphs:
            text = para.text.strip()
            if not text:
                continue
            level = _heading_level(para.style.name if para.style else "")
            if level is not None:
                append({"type": "heading", "level": level, "text": text})
            else:
                append({"type": "paragraph", "level": None, "text": text})
        # 表格按行拼接为段落，保证预览/入库都能看到表格文字
        for table in document.tables:
            for row in table.rows:
                cells = [cell.text.strip() for cell in row.cells]
                if any(cells):
                    append(
                        {
                            "type": "paragraph",
                            "level": None,
                            "text": " | ".join(cells),
                        }
                    )
    except Exception as exc:  # noqa: BLE001 - 统一转换为可读错误
        raise ExtractError(f"DOCX 内容读取失败：{exc}") from exc
    return blocks, truncated


def extract_xlsx_sheets(
    content: bytes,
    max_rows: int | None = None,
    max_cols: int | None = None,
) -> tuple[list[dict], bool]:
    """抽取 XLSX 工作表，返回 (sheets, truncated)。

    sheet: {"name": str, "rows": list[list[str]]}
    """
    from openpyxl import load_workbook

    try:
        workbook = load_workbook(io.BytesIO(content), read_only=True, data_only=True)
    except Exception as exc:  # noqa: BLE001
        raise ExtractError(f"XLSX 内容读取失败：{exc}") from exc

    sheets: list[dict] = []
    truncated = False
    try:
        for sheet in workbook.worksheets:
            rows: list[list[str]] = []
            for row in sheet.iter_rows(values_only=True):
                if max_rows is not None and len(rows) >= max_rows:
                    truncated = True
                    break
                values = list(row)
                if max_cols is not None and len(values) > max_cols:
                    values = values[:max_cols]
                    truncated = True
                texts = [_cell_text(value) for value in values]
                if any(texts):
                    rows.append(texts)
            sheets.append({"name": sheet.title, "rows": rows})
    except Exception as exc:  # noqa: BLE001
        raise ExtractError(f"XLSX 内容读取失败：{exc}") from exc
    finally:
        workbook.close()
    return sheets, truncated


def extract_pptx_slides(
    content: bytes, max_slides: int | None = None
) -> tuple[list[dict], bool]:
    """抽取 PPTX 每页文本，返回 (slides, truncated)。

    slide: {"index": int, "lines": list[str]}
    """
    try:
        archive = zipfile.ZipFile(io.BytesIO(content))
    except zipfile.BadZipFile as exc:
        raise ExtractError("PPTX 文件损坏或不是有效的演示文稿") from exc

    truncated = False
    slides: list[dict] = []
    try:
        entries: list[tuple[int, str]] = []
        total_bytes = 0
        for name in archive.namelist():
            match = _SLIDE_RE.match(name)
            if not match:
                continue
            size = archive.getinfo(name).file_size
            if size > _MAX_SLIDE_XML_BYTES:
                raise ExtractError("PPTX 单页内容过大，无法预览")
            total_bytes += size
            if total_bytes > _MAX_TOTAL_XML_BYTES:
                raise ExtractError("PPTX 内容过大，无法预览")
            entries.append((int(match.group(1)), name))
        entries.sort()
        for index, name in entries:
            if max_slides is not None and len(slides) >= max_slides:
                truncated = True
                break
            root = ElementTree.fromstring(archive.read(name))
            lines = [
                node.text.strip()
                for node in root.iter(f"{_DRAWING_NS}t")
                if node.text and node.text.strip()
            ]
            slides.append({"index": index, "lines": lines})
    except (ElementTree.ParseError, KeyError) as exc:
        raise ExtractError(f"PPTX 内容读取失败：{exc}") from exc
    finally:
        archive.close()
    return slides, truncated


def _cell_text(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()


# ── JSON ─────────────────────────────────────────────────────────────────────

MAX_JSON_NODES = 2000
MAX_JSON_VALUE_CHARS = 500
# 结构化数据的章节标题，让切片器能把 JSON 按顶层字段分组溯源
_JSON_TEXT_HEADER = "# JSON 结构化数据"


class JsonError(ExtractError):
    """JSON 解码/解析失败（沿用 ExtractError，路由统一按 400/unsupported 处理）。"""


def parse_json_bytes(content: bytes) -> tuple[object, str]:
    """解码并解析 JSON，返回 (对象, 源文本)。

    Raises:
        JsonError: 编码无法识别或 JSON 语法非法（含行列位置）
    """
    try:
        text, _encoding = decode_text(content)
    except TextDecodeError as exc:
        raise JsonError(f"JSON 文件解码失败：{exc}") from exc
    try:
        return json.loads(text), text
    except json.JSONDecodeError as exc:
        raise JsonError(f"JSON 格式非法：第 {exc.lineno} 行第 {exc.colno} 列：{exc.msg}") from exc
    except RecursionError as exc:
        raise JsonError("JSON 嵌套层级过深，无法解析") from exc


def _child_path(parent: str, key: object) -> str:
    if isinstance(key, int):
        return f"{parent}[{key}]"
    return f"{parent}.{key}" if parent else str(key)


def _scalar_text(value: object) -> str:
    rendered = json.dumps(value, ensure_ascii=False) if value is not None else "null"
    if len(rendered) > MAX_JSON_VALUE_CHARS:
        rendered = rendered[:MAX_JSON_VALUE_CHARS] + "…"
    return rendered


def json_to_text(obj: object) -> str:
    """把 JSON 展平成 `字段路径 = 值` 文本，保留键名与层级（供 RAG 切片）。"""
    lines: list[str] = [_JSON_TEXT_HEADER]
    _flatten_json(obj, "", lines)
    return "\n".join(lines)


def _flatten_json(node: object, path: str, lines: list[str]) -> None:
    label = path or "$"
    if isinstance(node, dict):
        if not node:
            lines.append(f"{label} = {{}}")
            return
        for key, value in node.items():
            _flatten_json(value, _child_path(path, key), lines)
    elif isinstance(node, list):
        if not node:
            lines.append(f"{label} = []")
            return
        for index, value in enumerate(node):
            _flatten_json(value, _child_path(path, index), lines)
    else:
        lines.append(f"{label} = {_scalar_text(node)}")


def build_json_tree(obj: object, max_nodes: int = MAX_JSON_NODES) -> tuple[list[dict], bool]:
    """构建 Ant Design Tree 可直接渲染的节点列表，返回 (nodes, truncated)。"""
    counter = [0]
    truncated = [False]

    def build(node: object, path: str) -> list[dict]:
        nodes: list[dict] = []
        if isinstance(node, dict):
            for key, value in node.items():
                child_path = _child_path(path, key)
                if counter[0] >= max_nodes:
                    truncated[0] = True
                    break
                counter[0] += 1
                if isinstance(value, (dict, list)) and value:
                    nodes.append(
                        {
                            "key": child_path,
                            "title": f"{key}（{len(value)}）",
                            "children": build(value, child_path),
                        }
                    )
                else:
                    nodes.append({"key": child_path, "title": f"{key}: {_scalar_text(value)}"})
        elif isinstance(node, list):
            for index, value in enumerate(node):
                child_path = _child_path(path, index)
                if counter[0] >= max_nodes:
                    truncated[0] = True
                    break
                counter[0] += 1
                if isinstance(value, (dict, list)) and value:
                    nodes.append(
                        {
                            "key": child_path,
                            "title": f"[{index}]（{len(value)}）",
                            "children": build(value, child_path),
                        }
                    )
                else:
                    nodes.append({"key": child_path, "title": f"[{index}]: {_scalar_text(value)}"})
        else:
            nodes.append({"key": path or "$", "title": _scalar_text(node)})
        return nodes

    if isinstance(obj, (dict, list)) and not obj:
        return [{"key": "$", "title": "（空数据）"}], False
    nodes = build(obj, "")
    if not nodes:
        return [{"key": "$", "title": _scalar_text(obj)}], truncated[0]
    return nodes, truncated[0]


# ── HTML ─────────────────────────────────────────────────────────────────────

# 这些标签的内容不是正文，抽取/清洗时连同子树一起丢弃（脚本、样式、媒体数据）
_HTML_DROP_TAGS = frozenset(
    {
        "script",
        "style",
        "noscript",
        "template",
        "svg",
        "math",
        "iframe",
        "frame",
        "frameset",
        "object",
        "embed",
        "applet",
        "form",
        "canvas",
        "audio",
        "video",
        "head",
    }
)
_HTML_BLOCK_TAGS = frozenset(
    {
        "address",
        "article",
        "aside",
        "blockquote",
        "div",
        "dl",
        "dt",
        "dd",
        "figure",
        "figcaption",
        "footer",
        "header",
        "hr",
        "main",
        "nav",
        "ol",
        "p",
        "section",
        "table",
        "thead",
        "tbody",
        "tfoot",
        "caption",
        "ul",
        "li",
        "tr",
        "br",
        "pre",
    }
)
_HTML_HEADINGS = {"h1": 1, "h2": 2, "h3": 3, "h4": 4, "h5": 5, "h6": 6}
_HTML_ALLOWED_TAGS = frozenset(
    {
        "a",
        "abbr",
        "b",
        "blockquote",
        "br",
        "caption",
        "code",
        "col",
        "colgroup",
        "dd",
        "del",
        "dfn",
        "div",
        "dl",
        "dt",
        "em",
        "figcaption",
        "figure",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "hr",
        "i",
        "img",
        "ins",
        "kbd",
        "li",
        "mark",
        "ol",
        "p",
        "pre",
        "q",
        "s",
        "samp",
        "small",
        "span",
        "strong",
        "sub",
        "sup",
        "table",
        "tbody",
        "td",
        "tfoot",
        "th",
        "thead",
        "tr",
        "u",
        "ul",
    }
)
_HTML_ALLOWED_ATTRS = frozenset({"href", "src", "alt", "title", "colspan", "rowspan"})
_HTML_VOID_TAGS = frozenset({"br", "hr", "img", "col"})
_SAFE_URL_SCHEMES = frozenset({"http", "https", "mailto", "tel"})
_SAFE_DATA_IMAGE = re.compile(r"^data:image/(png|jpe?g|gif|webp);base64,[a-z0-9+/=]+$", re.I)
_URL_SPLIT_RE = re.compile(r"^\s*([a-zA-Z][a-zA-Z0-9+.\-]*)\s*:")


def _is_safe_url(value: str) -> bool:
    """URL 白名单：http/https/mailto/tel、站内锚点与相对路径；拒绝 javascript:/data: 等。"""
    raw = (value or "").strip()
    if not raw:
        return False
    # 去掉控制字符后再判断，避免 "java\tscript:" 这类绕过
    cleaned = re.sub(r"[\x00-\x20\x7f]", "", raw).lower()
    if cleaned.startswith("#") or cleaned.startswith("/"):
        return True
    if cleaned.startswith(("mailto:", "tel:")):
        return True
    scheme = _URL_SPLIT_RE.match(cleaned)
    if scheme:
        return scheme.group(1) in _SAFE_URL_SCHEMES
    if cleaned.startswith("//"):
        return True  # 协议相对 URL：跟随当前协议，无脚本风险
    if cleaned.startswith("data:"):
        return bool(_SAFE_DATA_IMAGE.match(cleaned))
    return True  # 相对路径


def decode_html_bytes(content: bytes) -> tuple[str, str]:
    """解码 HTML 字节，返回 (源文本, 编码)。

    Raises:
        ExtractError: 无法识别编码
    """
    try:
        return decode_text(content)
    except TextDecodeError as exc:
        raise ExtractError(f"HTML 文件解码失败：{exc}") from exc


class _HtmlTextExtractor(HTMLParser):
    """HTML → 行式文本：标题转 markdown、表格按行 `a | b`、列表加 `- ` 前缀。"""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.lines: list[str] = []
        self._buffer: list[str] = []
        self._drop_tag: str | None = None
        self._drop_depth = 0
        self._in_row = False

    def _flush(self) -> None:
        text = "".join(self._buffer).strip()
        self._buffer = []
        if text:
            self.lines.append(text)

    def _append(self, text: str) -> None:
        if self._in_row and self._buffer and not self._buffer[-1].endswith(" "):
            self._buffer.append(" ")
        self._buffer.append(text)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if self._drop_tag:
            if tag == self._drop_tag:
                self._drop_depth += 1
            return
        if tag in _HTML_DROP_TAGS:
            self._drop_tag, self._drop_depth = tag, 1
            return
        if tag in _HTML_HEADINGS:
            self._flush()
            self._buffer.append("#" * _HTML_HEADINGS[tag] + " ")
            return
        if tag == "li":
            self._flush()
            self._buffer.append("- ")
            return
        if tag == "tr":
            self._flush()
            self._in_row = True
            return
        if tag in ("td", "th"):
            if self._buffer and "".join(self._buffer).strip():
                self._buffer.append(" | ")
            return
        if tag == "br":
            if self._in_row:
                self._buffer.append(" / ")
            else:
                self._flush()
            return
        if tag in _HTML_BLOCK_TAGS:
            self._flush()

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        if tag in ("td", "th"):
            return
        self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        if self._drop_tag:
            if tag == self._drop_tag:
                self._drop_depth -= 1
                if self._drop_depth == 0:
                    self._drop_tag = None
                    self._flush()
            return
        if tag == "tr":
            self._flush()
            self._in_row = False
            return
        if tag in _HTML_BLOCK_TAGS or tag in _HTML_HEADINGS:
            self._flush()

    def handle_data(self, data: str) -> None:
        if self._drop_tag or not data:
            return
        self._append(" " if data.isspace() else data)

    def close(self) -> None:
        super().close()
        self._flush()


def extract_html_text(content: bytes) -> str:
    """抽取 HTML 正文文本（跳过脚本/样式/head 等），返回带结构的行式文本。"""
    text, _encoding = decode_html_bytes(content)
    parser = _HtmlTextExtractor()
    parser.feed(text)
    parser.close()
    lines = [line for line in parser.lines if line.strip()]
    if not lines:
        raise ExtractError("HTML 中未提取到正文文本")
    return "\n\n".join(lines)


class _HtmlSanitizer(HTMLParser):
    """按白名单重建 HTML：丢弃标签与属性白名单外的一切，转义所有文本。"""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._drop_tag: str | None = None
        self._drop_depth = 0

    def _allowed_attrs(self, tag: str, attrs: list[tuple[str, str | None]]) -> str:
        pieces: list[str] = []
        for name, value in attrs:
            key = (name or "").lower()
            if key not in _HTML_ALLOWED_ATTRS or value is None:
                continue
            if key in ("href", "src") and not _is_safe_url(value):
                continue
            if key in ("colspan", "rowspan") and not str(value).strip().isdigit():
                continue
            pieces.append(f'{key}="{html.escape(str(value), quote=True)}"')
        return (" " + " ".join(pieces)) if pieces else ""

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if self._drop_tag:
            if tag == self._drop_tag:
                self._drop_depth += 1
            return
        if tag in _HTML_DROP_TAGS:
            self._drop_tag, self._drop_depth = tag, 1
            return
        if tag not in _HTML_ALLOWED_TAGS:
            return  # 未知/不安全标签：丢弃标签本身，保留子内容
        if tag in _HTML_VOID_TAGS:
            self.parts.append(f"<{tag}{self._allowed_attrs(tag, attrs)}>")
            return
        self.parts.append(f"<{tag}{self._allowed_attrs(tag, attrs)}>")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if self._drop_tag or tag not in _HTML_ALLOWED_TAGS:
            return
        self.parts.append(f"<{tag}{self._allowed_attrs(tag, attrs)} />")

    def handle_endtag(self, tag: str) -> None:
        if self._drop_tag:
            if tag == self._drop_tag:
                self._drop_depth -= 1
                if self._drop_depth == 0:
                    self._drop_tag = None
            return
        if tag in _HTML_ALLOWED_TAGS and tag not in _HTML_VOID_TAGS:
            self.parts.append(f"</{tag}>")

    def handle_data(self, data: str) -> None:
        if self._drop_tag or not data:
            return
        self.parts.append(html.escape(data, quote=False))


def sanitize_html(content: bytes) -> str:
    """把原始 HTML 清洗为可安全渲染的片段（无脚本、无事件属性、无危险协议）。"""
    text, _encoding = decode_html_bytes(content)
    sanitizer = _HtmlSanitizer()
    sanitizer.feed(text)
    sanitizer.close()
    cleaned = "".join(sanitizer.parts).strip()
    if not cleaned:
        raise ExtractError("HTML 清洗后没有可展示内容")
    return cleaned
