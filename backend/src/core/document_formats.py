"""文档格式分类常量：上传白名单、解析、预览与在线编辑能力共用。

集中定义可避免上传允许但解析/预览不识别，或前端 accept 与后端白名单漂移。
"""

TEXT_EXTENSIONS = frozenset({"txt", "md", "csv", "tsv", "json", "html"})
TABLE_EXTENSIONS = frozenset({"csv", "tsv"})
# 结构化文本：预览走专用视图（JSON 树 / HTML 渲染），编辑仍是源码文本
JSON_EXTENSIONS = frozenset({"json"})
HTML_EXTENSIONS = frozenset({"html"})
EDITABLE_EXTENSIONS = TEXT_EXTENSIONS
IMAGE_EXTENSIONS = frozenset({"png", "jpg", "jpeg", "gif", "webp", "bmp"})
OFFICE_EXTENSIONS = frozenset({"pdf", "docx", "xlsx", "pptx"})

ALLOWED_EXTENSIONS = TEXT_EXTENSIONS | IMAGE_EXTENSIONS | OFFICE_EXTENSIONS


def is_image(file_type: str) -> bool:
    return file_type.lower() in IMAGE_EXTENSIONS


def delimiter_for(file_type: str) -> str | None:
    """表格类扩展名的分隔符；非表格格式返回 None。"""
    ft = (file_type or "").lower()
    if ft == "tsv":
        return "\t"
    if ft == "csv":
        return ","
    return None
