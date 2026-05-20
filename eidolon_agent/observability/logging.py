"""loguru configuration. Writes JSON to stderr + a daily file under ``log_dir``."""

from __future__ import annotations

import logging
import sys
from pathlib import Path

from loguru import logger

from eidolon_agent.config.settings import ObservabilitySettings


class _InterceptHandler(logging.Handler):
    """Bridge stdlib logging → loguru. Important for FastAPI / SQLAlchemy / nats-py."""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            level = logger.level(record.levelname).name
        except ValueError:
            level = record.levelno
        depth = 2
        frame = logging.currentframe()
        while frame and frame.f_code.co_filename == logging.__file__:
            frame = frame.f_back
            depth += 1
        logger.opt(depth=depth, exception=record.exc_info).log(level, record.getMessage())


def configure_logging(settings: ObservabilitySettings) -> None:
    logger.remove()
    logger.add(
        sys.stderr,
        level=settings.log_level,
        serialize=settings.log_json,
        backtrace=False,
        diagnose=False,
        enqueue=False,
    )
    log_dir = Path(settings.log_dir).expanduser()
    log_dir.mkdir(parents=True, exist_ok=True)
    logger.add(
        log_dir / "eidolon-agent.log",
        level=settings.log_level,
        serialize=settings.log_json,
        rotation="10 MB",
        retention="14 days",
        enqueue=True,
    )
    # Bridge stdlib
    logging.basicConfig(handlers=[_InterceptHandler()], level=settings.log_level, force=True)
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access", "fastapi", "grpc", "sqlalchemy"):
        logging.getLogger(name).handlers = [_InterceptHandler()]
        logging.getLogger(name).propagate = False
