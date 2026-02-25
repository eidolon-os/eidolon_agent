"""统一日志配置：使用 loguru 或 logging 标准格式."""

import sys

from loguru import logger

from eidolon_agent.core.config import settings


def setup_logging() -> None:
    """配置 loguru：从 settings 读取 LOG_LEVEL，输出到 stderr."""
    logger.remove()
    logger.add(
        sys.stderr,
        level=settings.log_level.upper(),
        format="<green>{time:YYYY-MM-DD HH:mm:ss}</green> | <level>{level: <8}</level> | <cyan>{name}</cyan>:<cyan>{function}</cyan> - <level>{message}</level>",
    )
