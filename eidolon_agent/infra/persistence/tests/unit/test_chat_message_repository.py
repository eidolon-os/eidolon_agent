"""SqlChatMessageRepository — append, list_for_conversation, delete_for_user."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest

from eidolon_agent.core.types.messages import ChatMessage, MessageRole
from eidolon_agent.core.types.turn import (
    TriageKind,
    TurnResult,
    TurnStatus,
    TurnTrigger,
)

pytestmark = pytest.mark.unit


def _msg(text: str, role: MessageRole = MessageRole.USER, *, when: datetime | None = None) -> ChatMessage:
    return ChatMessage(
        id=uuid.uuid4().hex,
        role=role,
        content=text,
        created_at=when or datetime.now(timezone.utc),
    )


async def _seed_turn(uow, conv_id: str, turn_id: str) -> None:
    await uow.conversations.start(
        conversation_id=conv_id, tenant_id="t", user_id="u", agent_instance_id="i"
    )
    await uow.conversations.record_turn(
        TurnResult(
            turn_id=turn_id,
            conversation_id=conv_id,
            status=TurnStatus.OK,
            triage_kind=TriageKind.SIMPLE,
            trigger=TurnTrigger.USER_UTTERANCE,
            seq_count=2,
            started_at=datetime.now(timezone.utc),
            finished_at=datetime.now(timezone.utc),
            latency_first_delta_ms=100,
            total_latency_ms=200,
            seq_in_conversation=1,
        )
    )


async def test_append_and_list_for_turn(uow_factory) -> None:
    conv_id, turn_id = uuid.uuid4().hex, uuid.uuid4().hex
    async with uow_factory() as uow:
        await _seed_turn(uow, conv_id, turn_id)
        await uow.chat_messages.append(turn_id, _msg("user msg", MessageRole.USER))
        await uow.chat_messages.append(turn_id, _msg("bot msg", MessageRole.ASSISTANT))
        await uow.commit()
    async with uow_factory() as uow:
        msgs = await uow.chat_messages.list_for_turn(turn_id)
    assert [m.role for m in msgs] == [MessageRole.USER, MessageRole.ASSISTANT]


async def test_list_for_conversation_respects_limit(uow_factory) -> None:
    conv_id, turn_id = uuid.uuid4().hex, uuid.uuid4().hex
    now = datetime.now(timezone.utc)
    async with uow_factory() as uow:
        await _seed_turn(uow, conv_id, turn_id)
        for i in range(5):
            await uow.chat_messages.append(
                turn_id, _msg(f"m{i}", when=now + timedelta(seconds=i))
            )
        await uow.commit()
    async with uow_factory() as uow:
        msgs = await uow.chat_messages.list_for_conversation(conv_id, limit=3)
    # list_for_conversation orders desc internally then reverses → returns the
    # latest `limit` messages in chronological order.
    assert [m.content for m in msgs] == ["m2", "m3", "m4"]


async def test_list_for_conversation_before_filter(uow_factory) -> None:
    conv_id, turn_id = uuid.uuid4().hex, uuid.uuid4().hex
    now = datetime.now(timezone.utc)
    async with uow_factory() as uow:
        await _seed_turn(uow, conv_id, turn_id)
        for i in range(4):
            await uow.chat_messages.append(
                turn_id, _msg(f"m{i}", when=now + timedelta(seconds=i))
            )
        await uow.commit()
    cutoff = now + timedelta(seconds=2)
    async with uow_factory() as uow:
        msgs = await uow.chat_messages.list_for_conversation(conv_id, before=cutoff)
    assert [m.content for m in msgs] == ["m0", "m1"]


async def test_delete_for_user_cascades_messages(uow_factory) -> None:
    conv_id, turn_id = uuid.uuid4().hex, uuid.uuid4().hex
    async with uow_factory() as uow:
        await _seed_turn(uow, conv_id, turn_id)
        await uow.chat_messages.append(turn_id, _msg("hi"))
        await uow.commit()
    async with uow_factory() as uow:
        deleted = await uow.chat_messages.delete_for_user("u")
        await uow.commit()
    assert deleted >= 1
    async with uow_factory() as uow:
        msgs = await uow.chat_messages.list_for_turn(turn_id)
    assert msgs == []
