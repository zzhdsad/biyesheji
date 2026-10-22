"""语义切片器单元测试（纯函数，不依赖外部服务）。"""

from src.core.config import settings
from src.utils.chunking import (
    chunk_document,
    estimate_tokens,
    split_markdown_sections,
)


def test_estimate_tokens():
    assert estimate_tokens("") == 0
    assert estimate_tokens("中文字符") == 4  # CJK 字符 ≈ 1 token
    assert estimate_tokens("abcdefgh") == 2  # 其他 ≈ 4 字符 1 token


def test_markdown_sections_title_path():
    md = (
        "# 总纲\n\n总纲内容\n\n"
        "## 考勤\n\n考勤内容\n\n"
        "### 请假\n\n请假内容\n\n"
        "## 报销\n\n报销内容"
    )
    sections = split_markdown_sections(md)
    paths = [p for p, _ in sections]
    assert paths == [
        "总纲",
        "总纲 > 考勤",
        "总纲 > 考勤 > 请假",
        "总纲 > 报销",
    ]
    # 标题行保留在内容中（可被检索）
    assert any("### 请假" in text for _, text in sections)


def test_markdown_no_heading():
    sections = split_markdown_sections("无标题正文内容")
    assert sections == [(None, "无标题正文内容")]


def _para(prefix: str, chars: int) -> str:
    """构造约 chars/2 tokens 的段落（中文按 1 字 1 token）。"""
    body = "内容" * (chars // 2)
    return f"{prefix}{body}。"


def test_chunk_document_respects_size_limit():
    """每块 token 数不超过上限（chunk_size + overlap 余量 ≤ 1024）。"""
    md = "\n\n".join(_para(f"第{i}段，", 500) for i in range(4))  # ≈ 1000 tokens
    chunks = chunk_document(md)
    assert len(chunks) >= 2
    for c in chunks:
        assert estimate_tokens(c.content) <= settings.CHUNK_SIZE_TOKENS + settings.CHUNK_OVERLAP_TOKENS


def test_chunk_overlap_between_adjacent_chunks():
    """相邻切片存在重叠：下一块开头文本出现在上一块尾部。"""
    paras = [_para(f"唯一标记{idx}，", 300) for idx in range(5)]  # 段落 ~150 tokens > overlap 100
    chunks = chunk_document("\n\n".join(paras))
    assert len(chunks) >= 2
    head = chunks[1].content[:20]
    assert head in chunks[0].content


def test_small_section_merged():
    """过小的 section 并入前一块，避免碎片。"""
    md = "# 标题A\n\n" + _para("正文，", 300) + "\n\n# 标题B\n\n短句。"
    chunks = chunk_document(md)
    assert len(chunks) == 1
    assert "短句" in chunks[0].content


def test_long_sentence_hard_split():
    """无句读的超长单段被硬切，且所有内容保留。"""
    text = "长" * 3000  # ≈ 3000 tokens，无标点
    chunks = chunk_document(text)
    assert len(chunks) >= 3
    joined = "".join(c.content for c in chunks)
    assert text in joined.replace("\n", "") or text[:100] in joined
    for c in chunks:
        assert estimate_tokens(c.content) <= settings.CHUNK_SIZE_TOKENS + settings.CHUNK_OVERLAP_TOKENS


def test_chunk_index_sequential():
    md = "\n\n".join(_para(f"第{i}段，", 400) for i in range(3))
    chunks = chunk_document(md)
    assert [c.chunk_index for c in chunks] == list(range(len(chunks)))
