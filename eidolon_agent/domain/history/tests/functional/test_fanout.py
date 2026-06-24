"""HistoryFanout — publishes turn events to memory + emotion subjects."""

from __future__ import annotations

import asyncio

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
    await asyncio.sleep(0)
    assert len(received) == 1
    payload = received[0].payload
    assert payload["context"]["owner_user_id"] == "alice"
    assert payload["user_text"] == "你好"
    assert payload["assistant_text"] == "嗨"
    assert payload["metadata"]["source"] == "eidolon-agent"
    assert payload["metadata"]["source_turn_id"] == "turn-1"
    assert payload["metadata"]["tenant_id"] == "t"


async def test_publish_turn_carries_resolved_actor_context(event_bus) -> None:
    received: list = []

    async def _on(ev) -> None:
        received.append(ev)

    await event_bus.subscribe("agent.memory.conversation.turn.alice", _on)
    fanout = HistoryFanout(event_bus=event_bus)
    await fanout.publish_turn(
        tenant_id="acme",
        user_id="alice",
        session_id="s",
        turn_id="turn-1",
        user_text="hi",
        assistant_text="hello",
        timestamp_iso="2026-05-22T10:00:00Z",
        device_id="dev-1",
        agent_instance_id="inst-9",
        persona_id="mochi",
    )
    await asyncio.sleep(0)

    ctx = received[0].payload["context"]
    assert ctx["tenant_id"] == "acme"
    assert ctx["owner_user_id"] == "alice"
    assert ctx["persona_id"] == "mochi"
    assert ctx["device_id"] == "dev-1"
    assert ctx["instance_id"] == "inst-9"
    assert ctx["session_id"] == "s"
    # persona partition key reflects the real persona, not a default sentinel.
    assert ctx["memory_space_id"] == "acme.alice.mochi"


async def test_publish_turn_merges_memory_policy_metadata(event_bus) -> None:
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
        user_text="以后叫我小满",
        assistant_text="好的",
        timestamp_iso="2026-05-22T10:00:00Z",
        metadata={
            "memory_write_disposition": "semantic_upsert",
            "memory_write_reason": "stable_preference_or_identity",
            "conversation_id": "c1",
        },
    )
    await asyncio.sleep(0)

    metadata = received[0].payload["metadata"]
    assert metadata["source"] == "eidolon-agent"
    assert metadata["source_component"] == "history.fanout"
    assert metadata["memory_write_disposition"] == "semantic_upsert"
    assert metadata["conversation_id"] == "c1"


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
    await asyncio.sleep(0)
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
    await asyncio.sleep(0)
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
