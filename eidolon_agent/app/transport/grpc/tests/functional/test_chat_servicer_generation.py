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
            owner_id="owner-1",
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


@pytest.mark.parametrize("completed", [False, True])
async def test_exact_start_retry_generates_once_without_replaying_output(monkeypatch, completed):
    monkeypatch.setattr(chat_servicer, "current_identity", _identity)
    released = asyncio.Event()
    started = asyncio.Event()
    finished = asyncio.Event()
    calls = []

    class Agent:
        async def run_turn(self, ti):
            calls.append(ti.turn_id)
            started.set()
            await released.wait()
            yield TurnEvent.delta(ti.turn_id, 0, "one answer", 0.0)
            yield TurnEvent.done(ti.turn_id, 1, TurnStatus.OK, 0.0)
            finished.set()

    frame = pb.ChatRequest(start=pb.StartTurn(
        turn_id="one-turn", conversation_id="conv", text="one question", input_modality="voice"
    ))

    async def requests():
        yield frame
        await started.wait()
        if completed:
            released.set()
            await finished.wait()
        yield frame
        released.set()

    registry = _Registry([Agent()])
    context = _Context()
    service = EidolonAgentServicer(agent_registry=registry, signals_bus=_Signals(),
        proactive_bus=None, runtime_sessions=_runtime_sessions())
    await asyncio.wait_for(service.Chat(requests(), context), 2)
    assert calls == ["one-turn"]
    assert registry._idx == 1
    assert [e.data["text"] for e in context.written if e.kind == pb.TurnEvent.DELTA] == ["one answer"]


@pytest.mark.parametrize("changed", [{"text": "different"}, {"conversation_id": "other"}, {"speculative": True}])
async def test_turn_id_cannot_be_reused_for_changed_input(monkeypatch, changed):
    monkeypatch.setattr(chat_servicer, "current_identity", _identity)
    registry = _Registry([_ImmediateAgent("answer")])
    import grpc

    class Refused(Exception):
        pass

    class Context(_Context):
        async def abort(self, code, detail):
            assert code == grpc.StatusCode.ALREADY_EXISTS
            assert "different content" in detail
            raise Refused()

    context = Context()
    service = EidolonAgentServicer(agent_registry=registry, signals_bus=_Signals(),
        proactive_bus=None, runtime_sessions=_runtime_sessions())
    original = dict(turn_id="same", conversation_id="conv", text="first", input_modality="voice")
    with pytest.raises(Refused):
        await service.Chat(_YieldingRequests([
            pb.ChatRequest(start=pb.StartTurn(**original)),
            pb.ChatRequest(start=pb.StartTurn(**(original | changed))),
        ]), context)
    assert registry._idx == 1


async def test_retry_does_not_resurrect_cancelled_turn(monkeypatch):
    monkeypatch.setattr(chat_servicer, "current_identity", _identity)
    registry = _Registry([_CapturingLateAgent()])
    context = _Context()
    service = EidolonAgentServicer(agent_registry=registry, signals_bus=_Signals(),
        proactive_bus=None, runtime_sessions=_runtime_sessions())
    start = pb.ChatRequest(start=pb.StartTurn(turn_id="cancelled", conversation_id="conv",
                                              text="question", input_modality="voice"))
    await service.Chat(_YieldingRequests([
        start, pb.ChatRequest(cancel=pb.CancelTurn(turn_id="cancelled")), start,
    ]), context)
    assert registry._idx == 1
    assert all(event.kind == pb.TurnEvent.ACK for event in context.written)

