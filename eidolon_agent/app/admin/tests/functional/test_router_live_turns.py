"""One list, two halves: what is happening and what happened.

The turn log answers "what did this person talk about". It could never answer
"what is being said right now", because its rows are written when a turn ends —
which is why nothing on this Host could draw a conversation in progress even
though every consumer downstream was built for one.

So this route serves both from one shape. What these tests hold is the seam:

* **in flight comes first, and only on the newest page.** A caller walking
  backwards with ``before`` is reading history, and a row that is still changing
  is not history;
* **the durable row wins a tie.** A turn that ends between the two reads is in
  both, and the written one is the final answer;
* **the walk stays lossless.** Live rows must not push history off the end of a
  page and out of the cursor's reach.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime

import httpx
import pytest
from fastapi import FastAPI

from eidolon_agent.app.admin.routers import conversations as conv_router
from eidolon_agent.app.admin.tests.conftest import AUTHORITY_HEADERS
from eidolon_agent.core.types.turn import FSMState, TurnEvent, TurnInput, TurnTrigger
from eidolon_agent.core.types.turn_context import TurnContext
from eidolon_agent.infra.observability.live_turns import LiveTurnBoard
from eidolon_agent.infra.persistence.runtime_store import AgentRuntimeStore

pytestmark = pytest.mark.functional

_TURNS = "/api/admin/conversations/turns"


@asynccontextmanager
async def _app(tmp_path, board: LiveTurnBoard | None):
    """A real store and a real router, closed on the way out.

    The close matters: this suite treats an unclosed connection as a failure,
    and a test that leaves one behind fails a later, unrelated test instead.
    """

    store = AgentRuntimeStore.open(tmp_path / "eidolon-agent.sqlite3")
    await store.init_schema()
    app = FastAPI()
    app.state.runtime_store = store
    app.state.live_turns = board
    app.include_router(conv_router.router, prefix="/api/admin")
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://t", headers=AUTHORITY_HEADERS
    )
    try:
        yield client, store
    finally:
        await client.aclose()
        await store.close()


def _input(turn_id: str, *, owner: str = "owner-1") -> TurnInput:
    return TurnInput(
        turn_id=turn_id,
        conversation_id="conv-1",
        session_id="sess-1",
        context=TurnContext(
            owner_id=owner,
            companion_id="eidolon-1",
            device_id="dev-box3",
            memory_realm_id="realm-1",
            genome_id="genome-1",
            trace_id=f"trace-{turn_id}",
            request_id="req-1",
        ),
        input_modality="voice",
        trigger=TurnTrigger.USER_UTTERANCE,
        text="你好",
    )


async def _start(board: LiveTurnBoard, turn_id: str, *, owner: str = "owner-1"):
    """Open a turn and leave it open, which is the state under test."""

    async def stream():
        yield TurnEvent.state(turn_id, 1, FSMState.THINKING, 0.0)
        yield TurnEvent.state(turn_id, 2, FSMState.SPEAKING, 0.0)

    observed = board.observe(_input(turn_id, owner=owner), stream())
    await anext(observed)
    return observed


async def test_a_running_turn_is_served_with_the_word_every_consumer_knows(tmp_path) -> None:
    board = LiveTurnBoard()
    async with _app(tmp_path, board) as (client, _):
        open_turn = await _start(board, "turn-live")

        answer = await client.get(_TURNS, params={"owner_id": "owner-1"})

        assert answer.status_code == 200
        rows = answer.json()["turns"]
        assert [row["turn_id"] for row in rows] == ["turn-live"]
        row = rows[0]
        # `running` is not a new word for this system: the stage projection, the
        # activity projection and the app all already key on it.
        assert row["status"] == "running"
        assert row["finished_at"] is None
        # Absent, not zero. A turn still running has not said whether recall
        # happened, and `attempted: false` would claim it did not.
        assert "memory" not in row["observability_summary"]
        assert row["observability_summary"]["live"] is True
        await open_turn.aclose()


async def test_it_is_scoped_the_same_way_the_history_is(tmp_path) -> None:
    board = LiveTurnBoard()
    async with _app(tmp_path, board) as (client, _):
        mine = await _start(board, "turn-mine", owner="owner-1")
        theirs = await _start(board, "turn-theirs", owner="owner-2")

        answer = await client.get(_TURNS, params={"owner_id": "owner-1"})

        assert [row["turn_id"] for row in answer.json()["turns"]] == ["turn-mine"]
        await mine.aclose()
        await theirs.aclose()


async def test_history_pages_do_not_carry_a_turn_that_is_still_changing(tmp_path) -> None:
    board = LiveTurnBoard()
    async with _app(tmp_path, board) as (client, _):
        open_turn = await _start(board, "turn-live")

        answer = await client.get(
            _TURNS, params={"owner_id": "owner-1", "before": datetime.now(UTC).isoformat()}
        )

        assert answer.json()["turns"] == []
        await open_turn.aclose()


async def test_without_a_board_the_route_answers_exactly_as_it_did(tmp_path) -> None:
    """The board is an addition. A process without one is not a broken one."""

    async with _app(tmp_path, None) as (client, _):

        answer = await client.get(_TURNS, params={"owner_id": "owner-1"})

        assert answer.status_code == 200
        assert answer.json() == {"turns": [], "next_before": None}


async def test_live_rows_do_not_push_history_out_of_the_cursors_reach(tmp_path) -> None:
    """The subtle one, and the reason the cursor is derived from what was sent.

    A page holds ``limit`` rows. Put a running turn at its head and one written
    turn falls off the end — so the cursor has to point at the oldest row the
    caller actually received, not at the oldest the route happened to read.
    Getting this wrong loses a turn from history silently, which is the worst
    shape a paging bug takes.
    """

    from eidolon_agent.app.admin.tests.functional.test_router_conversations import _seed_turn

    board = LiveTurnBoard()
    async with _app(tmp_path, board) as (client, store):
        for index, minute in enumerate((10, 20)):
            await _seed_turn(
                store,
                owner_id="owner-1",
                companion_id="eidolon-1",
                conversation_id="conv-1",
                turn_id=f"turn-old-{index}",
                seq=index,
                user_text="喂",
                assistant_text="在",
                started_at=datetime(2026, 8, 27, 9, minute, tzinfo=UTC),
            )
        open_turn = await _start(board, "turn-live")

        first = await client.get(_TURNS, params={"owner_id": "owner-1", "limit": 2})
        body = first.json()

        assert [row["turn_id"] for row in body["turns"]] == ["turn-live", "turn-old-1"]
        assert body["next_before"] is not None

        second = await client.get(
            _TURNS, params={"owner_id": "owner-1", "limit": 2, "before": body["next_before"]}
        )

        # The turn the first page could not fit is reachable, and exactly once.
        assert [row["turn_id"] for row in second.json()["turns"]] == ["turn-old-0"]
        await open_turn.aclose()
