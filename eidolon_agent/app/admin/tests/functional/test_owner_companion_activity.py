"""When this Owner last spoke to each of their Companions.

The read a roster is composed from, and the replacement for a worse one. The
phone used to render the Agent's live registry as 「运行中 / 未运行」, so every
Eidolon that had simply not been addressed since the last Agent restart was
labelled as not running — a process fact, shown as a verdict about the Eidolon,
with no action behind it. This answers the question people were actually asking
of that chip: 我上次跟它说话是什么时候.

What the tests below pin is the part that makes it usable at all: it survives a
restart, it counts only what was really said, and a Companion that has never been
spoken to is absent rather than zero.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import pytest
from fastapi import FastAPI

from eidolon_agent.app.admin.routers import owner_runtime as owner_runtime_router
from eidolon_agent.app.admin.tests.conftest import AUTHORITY_HEADERS
from eidolon_agent.core.types.turn import TriageKind, TurnInput, TurnStatus, TurnTrigger
from eidolon_agent.core.types.turn_context import TurnContext
from eidolon_agent.infra.persistence.agent_runtime import build_agent_turn_persister
from eidolon_agent.infra.persistence.runtime_store import AgentRuntimeStore

pytestmark = pytest.mark.functional

_WHEN = datetime(2026, 9, 18, 10, 0, tzinfo=UTC)


@pytest.fixture
async def store(tmp_path):
    """One Agent store per test, closed afterwards.

    Closed explicitly rather than left to the collector: an undisposed aiosqlite
    connection surfaces as an unraisable ResourceWarning against whichever test
    happens to run last, which is a failure that names the wrong test.
    """

    opened = AgentRuntimeStore.open(tmp_path / "eidolon-agent.sqlite3")
    await opened.init_schema()
    try:
        yield opened
    finally:
        await opened.close()


def _app(store: AgentRuntimeStore | None) -> FastAPI:
    app = FastAPI()
    app.include_router(owner_runtime_router.router, prefix="/api/admin")
    if store is not None:
        app.state.runtime_store = store
    return app


async def _get(app: FastAPI, owner_id: str) -> httpx.Response:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://agent.test"
    ) as client:
        return await client.get(
            f"/api/admin/owners/{owner_id}/companion-activity",
            headers=AUTHORITY_HEADERS,
        )


async def _say(
    store: AgentRuntimeStore,
    *,
    owner_id: str,
    companion_id: str,
    conversation_id: str,
    turn_id: str,
    when: datetime,
) -> None:
    """One turn, through the writer the real chat path uses."""

    persist = build_agent_turn_persister(store, model_id_provider=lambda: "test/model-1")
    await persist(
        ti=TurnInput(
            turn_id=turn_id,
            conversation_id=conversation_id,
            session_id=f"session-{conversation_id}",
            context=TurnContext(
                owner_id=owner_id,
                companion_id=companion_id,
                device_id=f"device-{owner_id}",
                memory_realm_id=f"r_{owner_id}",
                genome_id=f"g-{companion_id}",
                trace_id=f"trace-{turn_id}",
                request_id=f"request-{turn_id}",
                schema_version="eidolon.persona_genome",
                realizer_version="eidolon.persona_realizer",
            ),
            input_modality="text",
            trigger=TurnTrigger.USER_UTTERANCE,
            text="在吗",
        ),
        status=TurnStatus.OK,
        triage_kind=TriageKind.SIMPLE,
        started_at=when,
        finished_at=when,
        first_delta_ms=120,
        total_ms=440,
        usage_in=20,
        usage_out=15,
        error_code=None,
        timings={},
        user_text="在吗",
        assistant_text="在",
    )


async def test_it_answers_for_every_companion_that_has_been_spoken_to(store) -> None:
    """Several at once, which is the ordinary case for one Owner."""

    await _say(
        store,
        owner_id="owner-1",
        companion_id="c-a",
        conversation_id="conv-a",
        turn_id="turn-a",
        when=_WHEN,
    )
    await _say(
        store,
        owner_id="owner-1",
        companion_id="c-b",
        conversation_id="conv-b",
        turn_id="turn-b",
        when=_WHEN - timedelta(days=3),
    )

    body = (await _get(_app(store), "owner-1")).json()

    assert [row["companion_id"] for row in body["companions"]] == ["c-a", "c-b"], (
        "newest first, so a caller that truncates keeps the useful end"
    )


async def test_a_companion_is_dated_by_its_newest_conversation(store) -> None:
    """Somebody who talks to one Eidolon in many sittings.

    The aggregate is what is under test here: two conversations, and the row has
    to carry the later one. A companion with a long history and one recent
    sitting is a *recently used* companion, and picking any other row of the
    group would date it by when they first met.
    """

    await _say(
        store,
        owner_id="owner-1",
        companion_id="c-a",
        conversation_id="conv-old",
        turn_id="turn-old",
        when=_WHEN - timedelta(days=200),
    )
    await _say(
        store,
        owner_id="owner-1",
        companion_id="c-a",
        conversation_id="conv-new",
        turn_id="turn-new",
        when=_WHEN,
    )

    rows = (await _get(_app(store), "owner-1")).json()["companions"]

    assert len(rows) == 1, "one row per Companion, not one per conversation"
    assert rows[0]["last_conversation_at"].startswith("2026-09-18T10:00:00")


async def test_a_conversation_is_dated_by_its_latest_turn(store) -> None:
    """And within one sitting, by the last thing said in it.

    ``conversations.updated_at`` is restamped by every persisted turn. Reading
    ``started_at`` instead would age an Eidolon somebody talks to daily inside a
    single long-running conversation, which is exactly backwards.
    """

    await _say(
        store,
        owner_id="owner-1",
        companion_id="c-a",
        conversation_id="conv-a",
        turn_id="turn-old",
        when=_WHEN - timedelta(days=30),
    )
    await _say(
        store,
        owner_id="owner-1",
        companion_id="c-a",
        conversation_id="conv-a",
        turn_id="turn-new",
        when=_WHEN,
    )

    rows = (await _get(_app(store), "owner-1")).json()["companions"]

    assert len(rows) == 1, "one row per Companion, not one per conversation"
    assert rows[0]["last_conversation_at"].startswith("2026-09-18T10:00:00")


async def test_a_companion_never_spoken_to_is_absent_rather_than_zero(store) -> None:
    """The distinction 「未运行」 could not express.

    A Companion with no history does not appear here at all, so the screen can
    say 「还没有聊过」 — a true sentence that leads somewhere — instead of
    inventing an instant or reporting a state that sounds broken.
    """

    await _say(
        store,
        owner_id="owner-1",
        companion_id="c-a",
        conversation_id="conv-a",
        turn_id="turn-a",
        when=_WHEN,
    )

    rows = (await _get(_app(store), "owner-1")).json()["companions"]

    assert [row["companion_id"] for row in rows] == ["c-a"]


async def test_it_survives_the_process_that_answered_the_old_question(tmp_path, store) -> None:
    """The whole point: this is a record, not a cache.

    The registry this replaces lived in the Agent's memory, so a deploy made
    every Eidolon look idle. Reopening the store here stands in for that restart
    — the answer has to be identical, because nothing about what was said changed.
    """

    await _say(
        store,
        owner_id="owner-1",
        companion_id="c-a",
        conversation_id="conv-a",
        turn_id="turn-a",
        when=_WHEN,
    )
    before = (await _get(_app(store), "owner-1")).json()

    await store.close()

    restarted = AgentRuntimeStore.open(tmp_path / "eidolon-agent.sqlite3")
    await restarted.init_schema()
    try:
        after = (await _get(_app(restarted), "owner-1")).json()
    finally:
        await restarted.close()

    assert after == before


async def test_it_answers_for_one_owner_only(store) -> None:
    """Two people on one Host must not appear in each other's list."""

    await _say(
        store,
        owner_id="owner-1",
        companion_id="c-a",
        conversation_id="conv-a",
        turn_id="turn-a",
        when=_WHEN,
    )
    await _say(
        store,
        owner_id="owner-2",
        companion_id="c-b",
        conversation_id="conv-b",
        turn_id="turn-b",
        when=_WHEN,
    )

    mine = (await _get(_app(store), "owner-1")).json()
    theirs = (await _get(_app(store), "owner-2")).json()

    assert [row["companion_id"] for row in mine["companions"]] == ["c-a"]
    assert [row["companion_id"] for row in theirs["companions"]] == ["c-b"]


