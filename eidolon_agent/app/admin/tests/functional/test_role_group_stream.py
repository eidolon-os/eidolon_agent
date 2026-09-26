"""Real ASGI socket drives scene preparation, reply streaming and receipts."""

from types import SimpleNamespace

import pytest
from eidolon_sdk.biz.control.coordination_stream import ROLE_GROUP_STREAM_PATH
from eidolon_sdk.biz.dialogue_control import CommittedTurnDecision, TurnCommitBoundary
from eidolon_sdk.device_foundation.v1.testing import named_device_instance_id
from fastapi.routing import APIWebSocketRoute
from fastapi.testclient import TestClient
from starlette.testclient import WebSocketDenialResponse
from starlette.websockets import WebSocketDisconnect

from eidolon_agent.app.admin.app import build_admin_app
from eidolon_agent.app.admin.authority import SERVICE_TOKEN_ENV, require_service_token
from eidolon_agent.config.settings import Settings
from eidolon_agent.core.types.turn import TurnEvent, TurnStatus
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
        mock_order=["b", "a"],
        selection=dict(
            scenario="ip_role_group",
            session_id="scene",
            input_device=ref("input"),
            members=[dict(companion_id=k, output_device=ref(k)) for k in ("a", "b")],
        ),
    )


def app(*, done_status=TurnStatus.OK):
    turns = []
    context = make_turn_input().context

    class Agent:
        async def run_turn(self, turn):
            turns.append(turn)
            yield TurnEvent.delta(turn.turn_id, 0, "hello", 0)
            yield TurnEvent.done(turn.turn_id, 1, done_status, 0)

    async def resolve(owner_id, companion_id):
        return SimpleNamespace(
            owner_id=owner_id,
            companion_id=companion_id,
            genome_id=context.genome_id,
            memory_realm_id="realm-" + companion_id,
            schema_version=context.schema_version,
            genome_hash=context.genome_hash,
            realizer_version=context.realizer_version,
            runtime_config={},
        )

    async def runtime(owner_id, companion_id, genome_id):
        return SimpleNamespace(
            owner_id=owner_id, companion_id=companion_id, genome_id=genome_id, agent=Agent()
        )

    instance = build_admin_app(
        settings=Settings(),
        agent_registry=SimpleNamespace(resolve_runtime=runtime),
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
