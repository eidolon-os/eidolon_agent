"""Agent-side contract tests for the eidolon_memory and eidolon_data seams.

These pin what eidolon_agent *consumes and produces* at each cross-project
boundary, and run entirely in-process — no NATS, no memory service, no live
neighbor. If a neighbor changes a contract out from under the agent, these
fail here instead of only at integration time.

Companion to the SDK-side contracts (dialogue_control, memory space id) which
pin the shared types themselves.
"""

from __future__ import annotations

import asyncio

import pytest
from eidolon_data import DataSettings, DataStore
from eidolon_memory_contracts import (
    ConversationTurnPayload,
    conversation_turn_subject,
    derive_memory_space_id,
    unwrap_memory_payload,
)

from eidolon_agent.core.types.event import Event
from eidolon_agent.domain.history.fanout import HistoryFanout
from eidolon_agent.infra.events.adapters.inmem import InMemoryEventBus

pytestmark = pytest.mark.integration


# --- agent -> eidolon_memory (NATS turn fanout) ----------------------------


async def test_fanout_payload_round_trips_to_memory_contract() -> None:
    """The agent's turn fanout must publish exactly the envelope+payload shape
    eidolon_memory ingests, on the SDK-derived subject."""
    bus = InMemoryEventBus()
    realm = "realm-abc"
    subject = conversation_turn_subject(derive_memory_space_id(realm))
    received: list[Event] = []

    async def _handler(ev: Event) -> None:
        received.append(ev)

    await bus.subscribe(subject, _handler)

    fanout = HistoryFanout(event_bus=bus)
    await fanout.publish_turn(
        owner_id="alice",
        companion_id="companion-1",
        memory_realm_id=realm,
        device_id="dev-1",
        session_id="s1",
        turn_id="t1",
        user_text="你好",
        assistant_text="你好呀",
        timestamp_iso="2026-07-03T00:00:00+00:00",
        trace_id="trace-xyz",
        metadata={"memory_write_disposition": "semantic_upsert"},
    )
    await asyncio.sleep(0)

    assert len(received) == 1
    # Memory unwraps the versioned envelope, then parses the turn payload.
    envelope = received[0].payload
    # The correlation id rides the envelope (channel->agent->memory tracing).
    assert envelope.get("trace_id") == "trace-xyz"
    payload = ConversationTurnPayload.model_validate(unwrap_memory_payload(envelope))
    assert payload.turn_id == "t1"
    assert payload.user_text == "你好"
    assert payload.assistant_text == "你好呀"
    # memory_space_id is what the steward keys ingestion on.
    assert payload.context.memory_space_id == derive_memory_space_id(realm)
    assert payload.metadata["memory_write_disposition"] == "semantic_upsert"


# --- agent -> eidolon_data (repository surface the adapters call) -----------

_REQUIRED_DATASTORE_ATTRS = (
    "events",
    "companions",
    "owner_service",
    "owner_data_ops",
    "runtime_callers",
    "runtime_sessions",
    "workspace_provisioning",
    "session_factory",
    "init_schema",
    "close",
)


async def test_datastore_exposes_surface_agent_depends_on(tmp_path) -> None:
    """Agent's persistence adapters call these DataStore members; pin them so a
    data-layer rename fails here, not deep in a background persist."""
    store = DataStore.open(DataSettings(sqlite_path=str(tmp_path / "eidolon.sqlite3")))
    try:
        await store.init_schema()
        for attr in _REQUIRED_DATASTORE_ATTRS:
            assert hasattr(store, attr), f"DataStore missing agent-required '{attr}'"
    finally:
        await store.close()


async def test_datastore_provisioning_roundtrip(tmp_path) -> None:
    """The provisioning path the agent relies on (owner -> workspace) works and
    the runtime persistence validation can see the companion."""
    store = DataStore.open(DataSettings(sqlite_path=str(tmp_path / "eidolon.sqlite3")))
    try:
        await store.init_schema()
        await store.owner_service.create_owner(owner_id="alice", display_name="alice")
        await store.workspace_provisioning.provision_workspace(
            owner_id="alice",
            companion_id="companion-1",
            genome_id="genome-1",
            realm_id="realm-1",
        )
        from eidolon_data.schema.models import CompanionRow

        async with store.session_factory() as session:
            row = await session.get(CompanionRow, "companion-1")
        assert row is not None
        assert row.owner_id == "alice"
    finally:
        await store.close()