async def test_motion_tool_receipt_on_authenticated_turn_stream(monkeypatch):
    from eidolon_sdk.biz.presentation.motion import MotionRequest
    monkeypatch.setattr(chat_servicer, 'current_identity', _identity)
    outgoing = asyncio.Queue()
    received = []
    class Agent:
        async def run_turn(self, ti):
            request = MotionRequest(turn_id=ti.turn_id,command_id='motion:1',action={'action':'shake','times':2})
            receipt = await ti.motion_executor.execute(request)
            received.append(receipt)
            assert await ti.motion_executor.execute(request) == receipt
            yield TurnEvent.done(ti.turn_id,1,TurnStatus.OK,0)
    class Context(_Context):
        async def write(self,event):
            await super().write(event)
            await outgoing.put(event)
    async def requests():
        yield pb.ChatRequest(start=pb.StartTurn(turn_id='t',conversation_id='c',text='摇头两次',input_modality='voice'))
        event = await asyncio.wait_for(outgoing.get(),1)
        assert event.kind == pb.TurnEvent.MOTION and event.motion.times == 2
        assert event.motion.action == 'shake' and event.turn_id == 't'
        feedback = pb.MotionFeedback(turn_id='t')
        feedback.receipt.update({'command_id':'motion:1','status':'completed','completion_basis':'software_sequence'})
        yield pb.ChatRequest(motion_feedback=feedback)
        done = await asyncio.wait_for(outgoing.get(),1)
        assert done.kind == pb.TurnEvent.DONE
    context = Context()
    servicer = EidolonAgentServicer(agent_registry=_Registry([Agent()]),signals_bus=_Signals(),
        proactive_bus=None,runtime_sessions=_runtime_sessions())
    await servicer.Chat(requests(),context)
    assert received[0].status == 'completed'
    assert sum(e.kind == pb.TurnEvent.MOTION for e in context.written) == 1


@pytest.mark.parametrize("explicit_cancel", [False, True])
@pytest.mark.parametrize("wait_for_model", [False, True])
async def test_real_engine_keeps_input_across_transport_cancellation(
    monkeypatch, turn_engine_factory, tmp_path, explicit_cancel, wait_for_model,
):
    from dataclasses import asdict

    from eidolon_sdk.biz.dialogue_control import CommittedTurnDecision, TurnCommitBoundary

    from eidolon_agent.core.types.llm import LLMDelta, LLMFinishReason
    from eidolon_agent.domain.agent.companion import CompanionAgent
    from eidolon_agent.infra.persistence.runtime_store import AgentRuntimeStore
    from tests.helpers import make_turn_input

    turn_context = make_turn_input().context
    identity = SimpleNamespace(owner_id=turn_context.owner_id,
        companion_id=turn_context.companion_id, device_id=turn_context.device_id, session_id="s1")
    monkeypatch.setattr(chat_servicer, "current_identity", lambda: identity)
    authority = SimpleNamespace(**asdict(turn_context), runtime_config={})
    runtime_sessions = RuntimeSessionAuthorizer(SimpleNamespace(resolve=_async_value(authority)))
    ready = asyncio.Event()
    release_write = asyncio.Event()
    prompts = []

    class LLM:
        model_id = "transport-history-probe"

        async def stream(self, messages, **_kwargs):
            prompts.append(messages)
            if messages[-1].content == "今天想讨论一下那个方案。":
                yield LLMDelta(text_delta="还没有播放的回答。")
                ready.set()
                await asyncio.Event().wait()
            yield LLMDelta(text_delta="收到补充。")
            yield LLMDelta(finish=LLMFinishReason.STOP)

    def start(turn_id, text):
        frame = pb.StartTurn(turn_id=turn_id, conversation_id="conv", text=text,
                             input_modality="voice")
        frame.metadata.update({"turn_decision": CommittedTurnDecision.create(
            text=text, boundary=TurnCommitBoundary.FRAMEWORK_COMPLETED, eot_score=0.8,
        ).as_metadata()})
        return pb.ChatRequest(start=frame)

    async def requests():
        yield start("old", "今天想讨论一下那个方案。")
        if wait_for_model:
            await asyncio.wait_for(ready.wait(), 2)
        if explicit_cancel:
            yield pb.ChatRequest(cancel=pb.CancelTurn(turn_id="old", played_chars=0))
        yield start("new", "就是上次说的实时语音方案。")

    store = AgentRuntimeStore.open(tmp_path / "transport-history.sqlite3")
    await store.init_schema()
    engine = turn_engine_factory(llm=LLM(), runtime_store=store)
    persist = engine._turn_persister

    async def delayed_persist(**kwargs):
        await release_write.wait()
        await persist(**kwargs)

    engine._turn_persister = delayed_persist
    agent = CompanionAgent(companion_id=turn_context.companion_id, turn_engine=engine)
    instance = SimpleNamespace(owner_id=turn_context.owner_id, companion_id=turn_context.companion_id,
        genome_id=turn_context.genome_id, agent=agent)
    registry = SimpleNamespace(resolve_runtime=_async_value(instance))
    service = EidolonAgentServicer(agent_registry=registry, signals_bus=_Signals(),
        proactive_bus=None, runtime_sessions=runtime_sessions)
    try:
        await asyncio.wait_for(service.Chat(requests(), _Context()), 3)
        assert len(prompts) == (2 if wait_for_model else 1)
        system = prompts[-1][0].content
        assert "今天想讨论一下那个方案。" in system
        assert "status=interrupted" in system
        assert prompts[-1][-1].content == "就是上次说的实时语音方案。"
        if explicit_cancel:
            assert "还没有播放的回答。" not in system
    finally:
        release_write.set()
        await engine._background.drain(timeout_s=2)
        await store.close()


