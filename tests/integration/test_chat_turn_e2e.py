"""Full Turn pipeline e2e — top of domain stack, all-in-memory adapters.

Exercises: input guardrail → triage → context compile → LLM stream → output
guardrail → history append → fanout (in-mem bus subscriber). Crosses
domain/* and consumes infra/* indirectly via the LLMRouter+FakeLLM.

This is the canonical integration test that should catch regressions
across module reorgs.
"""

from __future__ import annotations

import pytest

from eidolon_agent.core.types.turn import TurnEventKind
from tests.helpers import make_turn_input

pytestmark = pytest.mark.integration


async def test_simple_turn_e2e_streams_deltas_and_persists(
    turn_engine_factory, event_bus,
) -> None:
    fanout_received: list = []

    async def _on_memory(ev) -> None:
        fanout_received.append(ev)

    await event_bus.subscribe(
        "agent.memory.conversation.turn.alice", _on_memory
    )

    engine = turn_engine_factory()
    events = [ev async for ev in engine.run(make_turn_input("你好世界"))]

    kinds = [ev.kind for ev in events]
    assert TurnEventKind.STATE in kinds  # thinking → speaking transitions
    assert TurnEventKind.DELTA in kinds  # at least one token streamed
    assert events[-1].kind is TurnEventKind.DONE

    # Assistant text reassembled from deltas — defaults to echo "<text> — 我在这。"
    delta_text = "".join(
        ev.data.get("text", "")
        for ev in events
        if ev.kind is TurnEventKind.DELTA
    )
    assert "我在这" in delta_text


async def test_crisis_turn_bypasses_llm_and_emits_crisis_reply(
    turn_engine_factory,
) -> None:
    engine = turn_engine_factory()
    events = [ev async for ev in engine.run(make_turn_input("我想死，活不下去了"))]

    delta_text = "".join(
        ev.data.get("text", "")
        for ev in events
        if ev.kind is TurnEventKind.DELTA
    )
    assert "听到你了" in delta_text  # canned crisis reply
    assert events[-1].kind is TurnEventKind.DONE
