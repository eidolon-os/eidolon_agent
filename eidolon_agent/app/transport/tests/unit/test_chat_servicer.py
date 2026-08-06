"""EidolonAgentServicer — Chat / PushSignal runtime RPCs.

Here we mock the registry, signal bus, and the auth contextvar so we can
drive the servicer directly without spinning up a real gRPC server.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import grpc
import pytest

from eidolon_agent.app.transport.grpc.chat_servicer import EidolonAgentServicer
from eidolon_agent.app.transport.grpc.interceptors import _current_identity
from eidolon_agent.app.transport.grpc.proto import pb
from eidolon_agent.core.types.turn import TurnEventKind

pytestmark = pytest.mark.unit


# ---- helpers --------------------------------------------------------------


@dataclass
class _StubIdentity:
    owner_id: str = "owner-1"
    companion_id: str = "companion-1"
    memory_realm_id: str = "realm-1"
    genome_id: str = "genome-1"
    device_id: str | None = "dev-1"
    schema_version: str = "eidolon.persona_genome"
    genome_hash: str = "pg_stub"
    realizer_version: str = "eidolon.persona_realizer"


def _make_context() -> MagicMock:
    ctx = MagicMock()
    ctx.abort = AsyncMock(side_effect=grpc.RpcError("aborted"))
    ctx.write = AsyncMock()
    ctx.invocation_metadata = MagicMock(return_value=())
    return ctx


def _scripted_agent(events):
    """Returns a stub agent whose run_turn yields the given events."""

    async def _gen(_ti):
        for ev in events:
            yield ev

    agent = MagicMock()
    agent.run_turn = _gen
    return agent


def _stub_instance(agent):
    return SimpleNamespace(
        companion_id="companion-1",
        genome_id="genome-1",
        agent=agent,
    )


# ---- PushSignal -----------------------------------------------------------


async def test_push_signal_publishes_to_signal_bus() -> None:
    signals = MagicMock()
    signals.publish = AsyncMock()
    svc = EidolonAgentServicer(
        agent_registry=MagicMock(),
        signals_bus=signals, proactive_bus=MagicMock(),
    )
    req = pb.SignalRequest(
        session_id="sess-1",
        signal=pb.PushSignalInline(modality="face", label="smile", confidence=0.91),
    )
    ack = await svc.PushSignal(req, _make_context())
    assert ack.accepted
    signals.publish.assert_awaited_once()
    sess, sig = signals.publish.await_args.args
    assert sess == "sess-1"
    assert sig.label == "smile"
    assert 0.9 < sig.confidence <= 1.0


async def test_chat_cancels_active_turn_when_context_is_cancelled() -> None:
    """Raw TCP close / RPC cancellation must propagate to in-flight turns so we
    stop billing the LLM. The watcher task polls ``context.cancelled()`` and
    cancels the turn task within ~50 ms."""
    import asyncio

    from eidolon_agent.core.types.turn import TurnEvent

    cancelled_during_turn = asyncio.Event()

    async def _slow_turn(_ti):
        try:
            # Yield one event, then sleep — emulates an LLM that hasn't started
            # producing yet but is holding the upstream HTTP connection.
            yield TurnEvent(turn_id="t", seq=0, kind=TurnEventKind.STATE, data={"state": "speaking"})
            await asyncio.sleep(5.0)
        except asyncio.CancelledError:
            cancelled_during_turn.set()
            raise

    agent = MagicMock()
    agent.run_turn = _slow_turn

    registry = MagicMock()
    registry.resolve_runtime = AsyncMock(
        return_value=_stub_instance(agent)
    )
    svc = EidolonAgentServicer(
        agent_registry=registry,
        signals_bus=SimpleNamespace(recent=AsyncMock(return_value=[])),
        proactive_bus=MagicMock(),
    )

    # Build a request iterator that yields one start frame and then blocks,
    # simulating a client still holding the stream open.
    iterator_blocker = asyncio.Event()
    start_frame = pb.ChatRequest(
        start=pb.StartTurn(
            turn_id="t1", conversation_id="c", text="hi", input_modality="voice"
        )
    )

    async def _req_iter():
        yield start_frame
        await iterator_blocker.wait()

    ctx = _make_context()
    cancelled_flag = {"v": False}
    ctx.cancelled = lambda: cancelled_flag["v"]
    ctx.done = lambda: cancelled_flag["v"]

    token = _current_identity.set(_StubIdentity())
    try:
        chat_task = asyncio.create_task(svc.Chat(_req_iter(), ctx))
        # Give the turn time to start and write the first event.
        await asyncio.sleep(0.1)
        # Simulate the gRPC layer marking the RPC cancelled (TCP close).
        cancelled_flag["v"] = True
        iterator_blocker.set()  # release the iterator so Chat() can finish
        await asyncio.wait_for(chat_task, timeout=1.0)
    finally:
        _current_identity.reset(token)

    # The turn was actually interrupted by CancelledError, not by reaching its
    # natural end. This is the whole point of the watcher.
    assert cancelled_during_turn.is_set()


async def test_chat_does_not_cancel_active_turn_when_context_is_done_but_not_cancelled() -> None:
    """``context.done()`` can become true while the client is still draining
    responses. Only ``cancelled()`` should interrupt the active turn."""
    import asyncio

    from eidolon_agent.core.types.turn import TurnEvent

    cancelled_during_turn = asyncio.Event()
    done_seen = asyncio.Event()
    done_flag = {"v": False}

    async def _turn(_ti):
        try:
            yield TurnEvent(turn_id="t", seq=0, kind=TurnEventKind.STATE, data={"state": "speaking"})
            await asyncio.sleep(0.1)
            yield TurnEvent(turn_id="t", seq=1, kind=TurnEventKind.DONE, data={})
        except asyncio.CancelledError:
            cancelled_during_turn.set()
            raise

    async def _write(ev):
        if ev.kind == pb.TurnEvent.STATE:
            done_flag["v"] = True
        if ev.kind == pb.TurnEvent.DONE:
            done_seen.set()

    agent = MagicMock()
    agent.run_turn = _turn

    registry = MagicMock()
    registry.resolve_runtime = AsyncMock(
        return_value=_stub_instance(agent)
    )
    svc = EidolonAgentServicer(
        agent_registry=registry,
        signals_bus=SimpleNamespace(recent=AsyncMock(return_value=[])),
        proactive_bus=MagicMock(),
    )

    async def _req_iter():
        yield pb.ChatRequest(
            start=pb.StartTurn(
                turn_id="t1", conversation_id="c", text="hi", input_modality="voice"
            )
        )
        await done_seen.wait()

    ctx = _make_context()
    ctx.write = AsyncMock(side_effect=_write)
    ctx.cancelled = lambda: False
    ctx.done = lambda: done_flag["v"]

    token = _current_identity.set(_StubIdentity())
    try:
        await asyncio.wait_for(svc.Chat(_req_iter(), ctx), timeout=1.0)
    finally:
        _current_identity.reset(token)

    assert done_seen.is_set()
    assert not cancelled_during_turn.is_set()


async def test_chat_start_inline_realtime_reaches_turn_input() -> None:
    from eidolon_agent.core.types.turn import TurnEvent

    captured = {}

    async def _turn(ti):
        captured["realtime"] = ti.realtime
        yield TurnEvent(turn_id=ti.turn_id, seq=0, kind=TurnEventKind.DONE, data={})

    agent = MagicMock()
    agent.run_turn = _turn
    registry = MagicMock()
    registry.resolve_runtime = AsyncMock(
        return_value=_stub_instance(agent)
    )
    signals = MagicMock()
    signals.recent = AsyncMock(return_value=[])
    svc = EidolonAgentServicer(
        agent_registry=registry,
        signals_bus=signals, proactive_bus=MagicMock(),
    )

    async def _req_iter():
        yield pb.ChatRequest(
            start=pb.StartTurn(
                turn_id="t1",
                conversation_id="c",
                text="hi",
                input_modality="voice",
                realtime={
                    "window_ms": 1000,
                    "dominant_emotion": "sad",
                    "emotion_confidence": 0.8,
                    "confidence_overall": 0.8,
                },
            )
        )

    ctx = _make_context()
    ctx.cancelled = lambda: False
    ctx.done = lambda: False
    token = _current_identity.set(_StubIdentity())
    try:
        await svc.Chat(_req_iter(), ctx)
    finally:
        _current_identity.reset(token)

    assert captured["realtime"].dominant_emotion == "sad"
    signals.recent.assert_not_awaited()


async def test_chat_start_uses_explicit_text_input_modality() -> None:
    from eidolon_agent.core.types.turn import TurnEvent

    captured = {}

    async def _turn(ti):
        captured["input_modality"] = ti.input_modality
        captured["metadata"] = ti.metadata
        yield TurnEvent(turn_id=ti.turn_id, seq=0, kind=TurnEventKind.DONE, data={})

    agent = MagicMock()
    agent.run_turn = _turn
    registry = MagicMock()
    registry.resolve_runtime = AsyncMock(return_value=_stub_instance(agent))
    signals = MagicMock()
    signals.recent = AsyncMock(return_value=[])
    svc = EidolonAgentServicer(
        agent_registry=registry,
        signals_bus=signals,
        proactive_bus=MagicMock(),
    )

    async def _req_iter():
        yield pb.ChatRequest(
            start=pb.StartTurn(
                turn_id="t1",
                conversation_id="c",
                text="hi",
                input_modality="text",
            )
        )

    ctx = _make_context()
    ctx.cancelled = lambda: False
    ctx.done = lambda: False
    token = _current_identity.set(_StubIdentity())
    try:
        await svc.Chat(_req_iter(), ctx)
    finally:
        _current_identity.reset(token)

    assert captured["input_modality"] == "text"
    assert "caller_kind" not in captured["metadata"]
    assert "entrypoint" not in captured["metadata"]


async def test_chat_fuses_recent_signals_when_start_has_no_realtime() -> None:
    from datetime import timedelta

    from eidolon_agent.core.types.signal import RealtimeSignal, SignalModality
    from eidolon_agent.core.types.turn import TurnEvent

    captured = {}

    async def _turn(ti):
        captured["realtime"] = ti.realtime
        yield TurnEvent(turn_id=ti.turn_id, seq=0, kind=TurnEventKind.DONE, data={})

    agent = MagicMock()
    agent.run_turn = _turn
    registry = MagicMock()
    registry.resolve_runtime = AsyncMock(
        return_value=_stub_instance(agent)
    )
    now = datetime.now(timezone.utc)
    signals = MagicMock()
    signals.recent = AsyncMock(return_value=[
        RealtimeSignal(
            ts=now - timedelta(milliseconds=10),
            modality=SignalModality.PROSODY,
            label="calm",
            confidence=0.9,
        )
    ])
    svc = EidolonAgentServicer(
        agent_registry=registry,
        signals_bus=signals, proactive_bus=MagicMock(),
    )

    async def _req_iter():
        yield pb.ChatRequest(
            start=pb.StartTurn(
                turn_id="t1", conversation_id="c", text="hi", input_modality="voice"
            )
        )

    ctx = _make_context()
    ctx.cancelled = lambda: False
    ctx.done = lambda: False
    token = _current_identity.set(_StubIdentity())
    try:
        await svc.Chat(_req_iter(), ctx)
    finally:
        _current_identity.reset(token)

    assert captured["realtime"].dominant_emotion == "calm"
    signals.recent.assert_awaited_once()


async def test_push_signal_unknown_modality_falls_back_to_ambient() -> None:
    signals = MagicMock()
    signals.publish = AsyncMock()
    svc = EidolonAgentServicer(
        agent_registry=MagicMock(),
        signals_bus=signals, proactive_bus=MagicMock(),
    )
    from eidolon_agent.core.types.signal import SignalModality

    req = pb.SignalRequest(
        session_id="s",
        signal=pb.PushSignalInline(modality="invalid_modality", label="x", confidence=0.5),
    )
    await svc.PushSignal(req, _make_context())
    _, sig = signals.publish.await_args.args
    assert sig.modality is SignalModality.AMBIENT
