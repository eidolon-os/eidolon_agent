"""One conversation's turns, with what was said in them.

The route a transcript is read from, and the reason it had to be added rather
than assembled from what already existed: the owner-wide turn list deliberately
carries no message bodies (a page of fifty would be many MB), and reading one
turn at a time is twenty round trips for one screen. So a page here carries the
words and pays for it with a smaller page — not by trimming anyone's messages,
which is exactly what someone opened a transcript to read.

Two properties this must not get wrong:

- **It answers for one Owner.** This is the only list on the surface that carries
  message bodies, so `owner_id` is required rather than an optional filter;
  omitting it on the owner-wide list merely mixes summaries, and omitting it here
  would hand one caller everyone's words.
- **"Not yours" and "not there" are the same answer.** Otherwise a conversation
  id can be probed for existence.
"""

from __future__ import annotations

from datetime import datetime, timezone
from urllib.parse import quote

import pytest

from .test_router_conversations import _fresh_app, _seed_turn

pytestmark = pytest.mark.functional

OWNER = "manson"
OTHER = "alice"


def _at(minute: int) -> datetime:
    return datetime(2026, 8, 24, 9, minute, tzinfo=timezone.utc)


async def _seed_conversation(store, *, owner_id: str, conversation_id: str, turns: int) -> None:
    for index in range(turns):
        await _seed_turn(
            store,
            owner_id=owner_id,
            conversation_id=conversation_id,
            turn_id=f"{conversation_id}-t{index}",
            seq=index,
            user_text=f"问题 {index}",
            assistant_text=f"回答 {index}",
            started_at=_at(index),
        )


async def test_a_transcript_carries_what_was_said(tmp_path) -> None:
    client, store = await _fresh_app(tmp_path)
    try:
        await _seed_conversation(store, owner_id=OWNER, conversation_id="conv-a", turns=2)

        answered = await client.get(
            f"/api/admin/conversations/conv-a/turns?owner_id={OWNER}"
        )

        assert answered.status_code == 200
        body = answered.json()
        assert body["conversation_id"] == "conv-a"
        # Newest first, matching the cursor: "load earlier" is the gesture, and
        # which direction a person sees is the client's business.
        assert [turn["turn_id"] for turn in body["turns"]] == ["conv-a-t1", "conv-a-t0"]
        said = [
            (message["role"], message["content"])
            for turn in body["turns"]
            for message in turn["messages"]
        ]
        assert ("user", "问题 1") in said
        assert ("assistant", "回答 1") in said
    finally:
        await client.aclose()
        await store.close()


async def test_only_this_conversation(tmp_path) -> None:
    client, store = await _fresh_app(tmp_path)
    try:
        await _seed_conversation(store, owner_id=OWNER, conversation_id="conv-a", turns=1)
        await _seed_conversation(store, owner_id=OWNER, conversation_id="conv-b", turns=1)

        body = (
            await client.get(f"/api/admin/conversations/conv-a/turns?owner_id={OWNER}")
        ).json()

        assert [turn["turn_id"] for turn in body["turns"]] == ["conv-a-t0"]
    finally:
        await client.aclose()
        await store.close()


async def test_another_owners_conversation_is_not_readable(tmp_path) -> None:
    """404, and the same 404 a conversation that does not exist gets: an id must
    not be probeable for existence."""

    client, store = await _fresh_app(tmp_path)
    try:
        await _seed_conversation(store, owner_id=OTHER, conversation_id="conv-theirs", turns=1)

        theirs = await client.get(
            f"/api/admin/conversations/conv-theirs/turns?owner_id={OWNER}"
        )
        absent = await client.get(
            f"/api/admin/conversations/conv-nowhere/turns?owner_id={OWNER}"
        )

        assert (theirs.status_code, absent.status_code) == (404, 404)
        # Nothing of the conversation itself in either body: the id a caller
        # already sent back is all they learn.
        assert "问题" not in theirs.text
        assert "回答" not in theirs.text
    finally:
        await client.aclose()
        await store.close()


async def test_an_owner_must_be_named(tmp_path) -> None:
    """Required here, unlike the owner-wide list: this is the only list that
    carries message bodies."""

    client, store = await _fresh_app(tmp_path)
    try:
        await _seed_conversation(store, owner_id=OWNER, conversation_id="conv-a", turns=1)

        answered = await client.get("/api/admin/conversations/conv-a/turns")

        assert answered.status_code == 422
    finally:
        await client.aclose()
        await store.close()


async def test_a_page_walks_backwards_and_stops_at_the_beginning(tmp_path) -> None:
    """A short page is the start of the conversation, and a cursor there would
    make a client ask again for nothing."""

    client, store = await _fresh_app(tmp_path)
    try:
        await _seed_conversation(store, owner_id=OWNER, conversation_id="conv-a", turns=3)

        first = (
            await client.get(
                f"/api/admin/conversations/conv-a/turns?owner_id={OWNER}&limit=2"
            )
        ).json()
        assert [turn["turn_id"] for turn in first["turns"]] == ["conv-a-t2", "conv-a-t1"]
        assert first["next_before"] is not None

        second = (
            await client.get(
                f"/api/admin/conversations/conv-a/turns?owner_id={OWNER}&limit=2"
                f"&before={first['next_before']}"
            )
        ).json()

        assert [turn["turn_id"] for turn in second["turns"]] == ["conv-a-t0"]
        assert second["next_before"] is None
    finally:
        await client.aclose()
        await store.close()


async def test_a_conversation_with_no_turns_is_an_empty_transcript_not_a_404(
    tmp_path,
) -> None:
    """It is a real state — someone opened a conversation and said nothing — and
    reporting it as missing would send them looking for a fault."""

    client, store = await _fresh_app(tmp_path)
    try:
        await _seed_conversation(store, owner_id=OWNER, conversation_id="conv-a", turns=1)
        # A page starting before the only turn: the conversation is there and
        # this window of it holds nothing.
        # Encoded, because a "+" in a query string means a space and the offset
        # would arrive mangled. The cursor this route hands back is already
        # safe — pydantic writes it with a "Z" — but a hand-built instant is not,
        # and a client that builds one has to encode it.
        body = (
            await client.get(
                f"/api/admin/conversations/conv-a/turns?owner_id={OWNER}"
                f"&before={quote(_at(0).isoformat(), safe='')}"
            )
        ).json()

        assert body["turns"] == []
        assert body["next_before"] is None
    finally:
        await client.aclose()
        await store.close()
