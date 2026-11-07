"""文档解析器：Docling 为主（PDF/DOCX → Markdown，保留标题层级），轻量库/标准库兜底。

支持 PDF/DOCX/XLSX/PPTX/TXT/MD/CSV/TSV/JSON/HTML；图片仅登记预览，不参与文本检索。
扫描件 OCR 无法识别时提示用户上传可复制文本版本（AGENTS.md 约束）。
"""

import io
from abc import ABC, abstractmethod

from loguru import logger

from src.core.document_formats import IMAGE_EXTENSIONS
from src.infrastructure.document_extract import (
    ExtractError,
    extract_docx_blocks,
    extract_html_text,
    extract_pptx_slides,
    extract_xlsx_sheets,
    json_to_text,
    parse_json_bytes,
)
from src.utils.text_encoding import TextDecodeError, decode_text


class ParseError(Exception):
    """解析失败（含用户友好提示）。"""


class BaseParser(ABC):
    """解析器接口：输入原始字节，输出 Markdown（保留 # 标题层级）。"""

    @abstractmethod
    def parse(self, content: bytes, file_type: str) -> str:
        """解析文档为 Markdown 文本。

        Raises:
            ParseError: 解析失败或未提取出任何文本。
        """

    def parse_pages(self, content: bytes, file_type: str) -> list[tuple[int | None, str]]:
        """按页解析；默认退化为单页（page_num=None）。

        支持页码的解析器（如 PDF 降级解析器）覆盖此方法，返回 1-based 页码。
        """
        return [(None, self.parse(content, file_type))]


class DoclingParser(BaseParser):
    """Docling（IBM）：精准解析 PDF/DOCX 的表格、标题结构与阅读顺序。"""

    def parse_pages(self, content: bytes, file_type: str) -> list[tuple[int | None, str]]:
        """PDF 页码优先：Docling 导出 Markdown 不带稳定页码，PDF 用 pypdf 按页解析。

        DOCX 等格式仍走 Docling + 单页（无页码语义）。
        """
        if file_type == "pdf":
            return FallbackParser()._parse_pdf_pages(content)
        return super().parse_pages(content, file_type)

    _converter = None  # 惰性单例：初始化耗时，避免重复构建

    @classmethod
    def _get_converter(cls):
        if cls._converter is None:
            from docling.document_converter import DocumentConverter

            logger.info("初始化 Docling DocumentConverter（首次较慢）")
            cls._converter = DocumentConverter()
        return cls._converter

    def parse(self, content: bytes, file_type: str) -> str:
        # Docling 只负责 PDF/DOCX；纯文本/表格/PPTX/图片走降级解析器，
        # 避免把不支持的格式交给 Docling 后报错或产出乱码。
        if file_type not in ("pdf", "docx"):
            return FallbackParser().parse(content, file_type)
        try:
            from docling.datamodel.base_models import DocumentStream

            stream = DocumentStream(name=f"doc.{file_type}", stream=io.BytesIO(content))
            result = self._get_converter().convert(stream)
            md = result.document.export_to_markdown()
            if not md or not md.strip():
                raise ParseError(
                    "未解析出文本内容：可能是扫描件 PDF，请上传可复制文本的版本"
                )
            return md
        except ParseError:
            raise
        except Exception as exc:
            raise ParseError(f"Docling 解析失败：{exc}") from exc


