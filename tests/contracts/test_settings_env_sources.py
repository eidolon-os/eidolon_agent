"""Where the Agent is allowed to get its environment from.

On a Host the secrets arrive through systemd's ``EnvironmentFile``, which pid 1
reads as root; ``/etc/eidolon/agent.env`` is mode 0600 root:root and the
``eidolon`` service user cannot open it. The Agent used to insist on finding a
dotenv anyway and died at import with ``FileNotFoundError`` before it could
serve ``/readyz`` — the whole Pi install failed its readiness gate on it.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from eidolon_agent.config.settings import _resolve_env_path

pytestmark = pytest.mark.unit


def test_a_process_handed_its_environment_needs_no_dotenv(monkeypatch, tmp_path) -> None:
    monkeypatch.delenv("EIDOLON_AGENT_ENV_FILE", raising=False)
    monkeypatch.setattr(
        "eidolon_agent.config.settings._DEFAULT_ENV", tmp_path / "absent" / ".env"
    )

    assert _resolve_env_path() is None


def test_a_named_env_file_is_a_promise(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("EIDOLON_AGENT_ENV_FILE", str(tmp_path / "never-written.env"))

    with pytest.raises(FileNotFoundError, match="EIDOLON_AGENT_ENV_FILE"):
        _resolve_env_path()


def test_the_developer_dotenv_is_still_found(monkeypatch, tmp_path) -> None:
    dotenv = tmp_path / ".env"
    dotenv.write_text("EIDOLON_AGENT_LLM_API_KEY=sk-local\n", encoding="utf-8")
    monkeypatch.delenv("EIDOLON_AGENT_ENV_FILE", raising=False)
    monkeypatch.setattr("eidolon_agent.config.settings._DEFAULT_ENV", dotenv)

    assert _resolve_env_path() == dotenv.resolve()


def test_settings_load_without_any_dotenv(monkeypatch, tmp_path) -> None:
    """The end the Host actually exercises: no file anywhere, env already set."""

    from eidolon_agent.config.settings import load_settings

    settings_yaml = Path(__file__).resolve().parents[2] / "config" / "settings.yaml"
    if not settings_yaml.is_file():
        pytest.skip("no local config/settings.yaml to load")
    monkeypatch.delenv("EIDOLON_AGENT_ENV_FILE", raising=False)
    monkeypatch.setattr(
        "eidolon_agent.config.settings._DEFAULT_ENV", tmp_path / "absent" / ".env"
    )
    monkeypatch.setenv("EIDOLON_AGENT_SETTINGS_YAML", str(settings_yaml))
    os.environ.setdefault("EIDOLON_AGENT_LLM_API_KEY", "sk-test")

    assert load_settings() is not None
