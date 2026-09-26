"""Reply adapter uses AcceptReply, per-Companion scope and a streaming presenter."""

import asyncio
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from eidolon_sdk.biz.participation import Context, Message

from eidolon_agent.app.interaction import AcceptReply
from eidolon_agent.app.interaction.coordination import Member, Permit, ReplyTask
from eidolon_agent.app.interaction.coordination.replies import CoordinatedReplies
from eidolon_agent.core.errors import PermissionDeniedError
from eidolon_agent.core.types.companion_runtime import CompanionRuntimeConfig
from eidolon_agent.core.types.turn import TurnEvent, TurnStatus
from eidolon_agent.domain.runtime_session import AuthorizedRuntimeSession
from tests.helpers import make_turn_input

pytestmark = pytest.mark.unit


def scope(companion="companion-test", owner="alice"):
    context = make_turn_input().context
    return AuthorizedRuntimeSession(
        runtime=SimpleNamespace(
            owner_id=owner,
            companion_id=companion,
            genome_id=context.genome_id,
            memory_realm_id="realm-" + companion,
            schema_version=context.schema_version,
            genome_hash=context.genome_hash,
            realizer_version=context.realizer_version,
        ),
        device_id="waveshare",
        session_id="runtime-" + companion,
        config=CompanionRuntimeConfig(),
    )


def task(kind="user"):
    source = Message(
        message_id="input",
        author_kind=kind,
        author_id="waveshare" if kind == "user" else "companion-test",
        text="你好",
    )
    return ReplyTask("decision", "group", source, Context(recent_messages=(source,)))


def permit(valid=lambda: True):
    return Permit("turn", 1, valid, lambda _: None)


class Agent:
    def __init__(self):
        self.turns = []
        self.closed = False

    async def run_turn(self, turn):
        self.turns.append(turn)
        try:
            yield TurnEvent.delta(turn.turn_id, 0, "你好", 0)
            yield TurnEvent.done(turn.turn_id, 1, TurnStatus.OK, 0)
        finally:
            self.closed = True


def bridge(present, *, agent=None):
    agent = agent or Agent()
    registry = SimpleNamespace(
        resolve_runtime=AsyncMock(
            return_value=SimpleNamespace(
                owner_id="alice",
                companion_id="companion-test",
                genome_id="genome-test",
                agent=agent,
            )
        )
    )
    member = Member("companion-test", "stackchan")
    return (
        CoordinatedReplies(
            context_ref="group",
            input_device_id="waveshare",
            bindings=((member, scope()),),
            accept=AcceptReply(registry),
            present=present,
        ),
        member,
        agent,
    )


async def test_streams_existing_agent_and_requires_playback_completion():
    reached, played = asyncio.Event(), asyncio.Event()

    async def present(member, prepared, stream, grant):
        async for _ in stream:
            grant.check()
        reached.set()
        await played.wait()
        return True

    reply, member, agent = bridge(present)
    running = asyncio.create_task(reply(member, task(), permit()))
    await asyncio.wait_for(reached.wait(), 1)
    assert not running.done()
    played.set()
    result = await running
    assert result.completed and result.text == "你好"
    assert agent.turns[0].context.companion_id == member.companion_id
    assert agent.turns[0].context.device_id == "waveshare"
    assert agent.turns[0].coordination.trigger.author_kind == "user"
    assert agent.closed


async def test_presenter_cannot_claim_success_without_consuming_stream():
    async def present(*args):
        return True

    reply, member, agent = bridge(present)
    result = await reply(member, task(), permit())
    assert not result.completed
    assert not agent.turns


async def test_revocation_closes_stream_and_discards_late_completion():
    valid = True

    async def present(member, prepared, stream, grant):
        nonlocal valid
        async for _ in stream:
            valid = False
        return True

    reply, member, agent = bridge(present)
    with pytest.raises(asyncio.CancelledError):
        await reply(member, task(), permit(lambda: valid))
    assert agent.closed


async def test_attributed_peer_input_never_becomes_user_text():
    async def present(member, prepared, stream, grant):
        async for _ in stream:
            pass
        return True

    reply, member, agent = bridge(present)
    await reply(member, task("companion"), permit())
    assert agent.turns[0].text == ""
    assert agent.turns[0].trigger.value == "coordinated_reply"


async def test_wrong_member_group_or_source_cannot_select_runtime():
    reply, member, agent = bridge(AsyncMock())
    for target, request in (
        (replace(member, device_id="intruder"), task()),
        (member, replace(task(), context_ref="other-group")),
        (
            member,
            replace(
                task(),
                trigger=Message(
                    message_id="fake", author_kind="user", author_id="box3", text="hello"
                ),
            ),
        ),
    ):
        with pytest.raises(PermissionDeniedError):
            await reply(target, request, permit())
    assert not agent.turns


def test_mixed_owner_bindings_are_refused():
    with pytest.raises(PermissionDeniedError):
        CoordinatedReplies(
            context_ref="group",
            input_device_id="waveshare",
            bindings=(
                (Member("companion-test", "a"), scope()),
                (Member("other", "b"), scope("other", "another-owner")),
            ),
            accept=AcceptReply(None),
            present=AsyncMock(),
        )


async def test_adapter_runs_real_turn_engine(turn_engine_factory):
    engine = turn_engine_factory()

    async def present(member, prepared, stream, grant):
        events = [event async for event in stream]
        assert events[-1].kind.value == "done"
        return True

    reply, member, _ = bridge(present, agent=SimpleNamespace(run_turn=engine.run))
    result = await reply(member, task(), permit())
    assert result.completed and result.text


async def test_members_keep_distinct_conversation_and_memory_namespaces():
    agents = {name: Agent() for name in ("a", "b")}

    async def resolve_runtime(*, owner_id, companion_id, genome_id):
        return SimpleNamespace(
            owner_id=owner_id,
            companion_id=companion_id,
            genome_id=genome_id,
            agent=agents[companion_id],
        )

    async def present(member, prepared, stream, grant):
        async for _ in stream:
            pass
        return True

    members = (Member("a", "stackchan"), Member("b", "box3"))
    reply = CoordinatedReplies(
        context_ref="group",
        input_device_id="waveshare",
        bindings=tuple((member, scope(member.companion_id)) for member in members),
        accept=AcceptReply(SimpleNamespace(resolve_runtime=resolve_runtime)),
        present=present,
    )
    for member in members:
        await reply(member, task(), permit())
    a, b = agents["a"].turns[0], agents["b"].turns[0]
    assert a.conversation_id != b.conversation_id
    assert (a.context.memory_realm_id, b.context.memory_realm_id) == ("realm-a", "realm-b")
    assert a.coordination.public_context == b.coordination.public_context
    await reply(members[0], task(), permit())
    assert agents["a"].turns[1].conversation_id == a.conversation_id
