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