class FallbackParser(BaseParser):
    """降级解析器：pypdf（PDF 按页）/ python-docx（DOCX）/ openpyxl（XLSX）/
    标准库（PPTX、JSON、HTML）/ 编码识别直读（TXT/MD/CSV/TSV）。"""

    def parse_pages(self, content: bytes, file_type: str) -> list[tuple[int | None, str]]:
        """PDF 按页返回 (page_num, text)；其他格式退化为单页。"""
        if file_type == "pdf":
            return self._parse_pdf_pages(content)
        return super().parse_pages(content, file_type)

    def parse(self, content: bytes, file_type: str) -> str:
        try:
            if file_type == "pdf":
                text = self._parse_pdf(content)
            elif file_type == "docx":
                text = self._parse_docx(content)
            elif file_type == "xlsx":
                text = self._parse_xlsx(content)
            elif file_type == "pptx":
                text = self._parse_pptx(content)
            elif file_type == "json":
                text = self._parse_json(content)
            elif file_type == "html":
                text = self._parse_html(content)
            elif file_type in IMAGE_EXTENSIONS:
                raise ParseError("图片文件仅支持在线预览，不参与文本解析")
            else:  # txt / md / csv / tsv
                text = self._decode_text(content)
            if not text.strip():
                raise ParseError(
                    "未解析出文本内容：可能是扫描件 PDF，请上传可复制文本的版本"
                )
            return text
        except ParseError:
            raise
        except Exception as exc:
            raise ParseError(f"解析失败：{exc}") from exc

    @staticmethod
    def _parse_pdf_pages(content: bytes) -> list[tuple[int, str]]:
        from pypdf import PdfReader

        reader = PdfReader(io.BytesIO(content))
        pages: list[tuple[int, str]] = []
        for index, page in enumerate(reader.pages, start=1):
            text = (page.extract_text() or "").strip()
            if text:
                pages.append((index, text))
        if not pages:
            raise ParseError(
                "未解析出文本内容：可能是扫描件 PDF，请上传可复制文本的版本"
            )
        return pages

    @staticmethod
    def _parse_pdf(content: bytes) -> str:
        from pypdf import PdfReader

        reader = PdfReader(io.BytesIO(content))
        pages = [(page.extract_text() or "").strip() for page in reader.pages]
        return "\n\n".join(p for p in pages if p)

    @staticmethod
    def _parse_docx(content: bytes) -> str:
        try:
            blocks, _ = extract_docx_blocks(content)
        except ExtractError as exc:
            raise ParseError(str(exc)) from exc
        parts: list[str] = []
        for block in blocks:
            # Heading 1/2/... → markdown 标题，保住结构感知切片能力
            if block["type"] == "heading" and block["level"]:
                parts.append(f"{'#' * block['level']} {block['text']}")
            else:
                parts.append(block["text"])
        return "\n\n".join(parts)

    @staticmethod
    def _parse_xlsx(content: bytes) -> str:
        try:
            sheets, _ = extract_xlsx_sheets(content)
        except ExtractError as exc:
            raise ParseError(str(exc)) from exc
        parts: list[str] = []
        for sheet in sheets:
            parts.append(f"# {sheet['name']}")
            for row in sheet["rows"]:
                parts.append(" | ".join(row))
        return "\n\n".join(parts)

    @staticmethod
    def _parse_pptx(content: bytes) -> str:
        try:
            slides, _ = extract_pptx_slides(content)
        except ExtractError as exc:
            raise ParseError(str(exc)) from exc
        parts: list[str] = []
        for slide in slides:
            parts.append(f"# 第 {slide['index']} 页")
            parts.extend(slide["lines"])
        return "\n\n".join(parts)

    @staticmethod
    def _parse_json(content: bytes) -> str:
        """JSON → `字段路径 = 值` 行式文本，保留键名与层级供检索溯源。"""
        try:
            obj, _text = parse_json_bytes(content)
        except ExtractError as exc:
            raise ParseError(str(exc)) from exc
        return json_to_text(obj)

    @staticmethod
    def _parse_html(content: bytes) -> str:
        """HTML → 正文文本（脚本/样式不入库），标题转 markdown 以保留结构。"""
        try:
            return extract_html_text(content)
        except ExtractError as exc:
            raise ParseError(str(exc)) from exc

    @staticmethod
    def _decode_text(content: bytes) -> str:
        if b"\x00" in content:
            raise ParseError("文件包含二进制内容，无法按文本解析")
        try:
            return decode_text(content)[0]
        except TextDecodeError as exc:
            raise ParseError(str(exc)) from exc


def get_parser() -> BaseParser:
    """按依赖可用性选择解析器：装了 docling 用 Docling，否则降级。"""
    try:
        import docling  # noqa: F401

        return DoclingParser()
    except ImportError:
        logger.warning("docling 未安装，使用降级解析器（pypdf / python-docx）")
        return FallbackParser()
