"""时间工具：统一 UTC 时间语义，替代已弃用的 datetime.utcnow()。"""

from datetime import datetime, timezone


def utcnow() -> datetime:
    """当前 UTC 时间（naive），替代 datetime.utcnow()（Python 3.12 起弃用）。

    保持 naive 语义与库中 timestamp without time zone 列一致，
    避免 asyncpg 对 timezone=False 列写入 aware datetime 报类型不匹配。
    """
    return datetime.now(timezone.utc).replace(tzinfo=None)