async def test_every_instant_carries_its_offset(store) -> None:
    """A phone renders 「3 天前」 from this, so a naive string is a bug.

    SQLite hands these columns back without the offset they were written with,
    and a reader is entitled to call an offset-less instant local time. The route
    restores UTC rather than leaving that to whoever parses it.
    """

    await _say(
        store,
        owner_id="owner-1",
        companion_id="c-a",
        conversation_id="conv-a",
        turn_id="turn-a",
        when=_WHEN,
    )

    stamp = (await _get(_app(store), "owner-1")).json()["companions"][0]["last_conversation_at"]

    assert datetime.fromisoformat(stamp).utcoffset() is not None
    assert datetime.fromisoformat(stamp) == _WHEN


async def test_a_store_it_cannot_read_is_unknown_rather_than_none() -> None:
    """An empty list is an answer: "you have never talked to any of them".

    Returning that when the store simply could not be reached would put every
    Eidolon back in the state this change exists to remove — labelled by a read
    that failed, rather than by anything true about them.
    """

    response = await _get(_app(None), "owner-1")

    assert response.status_code == 503
    assert "runtime_store" in response.json()["detail"]


async def test_the_read_needs_the_authority_credential(store) -> None:
    app = _app(store)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://agent.test"
    ) as client:
        anonymous = await client.get("/api/admin/owners/owner-1/companion-activity")

    assert anonymous.status_code in (401, 403)
