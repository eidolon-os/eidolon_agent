"""SqlConversationRepository — start, finish, record_turn idempotency."""

from __future__ import annotations

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
