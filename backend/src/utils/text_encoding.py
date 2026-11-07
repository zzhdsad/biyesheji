"""文本编码识别：优先 UTF-8，其次 charset-normalizer（GBK/GB18030/Big5），避免乱码。

项目既有纯文本文件既有 UTF-8 也有 GBK；统一按 UTF-8 解码会得到替换字符。
解析入库与站内预览共用本模块，保证「预览所见」与「入库内容」一致。
"""

from charset_normalizer import from_bytes

# 可安全写回的编码（保存时只接受这些标签）
SUPPORTED_ENCODINGS = ("utf-8", "utf-8-sig", "gbk", "gb18030", "big5")

_ALIASES = {
    "utf8": "utf-8",
    "utf-8": "utf-8",
    "utf_8": "utf-8",
    "utf-8-sig": "utf-8-sig",
    "utf_8_sig": "utf-8-sig",
    "gb2312": "gbk",
    "gb_2312": "gbk",
    "cp936": "gbk",
    "gbk": "gbk",
    "gb18030": "gb18030",
    "big5": "big5",
    "big5hkscs": "big5",
}


class TextDecodeError(Exception):
    """无法在受支持的编码中解码文本。"""


def normalize_encoding(label: str | None) -> str | None:
    """把编码别名归一化为受支持标签；未知标签返回 None。"""
    if not label:
        return None
    return _ALIASES.get(label.strip().lower())


def decode_text(content: bytes, preferred: str | None = None) -> tuple[str, str]:
    """解码文本字节，返回 (text, encoding)。

    Args:
        content: 原始字节。
        preferred: 已知编码（如保存时前端回传），优先尝试。

    Raises:
        TextDecodeError: 所有候选编码均无法解码。
    """
    if content.startswith(b"\xef\xbb\xbf"):
        return content.decode("utf-8-sig"), "utf-8-sig"

    if preferred:
        encoding = normalize_encoding(preferred)
        if encoding:
            try:
                return content.decode(encoding), encoding
            except (UnicodeDecodeError, LookupError):
                pass

    try:
        return content.decode("utf-8"), "utf-8"
    except UnicodeDecodeError:
        pass

    # 按 charset-normalizer 的候选顺序取第一个「受支持」编码。
    # 不能只用 .best()：它可能选中 utf_16_be/cp932 等我们不支持的零 chaos 候选，
    # 丢弃后 fallback 到 gb18030，会把 Big5 文本解成乱码。
    for match in from_bytes(content):
        detected = normalize_encoding(match.encoding)
        if not detected:
            continue
        try:
            text = content.decode(detected)
        except UnicodeDecodeError:
            continue
        # GBK 是 GB18030 的子集：能按 GBK 解码时优先标注更常见的 GBK
        if detected == "gb18030":
            try:
                return content.decode("gbk"), "gbk"
            except UnicodeDecodeError:
                pass
        return text, detected

    # 最后兜底：GB18030 覆盖 GBK/GB2312，Big5 覆盖港台文本
    for fallback in ("gb18030", "gbk", "big5"):
        try:
            return content.decode(fallback), fallback
        except UnicodeDecodeError:
            continue

    raise TextDecodeError("无法识别文件编码，请使用 UTF-8 / GBK / GB18030 编码的文件")


def encode_text(text: str, preferred: str | None = None) -> tuple[bytes, str]:
    """按首选编码写回文本；字符无法表示时回退 UTF-8，返回 (bytes, 实际编码)。"""
    encoding = normalize_encoding(preferred) or "utf-8"
    try:
        return text.encode(encoding), encoding
    except (UnicodeEncodeError, LookupError):
        return text.encode("utf-8"), "utf-8"
