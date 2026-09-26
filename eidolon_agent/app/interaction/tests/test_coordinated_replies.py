"""Reply adapter uses scene-local role execution and the existing streaming presenter."""

import asyncio
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from eidolon_sdk.biz.participation import Context, Message
from eidolon_sdk.biz.persona import build_default_persona_genome

from eidolon_agent.app.interaction.coordination import Member, Permit, ReplyTask
from eidolon_agent.app.interaction.coordination.replies import CoordinatedReplies
from eidolon_agent.app.interaction.coordination.role_reply import RoleReplyExecutor
from eidolon_agent.core.errors import PermissionDeniedError
from eidolon_agent.core.types.companion_runtime import CompanionRuntimeConfig
from eidolon_agent.core.types.turn import TurnEvent, TurnStatus
from eidolon_agent.domain.runtime_session import AuthorizedRuntimeSession
from eidolon_agent.infra.llm.providers.fake import FakeLLM
from tests.helpers import make_turn_input

pytestmark = pytest.mark.unit


def scope(companion="companion-test", owner="alice"):
    context = make_turn_input().context
    return AuthorizedRuntimeSession(
        runtime=SimpleNamespace(
            owner_id=owner,
            companion_id=companion,
            genome_id=context.genome_id,
            genome=build_default_persona_genome(name="Test Companion"),
            genome_version=1,
            memory_realm_id="realm-" + companion,
            schema_version=context.schema_version,
            genome_hash=context.genome_hash,
            realizer_version=context.realizer_version,
        ),
        device_id="waveshare",
        session_id="group",
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

    async def run(self, turn):
        self.turns.append(turn)
        try:
            yield TurnEvent.delta(turn.turn_id, 0, "你好", 0)
            yield TurnEvent.done(turn.turn_id, 1, TurnStatus.OK, 0)
        finally:
            self.closed = True


def bridge(present, *, agent=None):
    agent = agent or Agent()
    member = Member("companion-test", "stackchan")
    return (
        CoordinatedReplies(
            context_ref="group",
            input_device_id="waveshare",
            bindings=((member, scope()),),
            executor=agent,
            present=present,
        ),
        member,
        agent,
    )


async def test_streams_role_executor_and_requires_playback_completion():
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
    assert agent.turns[0].scope.companion_id == member.companion_id
    assert agent.turns[0].scope.device_id == "waveshare"
    assert agent.turns[0].trigger.author_kind == "user"
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
    assert agent.turns[0].trigger.author_kind == "companion"


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
            executor=None,
            present=AsyncMock(),
        )


async def test_adapter_runs_independent_role_executor():
    executor = RoleReplyExecutor(FakeLLM(per_token_delay_s=0))

    async def present(member, prepared, stream, grant):
        events = [event async for event in stream]
        assert events[-1].kind.value == "done"
        return True

    reply, member, _ = bridge(present, agent=executor)
    result = await reply(member, task(), permit())
    assert result.completed and result.text


async def test_members_keep_distinct_authorized_identity_with_same_public_context():
    agent = Agent()
    async def present(member, prepared, stream, grant):
        async for _ in stream:
            pass
        return True
    members = (Member("a", "stackchan"), Member("b", "box3"))
    reply = CoordinatedReplies(context_ref="group", input_device_id="waveshare",
        bindings=tuple((m, scope(m.companion_id)) for m in members),
        executor=agent, present=present)
    for member in members:
        await reply(member, task(), permit())
    a, b = agent.turns
    assert a.scope.companion_id == "a" and b.scope.companion_id == "b"
    assert a.public_context == b.public_context
    assert a.members == b.members
