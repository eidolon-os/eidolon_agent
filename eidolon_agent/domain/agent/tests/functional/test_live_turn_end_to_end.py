"""A real turn, watched while it happens, and handed over when it ends.

Every other test of the board feeds it a stream written by hand. This one runs
the actual :class:`TurnEngine` and reads the board from inside the turn, because
the claim being made is about the *real* event stream: that what a turn already
emits to its caller is enough to say where it has got to, without one line added
inside the pipeline.

It also holds the seam with the durable row. The engine schedules that write as
background work after DONE is yielded, so there is an instant when the stream is
closed and the row does not exist yet — and this asserts the turn is readable
across it, with the status its own DONE carried rather than a stale ``running``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from eidolon_agent.domain.agent.companion import CompanionAgent
from eidolon_agent.infra.observability.live_turns import LiveTurnBoard
from eidolon_agent.infra.persistence.runtime_store import AgentRuntimeStore, TurnRow
from tests.helpers import make_turn_input

pytestmark = pytest.mark.functional


async def _runtime_store(tmp_path: Path) -> AgentRuntimeStore:
    store = AgentRuntimeStore.open(tmp_path / "eidolon-agent.sqlite3")
    await store.init_schema()
    return store


@pytest.mark.asyncio
async def test_the_real_engine_makes_a_turn_observable_then_hands_it_over(
    tmp_path: Path,
    turn_engine_factory,
) -> None:
    store = await _runtime_store(tmp_path)
    board = LiveTurnBoard()
    try:
        engine = turn_engine_factory(runtime_store=store)
        agent = CompanionAgent(
            companion_id="companion-test", turn_engine=engine, live_turns=board
        )
        ti = make_turn_input("铁锤几岁了？")

        during: list[tuple[str, int | None]] = []
        async for _event in agent.run_turn(ti):
            live = board.snapshot(owner_id=ti.context.owner_id)
            assert len(live) == 1, "the turn should be readable for its whole length"
            during.append((live[0].status, live[0].latency_first_delta_ms))

        # Running the whole way, then settled — and never going back. The last
        # reading is taken after DONE has been handed to the consumer, which is
        # why it is already the final status rather than one more `running`.
        statuses = [status for status, _ in during]
        assert statuses[0] == "running"
        assert statuses[-1] == "ok"
        assert statuses == sorted(statuses, key=lambda value: value != "running")
        assert board.snapshot(owner_id="somebody-else") == []
        # The answer started at some point, and the board noticed from the
        # stream alone — this is the one internal boundary it ever reports.
        assert during[-1][1] is not None, "first delta never observed"

        # The stream has closed; the row is still being written in the
        # background. The turn is readable across that gap, and honest about
        # having ended.
        settled = board.snapshot(owner_id=ti.context.owner_id)
        assert [view.status for view in settled] == ["ok"]
        assert settled[0].finished_at is not None

        await engine._background.drain(timeout_s=2)
        async with store.session_factory() as session:
            assert await session.get(TurnRow, ti.turn_id) is not None
    finally:
        await store.close()
