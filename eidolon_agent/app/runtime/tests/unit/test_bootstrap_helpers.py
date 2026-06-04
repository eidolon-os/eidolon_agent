"""Bootstrap helpers — small unit-testable bits of the DI wiring.

We don't bring up the full process. We test:
- ``_build_llm_router`` constructs a router with FakeLLM + configured models
- ``_build_llm_router`` falls back to "fake" when the default model isn't
  in the providers dict
- ``_generate_persisted_secret`` creates+reuses a secret file with 0600 mode

The full ``build_application`` call requires NATS + filesystem and is
covered by smoke tests, not here.
"""

from __future__ import annotations

import stat
import sys
from pathlib import Path

import pytest

from eidolon_agent.app.runtime.bootstrap import (
    _build_turn_engine,
    _build_llm_router,
    _generate_persisted_secret,
)
from eidolon_agent.app.runtime.container import Container
from eidolon_agent.config.settings import LLMModelConfig, Settings

pytestmark = pytest.mark.unit


def test_build_llm_router_includes_fake_and_configured_models() -> None:
    settings = Settings(
        llm={
            "models": [
                LLMModelConfig(name="openai/gpt-4o-mini"),
                LLMModelConfig(name="ollama/llama3"),
            ],
            "default_model": "openai/gpt-4o-mini",
        },
    )
    router = _build_llm_router(settings)
    assert router.model_id == "openai/gpt-4o-mini"
    # All three keys must be present in the router's provider table.
    keys = set(router._providers)  # type: ignore[attr-defined]
    assert {"fake", "openai/gpt-4o-mini", "ollama/llama3"} <= keys


def test_build_llm_router_falls_back_to_fake_when_default_missing() -> None:
    settings = Settings(
        llm={
            "models": [],
            "default_model": "openai/gpt-4o-mini",  # not in providers
        },
    )
    router = _build_llm_router(settings)
    assert router.model_id == "fake"


def test_persisted_secret_created_on_first_call(tmp_path: Path) -> None:
    target = tmp_path / "subdir" / "jwt-secret"
    s1 = _generate_persisted_secret(target)
    assert target.exists()
    assert len(s1) > 32
    # Mode 0600 (owner read/write only). Skip mode check on Windows.
    if sys.platform != "win32":
        mode = stat.S_IMODE(target.stat().st_mode)
        assert mode == 0o600


def test_persisted_secret_is_stable_across_calls(tmp_path: Path) -> None:
    target = tmp_path / "jwt-secret"
    s1 = _generate_persisted_secret(target)
    s2 = _generate_persisted_secret(target)
    assert s1 == s2  # reads existing file on second call


def test_build_turn_engine_wires_context_budget() -> None:
    settings = Settings(turn={"max_token_budget": 1234})
    container = Container(settings=settings)
    container.personas_service = object()
    container.history_manager = object()
    container.memory_port = object()
    container.llm_router = object()
    container.tool_dispatcher = object()
    container.history_fanout = object()
    container.triage_classifier = object()
    container.input_guardrail = object()
    container.output_guardrail = object()
    container.crisis_handler = object()
    container.event_bus = object()
    container.session_factory = object()

    engine = _build_turn_engine(
        container=container,
        instance_id="inst",
        template_id="tpl",
    )

    assert engine._compiler._context_budget_tokens == 1234  # type: ignore[attr-defined]
