"""TurnEngine — runs a single Turn end-to-end and yields TurnEvents.

This is the heart of the whole architecture. Hot path:

    input_guardrail → triage → compile → LLM.stream
      └── on tool_call: dispatch → continue
    → output_guardrail → DONE

Three Triage outcomes diverge:

* SIMPLE      — standard LLM stream + tool loop.
* COMPLEX_LONG — trace signal only; complex work is delegated through the
                 LLM tool-call path via ``delegate_to_coworker``.
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
from dataclasses import dataclass
from datetime import datetime, timezone

from eidolon_sdk.biz.chat_stream import DeltaRole, TerminationCause
from eidolon_sdk.biz.dialogue_control import InterruptIntent
from eidolon_sdk.core.runtime import BackgroundTaskRunner

from eidolon_agent.core.errors import GuardrailBlockedError, TurnCancelledError
from eidolon_agent.core.ports.llm import LLMPort
from eidolon_agent.core.ports.tool import ToolInvocationContext
from eidolon_agent.core.types import (
    ChatMessage,
    DevelopmentGuardTrace,
    Event,
    LatencyBreakdown,
    LLMFinishReason,
    MemoryWriteDispositionKind,
    MessageRole,
    PersonaTrace,
    ToolCall,
    ToolResult,
    ToolTrace,
    TurnTrace,
    classify_memory_write,
)
from eidolon_agent.core.types.topics import Topics
from eidolon_agent.core.types.turn import (
    FSMState,
    TriageKind,
    TurnEvent,
    TurnEventKind,
    TurnInput,
    TurnStatus,
)
from eidolon_agent.domain.agent.control_intent import ControlIntentClassifier
from eidolon_agent.domain.agent.triage import TaskClassifier
from eidolon_agent.domain.context.compiler import ContextCompiler
from eidolon_agent.domain.guardrails.crisis import CrisisHandler
from eidolon_agent.domain.guardrails.input_filter import InputGuardrail, SafetyAction
from eidolon_agent.domain.guardrails.output_filter import OutputGuardrail
from eidolon_agent.domain.harness import RealtimeAgentHarness
from eidolon_agent.domain.history.fanout import HistoryFanout
from eidolon_agent.domain.history.manager import HistoryManager
from eidolon_agent.domain.personas.types import PersonaInteractionEvent
from eidolon_agent.domain.runtime_policy import TurnRuntimePolicy
from eidolon_agent.domain.tools.builtin.submit_long_task import (
    DELEGATE_TO_COWORKER_TOOL,
)
from eidolon_agent.domain.tools.dispatcher import ToolDispatcher

_log = logging.getLogger(__name__)
_MAX_FAILURES_PER_TOOL_PER_TURN = 1


@dataclass(frozen=True, slots=True)
class ToolLatencyPolicy:
    """UX policy for tool calls that take long enough to need a spoken hint."""

    slow_hint_delay_s: float = 1.5
    slow_hint_text: str = "稍等，我处理一下。"
    slow_hint_role: str = DeltaRole.SLOW_TOOL_HINT.value


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
        control_classifier: ControlIntentClassifier | None = None,
        event_bus=None,
        personas_service=None,
        persona_template_id: str | None = None,
        memory_port=None,
        max_tool_iters: int = 4,
        memory_write_mode: str = "enabled",
        tool_schema_strict: bool = True,
        require_idempotency_for_side_effect_tools: bool = False,
        taboos_provider=lambda: (),  # () -> tuple[str, ...]
        turn_persister=None,  # async callable; None disables durable turn persistence
        harness: RealtimeAgentHarness | None = None,
        background_tasks: BackgroundTaskRunner | None = None,
        tool_latency_policy: ToolLatencyPolicy | None = None,
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
        self._control = control_classifier or ControlIntentClassifier()
        self._bus = event_bus
        self._personas = personas_service
        self._persona_template_id = persona_template_id
        self._memory = memory_port
        self._max_tool_iters = max_tool_iters
        self._memory_write_mode = _normalize_guard_mode(memory_write_mode)
        self._tool_schema_strict = tool_schema_strict
        self._require_idempotency_for_side_effect_tools = require_idempotency_for_side_effect_tools
        self._taboos_provider = taboos_provider
        self._turn_persister = turn_persister
        self._harness = harness or RealtimeAgentHarness()
        self._background = background_tasks or BackgroundTaskRunner()
        self._tool_latency_policy = tool_latency_policy or ToolLatencyPolicy()

    async def run(self, ti: TurnInput) -> AsyncIterator[TurnEvent]:
        """Run a single Turn. Yields TurnEvents until DONE or ERROR."""
        seq = _SeqGen()
        started_at = datetime.now(timezone.utc)
        t0 = time.monotonic()
        first_delta_ms: int | None = None
        triage_kind = TriageKind.SIMPLE
        status = TurnStatus.OK
        assistant_text_parts: list[str] = []
        assistant_text_for_persist = ""
        usage_in = usage_out = 0
        error_code: str | None = None
        # Per-phase wall clock for P0 diagnostics. Each phase records the
        # elapsed-since-t0 *at completion*; the diff between adjacent values is
        # the phase duration. A None means the phase didn't run on this path.
        ts_guard_ms: int | None = None
        ts_triage_ms: int | None = None
        ts_compile_ms: int | None = None
        ts_output_ms: int | None = None
        tool_ms_total = 0
        tool_trace: list[ToolTrace] = []
        tool_failure_counts: dict[str, int] = {}
        tool_repeat_suppressed_count = 0
        handoff_summaries: list[dict] = []
        runtime_policy = TurnRuntimePolicy.from_metadata(ti.metadata)
        post_turn_allowed = False
        post_turn_scheduled = False
        recent_history_persisted = False

        try:
            # ---- Input guardrail --------------------------------------------
            verdict = self._input_g.check(ti.text)
            ts_guard_ms = int((time.monotonic() - t0) * 1000)
            if verdict.action is SafetyAction.ESCALATE:
                crisis = await self._crisis.handle(
                    companion_id=ti.caller.companion_id,
                    owner_id=ti.caller.owner_id,
                    locale=ti.caller.locale,
                )
                ti.metadata["is_private"] = True
                runtime_policy = TurnRuntimePolicy.from_metadata(ti.metadata)
                assistant_text_for_persist = crisis.text
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
                # Persist user + crisis assistant message after DONE, but do NOT fan out.
                self._background.create(
                    self._persist_messages(ti, ti.text or "", crisis.text, is_private=True),
                    name=f"turn-{ti.turn_id}-crisis-history",
                )
                return
            if verdict.action is SafetyAction.REFUSE:
                refuse = "我不能那样做，不过我可以继续陪你聊点别的。"
                assistant_text_for_persist = refuse
                yield TurnEvent.delta(ti.turn_id, seq.next(), refuse, time.time())
                yield TurnEvent.done(ti.turn_id, seq.next(), TurnStatus.OK, time.time())
                return
            if verdict.action is SafetyAction.FORGET:
                # Tool-direct branch: forget memory (caller-side action; we ack here).
                removed = 0
                if self._memory is not None:
                    try:
                        removed = await self._memory.forget(
                            ti.caller.owner_id,
                            ti.caller.companion_id,
                            ti.caller.memory_realm_id,
                            ti.caller.device_id,
                            ti.text or "",
                            session_id=ti.session_id or "default",
                        )
                    except Exception:
                        _log.exception("memory forget failed")
                try:
                    removed += await self._history.forget_matching(
                        conversation_id=ti.conversation_id,
                        query=ti.text or "",
                    )
                except Exception:
                    _log.exception("local history forget failed")
                ack = "好的，我会忘掉的。"
                assistant_text_for_persist = ack
                yield TurnEvent.delta(ti.turn_id, seq.next(), ack, time.time())
                yield TurnEvent.done(
                    ti.turn_id,
                    seq.next(),
                    TurnStatus.OK,
                    time.time(),
                    action="memory_forget",
                    removed=removed,
                )
                return

            # ---- Reflex control intent (stop / topic switch) ---------------
            # Runs before triage on the final utterance. A whole-utterance
            # stop must never reach the LLM: emit a DONE tagged
            # ``termination_cause="user_stop"`` so the upstream channel can
            # circuit-break TTS/rendering. A topic switch tags the TurnInput
            # so the context compiler can fence off the prior topic. Mirrors
            # the channel's Tier0 fast-path via the shared SDK taxonomy.
            control = self._control.classify(ti.text)
            ti.metadata["control_intent"] = control.intent.value
            if control.short_circuit:
                yield TurnEvent.done(
                    ti.turn_id,
                    seq.next(),
                    TurnStatus.OK,
                    time.time(),
                    termination_cause=TerminationCause.USER_STOP.value,
                    control_intent=control.intent.value,
                )
                return
            if control.intent is InterruptIntent.TOPIC_SWITCH:
                ti.metadata["topic_switch"] = True

            # ---- Triage -----------------------------------------------------
            triage_kind = self._triage.classify(ti.text)
            ts_triage_ms = int((time.monotonic() - t0) * 1000)
            yield TurnEvent.state(ti.turn_id, seq.next(), FSMState.THINKING, time.time())

            # ``COMPLEX_LONG`` remains a trace signal, but coworker delegation
            # is unified through the LLM tool-call path via
            # ``delegate_to_coworker``. That keeps parameter extraction, tool
            # announcements, and result handling on one path.

            # ---- Compile context -------------------------------------------
            messages = await self._compiler.compile(ti)
            ts_compile_ms = int((time.monotonic() - t0) * 1000)

            # ---- LLM stream (with tool loop) -------------------------------
            tools = self._tool_schemas()
            tool_budget = self._harness.tool_schema_budget(tools)
            ti.metadata.setdefault("development_guards", {})[
                "tool_schema_budget"
            ] = tool_budget
            _update_harness_snapshot_tools(
                ti,
                [schema.name for schema in tools],
                tool_budget=tool_budget,
            )
            yield TurnEvent.state(ti.turn_id, seq.next(), FSMState.SPEAKING, time.time())

            tool_iters = 0
            # Substantive brain-injected acknowledgements (currently coworker
            # delegation) are answer content. Generic tool wait hints are latency
            # driven later, during dispatch, so fast tools stay silent.
            announced_answers: set[str] = set()
            slow_tool_hint_emitted = False
            while True:
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
                        answer_announcement = _tool_answer_announcement(delta.tool_call)
                        if (
                            answer_announcement
                            and answer_announcement not in announced_answers
                        ):
                            announced_answers.add(answer_announcement)
                            if first_delta_ms is None:
                                first_delta_ms = int((time.monotonic() - t0) * 1000)
                            # A substantive announcement (e.g. the coworker
                            # delegation ack) IS the turn's answer: count it as
                            # answer text and stream it with the default role.
                            assistant_text_parts.append(answer_announcement)
                            yield TurnEvent.delta(
                                ti.turn_id,
                                seq.next(),
                                answer_announcement,
                                time.time(),
                            )
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
                            data={
                                "tokens_in": delta.usage.tokens_in,
                                "tokens_out": delta.usage.tokens_out,
                            },
                            ts=time.time(),
                        )
                    if delta.finish is not None:
                        finish_reason = delta.finish

                if finish_reason is LLMFinishReason.TOOL_CALLS and tool_calls:
                    if tool_iters >= self._max_tool_iters:
                        yield TurnEvent.error(
                            ti.turn_id,
                            seq.next(),
                            "tool_loop_exceeded",
                            f"exceeded max_tool_iters={self._max_tool_iters}",
                            time.time(),
                        )
                        break
                    tool_iters += 1
                    dispatch_calls: list[ToolCall] = []
                    suppressed_results: list[ToolResult] = []
                    for call in tool_calls:
                        if (
                            tool_failure_counts.get(call.name, 0)
                            >= _MAX_FAILURES_PER_TOOL_PER_TURN
                        ):
                            tool_repeat_suppressed_count += 1
                            suppressed_results.append(_suppressed_tool_result(call))
                        else:
                            dispatch_calls.append(call)
                    tool_t0 = time.monotonic()
                    dispatched_results: list[ToolResult] = []
                    if dispatch_calls:
                        dispatch_task = asyncio.create_task(
                            self._tools.dispatch_batch(
                                dispatch_calls,
                                ctx=ToolInvocationContext(
                                    caller=ti.caller,
                                    turn_id=ti.turn_id,
                                    conversation_id=ti.conversation_id,
                                    session_id=ti.session_id,
                                    user_text=ti.text or "",
                                    companion_id=ti.caller.companion_id,
                                    memory_realm_id=ti.caller.memory_realm_id,
                                ),
                            ),
                            name=f"turn-{ti.turn_id}-tool-dispatch",
                        )
                        try:
                            slow_hint_delay_s = (
                                self._tool_latency_policy.slow_hint_delay_s
                            )
                            if (
                                not slow_tool_hint_emitted
                                and slow_hint_delay_s > 0
                                and any(
                                    call.name != DELEGATE_TO_COWORKER_TOOL
                                    for call in dispatch_calls
                                )
                            ):
                                done, _pending = await asyncio.wait(
                                    {dispatch_task},
                                    timeout=slow_hint_delay_s,
                                    return_when=asyncio.FIRST_COMPLETED,
                                )
                                if dispatch_task not in done:
                                    slow_tool_hint_emitted = True
                                    if first_delta_ms is None:
                                        first_delta_ms = int(
                                            (time.monotonic() - t0) * 1000
                                        )
                                    yield TurnEvent.delta(
                                        ti.turn_id,
                                        seq.next(),
                                        self._tool_latency_policy.slow_hint_text,
                                        time.time(),
                                        role=self._tool_latency_policy.slow_hint_role,
                                    )
                            dispatched_results = await dispatch_task
                        finally:
                            if not dispatch_task.done():
                                dispatch_task.cancel()
                    tool_ms_total += int((time.monotonic() - tool_t0) * 1000)
                    results = _merge_tool_results(
                        tool_calls,
                        [*dispatched_results, *suppressed_results],
                    )
                    for r in results:
                        if r.ok:
                            tool_failure_counts.pop(r.name, None)
                        else:
                            tool_failure_counts[r.name] = (
                                tool_failure_counts.get(r.name, 0) + 1
                            )
                    tool_trace.extend(
                        ToolTrace(
                            call_id=r.call_id,
                            name=r.name,
                            ok=r.ok,
                            latency_ms=r.latency_ms,
                            error_code=r.error_code,
                            cached=bool(r.metadata.get("idempotent_cache")),
                        )
                        for r in results
                    )
                    # Feed results back into the conversation as TOOL messages.
                    now = datetime.now(timezone.utc)
                    tool_result_messages: list[ChatMessage] = []
                    for r in results:
                        yield TurnEvent(
                            turn_id=ti.turn_id,
                            seq=seq.next(),
                            kind=TurnEventKind.TOOL_RESULT,
                            data={
                                "name": r.name,
                                "ok": r.ok,
                                "content": r.content,
                                "error": r.error_code,
                            },
                            ts=time.time(),
                        )
                        handoff = _handoff_from_tool_result(
                            turn_id=ti.turn_id,
                            seq=seq,
                            result=r,
                        )
                        if handoff is not None:
                            handoff_summaries.append(
                                _handoff_summary_from_tool_result(r)
                            )
                            _update_harness_snapshot_handoffs(ti, handoff_summaries)
                            yield handoff
                        tool_result_messages.append(
                            ChatMessage(
                                id=uuid.uuid4().hex,
                                role=MessageRole.TOOL,
                                content=str(r.content if r.ok else (r.error_message or "")),
                                tool_call_id=r.call_id,
                                tool_name=r.name,
                                created_at=now,
                            )
                        )
                    messages = [
                        *messages,
                        ChatMessage(
                            id=uuid.uuid4().hex,
                            role=MessageRole.ASSISTANT,
                            content="",
                            tool_calls=tuple(tool_calls),
                            created_at=now,
                        ),
                        *tool_result_messages,
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
            assistant_text_for_persist = final_text
            ts_output_ms = int((time.monotonic() - t0) * 1000)
            post_turn_allowed = True

            # Keep recent history deterministic for the next turn. This is a
            # cheap in-memory append, not durable persistence; durable SQLite
            # writes and external fanout remain after DONE.
            try:
                await self._persist_messages(
                    ti,
                    ti.text or "",
                    final_text,
                    is_private=runtime_policy.mark_messages_private,
                )
                recent_history_persisted = True
            except Exception:
                _log.exception("recent history append failed")

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
            post_turn_scheduled = True
            self._background.create(
                self._post_turn(
                    ti,
                    final_text,
                    started_at,
                    history_already_persisted=recent_history_persisted,
                ),
                name=f"turn-{ti.turn_id}-post-turn",
            )

        except asyncio.CancelledError:
            status = TurnStatus.CANCELLED
            error_code = TurnCancelledError.code
            # Barge-in truncation: persist only what the user actually heard.
            # The transport stashes the TTS playback boundary (character offset
            # into the streamed answer text) on CancelTurn; truncate the
            # accumulated assistant text there so history / chat_messages /
            # memory fanout never record words that were cut off before
            # playback. Absent boundary => keep the full streamed text
            # (unknown, not "nothing heard"). This keeps the companion's
            # memory aligned with the user's real auditory experience.
            heard_text = "".join(assistant_text_parts)
            played_chars = ti.metadata.get("cancel_played_chars")
            if played_chars is not None:
                heard_text = heard_text[: max(0, int(played_chars))]
            assistant_text_for_persist = heard_text
            # Let the heard exchange reach memory/history via the standard
            # post-turn path (skipped when there is nothing heard).
            if heard_text:
                post_turn_allowed = True
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
            # Fire-and-forget: the user already has their answer, and a durable
            # store hiccup must never affect the stream. Disabled when no
            # turn_persister is injected.
            if self._turn_persister is not None:
                total_ms = int((time.monotonic() - t0) * 1000)
                memory_write_trace = _memory_write_trace(
                    ti=ti,
                    assistant_text=assistant_text_for_persist,
                    policy=runtime_policy,
                    mode=self._memory_write_mode,
                )
                development_guards = _development_guard_trace(
                    ti=ti,
                    memory_write_trace=memory_write_trace,
                    max_tool_iters=self._max_tool_iters,
                    tool_schema_strict=self._tool_schema_strict,
                    require_idempotency_for_side_effect_tools=(
                        self._require_idempotency_for_side_effect_tools
                    ),
                )
                trace = TurnTrace(
                    turn_id=ti.turn_id,
                    conversation_id=ti.conversation_id,
                    status=status.value,
                    trigger=ti.trigger.value,
                    triage=triage_kind.value,
                    caller_kind=ti.caller.caller_kind.value,
                    model=getattr(self._llm, "model_id", None),
                    latency=LatencyBreakdown(
                        guard_ms=_duration(ts_guard_ms, None),
                        triage_ms=_duration(ts_triage_ms, ts_guard_ms),
                        compile_ms=_duration(ts_compile_ms, ts_triage_ms),
                        first_delta_ms=_duration(first_delta_ms, ts_compile_ms),
                        output_ms=_duration(ts_output_ms, first_delta_ms),
                        tool_ms=tool_ms_total,
                        total_ms=total_ms,
                    ),
                    context_ledger=ti.metadata.get("context_ledger"),
                    memory_trace=ti.metadata.get("memory_trace"),
                    memory_recall_query=ti.metadata.get("memory_recall_query"),
                    memory_write_trace=memory_write_trace,
                    tool_trace=tool_trace,
                    persona=PersonaTrace(
                        companion_id=ti.caller.companion_id,
                        genome_id=self._persona_template_id,
                    ),
                    privacy=runtime_policy.privacy,
                    proactive_reason=ti.metadata.get("proactive_reason"),
                    harness_snapshot=ti.metadata.get("harness_snapshot"),
                    context_structure_version=ti.metadata.get("context_structure_version"),
                    history_presentation=ti.metadata.get("history_presentation"),
                    context_tags=ti.metadata.get("context_tags") or [],
                    interrupted_context_dropped_count=int(
                        ti.metadata.get("interrupted_context_dropped_count") or 0
                    ),
                    stale_generation_dropped=int(
                        ti.metadata.get("stale_generation_dropped") or 0
                    ),
                    tool_repeat_suppressed_count=tool_repeat_suppressed_count,
                    development_guards=development_guards,
                    usage={"tokens_in": usage_in, "tokens_out": usage_out},
                ).to_metadata()
                timings = {
                    "guard_ms": ts_guard_ms,
                    "triage_ms": ts_triage_ms,
                    "compile_ms": ts_compile_ms,
                    "first_delta_ms": first_delta_ms,
                    "output_ms": ts_output_ms,
                    "tool_ms": tool_ms_total,
                    "context_ledger": ti.metadata.get("context_ledger"),
                    "memory_trace": ti.metadata.get("memory_trace"),
                    "memory_recall_query": ti.metadata.get("memory_recall_query"),
                    "memory_write_trace": memory_write_trace,
                    "tool_trace": [t.to_metadata() for t in tool_trace],
                    "context_structure_version": ti.metadata.get("context_structure_version"),
                    "history_presentation": ti.metadata.get("history_presentation"),
                    "context_tags": ti.metadata.get("context_tags") or [],
                    "interrupted_context_dropped_count": ti.metadata.get(
                        "interrupted_context_dropped_count"
                    )
                    or 0,
                    "stale_generation_dropped": ti.metadata.get(
                        "stale_generation_dropped"
                    )
                    or 0,
                    "tool_repeat_suppressed_count": tool_repeat_suppressed_count,
                    "turn_trace": trace,
                }
                # Phase 34.C: thread user + assistant text through so
                # both land in chat_messages atomically with the
                # TurnRow. ``assistant_text_parts`` is initialized at
                # the top of the method, so it's safe to read here in
                # the finally — empty list on exception paths produces
                # "" (and _persist_turn no-ops the message append).
                self._background.create(
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
                        user_text=ti.text or "",
                        assistant_text=assistant_text_for_persist,
                        is_private=runtime_policy.mark_messages_private,
                    ),
                    name=f"turn-{ti.turn_id}-persist-turn",
                )
            if post_turn_allowed and not post_turn_scheduled:
                self._background.create(
                    self._post_turn(
                        ti,
                        assistant_text_for_persist,
                        started_at,
                        history_already_persisted=recent_history_persisted,
                    ),
                    name=f"turn-{ti.turn_id}-post-turn",
                )
            if self._bus is not None:
                self._background.create(
                    self._publish_turn_completed(
                        ti=ti,
                        status=status,
                        triage_kind=triage_kind,
                    ),
                    name=f"turn-{ti.turn_id}-completed-event",
                )

    # ---- helpers -------------------------------------------------------------

    def _tool_schemas(self) -> list:
        return self._harness.visible_tool_schemas(self._tools.list_schemas())

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
        user_text: str = "",
        assistant_text: str = "",
        is_private: bool = False,
    ) -> None:
        """Background: write the TurnRow and optional user / assistant text.

        Errors are logged and swallowed — this runs after DONE and must never
        surface to the caller. The actual durable store is injected by app/infra
        wiring so domain does not depend on SQLite.
        """
        if self._turn_persister is None:
            return
        try:
            await self._turn_persister(
                ti=ti,
                status=status,
                triage_kind=triage_kind,
                started_at=started_at,
                finished_at=finished_at,
                first_delta_ms=first_delta_ms,
                total_ms=total_ms,
                usage_in=usage_in,
                usage_out=usage_out,
                error_code=error_code,
                timings=timings,
                user_text=user_text,
                assistant_text=assistant_text,
                is_private=is_private,
            )
        except Exception:
            _log.exception("persist turn %s failed", ti.turn_id)

    async def _publish_turn_completed(
        self,
        *,
        ti: TurnInput,
        status: TurnStatus,
        triage_kind: TriageKind,
    ) -> None:
        if self._bus is None:
            return
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

    async def _post_turn(
        self,
        ti: TurnInput,
        assistant_text: str,
        started_at: datetime,
        *,
        history_already_persisted: bool = False,
    ) -> None:
        """Background work that runs AFTER DONE was yielded.

        Errors here never reach the user; logged + swallowed.
        """
        policy = TurnRuntimePolicy.from_metadata(ti.metadata)
        if not history_already_persisted:
            try:
                await self._persist_messages(
                    ti,
                    ti.text or "",
                    assistant_text,
                    is_private=policy.mark_messages_private,
                )
            except Exception:
                _log.exception("post-turn: persist failed")
        if not policy.post_turn_side_effects_allowed:
            return
        write_trace = _memory_write_trace(
            ti=ti,
            assistant_text=assistant_text,
            policy=policy,
            mode=self._memory_write_mode,
        )
        if not write_trace["fanout_allowed"]:
            _log.info(
                "post-turn: skipped memory/persona side effects for turn %s reason=%s",
                ti.turn_id,
                write_trace["skipped_reason"],
            )
            return
        try:
            await self._fanout.publish_turn(
                owner_id=ti.caller.owner_id,
                companion_id=ti.caller.companion_id,
                memory_realm_id=ti.caller.memory_realm_id,
                device_id=ti.caller.device_id,
                session_id=ti.session_id,
                turn_id=ti.turn_id,
                user_text=ti.text or "",
                assistant_text=assistant_text,
                timestamp_iso=started_at.isoformat(),
                metadata={
                    "memory_write_disposition": write_trace["disposition"],
                    "memory_write_reason": write_trace["reason"],
                    "memory_policy_version": write_trace["policy_version"],
                    "source_component": "turn_engine",
                    "conversation_id": ti.conversation_id,
                    "persona_template_id": self._persona_template_id,
                    "privacy_mode": policy.privacy.mode,
                },
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
                    owner_id=ti.caller.owner_id,
                    companion_id=ti.caller.companion_id,
                    genome_id=self._persona_template_id,
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


def _tool_answer_announcement(call: ToolCall) -> str:
    """Substantive answer text injected for tool calls that complete async."""
    if call.name == DELEGATE_TO_COWORKER_TOOL:
        return "收到，我已交给后台 coworker 处理，会继续跟进。"
    return ""


def _suppressed_tool_result(call: ToolCall) -> ToolResult:
    return ToolResult(
        call_id=call.id,
        name=call.name,
        ok=False,
        error_code="tool_repeat_suppressed",
        error_message=(
            f"tool {call.name} already failed in this turn; repeated call was "
            "suppressed. Answer the user from the latest available failure/result "
            "instead of retrying the same tool again."
        ),
        metadata={"repeat_suppressed": True},
    )


def _merge_tool_results(
    calls: list[ToolCall],
    results: list[ToolResult],
) -> list[ToolResult]:
    by_call_id = {result.call_id: result for result in results}
    ordered: list[ToolResult] = []
    for call in calls:
        result = by_call_id.get(call.id)
        if result is not None:
            ordered.append(result)
    return ordered


def _handoff_from_tool_result(
    *,
    turn_id: str,
    seq: _SeqGen,
    result: ToolResult,
) -> TurnEvent | None:
    if result.name != DELEGATE_TO_COWORKER_TOOL or not result.ok:
        return None
    content = result.content if isinstance(result.content, dict) else {}
    task_id = content.get("task_id")
    progress_subject = content.get("progress_subject")
    if not task_id or not progress_subject:
        return None
    return TurnEvent(
        turn_id=turn_id,
        seq=seq.next(),
        kind=TurnEventKind.HANDOFF,
        data={"task_id": task_id, "progress_subject": progress_subject},
        ts=time.time(),
    )


def _handoff_summary_from_tool_result(result: ToolResult) -> dict:
    content = result.content if isinstance(result.content, dict) else {}
    return {
        "tool_name": result.name,
        "task_id": content.get("task_id"),
        "accepted": bool(content.get("accepted")),
        "latency_ms": result.latency_ms,
    }


def _update_harness_snapshot_tools(
    ti: TurnInput, names: list[str], *, tool_budget: dict | None = None
) -> None:
    snapshot = dict(ti.metadata.get("harness_snapshot") or {})
    snapshot.setdefault("kind", "realtime_agent_harness")
    tools = {"visible_names": list(names)}
    if tool_budget:
        tools.update(tool_budget)
    snapshot["tools"] = tools
    ti.metadata["harness_snapshot"] = snapshot


def _update_harness_snapshot_handoffs(
    ti: TurnInput,
    handoff_summaries: list[dict],
) -> None:
    snapshot = dict(ti.metadata.get("harness_snapshot") or {})
    snapshot.setdefault("kind", "realtime_agent_harness")
    snapshot["handoffs"] = [dict(item) for item in handoff_summaries]
    ti.metadata["harness_snapshot"] = snapshot


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


def _duration(end_ms: int | None, start_ms: int | None) -> int | None:
    if end_ms is None:
        return None
    return end_ms - (start_ms or 0)


def _memory_write_trace(
    *,
    ti: TurnInput,
    assistant_text: str,
    policy: TurnRuntimePolicy,
    mode: str = "enabled",
) -> dict:
    mode = _normalize_guard_mode(mode)
    disposition = None
    skipped_reason: str | None = None
    if mode == "disabled":
        skipped_reason = "policy_disabled"
    elif not ti.text:
        skipped_reason = "empty_user_text"
    elif not assistant_text:
        skipped_reason = "empty_assistant_text"
    elif not policy.post_turn_side_effects_allowed:
        skipped_reason = "privacy_policy"
    else:
        disposition = classify_memory_write(
            user_text=ti.text or "",
            assistant_text=assistant_text,
        )
        if mode == "shadow":
            skipped_reason = "shadow_only"
        elif disposition.kind is MemoryWriteDispositionKind.SENSITIVE_REQUIRES_CONSENT:
            skipped_reason = "requires_consent"

    if disposition is None and mode != "disabled":
        disposition = classify_memory_write(
            user_text=ti.text or "",
            assistant_text=assistant_text,
        )
    metadata = disposition.to_metadata() if disposition is not None else {}
    return {
        "trace_kind": "memory_write_intent",
        "durable_result": "async_memory_worker",
        "source_turn_id": ti.turn_id,
        "conversation_id": ti.conversation_id,
        "privacy_mode": policy.privacy.mode,
        "mode": mode,
        "shadow_only": mode == "shadow",
        "disposition": disposition.kind.value if disposition is not None else None,
        "reason": disposition.reason if disposition is not None else None,
        "policy_version": metadata.get("memory_policy_version"),
        "fanout_allowed": skipped_reason is None,
        "skipped_reason": skipped_reason,
    }


def _development_guard_trace(
    *,
    ti: TurnInput,
    memory_write_trace: dict,
    max_tool_iters: int,
    tool_schema_strict: bool,
    require_idempotency_for_side_effect_tools: bool,
) -> DevelopmentGuardTrace:
    guards = ti.metadata.get("development_guards") or {}
    return DevelopmentGuardTrace(
        context_budget=guards.get("context_budget"),
        memory_write_policy={
            "mode": memory_write_trace.get("mode"),
            "shadow_only": bool(memory_write_trace.get("shadow_only")),
            "fanout_allowed": bool(memory_write_trace.get("fanout_allowed")),
            "skipped_reason": memory_write_trace.get("skipped_reason"),
            "disposition": memory_write_trace.get("disposition"),
            "policy_version": memory_write_trace.get("policy_version"),
        },
        tool_policy={
            "schema_strict": tool_schema_strict,
            "require_idempotency_for_side_effect_tools": (
                require_idempotency_for_side_effect_tools
            ),
            "max_tool_iters": max_tool_iters,
        },
    )


def _normalize_guard_mode(mode: str) -> str:
    if mode in {"enabled", "shadow", "disabled"}:
        return mode
    _log.warning("unknown development guard mode=%s; falling back to enabled", mode)
    return "enabled"
