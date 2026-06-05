"""HistoryManager — in-memory window semantics."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

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
        created_at=created_at or datetime.now(timezone.utc),
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


async def test_forget_matching_extracts_forget_target_from_request() -> None:
    mgr = HistoryManager()
    await mgr.append(conversation_id="c1", message=_msg("以后叫我小满"))
    await mgr.append(conversation_id="c1", message=_msg("普通聊天"))

    removed = await mgr.forget_matching(
        conversation_id="c1",
        query="请忘记叫我小满",
    )
    items = await mgr.recent_window(conversation_id="c1", window=10)

    assert removed == 1
    assert [m.content for m in items] == ["普通聊天"]


async def test_db_hydrate_runs_when_window_is_insufficient() -> None:
    calls = []

    async def _hydrate(*, conversation_id: str, window: int):
        old = datetime.now(timezone.utc) - timedelta(minutes=5)
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
            created_at=datetime(2026, 1, 1, 8, 1, 0, tzinfo=timezone.utc),
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
