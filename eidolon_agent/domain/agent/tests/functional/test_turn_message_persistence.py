"""Verify turn/message persistence through the Agent runtime authority.

These tests drive a real TurnEngine against a temporary AgentRuntimeStore and
assert the durable ``turns`` and ``messages`` rows are
created with the admin-facing trace metadata intact.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import select

from eidolon_agent.core.types.turn import TriageKind, TurnStatus
from eidolon_agent.infra.persistence.runtime_store import (
    AgentRuntimeStore,
    ConversationRow,
    MessageRow,
    TurnRow,
)
from tests.helpers import make_turn_input

pytestmark = pytest.mark.functional


@pytest.mark.asyncio
async def test_turn_persists_user_and_assistant_messages(
    tmp_path: Path,
    turn_engine_factory,
) -> None:
    store = await _runtime_store(tmp_path)
    try:
        engine = turn_engine_factory(runtime_store=store)
        ti = make_turn_input("铁锤几岁了？")
        _events = [ev async for ev in engine.run(ti)]

        await engine._background.drain(timeout_s=1)

        async with store.session_factory() as session:
            turn_row = await session.get(TurnRow, ti.turn_id)
            assert turn_row is not None, "TurnRow missing - _persist_turn did not run"
            assert turn_row.conversation_id == ti.conversation_id
            assert turn_row.source_device_id == ti.context.device_id
            conversation_row = await session.get(ConversationRow, ti.conversation_id)
            assert conversation_row is not None
            assert conversation_row.updated_at is not None
            trace = turn_row.trace_json
            assert trace is not None
            assert trace["schema_version"] == "turn_trace.v1"
            assert trace["boundary"] == "eidolon_agent.brain"
            assert trace["turn"]["turn_id"] == ti.turn_id
            assert trace["context_ledger"]["segments"]
            assert trace["commitment_context_trace"]["attempted"] is False
            assert trace["latency"]["total_ms"] is not None
            assert trace["latency"]["first_delta_ms"] <= trace["latency"]["total_ms"]
            assert trace["privacy"]["mode"] == "normal"
            assert trace["memory_write_trace"]["source_turn_id"] == ti.turn_id
            # Semantic relevance belongs to Memory's steward. Agent forwards
            # every normal committed turn without phrase classification.
            assert trace["memory_write_trace"]["fanout_allowed"] is True
            assert trace["development_guards"]["context_budget"]["mode"] == "disabled"
            assert trace["development_guards"]["context_budget"]["configured"] is False
            assert trace["development_guards"]["memory_write_policy"]["mode"] == "enabled"
            assert trace["development_guards"]["tool_policy"]["max_tool_iters"] == 4

            messages = await _messages_for_turn(session, ti.turn_id)

        assert len(messages) == 2, (
            f"expected 2 messages (user + assistant), got {len(messages)}: "
            f"{[(m.role, m.content[:40]) for m in messages]}"
        )
        assert [m.role for m in messages] == ["user", "assistant"]
        assert [m.seq for m in messages] == [0, 1]
        assert messages[0].content == "铁锤几岁了？"
        assert messages[1].content, "assistant content empty - would render as blank bubble"
        assert messages[1].metadata_json.get("tokens") is not None
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_persist_skips_messages_when_text_empty(
    tmp_path: Path,
    turn_engine_factory,
) -> None:
    store = await _runtime_store(tmp_path)
    try:
        engine = turn_engine_factory(runtime_store=store)
        ti = make_turn_input("only-user-text")
        await engine._persist_turn(
            ti=ti,
            status=TurnStatus.ERRORED,
            triage_kind=TriageKind.SIMPLE,
            started_at=datetime.now(UTC),
            finished_at=datetime.now(UTC),
            first_delta_ms=None,
            total_ms=100,
            usage_in=0,
            usage_out=0,
            error_code="internal",
            timings={},
            user_text="",
            assistant_text="",
        )

        async with store.session_factory() as session:
            turn_row = await session.get(TurnRow, ti.turn_id)
            assert turn_row is not None
            assert turn_row.status == "errored"
            assert turn_row.source_device_id == ti.context.device_id
            messages = await _messages_for_turn(session, ti.turn_id)

        assert messages == [], "empty text should not insert blank message rows"
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_crisis_turn_persists_private_user_and_assistant_messages(
    tmp_path: Path,
    turn_engine_factory,
) -> None:
    store = await _runtime_store(tmp_path)
    try:
        engine = turn_engine_factory(runtime_store=store)
        ti = make_turn_input("我想死，活不下去了")
        events = [ev async for ev in engine.run(ti)]
        assert events[-1].data.get("crisis") is True

        await engine._background.drain(timeout_s=1)

        async with store.session_factory() as session:
            turn_row = await session.get(TurnRow, ti.turn_id)
            assert turn_row is not None
            messages = await _messages_for_turn(session, ti.turn_id)

        assert [m.role for m in messages] == ["user", "assistant"]
        assert all(m.visibility == "private" for m in messages)
        assert all(m.metadata_json.get("is_private") is True for m in messages)
        assert "听到你了" in messages[1].content
        assert turn_row.trace_json["privacy"]["mode"] == "private"
    finally:
        await store.close()


async def _runtime_store(tmp_path: Path) -> AgentRuntimeStore:
    store = AgentRuntimeStore.open(tmp_path / "eidolon-agent.sqlite3")
    await store.init_schema()
    return store


async def _messages_for_turn(session, turn_id: str) -> list[MessageRow]:
    rows = (
        (
            await session.execute(
                select(MessageRow)
                .where(MessageRow.turn_id == turn_id)
                .order_by(MessageRow.seq, MessageRow.message_id)
            )
        )
        .scalars()
        .all()
    )
    return list(rows)
