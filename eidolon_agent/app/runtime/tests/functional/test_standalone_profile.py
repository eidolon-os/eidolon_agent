"""Standalone profile smoke test — the brain builds and runs a turn offline.

No NATS, no memory service, no coworker: the whole agent must come up with
in-process fakes so it can be exercised end to end (input -> full TurnEvent
stream + trace) with zero external dependencies.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import replace

import pytest
from eidolon_sdk.biz.persona import PersonaObservationEvent

from eidolon_agent.app.runtime.bootstrap import build_application
from eidolon_agent.config.settings import Settings
from eidolon_agent.core.types.turn import TurnEventKind
from eidolon_agent.infra.events.adapters.inmem import InMemoryEventBus
from eidolon_agent.infra.memory.null_port import NullMemoryPort
from tests.helpers import make_turn_input

pytestmark = pytest.mark.functional


@asynccontextmanager
async def _standalone(tmp_path, monkeypatch):
    monkeypatch.setenv("EIDOLON_DATA_SQLITE_PATH", str(tmp_path / "eidolon.sqlite3"))
    monkeypatch.setenv("EIDOLON_RUNTIME_TOKEN_JWT_SECRET", "x" * 40)
    settings = Settings(
        runtime={
            "standalone": True,
            "warmup_enabled": False,
            "run_dir": str(tmp_path / "run"),
            "log_dir": str(tmp_path / "logs"),
            "debug_dir": str(tmp_path / "debug"),
        },
        observability={"log_dir": str(tmp_path / "logs")},
        persistence={"sqlite_path": str(tmp_path / "eidolon-agent.sqlite3")},
    )
    container = await build_application(settings=settings)
    await container.local_system_data.owner_commands.create_owner(
        owner_id="alice",
        display_name="Alice",
    )
    container.extras[
        "standalone_workspace"
    ] = await container.local_system_data.companion_workspaces.provision_workspace(
        owner_id="alice",
        companion_display_name="Test Companion",
    )
    try:
        yield container
    finally:
        # Drain post-turn persistence before disposing the store so no sqlite
        # connection is torn down under an in-flight write (a leaked connection
        # GC'd during a later test would surface as an unraisable warning).
        await container.background_tasks.drain(timeout_s=2.0)
        await container.local_system_data.close()
        await container.runtime_store.close()


async def _run_turn(container, text: str):
    workspace = container.extras["standalone_workspace"]
    genome = workspace.persona_genome
    inst = await container.agent_registry.resolve_runtime(
        owner_id="alice",
        companion_id=workspace.companion.companion_id,
        genome_id=genome.genome_id,
    )
    ti = make_turn_input(text)
    context = replace(
        ti.context,
        companion_id=workspace.companion.companion_id,
        memory_realm_id=workspace.memory_realm.realm_id,
        genome_id=genome.genome_id,
        schema_version=genome.schema_version,
        genome_hash=genome.genome_hash,
        realizer_version=genome.realizer_version,
    )
    ti = replace(ti, context=context)
    return [ev async for ev in inst.agent.run_turn(ti)]


async def test_standalone_builds_with_inprocess_fakes(tmp_path, monkeypatch) -> None:
    async with _standalone(tmp_path, monkeypatch) as container:
        # No external services wired.
        assert isinstance(container.event_bus, InMemoryEventBus)
        assert isinstance(container.memory_port, NullMemoryPort)
        # No coworker worker in standalone.
        assert "long_task_worker" not in container.extras
        # Core brain surfaces are present.
        assert container.agent_registry is not None
        assert container.grpc_server is not None
        assert container.kv_buckets  # in-memory KV buckets provisioned
        assert container.tool_registry.names() == [
            "delegate_to_coworker",
            "emit_event",
            "get_time",
            "get_weather",
        ]


async def test_standalone_runs_a_full_turn_offline(tmp_path, monkeypatch) -> None:
    async with _standalone(tmp_path, monkeypatch) as container:
        events = await _run_turn(container, "你好")
        kinds = [e.kind for e in events]
        # Full turn completes against FakeLLM with the null memory port; recall
        # is degraded but the turn still finishes.
        assert kinds[-1] is TurnEventKind.DONE


async def test_standalone_preserves_persona_observation_command(
    tmp_path,
    monkeypatch,
) -> None:
    async with _standalone(tmp_path, monkeypatch) as container:
        workspace = container.extras["standalone_workspace"]
        await container.personas_service.record_observation(
            PersonaObservationEvent(
                observation_id="standalone-observation-1",
                owner_id="alice",
                companion_id=workspace.companion.companion_id,
                kind="interaction",
                source="agent",
                summary="Owner prefers concise replies.",
            )
        )

        pending = await container.local_system_data.audit_outbox.list_pending()
        assert any(
            event.event_id == "standalone-observation-1"
            and event.action == "persona.observation.created"
            for event in pending
        )


async def test_standalone_stop_text_is_a_normal_committed_request(tmp_path, monkeypatch) -> None:
    async with _standalone(tmp_path, monkeypatch) as container:
        events = await _run_turn(container, "停，别说了")
        done = [e for e in events if e.kind is TurnEventKind.DONE]
        deltas = [e for e in events if e.kind is TurnEventKind.DELTA]
        assert done and deltas
        assert "termination_cause" not in done[0].data