@pytest.mark.parametrize("modality,speculative", [("voice", False), ("text", False), ("voice", True)])
async def test_music_capabilities_require_committed_voice_turn(monkeypatch, modality, speculative):
    monkeypatch.setattr(chat_servicer, "current_identity", _identity)
    captured = []
    class Agent:
        async def run_turn(self, ti):
            captured.append(ti.channel_tool_capabilities)
            yield TurnEvent.done(ti.turn_id, 0, TurnStatus.OK, 0.0)
    servicer = EidolonAgentServicer(agent_registry=_Registry([Agent()]), signals_bus=_Signals(),
                                    proactive_bus=None, runtime_sessions=_runtime_sessions())
    await servicer.Chat(_Requests([pb.ChatRequest(start=pb.StartTurn(
        turn_id="t", conversation_id="conv", text="play", input_modality=modality,
        speculative=speculative, channel_tool_capabilities=["music.play", "arbitrary.command"],
    ))]), _Context())
    assert captured == [frozenset({"music.play"}) if modality == "voice" and not speculative else frozenset()]


async def test_music_cancel_roundtrips_through_real_servicer(monkeypatch):
    from eidolon_agent.core.ports.channel_tool import ChannelToolRequest
    monkeypatch.setattr(chat_servicer, "current_identity", _identity)
    outgoing = asyncio.Queue()
    class Agent:
        async def run_turn(self, ti):
            await ti.channel_tool_executor.execute(ChannelToolRequest(ti.turn_id, "music:1", "music.play", {"song_id": "s"}))
            yield TurnEvent.done(ti.turn_id, 0, TurnStatus.OK, 0.0)
    class Context(_Context):
        async def write(self, event):
            await super().write(event)
            await outgoing.put(pb.TurnEvent.FromString(event.SerializeToString()))
    async def requests():
        yield pb.ChatRequest(start=pb.StartTurn(turn_id="t", conversation_id="conv", text="play",
                                               input_modality="voice", channel_tool_capabilities=["music.play"]))
        event = await asyncio.wait_for(outgoing.get(), 1)
        assert event.kind == pb.TurnEvent.CHANNEL_TOOL_CALL
        yield pb.ChatRequest(cancel=pb.CancelTurn(turn_id="t"))
    context = Context()
    servicer = EidolonAgentServicer(agent_registry=_Registry([Agent()]), signals_bus=_Signals(),
                                   proactive_bus=None, runtime_sessions=_runtime_sessions())
    await servicer.Chat(requests(), context)
    cancellations = [e for e in context.written if e.kind == pb.TurnEvent.CHANNEL_TOOL_CANCEL]
    assert len(cancellations) == 1
    assert cancellations[0].channel_tool_cancel.call_id == "music:1"
    assert cancellations[0].channel_tool_cancel.turn_id == "t"


class _Aborted(RuntimeError):
    pass


class _AbortingContext(_Context):
    async def abort(self, code, message):
        raise _Aborted(f"{code}: {message}")


def _personal_runtime():
    from dataclasses import dataclass, field
    from unittest.mock import AsyncMock

    @dataclass(frozen=True)
    class Facts:
        owner_id: str = "owner-1"
        companion_id: str = "companion-1"
        memory_realm_id: str = "default"
        genome_id: str = "genome-1"
        schema_version: str = "v1"
        genome_hash: str = "hash"
        realizer_version: str = "v1"
        runtime_config: dict = field(default_factory=dict)

    authority = SimpleNamespace(resolve=AsyncMock(return_value=Facts()),
        resolve_person_memory=AsyncMock(return_value="private-a"))
    identity = _identity()
    identity.person_id, identity.identity_epoch = "person-a", 1
    return identity, authority


