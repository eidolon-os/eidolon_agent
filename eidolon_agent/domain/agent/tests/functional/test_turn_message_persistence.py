"""Phase 34.C — verify user/assistant text lands in SQLite chat_messages.

Before 34.C, HistoryManager.append wrote only to an in-memory deque,
so admin's ``/conversations`` browse showed empty message arrays for
every real turn. This test drives a turn end-to-end against a real
SQLite database (no mocks) and asserts both the TurnRow AND the
USER + ASSISTANT chat_messages exist after the fire-and-forget
``_persist_turn`` task drains.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from sqlalchemy import select

from eidolon_agent.config.settings import SqliteSettings
from eidolon_agent.infra.persistence import (
    build_turn_persister,
    create_engine,
    create_session_factory,
    ensure_schema,
)
from eidolon_agent.infra.persistence.models import ChatMessageRow, TurnRow
from tests.helpers import make_turn_input

pytestmark = pytest.mark.functional


@pytest.mark.asyncio
async def test_turn_persists_user_and_assistant_messages(
    tmp_path: Path,
    turn_engine_factory,
) -> None:
    """A successful turn should write a TurnRow plus two chat_messages
    (user + assistant) into SQLite, FK-consistent, queryable by the
    admin conversations endpoint."""
    engine = create_engine(SqliteSettings(path=tmp_path / "agent.sqlite3"))
    await ensure_schema(engine)
    session_factory = create_session_factory(engine)

    # Rebuild the engine factory's TurnEngine with our session_factory.
    # Reaching past the fixture closure is intentional — we need the
    # SAME wiring as production EXCEPT that the persistence destination
    # is our tmp_path SQLite, not the test-default in-memory one.
    from eidolon_agent.domain.agent.turn import TurnEngine

    base_engine = turn_engine_factory()
    test_engine = TurnEngine(
        compiler=base_engine._compiler,
        llm=base_engine._llm,
        tool_dispatcher=base_engine._tools,
        history=base_engine._history,
        fanout=base_engine._fanout,
        triage=base_engine._triage,
        input_guardrail=base_engine._input_g,
        output_guardrail=base_engine._output_g,
        crisis=base_engine._crisis,
        event_bus=base_engine._bus,
        personas_service=base_engine._personas,
        persona_template_id="caretaker_jiezhi",
        turn_persister=build_turn_persister(
            session_factory,
            model_id_provider=lambda: getattr(base_engine._llm, "model_id", None),
        ),
    )

    # Drive the turn. We don't care about the exact text — the fake
    # LLM behind turn_engine_factory always produces something.
    ti = make_turn_input("铁锤几岁了？")
    _events = [ev async for ev in test_engine.run(ti)]

    # _persist_turn is fire-and-forget. Yield to the loop until the
    # task has had time to complete the write. Three yields is plenty
    # for an in-process SQLite write.
    for _ in range(5):
        await asyncio.sleep(0)
    # Some CI hardware is slow; give one bounded real wait too.
    await asyncio.sleep(0.05)

    async with session_factory() as session:
        turn_row = (
            await session.execute(select(TurnRow).where(TurnRow.id == ti.turn_id))
        ).scalar_one_or_none()
        assert turn_row is not None, "TurnRow missing — _persist_turn didn't run"
        assert turn_row.conversation_id == ti.conversation_id
        trace = (turn_row.metadata_ or {}).get("turn_trace")
        assert trace is not None
        assert trace["schema_version"] == "turn_trace.v1"
        assert trace["boundary"] == "eidolon_agent.brain"
        assert trace["turn"]["turn_id"] == ti.turn_id
        assert trace["context_ledger"]["segments"]
        assert trace["latency"]["total_ms"] is not None
        assert trace["latency"]["first_delta_ms"] <= trace["latency"]["total_ms"]
        assert trace["privacy"]["mode"] == "normal"
        assert trace["memory_write_trace"]["source_turn_id"] == ti.turn_id
        assert trace["memory_write_trace"]["fanout_allowed"] is True
        assert trace["development_guards"]["context_budget"]["mode"] == "disabled"
        assert trace["development_guards"]["context_budget"]["configured"] is False
        assert trace["development_guards"]["memory_write_policy"]["mode"] == "enabled"
        assert trace["development_guards"]["tool_policy"]["max_tool_iters"] == 4

        messages = (
            (
                await session.execute(
                    select(ChatMessageRow)
                    .where(ChatMessageRow.turn_id == ti.turn_id)
                    .order_by(ChatMessageRow.created_at)
                )
            )
            .scalars()
            .all()
        )

    assert len(messages) == 2, (
        f"expected 2 chat_messages (user + assistant), got {len(messages)}: "
        f"{[(m.role, m.content[:40]) for m in messages]}"
    )
    roles = {m.role for m in messages}
    assert roles == {"user", "assistant"}, (
        f"unexpected roles {roles}; user/assistant required"
    )
    user_msg = next(m for m in messages if m.role == "user")
    assistant_msg = next(m for m in messages if m.role == "assistant")
    assert user_msg.content == "铁锤几岁了？"
    assert assistant_msg.content, "assistant content empty — would render as blank bubble"
    assert assistant_msg.tokens is not None or assistant_msg.tokens == 0

    await engine.dispose()


@pytest.mark.asyncio
async def test_persist_skips_messages_when_text_empty(
    tmp_path: Path,
    turn_engine_factory,
) -> None:
    """The error path doesn't have an assistant_text; _persist_turn must
    still write the TurnRow (latency / status / error_code) but skip
    the chat_messages append entirely — no rows with empty content."""
    from eidolon_agent.core.types.turn import TriageKind, TurnStatus
    from eidolon_agent.domain.agent.turn import TurnEngine

    sql_engine = create_engine(SqliteSettings(path=tmp_path / "agent.sqlite3"))
    await ensure_schema(sql_engine)
    session_factory = create_session_factory(sql_engine)

    base_engine = turn_engine_factory()
    test_engine = TurnEngine(
        compiler=base_engine._compiler,
        llm=base_engine._llm,
        tool_dispatcher=base_engine._tools,
        history=base_engine._history,
        fanout=base_engine._fanout,
        triage=base_engine._triage,
        input_guardrail=base_engine._input_g,
        output_guardrail=base_engine._output_g,
        crisis=base_engine._crisis,
        event_bus=base_engine._bus,
        personas_service=base_engine._personas,
        persona_template_id="caretaker_jiezhi",
        turn_persister=build_turn_persister(
            session_factory,
            model_id_provider=lambda: getattr(base_engine._llm, "model_id", None),
        ),
    )

    from datetime import datetime, timezone

    ti = make_turn_input("only-user-text")
    await test_engine._persist_turn(
        ti=ti,
        status=TurnStatus.ERRORED,
        triage_kind=TriageKind.SIMPLE,
        started_at=datetime.now(timezone.utc),
        finished_at=datetime.now(timezone.utc),
        first_delta_ms=None,
        total_ms=100,
        usage_in=0,
        usage_out=0,
        error_code="internal",
        timings={},
        user_text="",
        assistant_text="",
    )

    async with session_factory() as session:
        turn_row = (
            await session.execute(select(TurnRow).where(TurnRow.id == ti.turn_id))
        ).scalar_one_or_none()
        assert turn_row is not None
        assert turn_row.status == "errored"
        messages = (
            (
                await session.execute(
                    select(ChatMessageRow).where(ChatMessageRow.turn_id == ti.turn_id)
                )
            )
            .scalars()
            .all()
        )

    assert messages == [], "empty text should not insert blank chat_messages rows"
    await sql_engine.dispose()


@pytest.mark.asyncio
async def test_crisis_turn_persists_private_user_and_assistant_messages(
    tmp_path: Path,
    turn_engine_factory,
) -> None:
    sql_engine = create_engine(SqliteSettings(path=tmp_path / "agent.sqlite3"))
    await ensure_schema(sql_engine)
    session_factory = create_session_factory(sql_engine)

    engine = turn_engine_factory(session_factory=session_factory)
    ti = make_turn_input("我想死，活不下去了")
    events = [ev async for ev in engine.run(ti)]
    assert events[-1].data.get("crisis") is True

    for _ in range(5):
        await asyncio.sleep(0)
    await asyncio.sleep(0.05)

    async with session_factory() as session:
        turn_row = (
            await session.execute(select(TurnRow).where(TurnRow.id == ti.turn_id))
        ).scalar_one_or_none()
        assert turn_row is not None
        messages = (
            (
                await session.execute(
                    select(ChatMessageRow)
                    .where(ChatMessageRow.turn_id == ti.turn_id)
                    .order_by(ChatMessageRow.created_at)
                )
            )
            .scalars()
            .all()
        )

    assert [m.role for m in messages] == ["user", "assistant"]
    assert all(m.is_private for m in messages)
    assert "听到你了" in messages[1].content
    assert turn_row.metadata_["turn_trace"]["privacy"]["mode"] == "private"
    await sql_engine.dispose()
