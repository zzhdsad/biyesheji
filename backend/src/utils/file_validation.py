"""文档内容合法性校验：用于在线编辑保存与文件替换，避免坏内容覆盖好文件。

设计取向：
- 只做「能挡住明显错误」的轻校验，不追求完整规范实现；
- 不引入第三方依赖（json / html.parser 均为标准库）；
- 返回错误信息字符串（None 表示通过），由调用方决定 HTTP 状态码。
"""

import json
from html.parser import HTMLParser

from src.infrastructure.document_extract import ExtractError, decode_html_bytes
from src.utils.text_encoding import TextDecodeError, decode_text

# 文件头魔法字节：改扩展名的文件在写入前就被拒绝
_MAGIC_PREFIXES: dict[str, tuple[bytes, ...]] = {
    "pdf": (b"%PDF-",),
    "docx": (b"PK\x03\x04",),
    "xlsx": (b"PK\x03\x04",),
    "pptx": (b"PK\x03\x04",),
    "png": (b"\x89PNG\r\n\x1a\n",),
    "jpg": (b"\xff\xd8\xff",),
    "jpeg": (b"\xff\xd8\xff",),
    "gif": (b"GIF87a", b"GIF89a"),
    "bmp": (b"BM",),
}
_MAGIC_RIFF = {"webp": b"WEBP"}

_AUTO_CLOSEABLE = frozenset({"p", "li", "dt", "dd", "td", "th", "tr", "option", "thead", "tbody"})
# 开始标签会隐式关闭的上一级标签（浏览器同样的自动闭合行为）
_CLOSES_PREVIOUS: dict[str, frozenset[str]] = {
    "p": frozenset({"p"}),
    "li": frozenset({"li"}),
    "dt": frozenset({"dt", "dd"}),
    "dd": frozenset({"dt", "dd"}),
    "td": frozenset({"td", "th"}),
    "th": frozenset({"td", "th"}),
    "tr": frozenset({"tr", "td", "th"}),
    "option": frozenset({"option"}),
    "thead": frozenset({"tbody", "tfoot"}),
    "tbody": frozenset({"thead", "tfoot"}),
    "tfoot": frozenset({"thead", "tbody"}),
    "h1": frozenset({f"h{i}" for i in range(1, 7)}),
    "h2": frozenset({f"h{i}" for i in range(1, 7)}),
    "h3": frozenset({f"h{i}" for i in range(1, 7)}),
    "h4": frozenset({f"h{i}" for i in range(1, 7)}),
    "h5": frozenset({f"h{i}" for i in range(1, 7)}),
    "h6": frozenset({f"h{i}" for i in range(1, 7)}),
}


class _TagBalanceChecker(HTMLParser):
    """基础结构校验：未闭合标签与多余的结束标签。"""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.stack: list[tuple[str, tuple[int, int]]] = []
        self.error: str | None = None

    def _pop_auto(self, tag: str) -> None:
        closers = _CLOSES_PREVIOUS.get(tag)
        while closers and self.stack and self.stack[-1][0] in closers:
            self.stack.pop()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if self.error or tag in {"br", "hr", "img", "meta", "link", "input", "col", "area", "base", "wbr", "source", "track", "embed"}:
            return
        self._pop_auto(tag)
        self.stack.append((tag, self.getpos()))

    handle_startendtag = handle_starttag

    def handle_endtag(self, tag: str) -> None:
        if self.error:
            return
        names = [name for name, _ in self.stack]
        if tag not in names:
            line, col = self.getpos()
            self.error = f"第 {line} 行第 {col} 列：多余的结束标签 </{tag}>"
            return
        while self.stack and self.stack[-1][0] != tag:
            self.stack.pop()  # 中间未闭合的标签由浏览器隐式关闭，不判错
        if self.stack:
            self.stack.pop()

    def result(self) -> str | None:
        if self.error:
            return self.error
        leftover = [(name, pos) for name, pos in self.stack if name not in _AUTO_CLOSEABLE]
        if leftover:
            name, (line, col) = leftover[0]
            return f"第 {line} 行第 {col} 列：标签 <{name}> 未闭合"
        return None


def validate_json_text(text: str) -> str | None:
    """校验 JSON 源码文本，返回可读错误信息（None 表示合法）。"""
    try:
        json.loads(text)
    except json.JSONDecodeError as exc:
        return f"JSON 格式非法：第 {exc.lineno} 行第 {exc.colno} 列：{exc.msg}"
    except RecursionError:
        return "JSON 嵌套层级过深，无法解析"
    return None


def validate_html_text(text: str) -> str | None:
    """校验 HTML 源码结构（未闭合/多余结束标签）。"""
    if "\x00" in text:
        return "HTML 内容包含非法空字符，无法保存"
    checker = _TagBalanceChecker()
    checker.feed(text)
    checker.close()
    return checker.result()


def validate_text_document(file_type: str, text: str) -> str | None:
    """校验在线编辑提交的源码文本（编码为字节之前），返回错误信息或 None。"""
    ft = (file_type or "").lower()
    if "\x00" in text:
        return "内容包含非法空字符，无法保存"
    if ft == "json":
        return validate_json_text(text)
    if ft == "html":
        return validate_html_text(text)
    return None


def validate_document_content(file_type: str, content: bytes) -> str | None:
    """按格式校验文件字节；返回错误信息或 None。

    空文件由调用方统一处理，这里只校验内容与扩展名是否相配。
    """
    ft = (file_type or "").lower()
    if not content:
        return "文件为空"

    prefixes = _MAGIC_PREFIXES.get(ft)
    if prefixes is not None:
        if not any(content.startswith(p) for p in prefixes):
            return f"文件内容与 .{ft} 扩展名不匹配（文件头校验失败）"
        return None

    if ft in _MAGIC_RIFF:
        marker = _MAGIC_RIFF[ft]
        if not (content.startswith(b"RIFF") and content[8:8 + len(marker)] == marker):
            return f"文件内容与 .{ft} 扩展名不匹配（文件头校验失败）"
        return None

    if ft == "json":
        try:
            text, _ = decode_text(content)
        except TextDecodeError as exc:
            return f"JSON 文件解码失败：{exc}"
        return validate_json_text(text)

    if ft == "html":
        try:
            text, _ = decode_html_bytes(content)
        except ExtractError as exc:
            return str(exc)
        return validate_html_text(text)

    if ft in ("txt", "md", "csv", "tsv"):
        if b"\x00" in content:
            return f"文件包含二进制内容，与 .{ft} 扩展名不匹配"
        try:
            decode_text(content)
        except TextDecodeError as exc:
            return str(exc)
        return None

    return None  # 未列举的类型不做深度校验
