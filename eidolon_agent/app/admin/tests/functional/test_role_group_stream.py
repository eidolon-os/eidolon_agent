"""Real ASGI socket drives scene preparation, reply streaming and receipts."""

import json
from types import SimpleNamespace

import pytest
from eidolon_sdk.biz.control.coordination_stream import ROLE_GROUP_STREAM_PATH
from eidolon_sdk.biz.dialogue_control import CommittedTurnDecision, TurnCommitBoundary
from eidolon_sdk.biz.persona import build_default_persona_genome
from eidolon_sdk.device_foundation.v1.testing import named_device_instance_id
from fastapi.routing import APIWebSocketRoute
from fastapi.testclient import TestClient
from starlette.testclient import WebSocketDenialResponse
from starlette.websockets import WebSocketDisconnect

from eidolon_agent.app.admin.app import build_admin_app
from eidolon_agent.app.admin.authority import SERVICE_TOKEN_ENV, require_service_token
from eidolon_agent.config.settings import Settings
from eidolon_agent.core.types.llm import LLMDelta, LLMFinishReason
from eidolon_agent.core.types.turn import TurnStatus
from tests.helpers import make_turn_input

pytestmark = pytest.mark.functional
TOKEN = "test-only-role-group-service-token"
HEADERS = {"Authorization": f"Bearer {TOKEN}"}


def opening():
    def ref(name):
        return dict(
            device_instance_id=named_device_instance_id(name),
            owner_domain_id="owner-domain-1",
            owner_domain_generation=1,
            claim_generation=1,
            trust_epoch=1,
        )

    return dict(
        type="open",
        owner_id="alice",
        selection=dict(
            scenario="ip_role_group",
            session_id="scene",
            input_device=ref("input"),
            members=[dict(companion_id=k, output_device=ref(k),
                          role=dict(name="孙悟空" if k == "a" else "猪八戒")) for k in ("a", "b")],
        ),
    )


def app(*, done_status=TurnStatus.OK, decision_port=None):
    turns = []
    context = make_turn_input().context

    class Model:
        async def stream(self, messages, **kwargs):
            rules = json.loads('{' + messages[0].content.split('\n{', 1)[1].split('\n公开消息')[0])
            public = json.loads(messages[1].content.split('\n', 1)[1])
            task = json.loads(messages[2].content[messages[2].content.index('{'):])
            turns.append(SimpleNamespace(context=SimpleNamespace(
                companion_id=rules['speaker_companion_id']), text=public['user_request']['text'],
                action=task['action'], instruction=task['task']))
            assert rules['speaker_role']['name'] == (
                '孙悟空' if rules['speaker_companion_id'] == 'a' else '猪八戒')
            assert kwargs['tools'] == []
            yield LLMDelta(text_delta='hello')
            yield LLMDelta(finish=LLMFinishReason.STOP if done_status == TurnStatus.OK else LLMFinishReason.ERROR)

    async def resolve(owner_id, companion_id):
        return SimpleNamespace(
            owner_id=owner_id,
            companion_id=companion_id,
            genome_id=context.genome_id,
            genome=build_default_persona_genome(name="Test Companion"), genome_version=1,
            memory_realm_id="realm-" + companion_id,
            schema_version=context.schema_version,
            genome_hash=context.genome_hash,
            realizer_version=context.realizer_version,
            runtime_config={},
        )

    from tests.decision_helpers import decided
    async def decision(request):
        if request.trigger.author_kind == 'user':
            return decided(request, speaker='b')
        if request.trigger.author_id == 'b':
            return decided(request, speaker='a')
        return decided(request, 'finish')

    instance = build_admin_app(
        settings=Settings(),
        participation_decision=decision_port if decision_port is not None else decision,
        agent_registry=None,
        llm_router=Model(),
        runtime_authority=SimpleNamespace(resolve=resolve),
    )
    return instance, turns


def receipt(socket, message, result="completed"):
    socket.send_json(
        dict(
            type="receipt",
            request_id=message["request_id"],
            device_id=message["device_id"],
            result=result,
        )
    )


def utterance(socket, capture="one", text="hello"):
    socket.send_json(dict(type="press", capture_id=capture))
    socket.send_json(dict(type="release", capture_id=capture))
    socket.send_json(
        dict(
            type="transcript",
            capture_id=capture,
            text=text,
            commitment=CommittedTurnDecision.create(
                text=text, boundary=TurnCommitBoundary.PTT_SEGMENT
            ).as_metadata(),
        )
    )


def stops(socket):
    for _ in range(2):
        frame = socket.receive_json()
        if frame["type"] == "capturing":
            assert frame["epoch"] > 0
            frame = socket.receive_json()
        assert frame["type"] == "stop", frame
        receipt(socket, frame)