async def test_personal_chat_rechecks_credentials_and_directory_per_turn(monkeypatch):
    from unittest.mock import AsyncMock
    identity, authority = _personal_runtime()
    monkeypatch.setattr(chat_servicer, "current_identity", lambda: identity)
    recheck = AsyncMock(return_value=identity)
    monkeypatch.setattr(chat_servicer, "reauthenticate_current", recheck)
    captured = []
    class Agent:
        async def run_turn(self, ti):
            captured.append(ti)
            yield TurnEvent.done(ti.turn_id, 0, TurnStatus.OK, 0)
    service = EidolonAgentServicer(agent_registry=_Registry([Agent()]), signals_bus=_Signals(),
        proactive_bus=None, runtime_sessions=RuntimeSessionAuthorizer(authority))
    await service.Chat(_Requests([pb.ChatRequest(start=pb.StartTurn(turn_id=str(i),
        conversation_id="private-conv", text="hello", input_modality="text"))
        for i in range(2)]), _Context())
    assert recheck.await_count == 2
    assert authority.resolve_person_memory.await_count == 3
    assert len(captured) == 2
    assert all(t.context.person_id == "person-a" and t.context.identity_epoch == 1
               and t.context.memory_realm_id == "private-a" for t in captured)


@pytest.mark.parametrize("failure", ["revoked", "inactive", "changed_realm"])
async def test_personal_stream_failure_cancels_old_output(monkeypatch, failure):
    from unittest.mock import AsyncMock
    from eidolon_agent.core.errors import PermissionDeniedError
    identity, authority = _personal_runtime()
    monkeypatch.setattr(chat_servicer, "current_identity", lambda: identity)
    recheck = AsyncMock(side_effect=[identity, RuntimeError("revoked")]
                        if failure == "revoked" else [identity, identity])
    monkeypatch.setattr(chat_servicer, "reauthenticate_current", recheck)
    if failure != "revoked":
        authority.resolve_person_memory.side_effect = ["private-a", "private-a",
            PermissionDeniedError("person inactive") if failure == "inactive" else "private-b"]
    context = _AbortingContext()
    registry = _Registry([_LateAfterCancelAgent("must-not-escape")])
    service = EidolonAgentServicer(agent_registry=registry, signals_bus=_Signals(),
        proactive_bus=None, runtime_sessions=RuntimeSessionAuthorizer(authority))
    with pytest.raises(_Aborted):
        await service.Chat(_YieldingRequests([pb.ChatRequest(start=pb.StartTurn(turn_id=str(i),
            conversation_id="private-conv", text="hello", input_modality="text"))
            for i in range(2)]), context)
    assert registry._idx == 1
    assert context.written == []


async def test_personal_proactive_subscription_is_not_companion_wide(monkeypatch):
    identity, authority = _personal_runtime()
    monkeypatch.setattr(chat_servicer, "current_identity", lambda: identity)
    service = EidolonAgentServicer(agent_registry=_Registry([]), signals_bus=_Signals(),
        proactive_bus=None, runtime_sessions=RuntimeSessionAuthorizer(authority))
    with pytest.raises(_Aborted, match="scoped Chat"):
        async for _ in service.SubscribeProactive(pb.SubscribeRequest(), _AbortingContext()):
            pytest.fail("unscoped proactive output")


async def test_personal_chat_without_turn_reauthentication_is_rejected(monkeypatch):
    identity, authority = _personal_runtime()
    monkeypatch.setattr(chat_servicer, "current_identity", lambda: identity)
    async def unavailable():
        raise RuntimeError("missing original credential")
    monkeypatch.setattr(chat_servicer, "reauthenticate_current", unavailable)
    registry = _Registry([])
    service = EidolonAgentServicer(agent_registry=registry, signals_bus=_Signals(),
        proactive_bus=None, runtime_sessions=RuntimeSessionAuthorizer(authority))
    with pytest.raises(_Aborted, match="credential"):
        await service.Chat(_Requests([pb.ChatRequest(start=pb.StartTurn(
            turn_id="t", conversation_id="c", text="private", input_modality="text"))]),
            _AbortingContext())
    assert registry._idx == 0


