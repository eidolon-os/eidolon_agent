"""Shared pytest fixtures."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from eidolon_agent.brain import LLMRouter
from eidolon_agent.brain.llm.fake import FakeLLM
from eidolon_agent.context.compiler import ContextCompiler
from eidolon_agent.context.providers import (
    HistoryProvider,
    MindStateProvider,
    PersonaContextProvider,
)
from eidolon_agent.dispatch.classifier import TaskClassifier
from eidolon_agent.events import InMemoryEventBus, InMemoryKVStore
from eidolon_agent.guardrails import CrisisHandler, InputGuardrail, OutputGuardrail
from eidolon_agent.history import HistoryFanout, HistoryManager
from eidolon_agent.hooks import HookExecutor
from eidolon_agent.mind import MindStateService
from eidolon_agent.persona import (
    PersonaOverlayStore,
    PersonaResolver,
    PersonaTemplateRegistry,
)
from eidolon_agent.tools import ToolDispatcher, ToolRegistry
from eidolon_agent.tools.builtin import EmitEventTool, GetTimeTool


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
async def template_registry():
    reg = PersonaTemplateRegistry(Path("personas/templates"), watch=False)
    await reg.load_all()
    return reg


@pytest.fixture
def overlay_store(tmp_path):
    return PersonaOverlayStore(tmp_path / "overlays")


@pytest.fixture
async def resolver(template_registry, overlay_store, kv):
    return PersonaResolver(template_registry, overlay_store, kv_store=kv)


@pytest.fixture
async def turn_engine_factory(template_registry, overlay_store, resolver, event_bus):
    """Builds a minimal TurnEngine for tests."""

    def _factory(*, llm=None, dispatch_port=None):
        from eidolon_agent.agent.turn import TurnEngine

        history = HistoryManager()
        mind = MindStateService()
        fanout = HistoryFanout(event_bus=event_bus)
        tools = ToolRegistry()
        tools.register(GetTimeTool())
        tools.register(EmitEventTool(event_bus=event_bus))
        dispatcher = ToolDispatcher(tools)

        def loc(_tenant, _user, _conv):
            return ("inst-test", "caretaker_jiezhi")

        compiler = ContextCompiler(
            [
                PersonaContextProvider(resolver=resolver, instance_locator=loc),
                HistoryProvider(history_manager=history, window=20),
                MindStateProvider(mind_service=mind),
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
        )

    return _factory


def make_turn_input(text: str = "你好"):
    from eidolon_agent.core.types.identity import CallerContext, CallerKind, Identity
    from eidolon_agent.core.types.turn import TurnInput, TurnTrigger

    return TurnInput(
        turn_id="t1",
        conversation_id="c1",
        session_id="s1",
        caller=CallerContext(
            identity=Identity(tenant_id="t", user_id="alice", agent_instance_id="inst-test"),
            caller_kind=CallerKind.WEB_CHAT,
            trace_id="tr",
            request_id="rq",
        ),
        trigger=TurnTrigger.USER_UTTERANCE,
        text=text,
    )