def reply(socket, companion):
    start = socket.receive_json()
    assert start["type"] == "reply_start" and start["companion_id"] == companion
    delta = socket.receive_json()
    assert delta["type"] == "reply_delta" and delta["text"] == "hello"
    end = socket.receive_json()
    assert end["type"] == "reply_end" and end["request_id"] == start["request_id"]
    return end


def test_socket_requires_existing_service_authority(monkeypatch):
    monkeypatch.setenv(SERVICE_TOKEN_ENV, TOKEN)
    instance, turns = app()
    routes = [r for r in instance.routes if isinstance(r, APIWebSocketRoute)]
    assert routes
    assert all(
        any(d.call is require_service_token for d in r.dependant.dependencies) for r in routes
    )
    with TestClient(instance) as client:
        for headers in ({}, {"Authorization": "Bearer wrong"}):
            with (
                pytest.raises(WebSocketDenialResponse) as denied,
                client.websocket_connect(ROLE_GROUP_STREAM_PATH, headers=headers),
            ):
                pass
            assert denied.value.status_code == 401
    assert not turns


def test_actual_socket_serializes_replies_until_matching_played_receipt(monkeypatch):
    from eidolon_agent.app.admin.routers import role_groups
    original_timeout = role_groups.asyncio.timeout
    def operation_timeout(seconds):
        # This route may bound preparation, never the active team's lifetime.
        assert seconds <= 10
        return original_timeout(seconds)
    monkeypatch.setattr(role_groups, "asyncio", SimpleNamespace(
        **(vars(role_groups.asyncio) | {"timeout": operation_timeout})))
    monkeypatch.setenv(SERVICE_TOKEN_ENV, TOKEN)
    instance, turns = app()
    with (
        TestClient(instance) as client,
        client.websocket_connect(ROLE_GROUP_STREAM_PATH, headers=HEADERS) as socket,
    ):
        socket.send_json(opening())
        prepared = socket.receive_json()
        assert prepared["type"] == "prepared"
        assert prepared["physical_devices_ready"] is False
        utterance(socket)
        stops(socket)
        first = reply(socket, "b")
        assert len(turns) == 1  # model DONE is not permission to start A
        receipt(socket, first)
        second = reply(socket, "a")
        receipt(socket, first)  # duplicate old receipt cannot satisfy A
        receipt(socket, second)
        state = socket.receive_json()
        assert state["type"] == "state" and state["state"] == "waiting"
        assert [t.context.companion_id for t in turns] == ["b", "a"]
        socket.send_json({"type": "close"})
        stops(socket)
        with pytest.raises(WebSocketDisconnect) as closed:
            socket.receive_json()
        assert closed.value.code == 1000
    assert not instance.state.role_group_connections.active
    assert not instance.state.role_group_connections.devices


def test_wrong_device_receipt_closes_scene_without_generating(monkeypatch):
    monkeypatch.setenv(SERVICE_TOKEN_ENV, TOKEN)
    instance, turns = app()
    with (
        TestClient(instance) as client,
        client.websocket_connect(ROLE_GROUP_STREAM_PATH, headers=HEADERS) as socket,
    ):
        socket.send_json(opening())
        socket.receive_json()
        utterance(socket)
        capturing = socket.receive_json()
        assert capturing["type"] == "capturing"
        stop = socket.receive_json()
        stop["device_id"] = named_device_instance_id("not-selected")
        receipt(socket, stop)
        with pytest.raises(WebSocketDisconnect) as closed:
            while True:
                socket.receive_json()
        assert closed.value.code == 1008
    assert not turns
    assert not instance.state.role_group_connections.active


def test_second_scene_cannot_steal_active_scene_devices(monkeypatch):
    monkeypatch.setenv(SERVICE_TOKEN_ENV, TOKEN)
    instance, _ = app()
    with (
        TestClient(instance) as client,
        client.websocket_connect(ROLE_GROUP_STREAM_PATH, headers=HEADERS) as first,
    ):
        first.send_json(opening())
        first.receive_json()
        with client.websocket_connect(ROLE_GROUP_STREAM_PATH, headers=HEADERS) as second:
            other = opening()
            other["selection"]["session_id"] = "another-scene"
            second.send_json(other)
            with pytest.raises(WebSocketDisconnect) as closed:
                second.receive_json()
            assert closed.value.code == 1008
        assert len(instance.state.role_group_connections.active) == 1
    assert not instance.state.role_group_connections.active


