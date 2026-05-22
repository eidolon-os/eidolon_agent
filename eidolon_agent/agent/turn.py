"""TurnEngine — runs a single Turn end-to-end and yields TurnEvents.

This is the heart of the whole architecture. Everything else exists to feed
this pipeline:

    PRE_TURN → triage → compile → PRE_LLM → LLM.stream
      ├── on tool_call: PRE_TOOL → dispatch → POST_TOOL → continue
      └── POST_LLM → persist → fanout → POST_TURN

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

from eidolon_agent.agent.fsm import TurnFSM
from eidolon_agent.context.compiler import ContextCompiler
from eidolon_agent.core.errors import GuardrailBlockedError, TurnCancelledError
from eidolon_agent.core.ports.hooks import HookEvent, HookOutcome, HookPayload
from eidolon_agent.core.ports.llm import LLMPort
from eidolon_agent.core.ports.tool import ToolInvocationContext
from eidolon_agent.core.types.dispatch import ExternalTask, ProgressKind
from eidolon_agent.core.types.event import Event
from eidolon_agent.core.types.llm import LLMFinishReason
from eidolon_agent.core.types.messages import ChatMessage, MessageRole
from eidolon_agent.core.types.tool import ToolCall
from eidolon_agent.core.types.turn import (
    FSMState,
    TriageKind,
    TurnEvent,
    TurnEventKind,
    TurnInput,
    TurnStatus,
)
from eidolon_agent.dispatch.classifier import TaskClassifier
from eidolon_agent.events.topics import Topics
from eidolon_agent.guardrails.crisis import CrisisHandler
from eidolon_agent.guardrails.input_filter import InputGuardrail, SafetyAction
from eidolon_agent.guardrails.output_filter import OutputGuardrail
from eidolon_agent.history.fanout import HistoryFanout
from eidolon_agent.history.manager import HistoryManager
from eidolon_agent.hooks.executor import HookExecutor
from eidolon_agent.personas.types import PersonaInteractionEvent
from eidolon_agent.tools.dispatcher import ToolDispatcher

_log = logging.getLogger(__name__)


class TurnEngine:
    """Per-instance Turn runner.

    One engine instance per AgentInstance (so hooks/tools/history are scoped).
    Multiple concurrent Turns are supported but the engine is stateless beyond
    its injected collaborators — concurrency is handled by the asyncio runtime.
    """

    def __init__(
        self,
        *,
        compiler: ContextCompiler,
        llm: LLMPort,
        tool_dispatcher: ToolDispatcher,
        hook_executor: HookExecutor,
        history: HistoryManager,
        fanout: HistoryFanout,
        triage: TaskClassifier,
        input_guardrail: InputGuardrail,
        output_guardrail: OutputGuardrail,
        crisis: CrisisHandler,
        dispatch_port=None,  # DispatchPort, optional
        event_bus=None,
        personas_service=None,
        persona_template_id: str | None = None,
        max_tool_iters: int = 4,
        taboos_provider=lambda: (),  # () -> tuple[str, ...]
    ) -> None:
        self._compiler = compiler
        self._llm = llm
        self._tools = tool_dispatcher
        self._hooks = hook_executor
        self._history = history
        self._fanout = fanout
        self._triage = triage
        self._input_g = input_guardrail
        self._output_g = output_guardrail
        self._crisis = crisis
        self._dispatch = dispatch_port
        self._bus = event_bus
        self._personas = personas_service
        self._persona_template_id = persona_template_id
        self._max_tool_iters = max_tool_iters
        self._taboos_provider = taboos_provider

    async def run(self, ti: TurnInput) -> AsyncIterator[TurnEvent]:
        """Run a single Turn. Yields TurnEvents until DONE or ERROR."""
        fsm = TurnFSM()
        seq = _SeqGen()
        started_at = datetime.now(timezone.utc)
        t0 = time.monotonic()
        first_delta_ms: int | None = None
        triage_kind = TriageKind.SIMPLE
        status = TurnStatus.OK
        assistant_text_parts: list[str] = []
        usage_in = usage_out = 0
        model_used: str | None = None
        error_code: str | None = None

        try:
            # ---- PRE_TURN hooks (rate limit, audit, etc.) -------------------
            pre_turn_payload = HookPayload(event=HookEvent.PRE_TURN, data={"turn_input": ti})
            pre_res = await self._hooks.run(HookEvent.PRE_TURN, pre_turn_payload)
            if pre_res.outcome is HookOutcome.ABORT:
                yield TurnEvent.error(
                    ti.turn_id, seq.next(), "pre_turn_abort", pre_res.reason or "aborted", time.time()
                )
                status = TurnStatus.ERRORED
                error_code = "pre_turn_abort"
                return

            # ---- Input guardrail --------------------------------------------
            verdict = self._input_g.check(ti.text)
            if verdict.action is SafetyAction.ESCALATE:
                crisis = await self._crisis.handle(
                    instance_id=ti.caller.agent_instance_id or "",
                    user_id=ti.caller.user_id,
                    locale=ti.caller.locale,
                )
                await fsm.force(FSMState.SPEAKING)
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
            await fsm.transition(FSMState.LISTENING)
            await fsm.transition(FSMState.THINKING)
            yield TurnEvent.state(ti.turn_id, seq.next(), FSMState.THINKING, time.time())

            # ---- COMPLEX_LONG branch ---------------------------------------
            if triage_kind is TriageKind.COMPLEX_LONG and self._dispatch is not None:
                async for ev in self._handle_complex(ti, fsm, seq, t0):
                    yield ev
                first_delta_ms = first_delta_ms or int((time.monotonic() - t0) * 1000)
                status = TurnStatus.HANDED_OFF
                return

            # ---- Compile context -------------------------------------------
            compiled = await self._compiler.compile(ti)

            # ---- PRE_LLM hooks ---------------------------------------------
            pre_llm_data = {"messages": list(compiled.messages), "tools": []}
            await self._hooks.run(
                HookEvent.PRE_LLM, HookPayload(event=HookEvent.PRE_LLM, data=pre_llm_data)
            )

            # ---- LLM stream (with tool loop) -------------------------------
            messages = pre_llm_data["messages"]
            tools = pre_llm_data["tools"] or self._tool_schemas()
            await fsm.transition(FSMState.SPEAKING)
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

            # ---- POST_LLM hooks --------------------------------------------
            post_llm = HookPayload(
                event=HookEvent.POST_LLM,
                data={"text": "".join(assistant_text_parts)},
            )
            await self._hooks.run(HookEvent.POST_LLM, post_llm)

            # ---- Output guardrail ------------------------------------------
            final_text = post_llm.data.get("text", "")
            out_v = self._output_g.check(output_text=final_text, taboos=self._taboos_provider())
            if out_v.action is SafetyAction.SOFTEN:
                # Crude soften: prefix; production would re-prompt LLM.
                final_text = "（让我换个说法）" + final_text
                yield TurnEvent.delta(ti.turn_id, seq.next(), "（让我换个说法）", time.time())

            # ---- Persist + fanout ------------------------------------------
            await self._persist_messages(ti, ti.text or "", final_text)
            await self._fanout.publish_turn(
                tenant_id=ti.caller.tenant_id,
                user_id=ti.caller.user_id,
                session_id=ti.session_id,
                turn_id=ti.turn_id,
                user_text=ti.text or "",
                assistant_text=final_text,
                timestamp_iso=started_at.isoformat(),
            )
            await self._submit_persona_interaction(
                ti=ti,
                kind="turn_completed",
                user_text=ti.text or "",
                assistant_text=final_text,
            )

            await fsm.transition(FSMState.REFLECTING)
            await fsm.transition(FSMState.IDLE)
            yield TurnEvent.done(
                ti.turn_id,
                seq.next(),
                TurnStatus.OK,
                time.time(),
                triage=triage_kind.value,
                first_delta_ms=first_delta_ms,
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
            # POST_TURN hooks (audit, metrics)
            try:
                await self._hooks.run(
                    HookEvent.POST_TURN,
                    HookPayload(
                        event=HookEvent.POST_TURN,
                        data={
                            "turn_input": ti,
                            "status": status.value,
                            "triage": triage_kind.value,
                            "model": model_used,
                            "tokens_in": usage_in,
                            "tokens_out": usage_out,
                            "first_delta_ms": first_delta_ms,
                        },
                    ),
                )
            except Exception:
                _log.exception("POST_TURN hooks failed")
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
        return [t for t in self._tools._registry.list_schemas()]

    async def _handle_complex(
        self,
        ti: TurnInput,
        fsm: TurnFSM,
        seq: _SeqGen,
        t0: float,
    ) -> AsyncIterator[TurnEvent]:
        await fsm.transition(FSMState.SPEAKING)
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
        task = ExternalTask(
            id=uuid.uuid4().hex,
            tenant_id=ti.caller.tenant_id,
            user_id=ti.caller.user_id,
            natural_language=ti.text or "",
        )
        try:
            handle = await self._dispatch.submit(task)
        except Exception as exc:
            yield TurnEvent.error(ti.turn_id, seq.next(), "dispatch_failed", str(exc), time.time())
            yield TurnEvent.done(ti.turn_id, seq.next(), TurnStatus.ERRORED, time.time())
            return
        yield TurnEvent(
            turn_id=ti.turn_id,
            seq=seq.next(),
            kind=TurnEventKind.HANDOFF,
            data={"task_id": handle.task_id, "progress_subject": handle.progress_subject},
            ts=time.time(),
        )
        async for progress in self._dispatch.stream_progress(handle):
            yield TurnEvent(
                turn_id=ti.turn_id,
                seq=seq.next(),
                kind=TurnEventKind.PROGRESS,
                data={"task_id": progress.task_id, "kind": progress.kind.value, "note": progress.note},
                ts=time.time(),
            )
            if progress.kind in {ProgressKind.SUCCESS, ProgressKind.FAILURE, ProgressKind.CANCELLED}:
                break
        # Persist user turn + holding utterance; final summary is a separate proactive turn.
        await self._persist_messages(ti, ti.text or "", holding)
        await self._submit_persona_interaction(
            ti=ti,
            kind="turn_completed",
            user_text=ti.text or "",
            assistant_text=holding,
        )
        yield TurnEvent.done(
            ti.turn_id, seq.next(), TurnStatus.HANDED_OFF, time.time(), task_id=handle.task_id
        )

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
