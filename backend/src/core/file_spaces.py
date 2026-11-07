"""文件管理空间：普通文档与结构化数据各自独立的文件夹树与文件列表。

两个页面共用同一套 folders / documents 接口，靠 ``space`` 字段隔离数据：
- ``document``：文档管理页「普通文档」Tab，走解析 + 向量化（现有 RAG 链路）。
- ``structured``：文档管理页「结构化数据」Tab，本阶段只做文件管理，
  **不解析、不切片、不写 Milvus**（关联解析与统一 RAG 属后续阶段）。

``space`` 只影响文件归属与是否进入解析，不改变知识库（``kb_id``）语义：
权限、检索隔离仍由 ``kb_id`` 决定，移动文件/文件夹不会改 ``kb_id``。
"""

from __future__ import annotations

DOCUMENT_SPACE = "document"
STRUCTURED_SPACE = "structured"

FILE_SPACES: frozenset[str] = frozenset({DOCUMENT_SPACE, STRUCTURED_SPACE})
DEFAULT_SPACE = DOCUMENT_SPACE

# 结构化空间的文档不参与解析流水线，parse_status 用该值如实标注（不是失败也不是成功）
PARSE_STATUS_SKIPPED = "skipped"

FOLDER_NAME_MAX = 64

# 同目录重名的判重索引需要处理 parent_id IS NULL（SQL NULL 不参与唯一比较）：
# 与 categories 表一致，用 COALESCE 表达式索引把根目录归一到零 UUID。
ROOT_FOLDER_SENTINEL = "00000000-0000-0000-0000-000000000000"


def is_valid_space(space: str | None) -> bool:
    return bool(space) and space in FILE_SPACES


def normalize_space(space: str | None) -> str:
    """空值回落为普通文档空间；非法值抛错由调用方转成 400。"""
    if not space:
        return DEFAULT_SPACE
    if space not in FILE_SPACES:
        raise ValueError(f"非法的文件空间：{space}")
    return space


def is_parse_enabled(space: str | None) -> bool:
    """该空间的文件是否进入「解析 → 切片 → 向量化」流水线。"""
    return normalize_space(space) == DOCUMENT_SPACE
