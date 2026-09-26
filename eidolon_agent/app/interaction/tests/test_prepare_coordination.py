from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from eidolon_sdk.biz.control.coordination import CoordinationSelection
from eidolon_sdk.device_foundation.v1.testing import named_device_instance_id
from test_coordinated_replies import Agent, scope

from eidolon_agent.app.interaction import AcceptReply
from eidolon_agent.app.interaction.coordination.mock_decision import MockDecision
from eidolon_agent.app.interaction.coordination.prepare import prepare_session
from eidolon_agent.core.errors import PermissionDeniedError
from eidolon_agent.domain.runtime_session import RuntimeSessionAuthorizer

pytestmark = pytest.mark.unit


def selection(**options):
    def ref(name):
        return dict(
            device_instance_id=named_device_instance_id(name),
            owner_domain_id="owner-domain-1",
            owner_domain_generation=1,
            claim_generation=1,
            trust_epoch=1,
        )

    return CoordinationSelection.model_validate(
        dict(
            scenario="ip_role_group",
            session_id="scene",
            input_device=ref("input"),
            members=[
                dict(companion_id="a", output_device=ref("output-a")),
                dict(companion_id="b", output_device=ref("output-b")),
            ],
            **options,
        )
    )


def dependencies(*, refused=None):
    agents = {key: Agent() for key in ("a", "b")}
    authorized = []

    async def resolve(owner_id, companion_id):
        authorized.append((owner_id, companion_id))
        if companion_id == refused:
            raise PermissionDeniedError("Companion unavailable to Owner")
        facts = scope(companion_id, owner_id).runtime
        facts.runtime_config = {}
        return facts

    async def resolve_runtime(owner_id, companion_id, genome_id):
        return SimpleNamespace(
            owner_id=owner_id,
            companion_id=companion_id,
            genome_id=genome_id,
            agent=agents[companion_id],
        )

    played = []

    async def present(member, prepared, stream, permit):
        async for _ in stream:
            permit.check()
        played.append((member.companion_id, member.device_id))
        return True

    registry = SimpleNamespace(resolve_runtime=AsyncMock(side_effect=resolve_runtime))
    args = dict(
        authenticated_owner_id="alice",
        runtime_sessions=RuntimeSessionAuthorizer(SimpleNamespace(resolve=resolve)),
        accept=AcceptReply(registry),
        present=present,
        decide=MockDecision(("b", "a")),
        stop=AsyncMock(),
        transcribe=AsyncMock(return_value="hello"),
    )
    return args, agents, authorized, played, registry


async def test_selection_authorizes_all_members_then_runs_existing_reply_pipeline():
    selected = selection(discussion=True, reply_budget=3)
    args, agents, authorized, played, registry = dependencies()
    session = await prepare_session(selected, **args)
    assert authorized == [("alice", "a"), ("alice", "b")]
    assert not played
    registry.resolve_runtime.assert_not_called()
    args["stop"].assert_not_called()
    try:
        device_id = selected.input_device.device_instance_id
        session.press(device_id=device_id, capture_id="capture")
        await session.release(device_id=device_id, capture_id="capture")
        assert session.state == "waiting"
        # Configuration order does not override the independent decision order.
        assert [key for key, _ in played] == ["b", "a", "b"]
        targets = {m.companion_id: m.output_device.device_instance_id for m in selected.members}
        assert all(device == targets[key] for key, device in played)
        args["transcribe"].assert_awaited_once_with("capture")
        for key, agent in agents.items():
            for turn in agent.turns:
                assert turn.context.owner_id == "alice"
                assert turn.context.companion_id == key
                assert turn.context.device_id == device_id
        assert agents["b"].turns[-1].coordination.trigger.author_kind == "companion"
    finally:
        await session.close()


@pytest.mark.parametrize("refused", ["a", "b"])
async def test_any_refused_member_prevents_all_model_and_device_actions(refused):
    args, agents, _, played, registry = dependencies(refused=refused)
    with pytest.raises(PermissionDeniedError):
        await prepare_session(selection(), **args)
    assert not played and all(not agent.turns for agent in agents.values())
    registry.resolve_runtime.assert_not_called()
    args["stop"].assert_not_called()
    args["transcribe"].assert_not_called()


async def test_missing_authenticated_owner_is_rejected_before_runtime_resolution():
    args, _, authorized, _, _ = dependencies()
    args["authenticated_owner_id"] = ""
    with pytest.raises(PermissionDeniedError):
        await prepare_session(selection(), **args)
    assert not authorized


async def test_each_scheduled_companion_receives_all_preceding_public_replies():
    selected = selection(discussion=True, reply_budget=5)
    args, agents, _, played, _ = dependencies()
    args['decide'] = MockDecision(('a', 'b'))
    session = await prepare_session(selected, **args)
    try:
        source = selected.input_device.device_instance_id
        session.press(device_id=source, capture_id='user-question')
        await session.release(device_id=source, capture_id='user-question')
        assert [key for key, _ in played] == ['a', 'b', 'a', 'b', 'a']
        turns = [agents['a'].turns[0], agents['b'].turns[0],
                 agents['a'].turns[1], agents['b'].turns[1], agents['a'].turns[2]]
        for index, turn in enumerate(turns):
            # Includes the original user's question and every earlier completed
            # speaker, with its own author identity and exact generated text.
            assert turn.coordination.public_context.recent_messages == tuple(
                session.history[:index + 1])
            if index >= 2:
                assert turn.coordination.trigger == session.history[index]
                assert turn.text == ''  # Peer speech is never user input/memory.
        args['transcribe'].assert_awaited_once_with('user-question')
        assert turns[0].conversation_id != turns[1].conversation_id
        assert turns[0].conversation_id == turns[2].conversation_id
    finally:
        await session.close()
