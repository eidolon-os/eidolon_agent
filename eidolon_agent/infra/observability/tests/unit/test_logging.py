"""configure_logging — verifies layered handler config + noise suppression."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from eidolon_agent.config.settings import ObservabilitySettings
from eidolon_agent.infra.observability import configure_logging

pytestmark = pytest.mark.unit


def test_configure_logging_attaches_root_handler(tmp_path: Path) -> None:
    settings = ObservabilitySettings(
        log_level="INFO",
        log_json=True,
        log_dir=tmp_path,
        metrics_enabled=False,
    )
    configure_logging(settings)
    root = logging.getLogger()
    # Exactly one handler — our InterceptHandler, not duplicated per child logger.
    assert len(root.handlers) == 1


def test_configure_logging_silences_noisy_third_parties(tmp_path: Path) -> None:
    settings = ObservabilitySettings(log_level="DEBUG", log_dir=tmp_path)
    configure_logging(settings)
    # SQLAlchemy and LiteLLM cost-map noise must be pinned to WARNING+
    for name in ("sqlalchemy.engine", "LiteLLM", "litellm", "litellm.utils"):
        assert logging.getLogger(name).level >= logging.WARNING, (
            f"{name} should be quieted to WARNING+, got level={logging.getLogger(name).level}"
        )


def test_log_dir_is_created(tmp_path: Path) -> None:
    target = tmp_path / "nested" / "logs"
    configure_logging(ObservabilitySettings(log_dir=target))
    assert target.is_dir()


def test_intercept_handler_forwards_stdlib_logs_to_loguru(tmp_path: Path) -> None:
    """Logs emitted via the stdlib ``logging`` module must reach loguru.

    We re-configure logging into ``tmp_path``, emit one INFO + one WARNING
    via the stdlib logger, and assert both lines land in the rotating file.
    """
    configure_logging(
        ObservabilitySettings(log_level="DEBUG", log_json=False, log_dir=tmp_path)
    )
    logger = logging.getLogger("eidolon.test.intercept")
    logger.info("hello-stdlib-info")
    logger.warning("hello-stdlib-warning")

    # loguru's file sink is enqueued; flush by detaching it.
    from loguru import logger as loguru_logger
    loguru_logger.remove()

    log_file = tmp_path / "eidolon-agent.log"
    assert log_file.exists()
    content = log_file.read_text(encoding="utf-8")
    assert "hello-stdlib-info" in content
    assert "hello-stdlib-warning" in content


def test_intercept_handler_falls_back_on_unknown_level(tmp_path: Path) -> None:
    """Custom log level names are not in loguru's table — handler must use
    the numeric level rather than raising ``ValueError``."""
    configure_logging(
        ObservabilitySettings(log_level="DEBUG", log_json=False, log_dir=tmp_path)
    )
    # Register a custom level (numerically valid, but unknown to loguru).
    logging.addLevelName(11, "CUSTOM")
    logger = logging.getLogger("eidolon.test.custom_level")
    logger.log(11, "custom-level-message")

    from loguru import logger as loguru_logger
    loguru_logger.remove()

    content = (tmp_path / "eidolon-agent.log").read_text(encoding="utf-8")
    assert "custom-level-message" in content
