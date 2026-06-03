"""Shared pytest fixtures."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from eidolon_agent.domain.agent.triage import TaskClassifier
from eidolon_agent.domain.context.compiler import ContextCompiler
from eidolon_agent.domain.guardrails import CrisisHandler, InputGuardrail, OutputGuardrail
from eidolon_agent.domain.history import HistoryFanout, HistoryManager
from eidolon_agent.domain.personas import (
    PersonasService,
    PersonaTemplateRegistry,
    YamlPersonaInstanceStore,
)
from eidolon_agent.domain.tools import ToolDispatcher, ToolRegistry
from eidolon_agent.domain.tools.builtin import EmitEventTool, GetTimeTool
from eidolon_agent.infra.events import InMemoryEventBus, InMemoryKVStore
from eidolon_agent.infra.llm import LLMRouter
from eidolon_agent.infra.llm.providers.fake import FakeLLM
from eidolon_agent.infra.persistence import build_history_hydrator, build_turn_persister


@pytest.fixture
def event_loop():
    """pytest-asyncio compatibility for fresh loop per test."""
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


@pytest.fixture
async def event_bus():
    return InMemoryEventBus()


@pytest.fixture
async def kv():
    return InMemoryKVStore("EIDOLON_CACHE")


@pytest.fixture
async def canonical_template_registry():
    reg = PersonaTemplateRegistry(Path("eidolon_agent/domain/personas/templates"))
    await reg.load_all()
    return reg


@pytest.fixture
def persona_instance_store(tmp_path):
    return YamlPersonaInstanceStore(tmp_path / "instances")


@pytest.fixture
async def personas_service(canonical_template_registry, persona_instance_store):
    return PersonasService(
        registry=canonical_template_registry,
        instances=persona_instance_store,
    )


@pytest.fixture
async def turn_engine_factory(personas_service, event_bus):
    """Builds a minimal TurnEngine for tests."""

    def _factory(*, llm=None, memory_port=None, tool_dispatcher=None, history=None, session_factory=None):
        from eidolon_agent.domain.agent.turn import TurnEngine

        history = history or HistoryManager(
            hydrate_messages=(
                build_history_hydrator(session_factory)
                if session_factory is not None
                else None
            )
        )
        fanout = HistoryFanout(event_bus=event_bus)
        if tool_dispatcher is None:
            tools = ToolRegistry()
            tools.register(GetTimeTool())
            tools.register(EmitEventTool(event_bus=event_bus))
            tool_dispatcher = ToolDispatcher(tools)

        def loc(_tenant, _user, _conv):
            return ("inst-test", "caretaker_jiezhi")

        compiler = ContextCompiler(
            personas_service=personas_service,
            instance_locator=loc,
            history_manager=history,
            memory_port=memory_port,
            history_window=20,
        )
        llm_router = LLMRouter(providers={"fake": llm or FakeLLM()}, default="fake")
        return TurnEngine(
            compiler=compiler,
            llm=llm_router,
            tool_dispatcher=tool_dispatcher,
            history=history,
            fanout=fanout,
            triage=TaskClassifier(),
            input_guardrail=InputGuardrail(),
            output_guardrail=OutputGuardrail(),
            crisis=CrisisHandler(event_bus=event_bus),
            event_bus=event_bus,
            personas_service=personas_service,
            persona_template_id="caretaker_jiezhi",
            memory_port=memory_port,
            turn_persister=(
                build_turn_persister(
                    session_factory,
                    model_id_provider=lambda: getattr(llm_router, "model_id", None),
                )
                if session_factory is not None
                else None
            ),
        )

    return _factory


# Non-fixture test helpers (e.g. make_turn_input) live in tests.helpers.
