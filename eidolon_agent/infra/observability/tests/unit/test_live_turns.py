"""What a turn in flight is allowed to say about itself.

The board exists so a conversation can be drawn while it is happening. That
makes two things load-bearing, and both of them are about restraint rather than
capability:

* **it must never affect the turn it is watching.** A telemetry bug that breaks
  a conversation is worse than a map that goes dark, so the wrapper passes the
  stream through untouched — order, exceptions, cancellation — and swallows its
  own failures;
* **it must not report what it cannot see.** The engine's internal boundaries
  never reach the event stream, so the board says nothing about them. What it
  claims is exactly what the stream carried.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime

import pytest

from eidolon_agent.core.types.turn import (
    FSMState,
    TurnEvent,
    TurnEventKind,
    TurnInput,
    TurnStatus,
    TurnTrigger,
)
from eidolon_agent.core.types.turn_context import TurnContext
from eidolon_agent.infra.observability.live_turns import LiveTurnBoard

pytestmark = pytest.mark.unit


def _input(turn_id: str = "turn-1", *, owner: str = "owner-1", companion: str = "eidolon-1") -> TurnInput:
    return TurnInput(
        turn_id=turn_id,
        conversation_id="conv-1",
        session_id="sess-1",
        context=TurnContext(
            owner_id=owner,
            companion_id=companion,
            device_id="dev-box3",
            memory_realm_id="realm-1",
            genome_id="genome-1",
            trace_id="trace-1",
            request_id="req-1",
        ),
        input_modality="voice",
        trigger=TurnTrigger.USER_UTTERANCE,
        text="你好",
    )


class _Clock:
    """A monotonic clock the test moves, so TTL is tested not waited for."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def _board(clock: _Clock | None = None, **kwargs) -> LiveTurnBoard:
    return LiveTurnBoard(
        monotonic=clock or _Clock(),
        wall_clock=lambda: datetime(2026, 8, 27, 9, 0, tzinfo=UTC),
        **kwargs,
    )


async def _drain(stream: AsyncIterator[TurnEvent]) -> list[TurnEvent]:
    return [event async for event in stream]


async def _events(*events: TurnEvent) -> AsyncIterator[TurnEvent]:
    for event in events:
        yield event


async def test_a_turn_is_visible_while_it_runs_and_hands_over_when_it_ends() -> None:
    """The hand-over, which is the whole reason the entry is not just deleted.

    The engine schedules the durable write as background work *after* DONE is
    yielded, so the stream closes before that row exists. An entry dropped at
    close leaves the turn in neither place, and a map sampling that instant
    shows the conversation blink out. So it stays — with the status its own DONE
    event carried, never a stale ``running`` — until the row takes over.
    """

    clock = _Clock()
    board = _board(clock, handover_seconds=10.0)
    seen: list[str] = []

    async def stream() -> AsyncIterator[TurnEvent]:
        yield TurnEvent.state("turn-1", 1, FSMState.THINKING, 0.0)
        # Mid-stream is the only moment a *live* turn can be observed at all.
        seen.append(board.snapshot()[0].status)
        yield TurnEvent.done("turn-1", 2, TurnStatus.OK, 0.0)

    await _drain(board.observe(_input(), stream()))

    assert seen == ["running"]
    handed_over = board.snapshot()
    assert [view.status for view in handed_over] == ["ok"]
    assert handed_over[0].finished_at is not None

    clock.now += 11.0

    # By now the written row is the answer, and a second, staler copy of it
    # would only be something to disagree with.
    assert board.snapshot() == []


async def test_the_stream_passes_through_untouched() -> None:
    board = _board()
    originals = [
        TurnEvent.state("turn-1", 1, FSMState.THINKING, 0.0),
        TurnEvent.delta("turn-1", 2, "你", 0.0),
        TurnEvent.done("turn-1", 3, TurnStatus.OK, 0.0),
    ]

    relayed = await _drain(board.observe(_input(), _events(*originals)))

    assert relayed == originals


async def test_it_reports_only_what_the_stream_carried() -> None:
    board = _board()
    captured = []

    async def stream() -> AsyncIterator[TurnEvent]:
        yield TurnEvent(
            turn_id="turn-1", seq=1, kind=TurnEventKind.TOOL_CALL, data={"name": "memory_search"}, ts=0.0
        )
        yield TurnEvent.delta("turn-1", 2, "你", 0.0)
        captured.append(board.snapshot()[0])
        yield TurnEvent.done("turn-1", 3, TurnStatus.OK, 0.0)

    await _drain(board.observe(_input(), stream()))

    view = captured[0]
    assert view.owner_id == "owner-1"
    assert view.companion_id == "eidolon-1"
    assert view.device_id == "dev-box3"
    assert view.trigger == "user_utterance"
    assert view.input_modality == "voice"
    # One call asked for, none returned yet: only a running turn can say this,
    # and it is what makes "the tool stage is happening" a fact rather than a
    # guess.
    assert (view.tools.count, view.tools.completed) == (1, 0)
    assert view.tools.running is True
    assert view.tools.names == ("memory_search",)
    # The answer has started. This is the one internal boundary the stream
    # exposes, and the only timing this board ever claims.
    assert view.latency_first_delta_ms is not None


