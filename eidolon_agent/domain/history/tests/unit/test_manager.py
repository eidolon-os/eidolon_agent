"""HistoryManager — in-memory window semantics."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from eidolon_agent.core.types.messages import ChatMessage, MessageRole
from eidolon_agent.domain.history import HistoryManager

pytestmark = pytest.mark.unit


def _msg(
    text: str,
    role: MessageRole = MessageRole.USER,
    *,
    private: bool = False,
    created_at: datetime | None = None,
) -> ChatMessage:
    return ChatMessage(
        id=uuid.uuid4().hex,
        role=role,
        content=text,
        created_at=created_at or datetime.now(UTC),
        metadata={"is_private": True} if private else {},
    )


async def test_append_and_recent_window() -> None:
    mgr = HistoryManager()
    for t in ("a", "b", "c"):
        await mgr.append(conversation_id="c1", message=_msg(t))
    items = await mgr.recent_window(conversation_id="c1", window=10)
    assert [m.content for m in items] == ["a", "b", "c"]


async def test_window_param_caps_returned_size() -> None:
    mgr = HistoryManager()
    for t in ("a", "b", "c", "d"):
        await mgr.append(conversation_id="c1", message=_msg(t))
    items = await mgr.recent_window(conversation_id="c1", window=2)
    assert [m.content for m in items] == ["c", "d"]


async def test_window_size_bounds_buffer() -> None:
    mgr = HistoryManager(window_size=3)
    for t in ("a", "b", "c", "d", "e"):
        await mgr.append(conversation_id="c1", message=_msg(t))
    items = await mgr.recent_window(conversation_id="c1", window=10)
    assert [m.content for m in items] == ["c", "d", "e"]  # oldest dropped


async def test_recent_returns_empty_for_unknown_conversation() -> None:
    mgr = HistoryManager()
    assert await mgr.recent_window(conversation_id="ghost", window=5) == []


async def test_drop_session_clears_window() -> None:
    mgr = HistoryManager()
    await mgr.append(conversation_id="c1", message=_msg("a"))
    await mgr.drop_session("c1")
    assert await mgr.recent_window(conversation_id="c1", window=10) == []


async def test_private_messages_are_filtered_from_recent_window() -> None:
    mgr = HistoryManager()
    await mgr.append(conversation_id="c1", message=_msg("public"))
    await mgr.append(conversation_id="c1", message=_msg("secret", private=True))

    items = await mgr.recent_window(conversation_id="c1", window=10)

    assert [m.content for m in items] == ["public"]


async def test_db_hydrate_runs_when_window_is_insufficient() -> None:
    calls = []

    async def _hydrate(*, conversation_id: str, window: int):
        old = datetime.now(UTC) - timedelta(minutes=5)
        calls.append((conversation_id, window))
        return [
            _msg("db-old", created_at=old),
            _msg("db-new", created_at=old + timedelta(minutes=1)),
        ]

    mgr = HistoryManager(hydrate_messages=_hydrate)
    await mgr.append(conversation_id="c1", message=_msg("cached"))

    items = await mgr.recent_window(conversation_id="c1", window=3)

    assert calls == [("c1", 3)]
    assert [m.content for m in items] == ["db-old", "db-new", "cached"]


async def test_db_and_memory_copies_of_same_turn_message_are_deduplicated() -> None:
    now = datetime.now(UTC)

    def copy(message_id: str, created_at: datetime) -> ChatMessage:
        return ChatMessage(
            id=message_id,
            role=MessageRole.ASSISTANT,
            content="same completed turn",
            created_at=created_at,
            metadata={"turn_id": "turn-1", "seq_in_turn": 1},
        )

    async def _hydrate(*, conversation_id: str, window: int):
        return [copy("sqlite-generated-id", now)]

    mgr = HistoryManager(hydrate_messages=_hydrate)
    await mgr.append(
        conversation_id="c1",
        message=copy("memory-generated-id", now + timedelta(milliseconds=10)),
    )

    items = await mgr.recent_window(conversation_id="c1", window=3)

    assert [message.content for message in items] == ["same completed turn"]
    assert items[0].id == "memory-generated-id"


async def test_db_hydrate_merges_sqlite_naive_and_in_memory_aware_timestamps() -> None:
    """SQLite returns naive datetimes while in-memory appends use aware UTC."""

    async def _hydrate(*, conversation_id: str, window: int):
        return [
            _msg(
                "db-old",
                created_at=datetime(2026, 1, 1, 8, 0, 0),
            )
        ]

    mgr = HistoryManager(hydrate_messages=_hydrate)
    await mgr.append(
        conversation_id="c1",
        message=_msg(
            "cached-new",
            created_at=datetime(2026, 1, 1, 8, 1, 0, tzinfo=UTC),
        ),
    )

    items = await mgr.recent_window(conversation_id="c1", window=3)

    assert [m.content for m in items] == ["db-old", "cached-new"]


async def test_db_hydrate_timeout_degrades_to_cached_window() -> None:
    import asyncio

    async def _slow_hydrate(*, conversation_id: str, window: int):
        await asyncio.sleep(1)
        return [_msg("db")]

    mgr = HistoryManager(hydrate_messages=_slow_hydrate, hydrate_timeout_s=0.001)
    await mgr.append(conversation_id="c1", message=_msg("cached"))

    items = await mgr.recent_window(conversation_id="c1", window=3)

    assert [m.content for m in items] == ["cached"]


async def test_current_turn_exclusion_preserves_full_window_without_hydration() -> None:
    async def unexpected_hydration(**_kwargs):
        pytest.fail("a full prior window must remain a hot read")

    mgr = HistoryManager(hydrate_messages=unexpected_hydration)
    for text in ("first", "second", "current"):
        msg = _msg(text)
        msg.metadata["turn_id"] = text
        await mgr.append(conversation_id="c1", message=msg)
    items = await mgr.recent_window(conversation_id="c1", window=2, exclude_turn_id="current")
    assert [m.content for m in items] == ["first", "second"]


async def test_current_turn_exclusion_also_applies_to_cold_history() -> None:
    rows = []
    for text in ("first", "second", "current"):
        msg = _msg(text)
        msg.metadata["turn_id"] = text
        rows.append(msg)

    async def hydrate(*, conversation_id, window):
        return rows[-window:]

    mgr = HistoryManager(hydrate_messages=hydrate)
    items = await mgr.recent_window(conversation_id="c1", window=2, exclude_turn_id="current")
    assert [m.content for m in items] == ["first", "second"]


async def test_turn_replacement_removes_unheard_answer_and_preserves_input_position() -> None:
    mgr = HistoryManager()
    user, answer, next_user = _msg("first"), _msg("unheard", MessageRole.ASSISTANT), _msg("next")
    user.metadata.update(turn_id="old", seq_in_turn=0, turn_status="active")
    answer.metadata.update(turn_id="old", seq_in_turn=1)
    await mgr.replace_turn(conversation_id="c1", turn_id="old", messages=[user, answer])
    await mgr.append(conversation_id="c1", message=next_user)
    from dataclasses import replace

    cancelled = replace(user, metadata={**user.metadata, "turn_status": "cancelled"})
    await mgr.replace_turn(conversation_id="c1", turn_id="old", messages=[cancelled])
    await mgr.replace_turn(conversation_id="c1", turn_id="old", messages=[cancelled])
    items = await mgr.recent_window(conversation_id="c1")
    assert [m.content for m in items] == ["first", "next"]
    assert items[0].created_at == user.created_at
    assert items[0].metadata["turn_status"] == "cancelled"


async def test_hydration_uses_history_finalized_while_read_was_pending() -> None:
    import asyncio
    from dataclasses import replace

    started, release = asyncio.Event(), asyncio.Event()

    async def hydrate(**_kwargs):
        started.set()
        await release.wait()
        return []

    mgr = HistoryManager(hydrate_messages=hydrate, hydrate_timeout_s=1)
    user = _msg("accepted")
    user.metadata.update(turn_id="old", seq_in_turn=0, turn_status="active")
    await mgr.replace_turn(conversation_id="c1", turn_id="old", messages=[user])
    read = asyncio.create_task(mgr.recent_window(conversation_id="c1"))
    await started.wait()
    await mgr.replace_turn(conversation_id="c1", turn_id="old", messages=[
        replace(user, metadata={**user.metadata, "turn_status": "cancelled"}),
    ])
    release.set()
    items = await read
    assert items[0].metadata["turn_status"] == "cancelled"
