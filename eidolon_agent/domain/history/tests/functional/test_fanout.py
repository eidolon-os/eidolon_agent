"""HistoryFanout — publishes turn events to memory + emotion subjects."""

from __future__ import annotations

import pytest

from eidolon_agent.domain.history import HistoryFanout

pytestmark = pytest.mark.functional


class _StubResolver:
    """Implements MemoryTurnSubjectResolver Protocol structurally."""

    def __init__(self, subject: str) -> None:
        self._subject = subject

    async def render_turn_subject(self, user_id: str) -> str:
        return self._subject


async def test_publish_turn_emits_memory_event(event_bus) -> None:
    received: list = []

    async def _on(ev) -> None:
        received.append(ev)

    await event_bus.subscribe("agent.memory.conversation.turn.alice", _on)
    fanout = HistoryFanout(event_bus=event_bus)
    await fanout.publish_turn(
        tenant_id="t",
        user_id="alice",
        session_id="s",
        turn_id="turn-1",
        user_text="你好",
        assistant_text="嗨",
        timestamp_iso="2026-05-22T10:00:00Z",
    )
    assert len(received) == 1
    payload = received[0].payload
    assert payload["user_id"] == "alice"
    assert payload["user_text"] == "你好"
    assert payload["assistant_text"] == "嗨"
    assert payload["metadata"]["source"] == "eidolon-agent"


async def test_publish_turn_uses_route_resolver_when_provided(event_bus) -> None:
    received: list = []

    async def _on(ev) -> None:
        received.append(ev)

    await event_bus.subscribe("custom.routed.subject", _on)
    fanout = HistoryFanout(
        event_bus=event_bus,
        memory_routes=_StubResolver("custom.routed.subject"),
    )
    await fanout.publish_turn(
        tenant_id="t",
        user_id="alice",
        session_id="s",
        turn_id="turn-1",
        user_text="hi",
        assistant_text="hello",
        timestamp_iso="2026-05-22T10:00:00Z",
    )
    assert len(received) == 1


async def test_publish_turn_emits_emotion_when_payload_given(event_bus) -> None:
    received: list = []

    async def _on(ev) -> None:
        received.append(ev)

    await event_bus.subscribe("agent.emotion.turn.alice", _on)
    fanout = HistoryFanout(event_bus=event_bus)
    await fanout.publish_turn(
        tenant_id="t",
        user_id="alice",
        session_id="s",
        turn_id="turn-2",
        user_text="x",
        assistant_text="y",
        timestamp_iso="2026-05-22T10:00:00Z",
        emotion_payload={"valence": -0.4},
    )
    assert len(received) == 1
    assert received[0].payload["emotion"]["valence"] == -0.4


async def test_publish_turn_is_noop_without_bus() -> None:
    fanout = HistoryFanout(event_bus=None)
    # Should not raise.
    await fanout.publish_turn(
        tenant_id="t",
        user_id="alice",
        session_id="s",
        turn_id="t",
        user_text="x",
        assistant_text="y",
        timestamp_iso="ts",
    )
