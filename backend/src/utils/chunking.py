"""语义切片工具：结构感知（按标题层级）+ 递归切分。

TECH_DESIGN 要求：chunk 512~1024 tokens，overlap 50~100。
token 计数为近似估算（中文 ≈ 字数，英文 ≈ 字符数/4），
TODO: 接入 BGE-m3 tokenizer（XLM-RoBERTa）后替换为精确计数。
"""

import re
from dataclasses import dataclass

from src.core.config import settings

# markdown 标题行：# ~ ######
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")
# 句子边界（中英文标点）
_SENTENCE_RE = re.compile(r"(?<=[。！？!?；;])\s*")


@dataclass
class RawChunk:
    """切片原始结果（入库前）。"""

    content: str
    title_path: str | None = None
    chunk_index: int = 0


def estimate_tokens(text: str) -> int:
    """近似 token 估算：CJK 字符 1:1，其余按 4 字符 ≈ 1 token。"""
    if not text:
        return 0
    cjk = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")
    other = len(text) - cjk
    return cjk + other // 4


def split_markdown_sections(md: str) -> list[tuple[str | None, str]]:
    """按 markdown 标题层级切分，返回 (title_path, section_text) 列表。

    title_path 形如 '公司制度 > 考勤管理 > 请假流程'，用于精确溯源（PRD 3.1）。
    无标题的文本归入 title_path=None 的单一 section。
    """
    sections: list[tuple[str | None, str]] = []
    stack: list[tuple[int, str]] = []  # (level, title)
    current: list[str] = []
    current_path: str | None = None

    def flush() -> None:
        text = "\n".join(current).strip()
        if text:
            sections.append((current_path, text))
        current.clear()

    for line in md.splitlines():
        m = _HEADING_RE.match(line)
        if m:
            flush()
            level, title = len(m.group(1)), m.group(2).strip()
            while stack and stack[-1][0] >= level:
                stack.pop()
            stack.append((level, title))
            current_path = " > ".join(t for _, t in stack)
            current.append(line)
        else:
            current.append(line)
    flush()
    return sections


def _split_sentences(text: str) -> list[str]:
    """按中英文句号切句，保留标点。"""
    parts = [s.strip() for s in _SENTENCE_RE.split(text) if s.strip()]
    return parts if parts else ([text.strip()] if text.strip() else [])


def _tail_prefix(text: str, overlap: int) -> str:
    """取文本尾部约 overlap tokens 作为下一块的重叠前缀。

    优先按句子回退；单句超过 overlap 时按 token 密度截取尾部字符。
    """
    acc: list[str] = []
    total = 0
    for sent in reversed(_split_sentences(text)):
        t = estimate_tokens(sent)
        if total + t > overlap:
            break
        acc.insert(0, sent)
        total += t
    if acc:
        return "".join(acc)
    est = estimate_tokens(text)
    if est <= 0:
        return ""
    n = len(text) * overlap // est
    return text[-n:].strip() if n > 0 else ""


def _sliding_window(
    units: list[str], chunk_size: int, overlap: int, joiner: str
) -> list[str]:
    """滑动窗口组包：每块 ≤ chunk_size tokens，相邻块重叠 ≤ overlap tokens。

    重叠策略：优先回退完整单元；单元大于 overlap 时，取当前块尾部
    约 overlap tokens 的句子作为下一块前缀。
    """
    results: list[str] = []
    i, n = 0, len(units)
    while i < n:
        buf: list[str] = []
        t = 0
        j = i
        while j < n and t + estimate_tokens(units[j]) <= chunk_size:
            buf.append(units[j])
            t += estimate_tokens(units[j])
            j += 1
        if not buf:
            # 单个单元超长 → 字符硬切（按 token 密度换算窗口与步长，步长含 overlap）
            unit = units[i]
            est = estimate_tokens(unit)
            if est > 0:
                window = max(len(unit) * chunk_size // est, 1)
                stride = max(window - len(unit) * overlap // est, 1)
            else:
                window, stride = chunk_size, 1
            pos = 0
            while pos < len(unit):
                results.append(unit[pos : pos + window])
                pos += stride
            i += 1
            continue
        results.append(joiner.join(buf))
        if j >= n:
            break
        # 重叠：优先回退完整单元
        back, acc = 0, 0
        k = j - 1
        while k > i and acc + estimate_tokens(units[k]) <= overlap:
            acc += estimate_tokens(units[k])
            back += 1
            k -= 1
        if back > 0:
            i = j - back
        else:
            # 单元大于 overlap：取当前块尾部句子作为下一块前缀
            tail = _tail_prefix(joiner.join(buf), overlap)
            rest = _sliding_window(units[j:], chunk_size, overlap, joiner)
            if tail and rest:
                rest[0] = tail + joiner + rest[0]
            results.extend(rest)
            return results
    return results


def _smart_split(text: str, chunk_size: int, overlap: int) -> list[str]:
    """两级切分：段落优先（保留语义边界），超长段落内部按句子切。"""
    text = text.strip()
    if not text:
        return []
    if estimate_tokens(text) <= chunk_size:
        return [text]

    flat: list[str] = []
    for para in re.split(r"\n{2,}", text):
        para = para.strip()
        if not para:
            continue
        if estimate_tokens(para) <= chunk_size:
            flat.append(para)
        else:
            flat.extend(_split_sentences(para))
    return _sliding_window(flat, chunk_size, overlap, joiner="\n")


def _merge_tiny_chunks(chunks: list[RawChunk], min_tokens: int) -> list[RawChunk]:
    """过小的切片并入前一块，避免碎片（标题+一句话的场景）。"""
    merged: list[RawChunk] = []
    for rc in chunks:
        if merged and estimate_tokens(rc.content) < min_tokens:
            prev = merged[-1]
            prev.content = f"{prev.content}\n{rc.content}".strip()
            if prev.title_path is None:
                prev.title_path = rc.title_path
        else:
            merged.append(rc)
    return merged


def chunk_document(md: str) -> list[RawChunk]:
    """结构感知切片：先按标题层级分节，再对每节递归切分。"""
    chunk_size = settings.CHUNK_SIZE_TOKENS
    overlap = settings.CHUNK_OVERLAP_TOKENS
    min_tokens = settings.CHUNK_MIN_TOKENS

    results: list[RawChunk] = []
    for title_path, text in split_markdown_sections(md):
        for piece in _smart_split(text, chunk_size, overlap):
            results.append(RawChunk(content=piece, title_path=title_path))
    if not results:  # 全空文档
        return []

    results = _merge_tiny_chunks(results, min_tokens)
    for idx, rc in enumerate(results):
        rc.chunk_index = idx
    return results
