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


@pytest.mark.parametrize("person_id", [None, "person-a"])
async def test_memory_policy_comes_only_from_authorized_person(person_id):
    from dataclasses import replace

    authorized = replace(scope(), person_id=person_id, identity_epoch=1 if person_id else None)
    result = await AcceptReply(registry()).prepare(authorized, ReplyRequest(
        conversation_id="new-session-conversation", text="hello", input_modality="text",
        metadata={"memory_enabled": True, "person_id": "forged", "identity_epoch": 100},
    ))
    assert result.turn.context.memory_enabled is (person_id is not None)
    assert result.turn.context.person_id == person_id


async def test_verified_speaker_is_context_not_memory_authorization():
    import time

    from eidolon_sdk.biz.speaker_attestation import sign_observation
    from eidolon_sdk.biz.speaker_identity import SpeakerEvidenceSource, SpeakerIdentityObservation

    from eidolon_agent.domain.context.speaker import speaker_context
    now=time.time()
    speaker=SpeakerIdentityObservation(owner_id='owner',session_id='session',turn_id='trace',
        status='identified',confidence='controlled_trial',reason='voice_match',
        person_id='p',person_revision=1,profile_id='v',profile_revision=1,
        display_name='Alice',model_id='m',weights_hash='a'*64,preprocessing_version='v',
        scoring_version='v',decision_version='trial',issued_at=now,expires_at=now+10,
        source=SpeakerEvidenceSource(request_id='trace',session_id='session',input_id='mic',generation=1,
            sample_rate=16000,start_sample=0,end_sample=32000))
    token=sign_observation(speaker,secret='s'*32,text='hello')
    request=ReplyRequest(conversation_id='conversation',text='hello',input_modality='voice',
        trace_id='trace',metadata={'speaker_observation_token':token})
    turn=(await AcceptReply(registry(),speaker_secret='s'*32).prepare(scope(),request)).turn
    assert turn.context.speaker_observation.person_id=='p'
    assert turn.context.person_id is None and turn.context.memory_realm_id=='realm'
    assert 'speaker_observation_token' not in turn.metadata
    prompt=speaker_context(turn.context.speaker_observation)
    assert 'Alice' in prompt and 'voice matched' in prompt
    assert 'controlled_trial' not in prompt and 'confidence' not in prompt


async def test_forged_speaker_degrades_without_breaking_reply():
    request=ReplyRequest(conversation_id='conversation',text='hello',input_modality='voice',
        trace_id='trace',metadata={'speaker_observation_token':'bad',
            'speaker_observation':{'person_id':'intruder'}})
    turn=(await AcceptReply(registry(),speaker_secret='s'*32).prepare(scope(),request)).turn
    assert turn.context.speaker_observation is None
    assert turn.context.person_id is None
    assert turn.metadata['speaker_observation_rejected']
    assert 'speaker_observation' not in turn.metadata


@pytest.mark.parametrize("evidence", ["matching", "missing", "other-person", "old-revision", "other-turn", "text"])
@pytest.mark.parametrize("memory_mode", ["personal", "provisional"])
async def test_shared_room_memory_requires_this_utterances_matching_evidence(evidence, memory_mode):
    import time
    from dataclasses import replace

    from eidolon_sdk.biz.speaker_attestation import sign_observation
    from eidolon_sdk.biz.speaker_identity import SpeakerEvidenceSource, SpeakerIdentityObservation
    now = time.time()
    observed = SpeakerIdentityObservation(owner_id="owner", session_id="session", turn_id="trace",
        status="identified", confidence="controlled_trial", reason="voice_match",
        person_id="other" if evidence == "other-person" else "p",
        person_revision=2 if evidence == "old-revision" else 1,
        profile_id="v", profile_revision=1, model_id="m", weights_hash="a"*64,
        preprocessing_version="v", scoring_version="v", decision_version="trial",
        issued_at=now, expires_at=now+10,
        source=SpeakerEvidenceSource(request_id="trace", session_id="session", input_id="mic",
            generation=1, sample_rate=16000, start_sample=0, end_sample=32000))
    token = sign_observation(observed, secret="s"*32, text="hello")
    authorized = replace(scope(), source_session_id="session", person_id="p", person_revision=1, identity_epoch=1,
        memory_mode=memory_mode, provisional_memory_save=memory_mode == "provisional",
        provisional_memory_recall=memory_mode == "provisional")
    request = ReplyRequest(conversation_id="room-session:session", text="hello",
        input_modality="text" if evidence == "text" else "voice",
        trace_id="other" if evidence == "other-turn" else "trace",
        metadata={} if evidence == "missing" else {"speaker_observation_token": token})
    turn = (await AcceptReply(registry(), speaker_secret="s"*32).prepare(authorized, request)).turn
    assert turn.context.memory_enabled == (evidence == "matching" and memory_mode == "personal")
    assert turn.context.provisional_memory_recall == (evidence == "matching" and memory_mode == "provisional")
    assert turn.context.provisional_memory_save == (evidence == "matching" and memory_mode == "provisional")
    assert turn.context.person_id == ("p" if evidence == "matching" else None)
    assert turn.session_id == "session"
    assert turn.context.source_session_id == "session"
