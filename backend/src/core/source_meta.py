"""中医文献来源可信度元数据（AGENTS.md 中医约束第 6 条）。

- source_type 与 era 为受控枚举，枚举外取值拒绝入库
- credibility_level 按 source_type 固定映射自动推导，不接受手工输入
- 映射为业务硬性规则，不得自行改动：
  国家标准=5、规划教材=4、经典古籍=3、后世医家=2、民间偏方=1
"""

# 来源类型（documents.source_type VARCHAR(16)，最长 4 个汉字）
SOURCE_TYPE_NATIONAL_STANDARD = "国家标准"
SOURCE_TYPE_TEXTBOOK = "规划教材"
SOURCE_TYPE_CLASSIC = "经典古籍"
SOURCE_TYPE_LATER_PHYSICIAN = "后世医家"
SOURCE_TYPE_FOLK_REMEDY = "民间偏方"

SOURCE_TYPES: tuple[str, ...] = (
    SOURCE_TYPE_NATIONAL_STANDARD,
    SOURCE_TYPE_TEXTBOOK,
    SOURCE_TYPE_CLASSIC,
    SOURCE_TYPE_LATER_PHYSICIAN,
    SOURCE_TYPE_FOLK_REMEDY,
)

# 成书/出版年代（documents.era VARCHAR(8)）
ERAS: tuple[str, ...] = ("先秦", "汉", "唐", "宋", "明", "清", "现代")

# 来源类型 → 可信度等级（固定映射，唯一事实源）
CREDIBILITY_MAP: dict[str, int] = {
    SOURCE_TYPE_NATIONAL_STANDARD: 5,
    SOURCE_TYPE_TEXTBOOK: 4,
    SOURCE_TYPE_CLASSIC: 3,
    SOURCE_TYPE_LATER_PHYSICIAN: 2,
    SOURCE_TYPE_FOLK_REMEDY: 1,
}

# 可信度等级 → 颜色语义（前端/日志展示用，后端不做样式）
CREDIBILITY_MIN = 1
CREDIBILITY_MAX = 5


def is_valid_source_type(value: str | None) -> bool:
    """来源类型是否为受控枚举值（None 视为未标注，允许，兼容历史文档）。"""
    return value is None or value in SOURCE_TYPES


def is_valid_era(value: str | None) -> bool:
    """年代是否为受控枚举值（None 视为未标注，允许）。"""
    return value is None or value in ERAS


def credibility_for(source_type: str | None) -> int | None:
    """按固定映射由来源类型推导可信度等级；未标注来源返回 None。"""
    if source_type is None:
        return None
    return CREDIBILITY_MAP.get(source_type)
