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
    # Bridge stdlib → loguru via a single root handler. Child loggers propagate
    # to root, so do NOT also attach handlers per-logger (that duplicates lines).
    root = logging.getLogger()
    root.handlers = [_InterceptHandler()]
    root.setLevel(settings.log_level)
    # Quiet noisy third-party libs.
    noisy = {
        "sqlalchemy.engine": logging.WARNING,  # per-cursor DEBUG dump
        "uvicorn.access": logging.INFO,
        # LiteLLM cost-map lookups for unmapped models trace at DEBUG every call.
        "LiteLLM": logging.WARNING,
        "litellm": logging.WARNING,
        "litellm.utils": logging.WARNING,
        "litellm.cost_calculator": logging.WARNING,
    }
    for name, level in noisy.items():
        logging.getLogger(name).setLevel(level)
