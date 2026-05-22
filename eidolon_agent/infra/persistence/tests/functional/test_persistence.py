"""SQLite + repositories smoke tests."""

import uuid
from datetime import datetime, timezone

import pytest

from eidolon_agent.config.settings import SqliteSettings
from eidolon_agent.core.types.messages import ChatMessage, MessageRole
from eidolon_agent.infra.persistence import (
    SqlAlchemyUnitOfWork,
    create_engine,
    create_session_factory,
    ensure_schema,
)

pytestmark = pytest.mark.functional

@pytest.fixture
async def uow_factory():
    eng = create_engine(SqliteSettings(path=":memory:"))
    await ensure_schema(eng)
    sf = create_session_factory(eng)

    def _factory():
        return SqlAlchemyUnitOfWork(sf)

    yield _factory
    await eng.dispose()


@pytest.mark.asyncio
async def test_chat_message_roundtrip(uow_factory):
    conv_id = uuid.uuid4().hex
    turn_id = uuid.uuid4().hex
    async with uow_factory() as uow:
        await uow.conversations.start(
            conversation_id=conv_id, tenant_id="t", user_id="u", agent_instance_id="i"
        )
        # Need a Turn row before chat_messages (FK)
        from eidolon_agent.core.types.turn import (
            TriageKind,
            TurnResult,
            TurnStatus,
            TurnTrigger,
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
                latency_first_delta_ms=120,
                total_latency_ms=350,
                model="fake:scripted",
                seq_in_conversation=1,
            )
        )
        await uow.chat_messages.append(
            turn_id,
            ChatMessage(id=uuid.uuid4().hex, role=MessageRole.USER, content="你好",
                       created_at=datetime.now(timezone.utc)),
        )
        await uow.chat_messages.append(
            turn_id,
            ChatMessage(id=uuid.uuid4().hex, role=MessageRole.ASSISTANT, content="嗨",
                       created_at=datetime.now(timezone.utc)),
        )
        await uow.commit()

    async with uow_factory() as uow:
        msgs = await uow.chat_messages.list_for_turn(turn_id)
    assert [m.role for m in msgs] == [MessageRole.USER, MessageRole.ASSISTANT]
    assert [m.content for m in msgs] == ["你好", "嗨"]
