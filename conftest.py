"""Shared pytest fixtures."""

from __future__ import annotations

import asyncio

import pytest
from eidolon_sdk.biz.persona import (
    PERSONA_REALIZER,
    build_default_persona_genome,
    persona_genome_hash,
)
from eidolon_sdk.core.runtime import BackgroundTaskRunner

from eidolon_agent.domain.agent.triage import TaskClassifier
from eidolon_agent.domain.context.compiler import ContextCompiler
from eidolon_agent.domain.guardrails import CrisisHandler, InputGuardrail, OutputGuardrail
from eidolon_agent.domain.history import HistoryFanout, HistoryManager
from eidolon_agent.domain.personas import PersonasService, StoredPersonaGenome
from eidolon_agent.domain.tools import ToolDispatcher, ToolRegistry
from eidolon_agent.domain.tools.builtin import EmitEventTool, SubmitLongTaskTool
from eidolon_agent.infra.events import InMemoryEventBus, InMemoryKVStore
from eidolon_agent.infra.llm import LLMRouter
from eidolon_agent.infra.llm.providers.fake import FakeLLM
from eidolon_agent.infra.persistence import (
    EidolonDataLongTaskStore,
    build_eidolon_data_history_hydrator,
    build_eidolon_data_turn_persister,
)


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
def persona_genome_store():
    return _InMemoryPersonaGenomeStore()


@pytest.fixture
async def personas_service(persona_genome_store):
    return PersonasService(store=persona_genome_store)


@pytest.fixture
async def turn_engine_factory(personas_service, event_bus):
    """Builds a minimal TurnEngine for tests."""

    def _factory(
        *,
        llm=None,
        memory_port=None,
        tool_dispatcher=None,
        history=None,
        data_store=None,
        tool_latency_policy=None,
        body_capability_provider=None,
    ):
        from eidolon_agent.domain.agent.turn import TurnEngine

        history = history or HistoryManager(
            hydrate_messages=(
                build_eidolon_data_history_hydrator(data_store)
                if data_store is not None
                else None
            )
        )
        fanout = HistoryFanout(event_bus=event_bus)
        if tool_dispatcher is None:
            tools = ToolRegistry()
            tools.register(EmitEventTool(event_bus=event_bus))
            task_store = (
                EidolonDataLongTaskStore(data_store)
                if data_store is not None
                else None
            )
            long_task_submitter = _ImmediateLongTaskSubmitter(
                task_store
            )
            tools.register(SubmitLongTaskTool(long_task_submitter=long_task_submitter))
            tool_dispatcher = ToolDispatcher(tools)

        def loc(_owner, _companion, _conv):
            return ("companion-test", "genome-test")

        compiler = ContextCompiler(
            personas_service=personas_service,
            instance_locator=loc,
            history_manager=history,
            memory_port=memory_port,
            history_window=20,
        )
        llm_router = LLMRouter(providers={"fake": llm or FakeLLM()}, default="fake")
        background_tasks = BackgroundTaskRunner(component="agent.test")
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
            genome_id="genome-test",
            memory_port=memory_port,
            turn_persister=(
                build_eidolon_data_turn_persister(
                    data_store,
                    model_id_provider=lambda: getattr(llm_router, "model_id", None),
                )
                if data_store is not None
                else None
            ),
            background_tasks=background_tasks,
            tool_latency_policy=tool_latency_policy,
            body_capability_provider=body_capability_provider,
        )

    return _factory


# Non-fixture test helpers (e.g. make_turn_input) live in tests.helpers.


class _ImmediateLongTaskSubmitter:
    def __init__(self, store: object | None = None) -> None:
        self.store = store
        self.records = []

    async def submit(self, record):
        self.records.append(record)
        if self.store is not None:
            await self.store.accept(record)


class _InMemoryPersonaGenomeStore:
    def __init__(self) -> None:
        genome = build_default_persona_genome(name="Test Companion")
        self.current = StoredPersonaGenome(
            owner_id="alice",
            companion_id="companion-test",
            genome_id="genome-test",
            genome_hash=persona_genome_hash(genome),
            realizer_version=PERSONA_REALIZER,
            version=1,
            genome=genome,
        )
        self.observations = []

    async def load_current(self, owner_id, companion_id):
        if (owner_id, companion_id) != (self.current.owner_id, self.current.companion_id):
            raise LookupError("persona not found")
        return self.current

    async def load_pinned(self, owner_id, companion_id, genome_id, genome_hash):
        current = await self.load_current(owner_id, companion_id)
        if (genome_id, genome_hash) != (current.genome_id, current.genome_hash):
            raise LookupError("persona pin not found")
        return current

    async def record_observation(self, event):
        self.observations.append(event)

    async def create_evolution_proposal(self, proposal):
        raise NotImplementedError

    async def approve_evolution(self, **kwargs):
        raise NotImplementedError

    async def reject_evolution(self, **kwargs):
        raise NotImplementedError

    async def rollback(self, **kwargs):
        raise NotImplementedError
