"""gRPC servicer — Chat / ChatOnce / PushSignal / SubscribeProactive / ExchangePairingCode.

The servicer is intentionally thin: it translates proto frames ↔ core types,
resolves the AgentInstance from the caller's identity, and delegates each Turn
to the :class:`CompanionAgent`. All business logic lives behind the ports.
"""

from __future__ import annotations

import asyncio
import logging
import uuid

import grpc

from eidolon_agent.app.transport.grpc.codec import struct_to_dict, turn_event_to_proto
from eidolon_agent.app.transport.grpc.interceptors import current_identity
from eidolon_agent.app.transport.grpc.proto import pb, pbg
from eidolon_agent.app.transport.pairing.coordinator import PairingCoordinator
from eidolon_agent.core.errors import EidolonError, NotFoundError, UnauthenticatedError
from eidolon_agent.core.types.identity import CallerContext, CallerKind, Identity
from eidolon_agent.core.types.turn import TurnInput, TurnTrigger

_log = logging.getLogger(__name__)


class EidolonAgentServicer(pbg.EidolonAgentServicer):
    def __init__(
        self,
        *,
        agent_registry,
        pairing: PairingCoordinator,
        signals_bus,
        proactive_bus,  # EventBus
        personas_service=None,
    ) -> None:
        self._registry = agent_registry
        self._pairing = pairing
        self._signals = signals_bus
        self._bus = proactive_bus
        self._personas = personas_service

    # ---- Pairing (public RPC, no auth) --------------------------------------

    async def ExchangePairingCode(
        self, request: pb.ExchangeRequest, context: grpc.aio.ServicerContext
    ) -> pb.ExchangeResponse:
        try:
            issued = await self._pairing.exchange(
                code=request.pairing_code,
                device_id=request.device_id or None,
            )
        except (NotFoundError, UnauthenticatedError) as exc:
            await context.abort(grpc.StatusCode.UNAUTHENTICATED, exc.message)
        resp = pb.ExchangeResponse(
            device_id=issued.device_id,
            device_token=issued.token,
            tenant_id=issued.tenant_id,
            user_id=issued.user_id,
            default_template_id=issued.default_template_id or "",
        )
        resp.expires_at.FromDatetime(issued.expires_at)
        return resp

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

        # Watch the gRPC context for cancellation (raw TCP close, RPC cancel
        # without a CancelTurn frame, deadline exceeded, …). When fired, cancel
        # every in-flight turn so we stop pulling tokens from the LLM provider
        # and don't bill against a disconnected client.
        async def _on_rpc_cancelled() -> None:
            while not context.cancelled() and not context.done():
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
                    for task in list(active_turns):
                        if not task.done() and task.get_name() == f"turn-{frame.cancel.turn_id}":
                            task.cancel()
                    continue
                if payload == "signal":
                    # Forward to signal bus; non-blocking.
                    _log.debug("inline signal: %s", frame.signal.label)
                    continue
                if payload != "start":
                    continue

                start = frame.start
                try:
                    inst = await self._registry.resolve_for_caller(
                        tenant_id=identity.tenant_id,
                        user_id=identity.user_id,
                        template_id=identity.default_template_id or None,
                    )
                    agent = inst.agent
                except NotFoundError as exc:
                    await context.abort(grpc.StatusCode.FAILED_PRECONDITION, exc.message)

                ti = TurnInput(
                    turn_id=start.turn_id or uuid.uuid4().hex,
                    conversation_id=start.conversation_id,
                    session_id=start.conversation_id,  # one-to-one for now
                    caller=CallerContext(
                        identity=Identity(
                            tenant_id=identity.tenant_id,
                            user_id=identity.user_id,
                            agent_instance_id=inst.instance_id,
                            device_id=identity.device_id,
                        ),
                        caller_kind=CallerKind.LIVEKIT_VOICE,
                        trace_id=dict(context.invocation_metadata()).get(
                            "x-trace-id", uuid.uuid4().hex
                        ),
                        request_id=dict(context.invocation_metadata()).get(
                            "x-request-id", uuid.uuid4().hex
                        ),
                    ),
                    trigger=TurnTrigger.USER_UTTERANCE,
                    text=start.text,
                    metadata=struct_to_dict(start.metadata),
                )

                async def _emit_turn(_agent=agent, _ti=ti) -> None:
                    try:
                        async for ev in _agent.run_turn(_ti):
                            await context.write(turn_event_to_proto(ev))
                    except asyncio.CancelledError:
                        raise
                    except EidolonError as exc:
                        _log.warning("turn %s failed: %s", _ti.turn_id, exc)

                task = asyncio.create_task(_emit_turn(), name=f"turn-{ti.turn_id}")
                active_turns.add(task)
                task.add_done_callback(active_turns.discard)
        finally:
            watcher.cancel()
            # If the iterator returned because the RPC was cancelled, propagate
            # the cancel to any in-flight turn. Legitimate stream-end (client
            # closed cleanly) still wants to drain.
            if context.cancelled() or context.done():
                for task in list(active_turns):
                    if not task.done():
                        task.cancel()
            for task in list(active_turns):
                try:
                    await task
                except asyncio.CancelledError:
                    pass

    # ---- One-shot ----------------------------------------------------------

    async def ChatOnce(
        self, request: pb.ChatOnceRequest, context: grpc.aio.ServicerContext
    ) -> pb.ChatOnceResponse:
        identity = current_identity()
        if identity is None:
            await context.abort(grpc.StatusCode.UNAUTHENTICATED, "no identity")
        inst = await self._registry.resolve_for_caller(
            tenant_id=identity.tenant_id,
            user_id=identity.user_id,
            template_id=identity.default_template_id or None,
        )
        agent = inst.agent
        ti = TurnInput(
            turn_id=request.turn_id or uuid.uuid4().hex,
            conversation_id=request.conversation_id,
            session_id=request.conversation_id,
            caller=CallerContext(
                identity=Identity(
                    tenant_id=identity.tenant_id,
                    user_id=identity.user_id,
                    agent_instance_id=inst.instance_id,
                ),
                caller_kind=CallerKind.WEB_CHAT,
                trace_id=uuid.uuid4().hex,
                request_id=uuid.uuid4().hex,
            ),
            trigger=TurnTrigger.USER_UTTERANCE,
            text=request.text,
        )
        assistant_text_parts: list[str] = []
        first_delta_ms = 0
        triage = "simple"
        async for ev in agent.run_turn(ti):
            if ev.kind.value == "delta":
                assistant_text_parts.append(ev.data.get("text", ""))
            elif ev.kind.value == "done":
                first_delta_ms = int(ev.data.get("first_delta_ms") or 0)
                triage = ev.data.get("triage", "simple")
        return pb.ChatOnceResponse(
            turn_id=ti.turn_id,
            assistant_text="".join(assistant_text_parts),
            triage=triage,
            latency_first_delta_ms=first_delta_ms,
        )

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
                inst = await self._registry.resolve_for_caller(
                    tenant_id=identity.tenant_id,
                    user_id=identity.user_id,
                    template_id=identity.default_template_id or None,
                )
                from eidolon_agent.domain.personas.types import PersonaSignalInput

                await self._personas.submit_signal(
                    PersonaSignalInput(
                        tenant_id=identity.tenant_id,
                        user_id=identity.user_id,
                        instance_id=inst.instance_id,
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
