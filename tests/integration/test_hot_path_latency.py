"""Hot-path latency benchmark — pure framework overhead, no real LLM.

The plan promises the hot path (excluding the LLM call) stays under
~250ms — most of which is the 200ms memory recall budget. With memory
disabled and FakeLLM serving deltas immediately, the framework should
itself complete a turn in well under 100ms wall-clock.

This test guards against regressions like "we accidentally added an
awaited NATS publish on the critical path" or "ContextCompiler started
loading something heavy on every call".
"""

from __future__ import annotations

import statistics
import time

import pytest

from eidolon_agent.core.types.turn import TurnEventKind
from eidolon_agent.domain.history import HistoryFanout
from eidolon_agent.infra.llm.providers.fake import FakeLLM
from eidolon_agent.infra.persistence.runtime_store import AgentRuntimeStore
from tests.helpers import make_turn_input

pytestmark = pytest.mark.integration


async def test_first_delta_under_100ms_with_fake_llm(turn_engine_factory) -> None:
    """Wall-clock from input → first DELTA must beat 100ms with a no-op LLM.

    Memory is None in the test factory (no recall), so this is pure framework.
    """
    engine = turn_engine_factory(llm=FakeLLM(per_token_delay_s=0))
    t0 = time.monotonic()
    saw_delta = False
    async for ev in engine.run(make_turn_input("你好")):
        if ev.kind is TurnEventKind.DELTA:
            saw_delta = True
            elapsed_ms = (time.monotonic() - t0) * 1000
            break
    assert saw_delta, "engine never emitted a DELTA"
    assert elapsed_ms < 100, (
        f"first DELTA took {elapsed_ms:.1f}ms — hot path regression "
        f"(target: < 100ms with FakeLLM + no memory)"
    )


async def test_done_under_200ms_with_fake_llm(turn_engine_factory) -> None:
    """Full turn (input → DONE) under 200ms with no-op LLM and no memory."""
    engine = turn_engine_factory(llm=FakeLLM(per_token_delay_s=0))
    t0 = time.monotonic()
    done = False
    async for ev in engine.run(make_turn_input("你好")):
        if ev.kind is TurnEventKind.DONE:
            done = True
            elapsed_ms = (time.monotonic() - t0) * 1000
            break
    assert done
    assert elapsed_ms < 200, f"turn took {elapsed_ms:.1f}ms — hot path regression (target: < 200ms)"


async def test_durable_memory_enqueue_p95_under_50ms(tmp_path) -> None:
    """The reply path waits only for a local durable commit, never NATS/Memory."""

    store = AgentRuntimeStore.open(tmp_path / "agent-runtime.sqlite3")
    await store.init_schema()
    fanout = HistoryFanout(memory_outbox=store.memory_turn_outbox)
    samples_ms: list[float] = []
    try:
        for index in range(50):
            started = time.perf_counter()
            status = await fanout.publish_turn(
                owner_id="owner-latency",
                companion_id="companion-latency",
                memory_realm_id="realm-latency",
                device_id="device-latency",
                session_id="session-latency",
                turn_id=f"turn-latency-{index}",
                user_text=f"natural turn {index}",
                assistant_text="",
                timestamp_iso="2026-08-31T00:00:00+00:00",
            )
            samples_ms.append((time.perf_counter() - started) * 1000)
            assert status.state == "queued"

        p95_ms = statistics.quantiles(samples_ms, n=100)[94]
        assert p95_ms < 50, (
            f"durable memory enqueue p95={p95_ms:.1f}ms; "
            "the turn path must only pay for a local SQLite commit"
        )
        assert await store.memory_turn_outbox.pending_count() == 50
    finally:
        await store.close()
