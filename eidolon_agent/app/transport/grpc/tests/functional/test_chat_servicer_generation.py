"""gRPC Chat turn generation isolation."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from eidolon_agent.app.transport.grpc import chat_servicer
from eidolon_agent.app.transport.grpc.chat_servicer import EidolonAgentServicer
from eidolon_agent.app.transport.grpc.proto import pb
from eidolon_agent.core.types.identity import Identity
from eidolon_agent.core.types.turn import TurnEvent, TurnStatus

pytestmark = pytest.mark.functional


async def test_new_start_supersedes_old_turn_and_drops_late_events(monkeypatch) -> None:
    identity = Identity(tenant_id="t", user_id="u", agent_instance_id="inst")
    monkeypatch.setattr(chat_servicer, "current_identity", lambda: identity)
    first_agent = _LateAfterCancelAgent("old-late")
    second_agent = _ImmediateAgent("new-answer")
    registry = _Registry([first_agent, second_agent])
    context = _Context()
    servicer = EidolonAgentServicer(
        agent_registry=registry,
        pairing=None,
        signals_bus=_Signals(),
        proactive_bus=None,
    )

    await servicer.Chat(
        _Requests(
            [
                pb.ChatRequest(
                    start=pb.StartTurn(
                        turn_id="old",
                        conversation_id="conv",
                        text="old request",
                    )
                ),
                pb.ChatRequest(
                    start=pb.StartTurn(
                        turn_id="new",
                        conversation_id="conv",
                        text="new request",
                    )
                ),
            ]
        ),
        context,
    )

    written_text = [
        event.data.fields["text"].string_value
        for event in context.written
        if event.kind == pb.TurnEvent.DELTA
    ]
    assert written_text == ["new-answer"]
    assert all(event.turn_id != "old" for event in context.written)


async def test_explicit_cancel_drops_late_events(monkeypatch) -> None:
    identity = Identity(tenant_id="t", user_id="u", agent_instance_id="inst")
    monkeypatch.setattr(chat_servicer, "current_identity", lambda: identity)
    cancelled_agent = _LateAfterCancelAgent("cancelled-late")
    registry = _Registry([cancelled_agent])
    context = _Context()
    servicer = EidolonAgentServicer(
        agent_registry=registry,
        pairing=None,
        signals_bus=_Signals(),
        proactive_bus=None,
    )

    await servicer.Chat(
        _Requests(
            [
                pb.ChatRequest(
                    start=pb.StartTurn(
                        turn_id="to-cancel",
                        conversation_id="conv",
                        text="old request",
                    )
                ),
                pb.ChatRequest(cancel=pb.CancelTurn(turn_id="to-cancel")),
            ]
        ),
        context,
    )

    assert context.written == []


async def test_parallel_conversations_do_not_supersede_each_other(monkeypatch) -> None:
    identity = Identity(tenant_id="t", user_id="u", agent_instance_id="inst")
    monkeypatch.setattr(chat_servicer, "current_identity", lambda: identity)
    first_agent = _DelayedAgent("conv-a-answer", delay_s=0.01)
    second_agent = _ImmediateAgent("conv-b-answer")
    registry = _Registry([first_agent, second_agent])
    context = _Context()
    servicer = EidolonAgentServicer(
        agent_registry=registry,
        pairing=None,
        signals_bus=_Signals(),
        proactive_bus=None,
    )

    await servicer.Chat(
        _Requests(
            [
                pb.ChatRequest(
                    start=pb.StartTurn(
                        turn_id="a",
                        conversation_id="conv-a",
                        text="first request",
                    )
                ),
                pb.ChatRequest(
                    start=pb.StartTurn(
                        turn_id="b",
                        conversation_id="conv-b",
                        text="second request",
                    )
                ),
            ]
        ),
        context,
    )

    written_text = [
        event.data.fields["text"].string_value
        for event in context.written
        if event.kind == pb.TurnEvent.DELTA
    ]
    assert set(written_text) == {"conv-a-answer", "conv-b-answer"}
    assert {event.turn_id for event in context.written} == {"a", "b"}


class _Registry:
    def __init__(self, agents) -> None:
        self._agents = list(agents)
        self._idx = 0

    async def resolve_for_caller(self, **_):
        agent = self._agents[min(self._idx, len(self._agents) - 1)]
        self._idx += 1
        return SimpleNamespace(instance_id="inst", agent=agent)


class _LateAfterCancelAgent:
    def __init__(self, text: str) -> None:
        self._text = text

    async def run_turn(self, ti):
        try:
            await asyncio.sleep(1)
        except asyncio.CancelledError:
            yield TurnEvent.delta(ti.turn_id, 0, self._text, 0.0)
            yield TurnEvent.done(ti.turn_id, 1, TurnStatus.OK, 0.0)


class _ImmediateAgent:
    def __init__(self, text: str) -> None:
        self._text = text

    async def run_turn(self, ti):
        yield TurnEvent.delta(ti.turn_id, 0, self._text, 0.0)
        yield TurnEvent.done(ti.turn_id, 1, TurnStatus.OK, 0.0)


class _DelayedAgent:
    def __init__(self, text: str, *, delay_s: float) -> None:
        self._text = text
        self._delay_s = delay_s

    async def run_turn(self, ti):
        await asyncio.sleep(self._delay_s)
        yield TurnEvent.delta(ti.turn_id, 0, self._text, 0.0)
        yield TurnEvent.done(ti.turn_id, 1, TurnStatus.OK, 0.0)


class _Signals:
    async def recent(self, *_args, **_kwargs):
        return []


class _Requests:
    def __init__(self, frames) -> None:
        self._frames = list(frames)

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._frames:
            raise StopAsyncIteration
        return self._frames.pop(0)


class _Context:
    def __init__(self) -> None:
        self.written = []

    def cancelled(self) -> bool:
        return False

    def invocation_metadata(self):
        return ()

    async def write(self, event):
        self.written.append(event)

    async def abort(self, *_args):
        raise AssertionError("abort should not be called")
