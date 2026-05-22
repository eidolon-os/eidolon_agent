"""Shared pytest fixtures."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from eidolon_agent.domain.context.compiler import ContextCompiler
from eidolon_agent.domain.context.providers import (
    HistoryProvider,
    PersonasContextProvider,
)
from eidolon_agent.domain.dispatch.classifier import TaskClassifier
from eidolon_agent.domain.guardrails import CrisisHandler, InputGuardrail, OutputGuardrail
from eidolon_agent.domain.history import HistoryFanout, HistoryManager
from eidolon_agent.domain.hooks import HookExecutor
from eidolon_agent.domain.personas import (
    PersonaInstanceStore,
    PersonasService,
    PersonaTemplateRegistry,
)
from eidolon_agent.domain.tools import ToolDispatcher, ToolRegistry
from eidolon_agent.domain.tools.builtin import EmitEventTool, GetTimeTool
from eidolon_agent.infra.events import InMemoryEventBus, InMemoryKVStore
from eidolon_agent.infra.llm import LLMRouter
from eidolon_agent.infra.llm.providers.fake import FakeLLM


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
    return PersonaInstanceStore(tmp_path / "instances")


@pytest.fixture
async def personas_service(canonical_template_registry, persona_instance_store):
    return PersonasService(
        registry=canonical_template_registry,
        instances=persona_instance_store,
    )


@pytest.fixture
async def turn_engine_factory(personas_service, event_bus):
    """Builds a minimal TurnEngine for tests."""

    def _factory(*, llm=None, dispatch_port=None):
        from eidolon_agent.domain.agent.turn import TurnEngine

        history = HistoryManager()
        fanout = HistoryFanout(event_bus=event_bus)
        tools = ToolRegistry()
        tools.register(GetTimeTool())
        tools.register(EmitEventTool(event_bus=event_bus))
        dispatcher = ToolDispatcher(tools)

        def loc(_tenant, _user, _conv):
            return ("inst-test", "caretaker_jiezhi")

        compiler = ContextCompiler(
            [
                PersonasContextProvider(
                    personas_service=personas_service,
                    instance_locator=loc,
                ),
                HistoryProvider(history_manager=history, window=20),
            ],
            max_token_budget=2000,
        )
        return TurnEngine(
            compiler=compiler,
            llm=LLMRouter(providers={"fake": llm or FakeLLM()}, default="fake"),
            tool_dispatcher=dispatcher,
            hook_executor=HookExecutor(),
            history=history,
            fanout=fanout,
            triage=TaskClassifier(),
            input_guardrail=InputGuardrail(),
            output_guardrail=OutputGuardrail(),
            crisis=CrisisHandler(event_bus=event_bus),
            dispatch_port=dispatch_port,
            event_bus=event_bus,
            personas_service=personas_service,
            persona_template_id="caretaker_jiezhi",
        )

    return _factory


# Non-fixture test helpers (e.g. make_turn_input) live in tests.helpers.
