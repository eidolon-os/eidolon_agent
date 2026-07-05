"""HistoryFanout — publishes turn events to memory + emotion subjects."""

from __future__ import annotations

import asyncio

import pytest
from eidolon_sdk.memory import (
    MEMORY_SCHEMA_VERSION,
    conversation_turn_subject,
    unwrap_memory_payload,
)

from eidolon_agent.domain.history import HistoryFanout

pytestmark = pytest.mark.functional


class _StubResolver:
    """Implements MemoryTurnSubjectResolver Protocol structurally."""

    def __init__(self, subject: str) -> None:
        self._subject = subject

    async def render_turn_subject(self, memory_space_id: str) -> str:
        assert memory_space_id == "t.alice.default"
        return self._subject


class _StatusSink:
    def __init__(self) -> None:
        self.statuses = []

    async def record_memory_fanout(self, status) -> None:
        self.statuses.append(status)


async def test_publish_turn_emits_memory_event(event_bus) -> None:
    received: list = []

    async def _on(ev) -> None:
        received.append(ev)

    await event_bus.subscribe(conversation_turn_subject("r_alice_default"), _on)
    sink = _StatusSink()
    fanout = HistoryFanout(event_bus=event_bus, status_sink=sink)
    status = await fanout.publish_turn(
        owner_id="alice",
        companion_id="companion-a",
        memory_realm_id="r_alice_default",
        device_id=None,
        session_id="s",
        turn_id="turn-1",
        user_text="你好",
        assistant_text="嗨",
        timestamp_iso="2026-05-22T10:00:00Z",
    )
    await asyncio.sleep(0)
    assert len(received) == 1
    assert received[0].payload["schema_version"] == MEMORY_SCHEMA_VERSION
    payload = unwrap_memory_payload(received[0].payload)
    assert payload["context"]["owner_id"] == "alice"
    assert payload["context"]["companion_id"] == "companion-a"
    assert payload["context"]["memory_realm_id"] == "r_alice_default"
    assert payload["user_text"] == "你好"
    assert payload["assistant_text"] == "嗨"
    assert payload["metadata"]["source"] == "eidolon-agent"
    assert payload["metadata"]["source_turn_id"] == "turn-1"
    assert status.state == "published"
    assert sink.statuses[-1].turn_id == "turn-1"
    # No explicit trace_id → falls back to turn_id, so the agent status and the
    # memory-absorbed event end up sharing one correlation id.
    assert status.trace_id == "turn-1"


async def test_publish_turn_threads_explicit_trace_id(event_bus) -> None:
    sink = _StatusSink()
    fanout = HistoryFanout(event_bus=event_bus, status_sink=sink)
    status = await fanout.publish_turn(
        owner_id="alice",
        companion_id="companion-a",
        memory_realm_id="r_alice_default",
        device_id=None,
        session_id="s",
        turn_id="turn-9",
        user_text="hi",
        assistant_text="yo",
        timestamp_iso="2026-05-22T10:00:00Z",
        trace_id="trace-abc",
    )
    assert status.trace_id == "trace-abc"
    assert sink.statuses[-1].trace_id == "trace-abc"


async def test_publish_turn_carries_resolved_actor_context(event_bus) -> None:
    received: list = []

    async def _on(ev) -> None:
        received.append(ev)

    await event_bus.subscribe(conversation_turn_subject("acme.alice.mochi"), _on)
    fanout = HistoryFanout(event_bus=event_bus)
    await fanout.publish_turn(
        owner_id="alice",
        companion_id="mochi",
        memory_realm_id="acme.alice.mochi",
        device_id="dev-1",
        session_id="s",
        turn_id="turn-1",
        user_text="hi",
        assistant_text="hello",
        timestamp_iso="2026-05-22T10:00:00Z",
    )
    await asyncio.sleep(0)

    ctx = unwrap_memory_payload(received[0].payload)["context"]
    assert ctx["owner_id"] == "alice"
    assert ctx["companion_id"] == "mochi"
    assert ctx["memory_realm_id"] == "acme.alice.mochi"
    assert ctx["device_id"] == "dev-1"
    assert ctx["session_id"] == "s"
    assert ctx["memory_space_id"] == "acme.alice.mochi"


async def test_publish_turn_merges_memory_policy_metadata(event_bus) -> None:
    received: list = []

    async def _on(ev) -> None:
        received.append(ev)

    await event_bus.subscribe(conversation_turn_subject("t.alice.default"), _on)
    fanout = HistoryFanout(event_bus=event_bus)
    await fanout.publish_turn(
        owner_id="alice",
        companion_id="default",
        memory_realm_id="t.alice.default",
        device_id=None,
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

    metadata = unwrap_memory_payload(received[0].payload)["metadata"]
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
        owner_id="alice",
        companion_id="default",
        memory_realm_id="t.alice.default",
        device_id=None,
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
        owner_id="alice",
        companion_id="default",
        memory_realm_id="t.alice.default",
        device_id=None,
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
    sink = _StatusSink()
    fanout = HistoryFanout(event_bus=None, status_sink=sink)
    # Should not raise.
    status = await fanout.publish_turn(
        owner_id="alice",
        companion_id="default",
        memory_realm_id="t.alice.default",
        device_id=None,
        session_id="s",
        turn_id="t",
        user_text="x",
        assistant_text="y",
        timestamp_iso="ts",
    )
    assert status.state == "skipped_no_bus"
    assert sink.statuses[-1].state == "skipped_no_bus"