def test_press_revokes_playing_reply_and_old_receipt_cannot_restart_queue(monkeypatch):
    monkeypatch.setenv(SERVICE_TOKEN_ENV, TOKEN)
    instance, turns = app()
    with (
        TestClient(instance) as client,
        client.websocket_connect(ROLE_GROUP_STREAM_PATH, headers=HEADERS) as socket,
    ):
        socket.send_json(opening())
        socket.receive_json()
        utterance(socket)
        stops(socket)
        old = reply(socket, "b")
        utterance(socket, capture="two", text="new question")
        stops(socket)
        receipt(socket, old)
        new_b = reply(socket, "b")
        receipt(socket, new_b)
        new_a = reply(socket, "a")
        receipt(socket, new_a)
        assert socket.receive_json()["state"] == "waiting"
        assert [t.context.companion_id for t in turns] == ["b", "b", "a"]
        assert [t.text for t in turns] == ["hello", "new question", "new question"]
        socket.send_json({"type": "close"})
        stops(socket)
        with pytest.raises(WebSocketDisconnect):
            socket.receive_json()


def test_new_press_while_waiting_for_final_asr_keeps_input_available(monkeypatch):
    monkeypatch.setenv(SERVICE_TOKEN_ENV, TOKEN)
    instance, turns = app()
    with (
        TestClient(instance) as client,
        client.websocket_connect(ROLE_GROUP_STREAM_PATH, headers=HEADERS) as socket,
    ):
        socket.send_json(opening())
        socket.receive_json()
        socket.send_json(dict(type="press", capture_id="old"))
        socket.send_json(dict(type="release", capture_id="old"))
        stops(socket)
        utterance(socket, capture="new", text="current")
        stops(socket)
        # A valid but obsolete final transcript cannot become another turn.
        socket.send_json(
            dict(
                type="transcript",
                capture_id="old",
                text="obsolete",
                commitment=CommittedTurnDecision.create(
                    text="obsolete", boundary=TurnCommitBoundary.PTT_SEGMENT
                ).as_metadata(),
            )
        )
        receipt(socket, reply(socket, "b"))
        receipt(socket, reply(socket, "a"))
        assert socket.receive_json()["state"] == "waiting"
        assert [t.text for t in turns] == ["current", "current"]
        socket.send_json({"type": "close"})
        stops(socket)
        with pytest.raises(WebSocketDisconnect):
            socket.receive_json()


def test_failed_model_completion_stops_without_requesting_played_receipt(monkeypatch):
    monkeypatch.setenv(SERVICE_TOKEN_ENV, TOKEN)
    instance, turns = app(done_status=TurnStatus.ERRORED)
    with (
        TestClient(instance) as client,
        client.websocket_connect(ROLE_GROUP_STREAM_PATH, headers=HEADERS) as socket,
    ):
        socket.send_json(opening())
        socket.receive_json()
        utterance(socket)
        stops(socket)
        stop_count = 0
        while True:
            frame = socket.receive_json()
            assert frame["type"] != "reply_end"
            if frame["type"] == "stop":
                stop_count += 1
                receipt(socket, frame)
            if frame["type"] == "state":
                assert frame["state"] == "failed"
                break
        assert stop_count == 2
        assert len(turns) == 1
        socket.send_json({"type": "close"})
        stops(socket)
        with pytest.raises(WebSocketDisconnect):
            socket.receive_json()


def test_stop_failure_is_round_scoped_and_next_capture_recovers_on_same_socket(monkeypatch):
    monkeypatch.setenv(SERVICE_TOKEN_ENV, TOKEN)
    instance, turns = app()
    with TestClient(instance) as client, client.websocket_connect(
        ROLE_GROUP_STREAM_PATH, headers=HEADERS
    ) as socket:
        socket.send_json(opening())
        socket.receive_json()
        utterance(socket, capture="failed")
        assert socket.receive_json()["type"] == "capturing"
        for _ in range(2):
            stop = socket.receive_json()
            assert stop["type"] == "stop" and stop["capture_id"] == "failed"
            socket.send_json(dict(type="receipt", request_id=stop["request_id"],
                device_id=stop["device_id"], result="failed", error_code="TEAM_STOP_TIMEOUT"))
        state = socket.receive_json()
        assert state["outcome"] == "error"
        assert state["error_code"] == "TEAM_STOP_UNCONFIRMED"
        assert not turns
        utterance(socket, capture="retry")
        stops(socket)
        receipt(socket, reply(socket, "b"))
        receipt(socket, reply(socket, "a"))
        assert socket.receive_json()["outcome"] == "finished"
        socket.send_json({"type": "close"})
        for _ in range(2):
            stop = socket.receive_json()
            assert stop["type"] == "stop" and stop["capture_id"] is None
            receipt(socket, stop)
        with pytest.raises(WebSocketDisconnect):
            socket.receive_json()