async def test_a_failed_tool_is_counted_once_it_comes_back() -> None:
    board = _board()
    captured = []

    async def stream() -> AsyncIterator[TurnEvent]:
        yield TurnEvent(turn_id="turn-1", seq=1, kind=TurnEventKind.TOOL_CALL, data={"name": "t"}, ts=0.0)
        yield TurnEvent(
            turn_id="turn-1", seq=2, kind=TurnEventKind.TOOL_RESULT, data={"name": "t", "ok": False}, ts=0.0
        )
        captured.append(board.snapshot()[0])

    await _drain(board.observe(_input(), stream()))

    view = captured[0]
    assert (view.tools.count, view.tools.completed, view.tools.error_count) == (1, 1, 1)
    assert view.tools.running is False


async def test_an_abandoned_stream_still_ends_the_entry() -> None:
    """A consumer that stops reading is the common case: the caller hung up."""

    board = _board()

    async def stream() -> AsyncIterator[TurnEvent]:
        yield TurnEvent.state("turn-1", 1, FSMState.THINKING, 0.0)
        yield TurnEvent.state("turn-1", 2, FSMState.SPEAKING, 0.0)

    observed = board.observe(_input(), stream())
    assert await anext(observed) is not None
    assert len(board.snapshot()) == 1
    await observed.aclose()

    assert board.snapshot() == []


async def test_a_failing_turn_propagates_and_leaves_nothing_behind() -> None:
    board = _board()

    async def stream() -> AsyncIterator[TurnEvent]:
        yield TurnEvent.state("turn-1", 1, FSMState.THINKING, 0.0)
        raise RuntimeError("the brain fell over")

    with pytest.raises(RuntimeError, match="the brain fell over"):
        await _drain(board.observe(_input(), stream()))

    assert board.snapshot() == []


async def test_a_broken_board_cannot_break_a_turn() -> None:
    """The rule this whole module is built around, asserted directly."""

    board = _board()
    board._saw = lambda event: (_ for _ in ()).throw(ValueError("telemetry bug"))  # type: ignore[method-assign]
    originals = [TurnEvent.delta("turn-1", 1, "你", 0.0), TurnEvent.done("turn-1", 2, TurnStatus.OK, 0.0)]

    relayed = await _drain(board.observe(_input(), _events(*originals)))

    assert relayed == originals


async def test_a_turn_that_never_ends_stops_being_reported() -> None:
    """Otherwise a leaked entry reads as "still talking" forever."""

    clock = _Clock()
    board = _board(clock, ttl_seconds=60.0)
    stream = board.observe(_input(), _events(TurnEvent.state("turn-1", 1, FSMState.THINKING, 0.0)))
    await anext(stream)
    assert len(board.snapshot()) == 1

    clock.now += 61.0

    assert board.snapshot() == []


async def test_the_board_is_bounded() -> None:
    clock = _Clock()
    board = _board(clock, max_turns=2)
    streams = []
    for index in range(3):
        stream = board.observe(_input(f"turn-{index}"), _events(TurnEvent.delta(f"turn-{index}", 1, "x", 0.0)))
        await anext(stream)
        streams.append(stream)
        clock.now += 1.0

    live = {view.turn_id for view in board.snapshot()}
    # The newest are kept: the interesting turn is the one starting now.
    assert live == {"turn-1", "turn-2"}
    for stream in streams:
        await stream.aclose()


async def test_a_reader_sees_only_the_scope_it_asked_about() -> None:
    """Same filters as the durable read, so one answer is scoped consistently."""

    board = _board()
    first = board.observe(_input("turn-a", owner="owner-1"), _events(TurnEvent.delta("turn-a", 1, "x", 0.0)))
    second = board.observe(_input("turn-b", owner="owner-2"), _events(TurnEvent.delta("turn-b", 1, "x", 0.0)))
    await anext(first)
    await anext(second)

    assert [view.turn_id for view in board.snapshot(owner_id="owner-1")] == ["turn-a"]
    assert [view.turn_id for view in board.snapshot(companion_id="nobody")] == []
    await first.aclose()
    await second.aclose()
