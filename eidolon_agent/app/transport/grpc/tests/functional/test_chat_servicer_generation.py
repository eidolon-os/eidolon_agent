"""gRPC Chat turn generation isolation."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from eidolon_agent.app.transport.grpc import chat_servicer
from eidolon_agent.app.transport.grpc.chat_servicer import EidolonAgentServicer
from eidolon_agent.app.transport.grpc.proto import pb
from eidolon_agent.core.types.turn import TurnEvent, TurnStatus
from eidolon_agent.domain.runtime_session import RuntimeSessionAuthorizer

pytestmark = pytest.mark.functional


def _identity() -> SimpleNamespace:
    return SimpleNamespace(
        owner_id="owner-1",
        companion_id="companion-1",
        device_id="dev-1",
        session_id="session-1",
    )


def _runtime_authority() -> SimpleNamespace:
    return SimpleNamespace(
        resolve=_async_value(
            SimpleNamespace(
                owner_id="owner-1",
                companion_id="companion-1",
                memory_realm_id="realm-1",
                genome_id="genome-1",
                schema_version="schema-v1",
                genome_hash="hash-v1",
                realizer_version="realizer-v1",
                runtime_config={},
            )
        )
    )


def _runtime_sessions() -> RuntimeSessionAuthorizer:
    return RuntimeSessionAuthorizer(_runtime_authority())


def _async_value(value):
    async def _resolve(**_kwargs):
        return value

    return _resolve


async def test_new_start_supersedes_old_turn_and_drops_late_events(monkeypatch) -> None:
    identity = _identity()
    monkeypatch.setattr(chat_servicer, "current_identity", lambda: identity)
    first_agent = _LateAfterCancelAgent("old-late")
    second_agent = _ImmediateAgent("new-answer")
    registry = _Registry([first_agent, second_agent])
    context = _Context()
    servicer = EidolonAgentServicer(
        agent_registry=registry,
        signals_bus=_Signals(),
        proactive_bus=None,
        runtime_sessions=_runtime_sessions(),
    )

    await servicer.Chat(
        _Requests(
            [
                pb.ChatRequest(
                    start=pb.StartTurn(
                        turn_id="old",
                        conversation_id="conv",
                        text="old request",
                        input_modality="voice",
                    )
                ),
                pb.ChatRequest(
                    start=pb.StartTurn(
                        turn_id="new",
                        conversation_id="conv",
                        text="new request",
                        input_modality="voice",
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
    identity = _identity()
    monkeypatch.setattr(chat_servicer, "current_identity", lambda: identity)
    cancelled_agent = _LateAfterCancelAgent("cancelled-late")
    registry = _Registry([cancelled_agent])
    context = _Context()
    servicer = EidolonAgentServicer(
        agent_registry=registry,
        signals_bus=_Signals(),
        proactive_bus=None,
        runtime_sessions=_runtime_sessions(),
    )

    await servicer.Chat(
        _Requests(
            [
                pb.ChatRequest(
                    start=pb.StartTurn(
                        turn_id="to-cancel",
                        conversation_id="conv",
                        text="old request",
                        input_modality="voice",
                    )
                ),
                pb.ChatRequest(cancel=pb.CancelTurn(turn_id="to-cancel")),
            ]
        ),
        context,
    )

    # The cancel is acknowledged explicitly; no turn events leak after it.
    assert len(context.written) == 1
    ack = context.written[0]
    assert ack.kind == pb.TurnEvent.ACK
    assert ack.data["cancelled_turn_id"] == "to-cancel"
    assert ack.data["already_done"] is False


async def test_cancel_of_finished_turn_acks_already_done(monkeypatch) -> None:
    identity = _identity()
    monkeypatch.setattr(chat_servicer, "current_identity", lambda: identity)
    context = _Context()
    servicer = EidolonAgentServicer(
        agent_registry=_Registry([_ImmediateAgent("hi")]),
        signals_bus=_Signals(),
        proactive_bus=None,
        runtime_sessions=_runtime_sessions(),
    )

    await servicer.Chat(
        _Requests([pb.ChatRequest(cancel=pb.CancelTurn(turn_id="never-started"))]),
        context,
    )

    assert len(context.written) == 1
    ack = context.written[0]
    assert ack.kind == pb.TurnEvent.ACK
    assert ack.data["cancelled_turn_id"] == "never-started"
    assert ack.data["already_done"] is True


async def test_cancel_played_chars_stashed_on_turn_input(monkeypatch) -> None:
    identity = _identity()
    monkeypatch.setattr(chat_servicer, "current_identity", lambda: identity)
    agent = _CapturingLateAgent()
    context = _Context()
    servicer = EidolonAgentServicer(
        agent_registry=_Registry([agent]),
        signals_bus=_Signals(),
        proactive_bus=None,
        runtime_sessions=_runtime_sessions(),
    )

    await servicer.Chat(
        _YieldingRequests(
            [
                pb.ChatRequest(
                    start=pb.StartTurn(
                        turn_id="to-truncate",
                        conversation_id="conv",
                        text="讲个故事",
                        input_modality="voice",
                    )
                ),
                pb.ChatRequest(
                    cancel=pb.CancelTurn(
                        turn_id="to-truncate",
                        played_chars=7,
                        played_ms=1234.5,
                    )
                ),
            ]
        ),
        context,
    )

    assert agent.ti is not None
    assert agent.ti.metadata["termination_cause"] == "client_cancel"
    assert agent.ti.metadata["cancel_played_chars"] == 7
    assert agent.ti.metadata["cancel_played_ms"] == 1234.5


async def test_start_turn_trace_id_reaches_turn_input(monkeypatch) -> None:
    identity = _identity()
    monkeypatch.setattr(chat_servicer, "current_identity", lambda: identity)
    agent = _CapturingLateAgent()
    context = _Context()
    servicer = EidolonAgentServicer(
        agent_registry=_Registry([agent]),
        signals_bus=_Signals(),
        proactive_bus=None,
        runtime_sessions=_runtime_sessions(),
    )

    await servicer.Chat(
        _YieldingRequests(
            [
                pb.ChatRequest(
                    start=pb.StartTurn(
                        turn_id="t1",
                        conversation_id="conv",
                        text="你好",
                        trace_id="channel-trace-123",
                        input_modality="voice",
                    )
                ),
            ]
        ),
        context,
    )

    # The per-turn trace id propagates onto the TurnInput context.
    assert agent.ti is not None
    assert agent.ti.context.trace_id == "channel-trace-123"


async def test_parallel_conversations_do_not_supersede_each_other(monkeypatch) -> None:
    identity = _identity()
    monkeypatch.setattr(chat_servicer, "current_identity", lambda: identity)
    first_agent = _DelayedAgent("conv-a-answer", delay_s=0.01)
    second_agent = _ImmediateAgent("conv-b-answer")
    registry = _Registry([first_agent, second_agent])
    context = _Context()
    servicer = EidolonAgentServicer(
        agent_registry=registry,
        signals_bus=_Signals(),
        proactive_bus=None,
        runtime_sessions=_runtime_sessions(),
    )

    await servicer.Chat(
        _Requests(
            [
                pb.ChatRequest(
                    start=pb.StartTurn(
                        turn_id="a",
                        conversation_id="conv-a",
                        text="first request",
                        input_modality="voice",
                    )
                ),
                pb.ChatRequest(
                    start=pb.StartTurn(
                        turn_id="b",
                        conversation_id="conv-b",
                        text="second request",
                        input_modality="voice",
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

    async def resolve_runtime(self, **_):
        agent = self._agents[min(self._idx, len(self._agents) - 1)]
        self._idx += 1
        return SimpleNamespace(
            instance_id="inst",
            companion_id="companion-1",
            genome_id="genome-1",
            agent=agent,
        )


class _LateAfterCancelAgent:
    def __init__(self, text: str) -> None:
        self._text = text

    async def run_turn(self, ti):
        try:
            await asyncio.sleep(1)
        except asyncio.CancelledError:
            yield TurnEvent.delta(ti.turn_id, 0, self._text, 0.0)
            yield TurnEvent.done(ti.turn_id, 1, TurnStatus.OK, 0.0)


class _CapturingLateAgent:
    """Never finishes on its own; captures the TurnInput for assertions."""

    def __init__(self) -> None:
        self.ti = None

    async def run_turn(self, ti):
        self.ti = ti
        await asyncio.sleep(1)
        yield TurnEvent.done(ti.turn_id, 0, TurnStatus.OK, 0.0)


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


class _YieldingRequests(_Requests):
    """Yields to the event loop between frames so spawned turns get to run."""

    async def __anext__(self):
        await asyncio.sleep(0)
        return await super().__anext__()


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
