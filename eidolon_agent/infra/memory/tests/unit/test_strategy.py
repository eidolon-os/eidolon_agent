"""MemoryStrategy — planning + delegation to MemoryPort."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from eidolon_agent.infra.memory.strategy import MemoryStrategy

pytestmark = pytest.mark.unit


def test_plan_for_turn_default_shape() -> None:
    plan = MemoryStrategy(port=None).plan_for_turn(user_text="你好", voice=True)
    assert plan.working_k == 20
    assert plan.episodic_query == "你好"
    assert plan.episodic_k == 3
    assert plan.semantic_query == "你好"
    assert plan.semantic_k == 5
    assert plan.promise_force is True
    assert plan.voice is True


def test_plan_for_turn_propagates_voice_false() -> None:
    plan = MemoryStrategy(port=None).plan_for_turn(user_text="x", voice=False)
    assert plan.voice is False


async def test_retrieve_calls_port_recall_context_with_plan() -> None:
    port = AsyncMock()
    port.recall_context = AsyncMock(return_value=("ctx", [], False))
    strat = MemoryStrategy(port=port)
    out = await strat.retrieve(user_id="alice", user_text="hello", voice=True)
    assert out == ("ctx", [], False)
    port.recall_context.assert_awaited_once()
    kwargs = port.recall_context.await_args.kwargs
    assert kwargs["user_id"] == "alice"
    assert kwargs["query"] == "hello"
    assert kwargs["plan"].semantic_k == 5  # plan_for_turn default


async def test_write_turn_delegates_positionally() -> None:
    port = AsyncMock()
    port.write_turn = AsyncMock()
    await MemoryStrategy(port=port).write_turn(
        user_id="alice", session_id="s1", turn_id="t1",
        user_text="hi", assistant_text="hello",
    )
    port.write_turn.assert_awaited_once_with("alice", "s1", "t1", "hi", "hello")


async def test_forget_returns_count_from_port() -> None:
    port = AsyncMock()
    port.forget = AsyncMock(return_value=7)
    assert await MemoryStrategy(port=port).forget(user_id="alice", query="x") == 7
    port.forget.assert_awaited_once_with("alice", "x")
