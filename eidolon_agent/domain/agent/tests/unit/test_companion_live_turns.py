"""The façade is where a turn becomes observable, or nowhere is.

``CompanionAgent.run_turn`` is the single entry point every transport goes
through. If the board is not attached here it is attached at five call sites
inside the engine that will drift, or at one transport and not the others — so
this file holds the wiring itself, not just the board's behaviour.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from eidolon_agent.core.types.turn import FSMState, TurnEvent, TurnInput, TurnStatus, TurnTrigger
from eidolon_agent.core.types.turn_context import TurnContext
from eidolon_agent.domain.agent.companion import CompanionAgent
from eidolon_agent.infra.observability.live_turns import LiveTurnBoard

pytestmark = pytest.mark.unit


class _Engine:
    """Stands in for the 640-line pipeline: it only has to emit a stream."""

    def __init__(self) -> None:
        self.mid_flight: list[int] = []
        self.board: LiveTurnBoard | None = None

    def run(self, ti: TurnInput) -> AsyncIterator[TurnEvent]:
        async def stream() -> AsyncIterator[TurnEvent]:
            yield TurnEvent.state(ti.turn_id, 1, FSMState.THINKING, 0.0)
            assert self.board is not None
            self.mid_flight.append(len(self.board.snapshot()))
            yield TurnEvent.done(ti.turn_id, 2, TurnStatus.OK, 0.0)

        return stream()


def _input() -> TurnInput:
    return TurnInput(
        turn_id="turn-1",
        conversation_id="conv-1",
        session_id="sess-1",
        context=TurnContext(
            owner_id="owner-1",
            companion_id="eidolon-1",
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


async def test_a_turn_run_through_the_facade_is_observable_while_it_runs() -> None:
    board = LiveTurnBoard()
    engine = _Engine()
    engine.board = board
    agent = CompanionAgent(companion_id="eidolon-1", turn_engine=engine, live_turns=board)

    events = [event async for event in agent.run_turn(_input())]

    assert engine.mid_flight == [1]
    assert len(events) == 2
    assert board.snapshot() == []


async def test_without_a_board_the_turn_runs_exactly_as_before() -> None:
    """No observer configured must mean no wrapper, not an empty one."""

    engine = _Engine()
    engine.board = LiveTurnBoard()
    agent = CompanionAgent(companion_id="eidolon-1", turn_engine=engine)

    events = [event async for event in agent.run_turn(_input())]

    assert len(events) == 2
    assert engine.mid_flight == [0]