async def test_personal_chat_real_engine_history_and_persistence_are_isolated(
    monkeypatch, turn_engine_factory, tmp_path,
):
    from eidolon_sdk.biz.runtime import RuntimeTokenVerifier, sign_runtime_token
    from eidolon_agent.core.types.companion_runtime import CompanionRuntimeFacts
    from eidolon_agent.core.types.llm import LLMDelta, LLMFinishReason
    from eidolon_agent.domain.agent.companion import CompanionAgent
    from eidolon_agent.infra.persistence.agent_runtime import build_agent_conversation_guard
    from eidolon_agent.infra.persistence.runtime_store import AgentRuntimeStore, ConversationRow
    from tests.helpers import make_turn_input

    base = make_turn_input().context
    facts = CompanionRuntimeFacts(owner_id=base.owner_id, companion_id=base.companion_id,
        memory_realm_id=base.memory_realm_id, genome_id=base.genome_id, genome_version=1,
        schema_version=base.schema_version, genome_hash=base.genome_hash,
        realizer_version=base.realizer_version, genome=None, runtime_config={})
    async def personal_realm(*, owner_id, person_id):
        assert owner_id == base.owner_id
        return {"a":"realm-a", "b":"realm-b"}[person_id]
    authority = SimpleNamespace(resolve=_async_value(facts), resolve_person_memory=personal_realm)
    prompts = []
    class Model:
        model_id = "private-history-probe"
        async def stream(self, messages, **kwargs):
            prompts.append("\n".join(str(m.content) for m in messages))
            yield LLMDelta(text_delta="收到。")
            yield LLMDelta(finish=LLMFinishReason.STOP)
    store = AgentRuntimeStore.open(tmp_path / "private-chat.sqlite3")
    await store.init_schema()
    engine = turn_engine_factory(llm=Model(), runtime_store=store)
    engine._conversation_guard = build_agent_conversation_guard(store)
    agent = CompanionAgent(companion_id=base.companion_id, turn_engine=engine)
    registry = SimpleNamespace(resolve_runtime=_async_value(SimpleNamespace(
        owner_id=base.owner_id, companion_id=base.companion_id, genome_id=base.genome_id, agent=agent)))
    service = EidolonAgentServicer(agent_registry=registry, signals_bus=_Signals(),
        proactive_bus=None, runtime_sessions=RuntimeSessionAuthorizer(authority))
    secret = "test-secret-with-enough-entropy-32b"
    verifier = RuntimeTokenVerifier(secret=secret)
    try:
        async def chat(person, conversation, text, turn_id):
            token, _ = sign_runtime_token(secret=secret, owner_id=base.owner_id,
                companion_id=base.companion_id, device_id=base.device_id,
                session_id=f"session-{person}", person_id=person, identity_epoch=0, ttl_seconds=60)
            identity = await verifier.verify(token)
            monkeypatch.setattr(chat_servicer, "current_identity", lambda: identity)
            async def recheck():
                return await verifier.verify(token)
            monkeypatch.setattr(chat_servicer, "reauthenticate_current", recheck)
            context = _Context()
            await service.Chat(_Requests([pb.ChatRequest(start=pb.StartTurn(turn_id=turn_id,
                conversation_id=conversation, text=text, input_modality="text"))]), context)
            await engine._background.drain(timeout_s=2)
            return context
        await chat("a", "conv-a", "我的私人代号是紫色海鸥。", "a-1")
        await chat("a", "conv-a", "继续刚才的话题。", "a-2")
        await chat("b", "conv-b", "我们今天聊什么？", "b-1")
        assert len(prompts) == 3
        assert "紫色海鸥" in prompts[1]
        assert "紫色海鸥" not in prompts[2]
        refused = await chat("b", "conv-a", "读取上一个人的内容。", "b-wrong")
        assert len(prompts) == 3
        assert any(event.kind == pb.TurnEvent.ERROR for event in refused.written)
        async with store.read_session_factory() as session:
            for person in ("a", "b"):
                row = await session.get(ConversationRow, f"conv-{person}")
                assert row.metadata_json["person_id"] == person
                assert row.metadata_json["memory_realm_id"] == f"realm-{person}"
    finally:
        await engine._background.drain(timeout_s=2)
        await store.close()
