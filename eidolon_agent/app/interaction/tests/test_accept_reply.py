"""Authorized reply ingress, independent of protobuf and device transport."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from eidolon_agent.app.interaction import AcceptReply, ReplyRequest
from eidolon_agent.core.errors import PermissionDeniedError
from eidolon_agent.core.types.companion_runtime import CompanionRuntimeConfig
from eidolon_agent.domain.runtime_session import AuthorizedRuntimeSession

pytestmark = pytest.mark.unit


def scope():
    return AuthorizedRuntimeSession(
        runtime=SimpleNamespace(
            owner_id="owner",
            companion_id="wukong",
            genome_id="genome",
            memory_realm_id="realm",
            schema_version="schema",
            genome_hash="hash",
            realizer_version="realizer",
        ),
        device_id="ptt",
        session_id="session",
        config=CompanionRuntimeConfig(),
    )


def registry(**overrides):
    instance = dict(owner_id="owner", companion_id="wukong", genome_id="genome", agent=object())
    instance.update(overrides)
    return SimpleNamespace(resolve_runtime=AsyncMock(return_value=SimpleNamespace(**instance)))


async def test_authority_alone_selects_runtime_and_source_device():
    r = registry()
    accepted = await AcceptReply(r).prepare(
        scope(),
        ReplyRequest(
            conversation_id="conversation",
            text="hello",
            input_modality="text",
            metadata={"owner_id": "intruder", "companion_id": "bajie", "device_id": "fake"},
        ),
    )
    r.resolve_runtime.assert_awaited_once_with(
        owner_id="owner", companion_id="wukong", genome_id="genome"
    )
    assert accepted.turn.context.owner_id == "owner"
    assert accepted.turn.context.companion_id == "wukong"
    assert accepted.turn.context.device_id == "ptt"
    assert accepted.turn.session_id == "session"
    assert accepted.turn.context.memory_realm_id == "realm"
    assert accepted.turn.turn_id


async def test_voice_commit_and_speculation_survive_without_mutating_request():
    metadata = {"committed_turn_decision": {"transcript": "hello"}}
    request = ReplyRequest(
        conversation_id="conversation",
        text="hello",
        input_modality="voice",
        turn_id="turn",
        trace_id="trace",
        request_id="request",
        metadata=metadata,
        speculative=True,
    )
    accepted = await AcceptReply(registry()).prepare(scope(), request)
    assert accepted.turn.input_modality == "voice"
    assert accepted.turn.metadata["speculative"] is True
    assert accepted.turn.metadata["committed_turn_decision"] == metadata["committed_turn_decision"]
    assert accepted.turn.context.trace_id == "trace"
    assert accepted.turn.context.request_id == "request"
    accepted.turn.metadata["committed_turn_decision"]["transcript"] = "changed"
    assert metadata == {"committed_turn_decision": {"transcript": "hello"}}


@pytest.mark.parametrize(
    "field,value",
    [
        ("owner_id", "other"),
        ("companion_id", "other"),
        ("genome_id", "other"),
    ],
)
async def test_registry_cannot_replace_authorized_runtime(field, value):
    with pytest.raises(PermissionDeniedError):
        await AcceptReply(registry(**{field: value})).prepare(
            scope(),
            ReplyRequest(conversation_id="conversation", text="hello", input_modality="text"),
        )


@pytest.mark.parametrize("changes", [{"conversation_id": " "}, {"input_modality": "video"}])
async def test_invalid_request_rejected_before_registry(changes):
    r = registry()
    args = dict(conversation_id="conversation", text="hello", input_modality="text")
    args.update(changes)
    with pytest.raises(ValueError):
        await AcceptReply(r).prepare(scope(), ReplyRequest(**args))
    r.resolve_runtime.assert_not_awaited()


async def test_virtual_companion_does_not_require_source_device():
    original = scope()
    virtual = AuthorizedRuntimeSession(
        runtime=original.runtime,
        device_id=None,
        session_id=original.session_id,
        config=original.config,
    )
    prepared = await AcceptReply(registry()).prepare(
        virtual, ReplyRequest(conversation_id="conversation", text="hello", input_modality="text")
    )
    assert prepared.turn.context.device_id is None


async def test_missing_agent_fails_before_execution():
    from eidolon_agent.core.errors import NotFoundError

    with pytest.raises(NotFoundError):
        await AcceptReply(registry(agent=None)).prepare(
            scope(),
            ReplyRequest(conversation_id="conversation", text="hello", input_modality="text"),
        )
