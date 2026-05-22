"""HistoryManager — in-memory window semantics."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest

from eidolon_agent.core.types.messages import ChatMessage, MessageRole
from eidolon_agent.domain.history import HistoryManager

pytestmark = pytest.mark.unit


def _msg(text: str, role: MessageRole = MessageRole.USER) -> ChatMessage:
    return ChatMessage(
        id=uuid.uuid4().hex,
        role=role,
        content=text,
        created_at=datetime.now(timezone.utc),
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
