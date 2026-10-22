"""文档解析器：Docling 为主（PDF/DOCX → Markdown，保留标题层级），轻量库降级。

TECH_DESIGN：Docling 为主 + PyPDF2/docx 降级；失败支持重试；
扫描件 OCR 无法识别时提示用户上传可复制文本版本（AGENTS.md 约束）。
"""

import io
from abc import ABC, abstractmethod

from loguru import logger


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


class DoclingParser(BaseParser):
    """Docling（IBM）：精准解析 PDF/DOCX 的表格、标题结构与阅读顺序。"""

    _converter = None  # 惰性单例：初始化耗时，避免重复构建

    @classmethod
    def _get_converter(cls):
        if cls._converter is None:
            from docling.document_converter import DocumentConverter

            logger.info("初始化 Docling DocumentConverter（首次较慢）")
            cls._converter = DocumentConverter()
        return cls._converter

    def parse(self, content: bytes, file_type: str) -> str:
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
    """降级解析器：pypdf（PDF）/ python-docx（DOCX，可从 Heading 样式恢复标题层级）/ 直读（TXT）。"""

    def parse(self, content: bytes, file_type: str) -> str:
        try:
            if file_type == "pdf":
                text = self._parse_pdf(content)
            elif file_type == "docx":
                text = self._parse_docx(content)
            else:  # txt
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
    def _parse_pdf(content: bytes) -> str:
        from pypdf import PdfReader

        reader = PdfReader(io.BytesIO(content))
        pages = [(page.extract_text() or "").strip() for page in reader.pages]
        return "\n\n".join(p for p in pages if p)

    @staticmethod
    def _parse_docx(content: bytes) -> str:
        from docx import Document as DocxDocument

        doc = DocxDocument(io.BytesIO(content))
        parts: list[str] = []
        for para in doc.paragraphs:
            text = para.text.strip()
            if not text:
                continue
            style = (para.style.name or "").lower()
            # Heading 1/2/... → markdown 标题，保住结构感知切片能力
            if style.startswith("heading"):
                try:
                    level = min(int(style.split()[-1]), 6)
                except ValueError:
                    level = 2
                parts.append(f"{'#' * level} {text}")
            else:
                parts.append(text)
        return "\n\n".join(parts)

    @staticmethod
    def _decode_text(content: bytes) -> str:
        for encoding in ("utf-8", "gbk"):
            try:
                return content.decode(encoding)
            except UnicodeDecodeError:
                continue
        raise ParseError("文本编码无法识别，请使用 UTF-8 编码的文件")


def get_parser() -> BaseParser:
    """按依赖可用性选择解析器：装了 docling 用 Docling，否则降级。"""
    try:
        import docling  # noqa: F401

        return DoclingParser()
    except ImportError:
        logger.warning("docling 未安装，使用降级解析器（pypdf / python-docx）")
        return FallbackParser()
