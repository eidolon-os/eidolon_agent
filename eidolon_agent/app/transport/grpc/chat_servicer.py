"""gRPC servicer — Chat / PushSignal / SubscribeProactive.

The servicer is intentionally thin: it translates proto frames ↔ core types,
resolves the AgentInstance from the verified Owner runtime binding, and delegates each Turn
to the :class:`CompanionAgent`. All business logic lives behind the ports.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid

import grpc
from eidolon_sdk.biz.chat_stream import TerminationCause

from eidolon_agent.app.transport.grpc.codec import struct_to_dict, turn_event_to_proto
from eidolon_agent.app.transport.grpc.interceptors import current_identity
from eidolon_agent.app.transport.grpc.proto import pb, pbg
from eidolon_agent.core.errors import EidolonError, NotFoundError
from eidolon_agent.core.types.signal import SignalDigest
from eidolon_agent.core.types.turn import (
    TurnEvent,
    TurnEventKind,
    TurnInput,
    TurnTrigger,
)
from eidolon_agent.core.types.turn_context import InputModality, TurnContext
from eidolon_agent.domain.signals import SignalFuser

_log = logging.getLogger(__name__)


class EidolonAgentServicer(pbg.EidolonAgentServicer):
    def __init__(
        self,
        *,
        agent_registry,
        signals_bus,
        proactive_bus,  # EventBus
        personas_service=None,
    ) -> None:
        self._registry = agent_registry
        self._signals = signals_bus
        self._bus = proactive_bus
        self._personas = personas_service

    # ---- Chat bidi stream ---------------------------------------------------

    async def Chat(
        self,
        request_iterator,
        context: grpc.aio.ServicerContext,
    ):
        identity = current_identity()
        if identity is None:
            await context.abort(grpc.StatusCode.UNAUTHENTICATED, "no identity")
        active_turns: set[asyncio.Task] = set()
        active_by_conversation: dict[str, asyncio.Task] = {}
        conversation_by_turn: dict[str, str] = {}
        input_by_turn: dict[str, TurnInput] = {}
        generation_by_conversation: dict[str, int] = {}
        write_lock = asyncio.Lock()
        last_session_id: str | None = None
        signal_fuser = SignalFuser()

        # Watch the gRPC context for cancellation (raw TCP close, RPC cancel
        # without a CancelTurn frame, deadline exceeded, …). When fired, cancel
        # every in-flight turn so we stop pulling tokens from the LLM provider
        # and don't bill against a disconnected client.
        async def _on_rpc_cancelled() -> None:
            while not context.cancelled():
                await asyncio.sleep(0.05)
            for task in list(active_turns):
                if not task.done():
                    task.cancel()

        watcher = asyncio.create_task(_on_rpc_cancelled(), name="chat-cancel-watcher")

        try:
            async for frame in request_iterator:
                payload = frame.WhichOneof("payload")
                if payload == "cancel":
                    # Explicit cancel of a specific turn from the client.
                    cancel_turn_id = frame.cancel.turn_id
                    cancelled_live = False
                    for task in list(active_turns):
                        if not task.done() and task.get_name() == f"turn-{cancel_turn_id}":
                            conv_id = conversation_by_turn.get(cancel_turn_id)
                            if conv_id:
                                generation_by_conversation[conv_id] = (
                                    generation_by_conversation.get(conv_id, 0) + 1
                                )
                            # Stash the barge-in playback boundary on the
                            # TurnInput before cancelling so the post-turn
                            # persistence path can truncate the assistant text
                            # to what the user actually heard.
                            cancelled_ti = input_by_turn.get(cancel_turn_id)
                            if cancelled_ti is not None:
                                cancelled_ti.metadata["termination_cause"] = (
                                    TerminationCause.CLIENT_CANCEL.value
                                )
                                if frame.cancel.HasField("played_chars"):
                                    cancelled_ti.metadata["cancel_played_chars"] = int(
                                        frame.cancel.played_chars
                                    )
                                if frame.cancel.HasField("played_ms"):
                                    cancelled_ti.metadata["cancel_played_ms"] = float(
                                        frame.cancel.played_ms
                                    )
                            task.cancel()
                            cancelled_live = True
                    # Acknowledge the cancel so the client can distinguish
                    # "cancel landed" from "turn had already finished" without
                    # waiting for (or missing) the DONE event.
                    async with write_lock:
                        await context.write(
                            turn_event_to_proto(
                                TurnEvent(
                                    turn_id=cancel_turn_id,
                                    seq=0,
                                    kind=TurnEventKind.ACK,
                                    data={
                                        "cancelled_turn_id": cancel_turn_id,
                                        "already_done": not cancelled_live,
                                    },
                                    ts=time.time(),
                                )
                            )
                        )
                    continue
                if payload == "signal":
                    if last_session_id:
                        await self._signals.publish(
                            last_session_id,
                            _signal_from_proto(frame.signal),
                        )
                    continue
                if payload != "start":
                    continue

                start = frame.start
                last_session_id = start.conversation_id
                conversation_id = start.conversation_id
                previous = active_by_conversation.get(conversation_id)
                if previous is not None and not previous.done():
                    previous.cancel()
                generation = generation_by_conversation.get(conversation_id, 0) + 1
                generation_by_conversation[conversation_id] = generation
                try:
                    inst = await self._registry.resolve_runtime(
                        owner_id=identity.owner_id,
                        companion_id=identity.companion_id,
                        genome_id=identity.genome_id,
                    )
                    agent = inst.agent
                except NotFoundError as exc:
                    await context.abort(grpc.StatusCode.FAILED_PRECONDITION, exc.message)

                start_metadata = struct_to_dict(start.metadata)
                # Preemptive/speculative turn: warm the LLM on a partial
                # transcript; the turn engine keeps it ephemeral (no persist /
                # fanout) until it would be confirmed.
                if start.speculative:
                    start_metadata["speculative"] = True
                realtime = _digest_from_dict(struct_to_dict(start.realtime))
                if realtime is None:
                    recent_signals = await self._signals.recent(
                        start.conversation_id,
                        window_ms=signal_fuser.window_ms,
                    )
                    realtime = signal_fuser.fuse(recent_signals)
                runtime_session_id = str(
                    getattr(identity, "session_id", None) or conversation_id
                ).strip()
                try:
                    input_modality = _input_modality(start.input_modality)
                except ValueError as exc:
                    await context.abort(grpc.StatusCode.INVALID_ARGUMENT, str(exc))
                # Correlation id for cross-hop tracing (channel->agent->memory).
                # Prefer the per-turn trace_id the transport client minted; fall back to
                # call-level x-trace-id, then a fresh uuid.
                trace_id = (
                    str(start.trace_id or "").strip()
                    or dict(context.invocation_metadata()).get("x-trace-id", "")
                    or uuid.uuid4().hex
                )

                ti = TurnInput(
                    turn_id=start.turn_id or uuid.uuid4().hex,
                    conversation_id=conversation_id,
                    session_id=runtime_session_id,
                    context=TurnContext(
                        owner_id=identity.owner_id,
                        companion_id=inst.companion_id,
                        device_id=identity.device_id,
                        memory_realm_id=identity.memory_realm_id,
                        genome_id=inst.genome_id,
                        trace_id=trace_id,
                        request_id=dict(context.invocation_metadata()).get(
                            "x-request-id", uuid.uuid4().hex
                        ),
                        schema_version=identity.schema_version,
                        genome_hash=identity.genome_hash,
                        realizer_version=identity.realizer_version,
                    ),
                    input_modality=input_modality,
                    trigger=TurnTrigger.USER_UTTERANCE,
                    text=start.text,
                    realtime=realtime,
                    metadata=start_metadata,
                )

                async def _emit_turn(_agent=agent, _ti=ti, _generation=generation) -> None:
                    try:
                        async for ev in _agent.run_turn(_ti):
                            if (
                                generation_by_conversation.get(_ti.conversation_id)
                                != _generation
                            ):
                                _ti.metadata["stale_generation_dropped"] = int(
                                    _ti.metadata.get("stale_generation_dropped") or 0
                                ) + 1
                                continue
                            async with write_lock:
                                await context.write(turn_event_to_proto(ev))
                    except asyncio.CancelledError:
                        raise
                    except EidolonError as exc:
                        _log.warning("turn %s failed: %s", _ti.turn_id, exc)

                task = asyncio.create_task(_emit_turn(), name=f"turn-{ti.turn_id}")
                active_turns.add(task)
                active_by_conversation[conversation_id] = task
                conversation_by_turn[ti.turn_id] = conversation_id
                input_by_turn[ti.turn_id] = ti

                def _discard_done(
                    done_task, *, conv_id=conversation_id, gen=generation, turn_id=ti.turn_id
                ):
                    active_turns.discard(done_task)
                    conversation_by_turn.pop(turn_id, None)
                    input_by_turn.pop(turn_id, None)
                    if (
                        generation_by_conversation.get(conv_id) == gen
                        and active_by_conversation.get(conv_id) is done_task
                    ):
                        active_by_conversation.pop(conv_id, None)

                task.add_done_callback(_discard_done)
        finally:
            watcher.cancel()
            # If the iterator returned because the RPC was cancelled, propagate
            # the cancel to any in-flight turn. Legitimate stream-end (client
            # closed cleanly) still wants to drain.
            if context.cancelled():
                for task in list(active_turns):
                    if not task.done():
                        task.cancel()
            for task in list(active_turns):
                try:
                    await task
                except asyncio.CancelledError:
                    pass

    # ---- PushSignal --------------------------------------------------------

    async def PushSignal(
        self, request: pb.SignalRequest, context: grpc.aio.ServicerContext
    ) -> pb.Ack:
        # The signals bus expects a RealtimeSignal; we adapt opportunistically.
        from datetime import datetime, timezone

        from eidolon_agent.core.types.signal import RealtimeSignal, SignalModality

        identity = current_identity()
        try:
            modality = SignalModality(request.signal.modality)
        except ValueError:
            modality = SignalModality.AMBIENT
        sig = RealtimeSignal(
            ts=datetime.now(timezone.utc),
            modality=modality,
            label=request.signal.label,
            confidence=float(request.signal.confidence),
            raw=struct_to_dict(request.signal.raw),
        )
        await self._signals.publish(request.session_id, sig)
        if self._personas is not None and identity is not None:
            try:
                inst = await self._registry.resolve_runtime(
                    owner_id=identity.owner_id,
                    companion_id=identity.companion_id,
                    genome_id=identity.genome_id,
                )
                from eidolon_agent.domain.personas.types import PersonaSignalInput

                await self._personas.submit_signal(
                    PersonaSignalInput(
                        owner_id=identity.owner_id,
                        companion_id=inst.companion_id,
                        dominant_emotion=request.signal.label,
                        emotion_confidence=float(request.signal.confidence),
                        presence=_presence_from_signal(request.signal.modality, request.signal.label),
                        confidence_overall=float(request.signal.confidence),
                    )
                )
            except Exception:
                _log.exception("submit persona signal failed")
        return pb.Ack(accepted=True)

    # ---- SubscribeProactive ------------------------------------------------

    async def SubscribeProactive(
        self, request: pb.SubscribeRequest, context: grpc.aio.ServicerContext
    ):
        identity = current_identity()
        if identity is None:
            await context.abort(grpc.StatusCode.UNAUTHENTICATED, "no identity")
        queue: asyncio.Queue = asyncio.Queue()

        async def _handler(event):  # type: ignore[no-untyped-def]
            await queue.put(event.payload)

        # Subject pattern: agent.proactive.triggered.<instance_id>
        pattern = (
            f"agent.proactive.triggered.{request.instance_id}"
            if request.instance_id
            else "agent.proactive.triggered.>"
        )
        unsub = await self._bus.subscribe(pattern, _handler)
        try:
            while True:
                payload = await queue.get()
                ev = pb.ProactiveEvent(
                    instance_id=payload.get("instance_id", ""),
                    intent=payload.get("intent", ""),
                    text=payload.get("text", ""),
                    style_hint=payload.get("style_hint", ""),
                )
                yield ev
        finally:
            await unsub()


def _presence_from_signal(modality: str, label: str) -> str:
    if modality != "ambient":
        return "present"
    if label in {"away", "distracted", "present"}:
        return label
    return "present"


def _digest_from_dict(data: dict) -> SignalDigest | None:
    if not data:
        return None
    return SignalDigest(
        window_ms=int(data.get("window_ms") or 0),
        dominant_emotion=data.get("dominant_emotion"),
        emotion_confidence=float(data.get("emotion_confidence") or 0.0),
        speech_rate=data.get("speech_rate"),
        presence=data.get("presence") or "present",
        confidence_overall=float(data.get("confidence_overall") or 0.0),
        notable_events=tuple(data.get("notable_events") or ()),
    )


def _input_modality(raw: str) -> InputModality:
    value = str(raw or "").strip().lower()
    if value == "voice":
        return "voice"
    if value == "text":
        return "text"
    raise ValueError("input_modality must be 'voice' or 'text'")


def _signal_from_proto(signal) -> object:  # type: ignore[no-untyped-def]
    from datetime import datetime, timezone

    from eidolon_agent.core.types.signal import RealtimeSignal, SignalModality

    try:
        modality = SignalModality(signal.modality)
    except ValueError:
        modality = SignalModality.AMBIENT
    return RealtimeSignal(
        ts=datetime.now(timezone.utc),
        modality=modality,
        label=signal.label,
        confidence=float(signal.confidence),
        raw=struct_to_dict(signal.raw),
    )
