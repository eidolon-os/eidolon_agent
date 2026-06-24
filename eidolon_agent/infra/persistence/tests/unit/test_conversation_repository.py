"""SqlConversationRepository — start, finish, record_turn idempotency."""

from __future__ import annotations

import dataclasses
import uuid
from datetime import datetime, timezone

import pytest

from eidolon_agent.core.types.turn import (
    TriageKind,
    TurnResult,
    TurnStatus,
    TurnTrigger,
)
from eidolon_agent.infra.persistence.models import ConversationRow, TurnRow

pytestmark = pytest.mark.unit


def _result(turn_id: str, conv_id: str, *, status: TurnStatus = TurnStatus.OK) -> TurnResult:
    return TurnResult(
        turn_id=turn_id,
        conversation_id=conv_id,
        status=status,
        triage_kind=TriageKind.SIMPLE,
        trigger=TurnTrigger.USER_UTTERANCE,
        seq_count=1,
        started_at=datetime.now(timezone.utc),
        finished_at=datetime.now(timezone.utc),
        latency_first_delta_ms=100,
        total_latency_ms=200,
        model="fake",
        seq_in_conversation=1,
    )


async def test_start_inserts_conversation_row(uow_factory) -> None:
    conv_id = uuid.uuid4().hex
    async with uow_factory() as uow:
        await uow.conversations.start(
            conversation_id=conv_id, tenant_id="t", user_id="u", agent_instance_id="i"
        )
        await uow.commit()
    async with uow_factory() as uow:
        row = await uow._session.get(ConversationRow, conv_id)  # type: ignore[attr-defined]
    assert row is not None
    assert row.user_id == "u"
    assert row.ended_at is None


async def test_finish_sets_ended_at_and_title(uow_factory) -> None:
    conv_id = uuid.uuid4().hex
    async with uow_factory() as uow:
        await uow.conversations.start(
            conversation_id=conv_id, tenant_id="t", user_id="u", agent_instance_id="i"
        )
        await uow.conversations.finish(conv_id, title="A nice chat")
        await uow.commit()
    async with uow_factory() as uow:
        row = await uow._session.get(ConversationRow, conv_id)  # type: ignore[attr-defined]
    assert row.ended_at is not None
    assert row.title == "A nice chat"


async def test_finish_unknown_conversation_is_noop(uow_factory) -> None:
    async with uow_factory() as uow:
        # Should not raise.
        await uow.conversations.finish("ghost-conv", title="x")
        await uow.commit()


async def test_record_turn_inserts_then_updates(uow_factory) -> None:
    conv_id, turn_id = uuid.uuid4().hex, uuid.uuid4().hex
    async with uow_factory() as uow:
        await uow.conversations.start(
            conversation_id=conv_id, tenant_id="t", user_id="u", agent_instance_id="i"
        )
        await uow.conversations.record_turn(_result(turn_id, conv_id))
        await uow.commit()

    async with uow_factory() as uow:
        # Re-record with a different status — upsert path should run.
        await uow.conversations.record_turn(_result(turn_id, conv_id, status=TurnStatus.ERRORED))
        await uow.commit()

    async with uow_factory() as uow:
        row = await uow._session.get(TurnRow, turn_id)  # type: ignore[attr-defined]
    assert row.status == TurnStatus.ERRORED.value


async def test_ensure_started_is_idempotent(uow_factory) -> None:
    conv_id = uuid.uuid4().hex
    async with uow_factory() as uow:
        await uow.conversations.ensure_started(
            conversation_id=conv_id, tenant_id="t", user_id="u", agent_instance_id="i"
        )
        # Second call with different fields must NOT raise (no duplicate insert)
        # and must NOT overwrite the original row.
        await uow.conversations.ensure_started(
            conversation_id=conv_id, tenant_id="t2", user_id="u2", agent_instance_id="i2"
        )
        await uow.commit()
    async with uow_factory() as uow:
        row = await uow._session.get(ConversationRow, conv_id)  # type: ignore[attr-defined]
    assert row.user_id == "u"  # original preserved


async def test_count_turns_assigns_unique_seq(uow_factory) -> None:
    conv_id = uuid.uuid4().hex
    async with uow_factory() as uow:
        await uow.conversations.ensure_started(
            conversation_id=conv_id, tenant_id="t", user_id="u", agent_instance_id="i"
        )
        assert await uow.conversations.count_turns(conv_id) == 0
        # Record two turns using count as the per-conversation seq — the
        # (conversation_id, seq) UNIQUE constraint must not be violated.
        for _ in range(2):
            seq = await uow.conversations.count_turns(conv_id)
            r = _result(uuid.uuid4().hex, conv_id)
            await uow.conversations.record_turn(
                dataclasses.replace(r, seq_in_conversation=seq)
            )
            await uow.commit()
        assert await uow.conversations.count_turns(conv_id) == 2


async def test_record_turn_persists_metadata_timings(uow_factory) -> None:
    conv_id, turn_id = uuid.uuid4().hex, uuid.uuid4().hex
    timings = {"guard_ms": 1, "triage_ms": 0, "compile_ms": 20, "first_delta_ms": 180}
    async with uow_factory() as uow:
        await uow.conversations.ensure_started(
            conversation_id=conv_id, tenant_id="t", user_id="u", agent_instance_id="i"
        )
        base = _result(turn_id, conv_id)
        await uow.conversations.record_turn(
            dataclasses.replace(base, metadata=timings)
        )
        await uow.commit()
    async with uow_factory() as uow:
        row = await uow._session.get(TurnRow, turn_id)  # type: ignore[attr-defined]
    assert row.metadata_ == timings


async def test_record_turn_persists_device_id(uow_factory) -> None:
    conv_id, turn_id = uuid.uuid4().hex, uuid.uuid4().hex
    async with uow_factory() as uow:
        await uow.conversations.ensure_started(
            conversation_id=conv_id, tenant_id="t", user_id="u", agent_instance_id="i"
        )
        base = _result(turn_id, conv_id)
        await uow.conversations.record_turn(
            dataclasses.replace(base, device_id="1c:db:d4:7a:ef:0c", caller_kind="livekit_voice")
        )
        await uow.commit()
    async with uow_factory() as uow:
        row = await uow._session.get(TurnRow, turn_id)  # type: ignore[attr-defined]
    assert row.device_id == "1c:db:d4:7a:ef:0c"
    assert row.caller_kind == "livekit_voice"


async def test_record_turn_reupsert_keeps_device_id(uow_factory) -> None:
    """A later partial re-record (device_id=None) must not null the stamped value."""
    conv_id, turn_id = uuid.uuid4().hex, uuid.uuid4().hex
    async with uow_factory() as uow:
        await uow.conversations.ensure_started(
            conversation_id=conv_id, tenant_id="t", user_id="u", agent_instance_id="i"
        )
        await uow.conversations.record_turn(
            dataclasses.replace(_result(turn_id, conv_id), device_id="dev-1")
        )
        await uow.commit()
    async with uow_factory() as uow:
        # Re-record without a device_id (default None) — must preserve "dev-1".
        await uow.conversations.record_turn(
            _result(turn_id, conv_id, status=TurnStatus.ERRORED)
        )
        await uow.commit()
    async with uow_factory() as uow:
        row = await uow._session.get(TurnRow, turn_id)  # type: ignore[attr-defined]
    assert row.device_id == "dev-1"
