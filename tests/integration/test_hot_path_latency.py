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

import time

import pytest

from eidolon_agent.core.types.turn import TurnEventKind
from eidolon_agent.infra.llm.providers.fake import FakeLLM
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
    assert elapsed_ms < 200, (
        f"turn took {elapsed_ms:.1f}ms — hot path regression (target: < 200ms)"
    )
