"""EidolonAgentServicer — unary RPCs (ExchangePairingCode / ChatOnce / PushSignal).

The bidi Chat RPC is covered end-to-end by tests/integration/. Here we
mock the registry, pairing coordinator, signal bus, and the auth
contextvar so we can drive the servicer directly without spinning up
a real gRPC server.
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
from eidolon_agent.core.errors import NotFoundError
from eidolon_agent.core.types.turn import TurnEventKind

pytestmark = pytest.mark.unit


# ---- helpers --------------------------------------------------------------


@dataclass
class _StubIdentity:
    tenant_id: str = "t"
    user_id: str = "alice"
    device_id: str | None = "dev-1"


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


# ---- ExchangePairingCode --------------------------------------------------


async def test_exchange_pairing_code_happy_path() -> None:
    pairing = MagicMock()
    pairing.exchange = AsyncMock(
        return_value=SimpleNamespace(
            device_id="dev-x",
            token="JWT",
            tenant_id="t",
            user_id="alice",
            default_template_id="tpl",
            expires_at=datetime(2030, 1, 1, tzinfo=timezone.utc),
        )
    )
    svc = EidolonAgentServicer(
        agent_registry=MagicMock(),
        pairing=pairing,
        signals_bus=MagicMock(),
        proactive_bus=MagicMock(),
    )
    resp = await svc.ExchangePairingCode(
        pb.ExchangeRequest(pairing_code="ABCDEFGH", device_id="dev-x"),
        _make_context(),
    )
    assert resp.device_id == "dev-x"
    assert resp.device_token == "JWT"
    assert resp.default_template_id == "tpl"


async def test_exchange_pairing_code_unknown_aborts_unauth() -> None:
    pairing = MagicMock()
    pairing.exchange = AsyncMock(side_effect=NotFoundError("no such code"))
    ctx = _make_context()
    svc = EidolonAgentServicer(
        agent_registry=MagicMock(), pairing=pairing,
        signals_bus=MagicMock(), proactive_bus=MagicMock(),
    )
    with pytest.raises(grpc.RpcError):
        await svc.ExchangePairingCode(
            pb.ExchangeRequest(pairing_code="GHOSTCDE"), ctx
        )
    ctx.abort.assert_awaited_once()
    args, _ = ctx.abort.call_args
    assert args[0] is grpc.StatusCode.UNAUTHENTICATED


# ---- ChatOnce -------------------------------------------------------------


async def test_chat_once_returns_assembled_assistant_text() -> None:
    # Scripted events: STATE → DELTA × 2 → DONE
    from eidolon_agent.core.types.turn import TurnEvent

    events = [
        TurnEvent(turn_id="t", seq=0, kind=TurnEventKind.STATE, data={"state": "speaking"}),
        TurnEvent(turn_id="t", seq=1, kind=TurnEventKind.DELTA, data={"text": "hello "}),
        TurnEvent(turn_id="t", seq=2, kind=TurnEventKind.DELTA, data={"text": "world"}),
        TurnEvent(
            turn_id="t",
            seq=3,
            kind=TurnEventKind.DONE,
            data={"first_delta_ms": 120, "triage": "simple"},
        ),
    ]
    registry = MagicMock()
    registry.resolve_for_caller = MagicMock(
        return_value=SimpleNamespace(
            instance_id="inst-1", agent=_scripted_agent(events)
        )
    )
    svc = EidolonAgentServicer(
        agent_registry=registry, pairing=MagicMock(),
        signals_bus=MagicMock(), proactive_bus=MagicMock(),
    )
    token = _current_identity.set(_StubIdentity())
    try:
        resp = await svc.ChatOnce(
            pb.ChatOnceRequest(conversation_id="c1", text="say hi"),
            _make_context(),
        )
    finally:
        _current_identity.reset(token)
    assert resp.assistant_text == "hello world"
    assert resp.triage == "simple"
    assert resp.latency_first_delta_ms == 120


async def test_chat_once_without_identity_aborts() -> None:
    svc = EidolonAgentServicer(
        agent_registry=MagicMock(), pairing=MagicMock(),
        signals_bus=MagicMock(), proactive_bus=MagicMock(),
    )
    ctx = _make_context()
    with pytest.raises(grpc.RpcError):
        await svc.ChatOnce(pb.ChatOnceRequest(conversation_id="c", text="x"), ctx)
    args, _ = ctx.abort.call_args
    assert args[0] is grpc.StatusCode.UNAUTHENTICATED


# ---- PushSignal -----------------------------------------------------------


async def test_push_signal_publishes_to_signal_bus() -> None:
    signals = MagicMock()
    signals.publish = AsyncMock()
    svc = EidolonAgentServicer(
        agent_registry=MagicMock(), pairing=MagicMock(),
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


async def test_push_signal_unknown_modality_falls_back_to_ambient() -> None:
    signals = MagicMock()
    signals.publish = AsyncMock()
    svc = EidolonAgentServicer(
        agent_registry=MagicMock(), pairing=MagicMock(),
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
