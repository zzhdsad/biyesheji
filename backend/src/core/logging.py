"""Loguru 结构化日志配置。"""

import sys

from loguru import logger

from src.core.config import settings


def setup_logging() -> None:
    """初始化日志：控制台 + 按天轮转文件，关键链路记录耗时与状态。"""
    logger.remove()
    logger.add(
        sys.stderr,
        level=settings.LOG_LEVEL,
        format=(
            "<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> | "
            "<level>{level: <8}</level> | "
            "<cyan>{name}</cyan>:<cyan>{line}</cyan> - <level>{message}</level>"
        ),
    )
    logger.add(
        "logs/app_{time:YYYY-MM-DD}.log",
        rotation="00:00",
        retention="7 days",
        level=settings.LOG_LEVEL,
        encoding="utf-8",
        enqueue=True,
    )
