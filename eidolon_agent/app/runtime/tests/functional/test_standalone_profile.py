"""Standalone profile smoke test — the brain builds and runs a turn offline.

No NATS, no memory service, no coworker: the whole agent must come up with
in-process fakes so it can be exercised end to end (input -> full TurnEvent
stream + trace) with zero external dependencies.
"""

from __future__ import annotations

from contextlib import asynccontextmanager

import pytest

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
    )
    container = await build_application(settings=settings)
    try:
        yield container
    finally:
        # Drain post-turn persistence before disposing the store so no sqlite
        # connection is torn down under an in-flight write (a leaked connection
        # GC'd during a later test would surface as an unraisable warning).
        await container.background_tasks.drain(timeout_s=2.0)
        await container.data_store.close()


async def _run_turn(container, text: str):
    genome_id = container.agent_registry._default_genome_id  # type: ignore[attr-defined]
    inst = await container.agent_registry.resolve_for_caller(
        owner_id="alice",
        companion_id="companion-standalone",
        genome_id=genome_id,
    )
    return [ev async for ev in inst.agent.run_turn(make_turn_input(text))]


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


async def test_standalone_runs_a_full_turn_offline(tmp_path, monkeypatch) -> None:
    async with _standalone(tmp_path, monkeypatch) as container:
        events = await _run_turn(container, "你好")
        kinds = [e.kind for e in events]
        # Full turn completes against FakeLLM with the null memory port; recall
        # is degraded but the turn still finishes.
        assert kinds[-1] is TurnEventKind.DONE


async def test_standalone_stop_command_short_circuits_offline(tmp_path, monkeypatch) -> None:
    async with _standalone(tmp_path, monkeypatch) as container:
        events = await _run_turn(container, "停，别说了")
        done = [e for e in events if e.kind is TurnEventKind.DONE]
        assert done and done[0].data.get("termination_cause") == "user_stop"
