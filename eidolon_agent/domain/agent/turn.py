"""TurnEngine — runs a single Turn end-to-end and yields TurnEvents.

This is the heart of the whole architecture. Hot path:

    input_guardrail → triage → compile → LLM.stream
      └── on tool_call: dispatch → continue
    → output_guardrail → DONE

Three Triage outcomes diverge:

* SIMPLE      — standard LLM stream + tool loop.
* COMPLEX_LONG — emit ACK + holding DELTA + HANDOFF, then submit to workstation
                 and forward Progress events.
* TOOL_DIRECT — skip LLM, dispatch a single explicit tool (caller hint or
                first registered matching tool name in input text), return result.

The engine is *cancellation aware*: closing the result iterator propagates
CancelledError through the LLM stream and any in-flight tool calls. Side-effect
tools receive cancellation but should run their own compensation.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections.abc import AsyncIterator
from datetime import datetime, timezone

from eidolon_agent.core.errors import GuardrailBlockedError, TurnCancelledError
from eidolon_agent.core.ports.llm import LLMPort
from eidolon_agent.core.ports.tool import ToolInvocationContext
from eidolon_agent.core.types.event import Event
from eidolon_agent.core.types.llm import LLMFinishReason
from eidolon_agent.core.types.messages import ChatMessage, MessageRole
from eidolon_agent.core.types.tool import ToolCall
from eidolon_agent.core.types.topics import Topics
from eidolon_agent.core.types.turn import (
    FSMState,
    TriageKind,
    TurnEvent,
    TurnEventKind,
    TurnInput,
    TurnStatus,
)
from eidolon_agent.domain.agent.triage import TaskClassifier
from eidolon_agent.domain.agent.workstation import submit_to_workstation
from eidolon_agent.domain.context.compiler import ContextCompiler
from eidolon_agent.domain.guardrails.crisis import CrisisHandler
from eidolon_agent.domain.guardrails.input_filter import InputGuardrail, SafetyAction
from eidolon_agent.domain.guardrails.output_filter import OutputGuardrail
from eidolon_agent.domain.history.fanout import HistoryFanout
from eidolon_agent.domain.history.manager import HistoryManager
from eidolon_agent.domain.personas.types import PersonaInteractionEvent
from eidolon_agent.domain.tools.dispatcher import ToolDispatcher

_log = logging.getLogger(__name__)


class TurnEngine:
    """Per-instance Turn runner.

    One engine instance per AgentInstance (so tools/history are scoped).
    Multiple concurrent Turns are supported but the engine is stateless beyond
    its injected collaborators — concurrency is handled by the asyncio runtime.
    """

    def __init__(
        self,
        *,
        compiler: ContextCompiler,
        llm: LLMPort,
        tool_dispatcher: ToolDispatcher,
        history: HistoryManager,
        fanout: HistoryFanout,
        triage: TaskClassifier,
        input_guardrail: InputGuardrail,
        output_guardrail: OutputGuardrail,
        crisis: CrisisHandler,
        event_bus=None,
        personas_service=None,
        persona_template_id: str | None = None,
        max_tool_iters: int = 4,
        taboos_provider=lambda: (),  # () -> tuple[str, ...]
        session_factory=None,  # async_sessionmaker; None disables turn persistence
    ) -> None:
        self._compiler = compiler
        self._llm = llm
        self._tools = tool_dispatcher
        self._history = history
        self._fanout = fanout
        self._triage = triage
        self._input_g = input_guardrail
        self._output_g = output_guardrail
        self._crisis = crisis
        self._bus = event_bus
        self._personas = personas_service
        self._persona_template_id = persona_template_id
        self._max_tool_iters = max_tool_iters
        self._taboos_provider = taboos_provider
        self._session_factory = session_factory

    async def run(self, ti: TurnInput) -> AsyncIterator[TurnEvent]:
        """Run a single Turn. Yields TurnEvents until DONE or ERROR."""
        seq = _SeqGen()
        started_at = datetime.now(timezone.utc)
        t0 = time.monotonic()
        first_delta_ms: int | None = None
        triage_kind = TriageKind.SIMPLE
        status = TurnStatus.OK
        assistant_text_parts: list[str] = []
        usage_in = usage_out = 0
        error_code: str | None = None
        # Per-phase wall clock for P0 diagnostics. Each phase records the
        # elapsed-since-t0 *at completion*; the diff between adjacent values is
        # the phase duration. A None means the phase didn't run on this path.
        ts_guard_ms: int | None = None
        ts_triage_ms: int | None = None
        ts_compile_ms: int | None = None
        ts_output_ms: int | None = None

        try:
            # ---- Input guardrail --------------------------------------------
            verdict = self._input_g.check(ti.text)
            ts_guard_ms = int((time.monotonic() - t0) * 1000)
            if verdict.action is SafetyAction.ESCALATE:
                crisis = await self._crisis.handle(
                    instance_id=ti.caller.agent_instance_id or "",
                    user_id=ti.caller.user_id,
                    locale=ti.caller.locale,
                )
                yield TurnEvent.state(ti.turn_id, seq.next(), FSMState.SPEAKING, time.time())
                yield TurnEvent.delta(ti.turn_id, seq.next(), crisis.text, time.time())
                yield TurnEvent.done(
                    ti.turn_id,
                    seq.next(),
                    TurnStatus.OK,
                    time.time(),
                    crisis=True,
                    resources=list(crisis.crisis_resources),
                )
                # Persist user + crisis assistant message but do NOT fan out.
                await self._persist_messages(ti, ti.text or "", crisis.text, is_private=True)
                return
            if verdict.action is SafetyAction.REFUSE:
                refuse = "我不能那样做，不过我可以继续陪你聊点别的。"
                yield TurnEvent.delta(ti.turn_id, seq.next(), refuse, time.time())
                yield TurnEvent.done(ti.turn_id, seq.next(), TurnStatus.OK, time.time())
                return
            if verdict.action is SafetyAction.FORGET:
                # Tool-direct branch: forget memory (caller-side action; we ack here).
                ack = "好的，我会忘掉的。"
                yield TurnEvent.delta(ti.turn_id, seq.next(), ack, time.time())
                yield TurnEvent.done(
                    ti.turn_id, seq.next(), TurnStatus.OK, time.time(), action="memory_forget"
                )
                return

            # ---- Triage -----------------------------------------------------
            triage_kind = self._triage.classify(ti.text)
            ts_triage_ms = int((time.monotonic() - t0) * 1000)
            yield TurnEvent.state(ti.turn_id, seq.next(), FSMState.THINKING, time.time())

            # ---- COMPLEX_LONG branch ---------------------------------------
            if triage_kind is TriageKind.COMPLEX_LONG and self._bus is not None:
                async for ev in self._handle_complex(ti, seq, t0):
                    yield ev
                first_delta_ms = first_delta_ms or int((time.monotonic() - t0) * 1000)
                status = TurnStatus.HANDED_OFF
                return

            # ---- Compile context -------------------------------------------
            messages = await self._compiler.compile(ti)
            ts_compile_ms = int((time.monotonic() - t0) * 1000)

            # ---- LLM stream (with tool loop) -------------------------------
            tools = self._tool_schemas()
            yield TurnEvent.state(ti.turn_id, seq.next(), FSMState.SPEAKING, time.time())

            iters = 0
            while iters <= self._max_tool_iters:
                iters += 1
                tool_calls: list[ToolCall] = []
                finish_reason: LLMFinishReason | None = None
                async for delta in self._llm.stream(messages, tools=tools, request_id=ti.turn_id):
                    if delta.text_delta:
                        if first_delta_ms is None:
                            first_delta_ms = int((time.monotonic() - t0) * 1000)
                        assistant_text_parts.append(delta.text_delta)
                        yield TurnEvent.delta(ti.turn_id, seq.next(), delta.text_delta, time.time())
                    if delta.tool_call is not None:
                        tool_calls.append(delta.tool_call)
                        yield TurnEvent(
                            turn_id=ti.turn_id,
                            seq=seq.next(),
                            kind=TurnEventKind.TOOL_CALL,
                            data={
                                "id": delta.tool_call.id,
                                "name": delta.tool_call.name,
                                "arguments": delta.tool_call.arguments,
                            },
                            ts=time.time(),
                        )
                    if delta.usage is not None:
                        usage_in += delta.usage.tokens_in
                        usage_out += delta.usage.tokens_out
                        yield TurnEvent(
                            turn_id=ti.turn_id,
                            seq=seq.next(),
                            kind=TurnEventKind.USAGE,
                            data={"tokens_in": delta.usage.tokens_in, "tokens_out": delta.usage.tokens_out},
                            ts=time.time(),
                        )
                    if delta.finish is not None:
                        finish_reason = delta.finish

                if finish_reason is LLMFinishReason.TOOL_CALLS and tool_calls and iters <= self._max_tool_iters:
                    results = await self._tools.dispatch_batch(
                        tool_calls,
                        ctx=ToolInvocationContext(caller=ti.caller, turn_id=ti.turn_id),
                    )
                    # Feed results back into the conversation as TOOL messages.
                    now = datetime.now(timezone.utc)
                    for r in results:
                        yield TurnEvent(
                            turn_id=ti.turn_id,
                            seq=seq.next(),
                            kind=TurnEventKind.TOOL_RESULT,
                            data={"name": r.name, "ok": r.ok, "content": r.content, "error": r.error_code},
                            ts=time.time(),
                        )
                        messages = [
                            *messages,
                            ChatMessage(
                                id=uuid.uuid4().hex,
                                role=MessageRole.TOOL,
                                content=str(r.content if r.ok else (r.error_message or "")),
                                tool_call_id=r.call_id,
                                tool_name=r.name,
                                created_at=now,
                            ),
                        ]
                    continue  # loop the LLM again with tool results in context
                break

            # ---- Output guardrail ------------------------------------------
            final_text = "".join(assistant_text_parts)
            out_v = self._output_g.check(output_text=final_text, taboos=self._taboos_provider())
            if out_v.action is SafetyAction.SOFTEN:
                # Crude soften: prefix; production would re-prompt LLM.
                final_text = "（让我换个说法）" + final_text
                yield TurnEvent.delta(ti.turn_id, seq.next(), "（让我换个说法）", time.time())
            ts_output_ms = int((time.monotonic() - t0) * 1000)

            # ---- P0 diagnostic: phase timings ------------------------------
            _log_turn_timings(
                turn_id=ti.turn_id,
                t0=t0,
                guard_at=ts_guard_ms,
                triage_at=ts_triage_ms,
                compile_at=ts_compile_ms,
                first_delta_at=first_delta_ms,
                output_at=ts_output_ms,
                tokens_in=usage_in,
                tokens_out=usage_out,
            )

            # ---- Yield DONE FIRST, then handle post-turn in background ----
            yield TurnEvent.done(
                ti.turn_id,
                seq.next(),
                TurnStatus.OK,
                time.time(),
                triage=triage_kind.value,
                first_delta_ms=first_delta_ms,
            )

            # User has their answer; persistence + fanout can take their time.
            asyncio.create_task(  # noqa: RUF006 — fire-and-forget by design
                self._post_turn(ti, final_text, started_at)
            )

        except asyncio.CancelledError:
            status = TurnStatus.CANCELLED
            error_code = TurnCancelledError.code
            yield TurnEvent.error(ti.turn_id, seq.next(), error_code, "cancelled", time.time())
            raise
        except GuardrailBlockedError as exc:
            status = TurnStatus.ERRORED
            error_code = exc.code
            yield TurnEvent.error(ti.turn_id, seq.next(), error_code, exc.message, time.time())
        except Exception as exc:
            _log.exception("turn %s failed", ti.turn_id)
            status = TurnStatus.ERRORED
            error_code = "internal"
            yield TurnEvent.error(ti.turn_id, seq.next(), error_code, str(exc), time.time())
        finally:
            # Persist the turn row (latency + phase timings) for every outcome.
            # Fire-and-forget: the user already has their answer, and a DB hiccup
            # must never affect the stream. Disabled when no session_factory.
            if self._session_factory is not None:
                total_ms = int((time.monotonic() - t0) * 1000)
                timings = {
                    "guard_ms": ts_guard_ms,
                    "triage_ms": ts_triage_ms,
                    "compile_ms": ts_compile_ms,
                    "first_delta_ms": first_delta_ms,
                    "output_ms": ts_output_ms,
                }
                asyncio.create_task(  # noqa: RUF006 — fire-and-forget by design
                    self._persist_turn(
                        ti=ti,
                        status=status,
                        triage_kind=triage_kind,
                        started_at=started_at,
                        finished_at=datetime.now(timezone.utc),
                        first_delta_ms=first_delta_ms,
                        total_ms=total_ms,
                        usage_in=usage_in,
                        usage_out=usage_out,
                        error_code=error_code,
                        timings=timings,
                    )
                )
            if self._bus is not None:
                try:
                    await self._bus.publish(
                        Event(
                            subject=Topics.turn_completed(ti.conversation_id),
                            payload={
                                "turn_id": ti.turn_id,
                                "status": status.value,
                                "triage": triage_kind.value,
                            },
                            trace_id=ti.caller.trace_id,
                            source="agent.turn",
                        )
                    )
                except Exception:
                    _log.exception("publish turn.completed failed")

    # ---- helpers -------------------------------------------------------------

    def _tool_schemas(self) -> list:
        return list(self._tools._registry.list_schemas())

    async def _handle_complex(
        self,
        ti: TurnInput,
        seq: _SeqGen,
        t0: float,
    ) -> AsyncIterator[TurnEvent]:
        """Fire-and-forget workstation handoff.

        We publish to NATS, emit a holding utterance + HANDOFF event, then
        return. Progress flows independently through whatever subscriber the
        workstation service publishes to — not back through this Turn stream.
        """
        yield TurnEvent.state(ti.turn_id, seq.next(), FSMState.SPEAKING, time.time())
        holding = "好的，我让工作站去帮你做。"
        yield TurnEvent.delta(ti.turn_id, seq.next(), holding, time.time())
        yield TurnEvent(
            turn_id=ti.turn_id,
            seq=seq.next(),
            kind=TurnEventKind.ACK,
            data={"intent": "complex_long"},
            ts=time.time(),
        )
        task_id = await submit_to_workstation(self._bus, ti)
        if not task_id:
            yield TurnEvent.error(ti.turn_id, seq.next(), "dispatch_failed", "workstation unavailable", time.time())
            yield TurnEvent.done(ti.turn_id, seq.next(), TurnStatus.ERRORED, time.time())
            return
        yield TurnEvent(
            turn_id=ti.turn_id,
            seq=seq.next(),
            kind=TurnEventKind.HANDOFF,
            data={"task_id": task_id, "progress_subject": Topics.workstation_progress(task_id)},
            ts=time.time(),
        )
        await self._persist_messages(ti, ti.text or "", holding)
        await self._submit_persona_interaction(
            ti=ti,
            kind="turn_completed",
            user_text=ti.text or "",
            assistant_text=holding,
        )
        yield TurnEvent.done(
            ti.turn_id, seq.next(), TurnStatus.HANDED_OFF, time.time(), task_id=task_id
        )

    async def _persist_turn(
        self,
        *,
        ti: TurnInput,
        status: TurnStatus,
        triage_kind: TriageKind,
        started_at: datetime,
        finished_at: datetime,
        first_delta_ms: int | None,
        total_ms: int,
        usage_in: int,
        usage_out: int,
        error_code: str | None,
        timings: dict,
    ) -> None:
        """Background: write the TurnRow (latency + phase timings) to SQLite.

        Ensures the parent conversation row exists (idempotent) and assigns a
        per-conversation ``seq`` via a COUNT. Errors are logged and swallowed —
        this runs after DONE and must never surface to the caller.
        """
        from eidolon_agent.core.types.turn import TurnResult
        from eidolon_agent.infra.persistence.unit_of_work import SqlAlchemyUnitOfWork

        try:
            async with SqlAlchemyUnitOfWork(self._session_factory) as uow:
                await uow.conversations.ensure_started(
                    conversation_id=ti.conversation_id,
                    tenant_id=ti.caller.tenant_id,
                    user_id=ti.caller.user_id,
                    agent_instance_id=ti.caller.agent_instance_id or "",
                )
                seq_in_conv = await uow.conversations.count_turns(ti.conversation_id)
                await uow.conversations.record_turn(
                    TurnResult(
                        turn_id=ti.turn_id,
                        conversation_id=ti.conversation_id,
                        status=status,
                        triage_kind=triage_kind,
                        trigger=ti.trigger,
                        seq_count=0,
                        started_at=started_at,
                        finished_at=finished_at,
                        latency_first_delta_ms=first_delta_ms,
                        total_latency_ms=total_ms,
                        tokens_in=usage_in,
                        tokens_out=usage_out,
                        model=getattr(self._llm, "model_id", None),
                        error_code=error_code,
                        seq_in_conversation=seq_in_conv,
                        metadata=timings,
                    )
                )
                await uow.commit()
        except Exception:
            _log.exception("persist turn %s failed", ti.turn_id)

    async def _post_turn(
        self,
        ti: TurnInput,
        assistant_text: str,
        started_at: datetime,
    ) -> None:
        """Background work that runs AFTER DONE was yielded.

        Errors here never reach the user; logged + swallowed.
        """
        try:
            await self._persist_messages(ti, ti.text or "", assistant_text)
        except Exception:
            _log.exception("post-turn: persist failed")
        try:
            await self._fanout.publish_turn(
                tenant_id=ti.caller.tenant_id,
                user_id=ti.caller.user_id,
                session_id=ti.session_id,
                turn_id=ti.turn_id,
                user_text=ti.text or "",
                assistant_text=assistant_text,
                timestamp_iso=started_at.isoformat(),
            )
        except Exception:
            _log.exception("post-turn: fanout failed")
        try:
            await self._submit_persona_interaction(
                ti=ti,
                kind="turn_completed",
                user_text=ti.text or "",
                assistant_text=assistant_text,
            )
        except Exception:
            _log.exception("post-turn: persona interaction failed")

    async def _persist_messages(
        self,
        ti: TurnInput,
        user_text: str,
        assistant_text: str,
        *,
        is_private: bool = False,
    ) -> None:
        now = datetime.now(timezone.utc)
        if user_text:
            await self._history.append(
                conversation_id=ti.conversation_id,
                message=ChatMessage(
                    id=uuid.uuid4().hex,
                    role=MessageRole.USER,
                    content=user_text,
                    created_at=now,
                    metadata={"is_private": is_private} if is_private else {},
                ),
            )
        if assistant_text:
            await self._history.append(
                conversation_id=ti.conversation_id,
                message=ChatMessage(
                    id=uuid.uuid4().hex,
                    role=MessageRole.ASSISTANT,
                    content=assistant_text,
                    created_at=now,
                    metadata={"is_private": is_private} if is_private else {},
                ),
            )

    async def _submit_persona_interaction(
        self,
        *,
        ti: TurnInput,
        kind: str,
        user_text: str,
        assistant_text: str,
    ) -> None:
        if self._personas is None:
            return
        try:
            await self._personas.submit_interaction(
                PersonaInteractionEvent(
                    tenant_id=ti.caller.tenant_id,
                    user_id=ti.caller.user_id,
                    instance_id=ti.caller.agent_instance_id or "",
                    template_id=self._persona_template_id,
                    kind=kind,
                    user_text=user_text,
                    assistant_text=assistant_text,
                )
            )
        except Exception:
            _log.exception("submit persona interaction failed")


class _SeqGen:
    __slots__ = ("_n",)

    def __init__(self) -> None:
        self._n = -1

    def next(self) -> int:
        self._n += 1
        return self._n


def _log_turn_timings(
    *,
    turn_id: str,
    t0: float,
    guard_at: int | None,
    triage_at: int | None,
    compile_at: int | None,
    first_delta_at: int | None,
    output_at: int | None,
    tokens_in: int,
    tokens_out: int,
) -> None:
    """Emit P0 diagnostic: per-phase ms breakdown for a single Turn.

    Each ``*_at`` is the elapsed ms from turn start *at the end of that phase*.
    Phase durations are deltas between adjacent checkpoints, with the previous
    phase's end (or 0 / t0) as the start. ``llm_ttft_ms`` here is the wall time
    from compile-done to first DELTA — the *brain-side* TTFT, distinct from the
    LiteLLM provider's connect+prefill measurement (logged separately).
    """
    total_ms = int((time.monotonic() - t0) * 1000)

    def _delta(end: int | None, prev: int | None) -> int | None:
        if end is None:
            return None
        return end - (prev or 0)

    guard_ms = _delta(guard_at, None)
    triage_ms = _delta(triage_at, guard_at)
    compile_ms = _delta(compile_at, triage_at)
    llm_ttft_ms = _delta(first_delta_at, compile_at)
    output_ms = _delta(output_at, first_delta_at)

    _log.info(
        "turn_timings turn=%s total_ms=%d guard=%s triage=%s compile=%s "
        "llm_ttft=%s output=%s tokens_in=%d tokens_out=%d",
        turn_id,
        total_ms,
        guard_ms,
        triage_ms,
        compile_ms,
        llm_ttft_ms,
        output_ms,
        tokens_in,
        tokens_out,
    )
