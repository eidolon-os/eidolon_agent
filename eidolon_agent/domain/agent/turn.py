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
from datetime import UTC, datetime

from eidolon_sdk.biz.chat_stream import DeltaRole
from eidolon_sdk.biz.presentation import FACE_PROFILE, ResponseIntent
from eidolon_sdk.core.runtime import BackgroundTaskRunner

from eidolon_agent.core.errors import GuardrailBlockedError, TurnCancelledError
from eidolon_agent.core.ports.llm import LLMPort
from eidolon_agent.core.ports.tool import ToolInvocationContext, ToolPort
from eidolon_agent.core.types import (
    ChatMessage,
    DevelopmentGuardTrace,
    Event,
    LatencyBreakdown,
    LLMFinishReason,
    MessageRole,
    PersonaTrace,
    ToolCall,
    ToolResult,
    ToolTrace,
    TurnTrace,
)
from eidolon_agent.core.types.companion_runtime import CompanionRuntimeConfig
from eidolon_agent.core.types.topics import Topics
from eidolon_agent.core.types.turn import (
    FSMState,
    TriageKind,
    TurnEvent,
    TurnEventKind,
    TurnInput,
    TurnStatus,
)
from eidolon_agent.domain.agent.committed_turn import validate_committed_turn
from eidolon_agent.domain.agent.presentation import (
    RESPONSE_SCHEMA,
    RESPONSE_TOOL,
    InvalidPresentationError,
    validate_response,
)
from eidolon_agent.domain.context.compiler import ContextCompiler
from eidolon_agent.domain.guardrails.crisis import CrisisHandler
from eidolon_agent.domain.guardrails.input_filter import InputGuardrail, SafetyAction
from eidolon_agent.domain.guardrails.output_filter import OutputGuardrail
from eidolon_agent.domain.harness import RealtimeAgentHarness
from eidolon_agent.domain.history.fanout import HistoryFanout
from eidolon_agent.domain.history.manager import HistoryManager
from eidolon_agent.domain.runtime_policy import TurnRuntimePolicy
from eidolon_agent.domain.tools.body_capability_provider import RuntimeCapabilityToolProvider
from eidolon_agent.domain.tools.builtin.submit_long_task import (
    DELEGATE_TO_COWORKER_TOOL,
)
from eidolon_agent.domain.tools.dispatcher import ToolDispatcher
from eidolon_agent.domain.tools.visibility import ToolVisibilityPolicy

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
    Multiple concurrent Turns are supported. Memory semantic and privacy
    decisions are delegated to the independent Memory steward rather than
    inferred from wording in this request path.
    """

    def __init__(
        self,
        *,
        compiler: ContextCompiler,
        llm: LLMPort,
        tool_dispatcher: ToolDispatcher,
        history: HistoryManager,
        fanout: HistoryFanout,
        input_guardrail: InputGuardrail,
        output_guardrail: OutputGuardrail,
        crisis: CrisisHandler,
        event_bus=None,
        personas_service=None,
        genome_id: str | None = None,
        max_tool_iters: int = 4,
        memory_write_mode: str = "enabled",
        tool_schema_strict: bool = True,
        require_idempotency_for_side_effect_tools: bool = False,
        taboos_provider=lambda: (),  # () -> tuple[str, ...]
        turn_persister=None,  # async callable; None disables durable turn persistence
        harness: RealtimeAgentHarness | None = None,
        background_tasks: BackgroundTaskRunner | None = None,
        tool_latency_policy: ToolLatencyPolicy | None = None,
        body_capability_provider: RuntimeCapabilityToolProvider | None = None,
    ) -> None:
        self._compiler = compiler
        self._llm = llm
        self._tools = tool_dispatcher
        self._history = history
        self._fanout = fanout
        self._input_g = input_guardrail
        self._output_g = output_guardrail
        self._crisis = crisis
        self._bus = event_bus
        self._personas = personas_service
        self._genome_id = genome_id
        self._max_tool_iters = max_tool_iters
        self._memory_write_mode = _normalize_guard_mode(memory_write_mode)
        self._tool_schema_strict = tool_schema_strict
        self._require_idempotency_for_side_effect_tools = require_idempotency_for_side_effect_tools
        self._taboos_provider = taboos_provider
        self._turn_persister = turn_persister
        self._harness = harness or RealtimeAgentHarness()
        self._background = background_tasks or BackgroundTaskRunner()
        self._tool_latency_policy = tool_latency_policy or ToolLatencyPolicy()
        self._body_capability_provider = body_capability_provider

    async def run(self, ti: TurnInput) -> AsyncIterator[TurnEvent]:
        """Run a single Turn. Yields TurnEvents until DONE or ERROR."""
        seq = _SeqGen()
        started_at = datetime.now(UTC)
        t0 = time.monotonic()
        first_delta_ms: int | None = None
        first_model_activity_ms: int | None = None
        model_activity_counts: dict[str, int] = {}
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
        # Speculative (preemptive) turn: streamed to warm the LLM on a partial
        # transcript, but ephemeral — no history append, no durable turn row,
        # no memory fanout, no persona interaction. Nothing about an unconfirmed
        # guess may leak into memory/history.
        speculative = bool(ti.metadata.get("speculative"))
        presentation_mode = ti.metadata.get("presentation_profile") == FACE_PROFILE
        response_candidate = None
        response_intent = None
        completed_outcomes: dict[str, ToolResult] = {}
        post_turn_allowed = False
        post_turn_scheduled = False
        recent_history_persisted = False

        # Establish the persistence boundary before entering the turn body so
        # every cleanup/finally path has a defined, fail-closed decision.
        committed_turn = validate_committed_turn(
            ti.metadata.get("turn_decision"),
            text=ti.text,
            input_modality=ti.input_modality,
        )
        ti.metadata["committed_turn_decision_valid"] = committed_turn.valid
        ti.metadata["committed_turn_decision_reason"] = committed_turn.reason
        ti.metadata["committed_turn_persistence_allowed"] = committed_turn.persistence_allowed

        try:
            # Voice crosses the Channel→Agent process boundary and may only
            # become history/memory after Channel publishes an exact,
            # transcript-bound commit. Text/Admin input is synchronous and
            # owns its boundary locally. Invalid voice provenance does not
            # block the reply path, only durable side effects.
            # ---- Input guardrail --------------------------------------------
            verdict = self._input_g.check(ti.text)
            ts_guard_ms = int((time.monotonic() - t0) * 1000)
            if verdict.action is SafetyAction.ESCALATE:
                crisis = await self._crisis.handle(
                    companion_id=ti.context.companion_id,
                    owner_id=ti.context.owner_id,
                    locale=ti.context.locale,
                )
                ti.metadata["is_private"] = True
                runtime_policy = TurnRuntimePolicy.from_metadata(ti.metadata)
                assistant_text_for_persist = crisis.text
                yield TurnEvent.state(ti.turn_id, seq.next(), FSMState.SPEAKING, time.time())
                if presentation_mode and not speculative:
                    safe_intent = ResponseIntent(intent="comfort", response_id=f"response:{ti.turn_id}",
                        turn_id=ti.turn_id, session_id=ti.session_id)
                    yield TurnEvent(ti.turn_id, seq.next(), TurnEventKind.PRESENTATION,
                                    safe_intent.model_dump(mode="json"), time.time())
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
                if committed_turn.persistence_allowed:
                    self._background.create(
                        self._persist_messages(
                            ti,
                            ti.text or "",
                            crisis.text,
                            is_private=True,
                        ),
                        name=f"turn-{ti.turn_id}-crisis-history",
                    )
                return
            if verdict.action is SafetyAction.REFUSE:
                refuse = "我不能那样做，不过我可以继续陪你聊点别的。"
                assistant_text_for_persist = refuse
                if presentation_mode and not speculative:
                    safe_intent = ResponseIntent(intent="decline", response_id=f"response:{ti.turn_id}",
                        turn_id=ti.turn_id, session_id=ti.session_id)
                    yield TurnEvent(ti.turn_id, seq.next(), TurnEventKind.PRESENTATION,
                                    safe_intent.model_dump(mode="json"), time.time())
                yield TurnEvent.delta(ti.turn_id, seq.next(), refuse, time.time())
                yield TurnEvent.done(ti.turn_id, seq.next(), TurnStatus.OK, time.time())
                return
            # A committed user turn is the only input to long-term extraction.
            # Publish it independently of context compilation, LLM generation,
            # tools and TTS; the Memory service owns semantic relevance,
            # sensitivity and projection decisions. Assistant output is never
            # evidence for this event.
            memory_observation = _memory_write_trace(
                ti=ti,
                policy=runtime_policy,
                mode=self._memory_write_mode,
            )
            ti.metadata["memory_write_trace"] = memory_observation
            if not speculative and memory_observation["fanout_allowed"]:
                ti.metadata["memory_observation_scheduled"] = True
                if getattr(self._fanout, "is_durable", False):
                    enqueue_started = time.monotonic()
                    await self._publish_memory_observation(ti, started_at)
                    ti.metadata["memory_observation_enqueue_ms"] = int(
                        (time.monotonic() - enqueue_started) * 1000
                    )
                else:
                    self._background.create(
                        self._publish_memory_observation(ti, started_at),
                        name=f"turn-{ti.turn_id}-memory-observation",
                    )

            # Tool use and delegation are selected by the model from typed
            # schemas. Keep the legacy trace field stable without interpreting
            # the user's words in a second, phrase-based router.
            ts_triage_ms = int((time.monotonic() - t0) * 1000)
            yield TurnEvent.state(ti.turn_id, seq.next(), FSMState.THINKING, time.time())

            # ``COMPLEX_LONG`` remains a trace signal, but coworker delegation
            # is unified through the LLM tool-call path via
            # ``delegate_to_coworker``. That keeps parameter extraction, tool
            # announcements, and result handling on one path.

            # ---- Compile context -------------------------------------------
            messages = await self._compiler.compile(ti)
            ts_compile_ms = int((time.monotonic() - t0) * 1000)

            # ---- Per-companion runtime config (model / tools / policy) -----
            # Same code, differentiated per companion: model routing + tool
            # allow/deny + policy toggles come from companions.runtime_config_json.
            cfg = ti.runtime_config

            # ---- LLM stream (with tool loop) -------------------------------
            tools, extra_tools = await self._tool_schemas(ti, cfg)
            if presentation_mode:
                if any(tool.name == RESPONSE_TOOL for tool in tools):
                    raise ValueError("RESERVED_RESPONSE_TOOL_COLLISION")
                tools = [*tools, RESPONSE_SCHEMA]
                messages = [*messages, ChatMessage(
                    id=uuid.uuid4().hex, role=MessageRole.SYSTEM,
                    content=RESPONSE_SCHEMA.description, created_at=datetime.now(UTC),
                )]
            tool_budget = self._harness.tool_schema_budget(tools)
            ti.metadata.setdefault("development_guards", {})["tool_schema_budget"] = tool_budget
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
                async for delta in self._llm.stream(
                    messages,
                    tools=tools,
                    model=cfg.model,
                    temperature=cfg.temperature,
                    **({"max_tokens": cfg.max_output_tokens} if cfg.max_output_tokens else {}),
                    request_id=ti.turn_id,
                ):
                    if delta.activity is not None:
                        activity_kind = delta.activity.value
                        if first_model_activity_ms is None:
                            first_model_activity_ms = int((time.monotonic() - t0) * 1000)
                        model_activity_counts[activity_kind] = (
                            model_activity_counts.get(activity_kind, 0) + 1
                        )
                        yield TurnEvent(
                            turn_id=ti.turn_id,
                            seq=seq.next(),
                            kind=TurnEventKind.PROGRESS,
                            data={"phase": "model", "kind": activity_kind},
                            ts=time.time(),
                        )
                    if delta.text_delta and not presentation_mode:
                        if first_delta_ms is None:
                            first_delta_ms = int((time.monotonic() - t0) * 1000)
                        assistant_text_parts.append(delta.text_delta)
                        yield TurnEvent.delta(ti.turn_id, seq.next(), delta.text_delta, time.time())
                    if delta.tool_call is not None:
                        tool_calls.append(delta.tool_call)
                    if delta.tool_call is not None and (not presentation_mode or delta.tool_call.name != RESPONSE_TOOL):
                        answer_announcement = None if presentation_mode else _tool_answer_announcement(delta.tool_call, ti)
                        if answer_announcement and answer_announcement not in announced_answers:
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

                terminal_calls = [call for call in tool_calls if call.name == RESPONSE_TOOL] if presentation_mode else []
                if terminal_calls:
                    if len(tool_calls) != 1 or finish_reason is not LLMFinishReason.TOOL_CALLS:
                        raise InvalidPresentationError("TERMINAL_RESPONSE_MUST_BE_COMPLETE_AND_ALONE")
                    response_candidate, response_intent = validate_response(
                        terminal_calls[0].arguments, turn_id=ti.turn_id,
                        session_id=ti.session_id, outcomes=completed_outcomes,
                    )
                    break
                if finish_reason is LLMFinishReason.LENGTH:
                    ti.metadata["output_truncated"] = True
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
                        if tool_failure_counts.get(call.name, 0) >= _MAX_FAILURES_PER_TOOL_PER_TURN:
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
                                    turn_context=ti.context,
                                    input_modality=ti.input_modality,
                                    turn_id=ti.turn_id,
                                    conversation_id=ti.conversation_id,
                                    session_id=ti.session_id,
                                    user_text=ti.text or "",
                                    companion_id=ti.context.companion_id,
                                    memory_realm_id=ti.context.memory_realm_id,
                                    extra_tools=extra_tools,
                                    denied_tools=cfg.tool_deny,
                                ),
                            ),
                            name=f"turn-{ti.turn_id}-tool-dispatch",
                        )
                        try:
                            slow_hint_delay_s = self._tool_latency_policy.slow_hint_delay_s
                            if (
                                not slow_tool_hint_emitted and not presentation_mode
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
                                        first_delta_ms = int((time.monotonic() - t0) * 1000)
                                    yield TurnEvent.delta(
                                        ti.turn_id,
                                        seq.next(),
                                        _persona_phrase(
                                            ti,
                                            "slow_tool_hint",
                                            self._tool_latency_policy.slow_hint_text,
                                        ),
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
                            tool_failure_counts[r.name] = tool_failure_counts.get(r.name, 0) + 1
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
                    now = datetime.now(UTC)
                    tool_result_messages: list[ChatMessage] = []
                    for r in results:
                        completed_outcomes[r.call_id] = r
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
                            handoff_summaries.append(_handoff_summary_from_tool_result(r))
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
            if presentation_mode and response_intent is None:
                raise InvalidPresentationError("STRUCTURED_RESPONSE_REQUIRED")
            final_text = (response_candidate.public_text or "") if response_candidate else "".join(assistant_text_parts)
            out_v = self._output_g.check(output_text=final_text, taboos=self._taboos_provider())
            if out_v.action is SafetyAction.SOFTEN:
                # Crude soften: prefix; production would re-prompt LLM. The
                # prefix is persona-overridable via the genome's spoken_phrases.
                soften_prefix = _persona_phrase(ti, "soften_prefix", "（让我换个说法）")
                final_text = soften_prefix + final_text
                if not presentation_mode:
                    yield TurnEvent.delta(ti.turn_id, seq.next(), soften_prefix, time.time())
            if response_intent is not None and not speculative:
                # The event is semantic intent, not a claim that the screen has
                # displayed it. Channel owns the subsequent presentation receipt.
                ti.metadata["response_intent"] = response_intent.model_dump(mode="json")
                ti.metadata["presentation_delivery"] = "unconfirmed"
                feedback = ti.presentation_feedback
                if feedback is not None and response_intent.intent != "none":
                    feedback.expect(response_intent)
                yield TurnEvent(ti.turn_id, seq.next(), TurnEventKind.PRESENTATION,
                                response_intent.model_dump(mode="json"), time.time())
                receipt = await feedback.wait() if feedback is not None and response_intent.intent != "none" else None
                if receipt is not None:
                    ti.metadata["presentation_delivery"] = receipt.status
                    ti.metadata["presentation_receipt"] = receipt.model_dump(mode="json")
                if final_text:
                    yield TurnEvent.delta(ti.turn_id, seq.next(), final_text, time.time())
                # Keep history honest for a nonverbal response. Public text is
                # optional renderable content; device delivery is not yet known.
                delivery_state = ti.metadata["presentation_delivery"]
                final_text = f"[表达意图：{response_intent.intent}；设备展示状态：{delivery_state}]"
            assistant_text_for_persist = final_text
            ts_output_ms = int((time.monotonic() - t0) * 1000)
            # Speculative turns never persist/fan out (unconfirmed guess).
            post_turn_allowed = not speculative and committed_turn.persistence_allowed

            # Keep recent history deterministic for the next turn. This is a
            # cheap in-memory append, not durable persistence; durable SQLite
            # writes and external fanout remain after DONE.
            if not speculative and committed_turn.persistence_allowed:
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
                first_activity_at=first_model_activity_ms,
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

            # User has their answer; durable persistence can take its time.
            # Speculative turns are ephemeral — skip entirely.
            if not speculative and committed_turn.persistence_allowed:
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
            # post-turn path (skipped when nothing heard, or when speculative —
            # an unconfirmed guess never persists even the part that streamed).
            if heard_text and not speculative and committed_turn.persistence_allowed:
                post_turn_allowed = True
            yield TurnEvent.error(ti.turn_id, seq.next(), error_code, "cancelled", time.time())
            raise
        except InvalidPresentationError as exc:
            status = TurnStatus.ERRORED
            error_code = "invalid_presentation"
            yield TurnEvent.error(ti.turn_id, seq.next(), error_code, str(exc), time.time())
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
            # turn_persister is injected, or for speculative (ephemeral) turns.
            if self._turn_persister is not None and not speculative:
                total_ms = int((time.monotonic() - t0) * 1000)
                memory_write_trace = _memory_write_trace(
                    ti=ti,
                    policy=runtime_policy,
                    mode=self._memory_write_mode,
                )
                ti.metadata["memory_write_trace"] = memory_write_trace
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
                    input_modality=ti.input_modality,
                    model=getattr(self._llm, "model_id", None),
                    trace_id=ti.context.trace_id,
                    termination_cause=ti.metadata.get("termination_cause"),
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
                    commitment_context_trace=ti.metadata.get("commitment_context_trace"),
                    memory_recall_query=ti.metadata.get("memory_recall_query"),
                    memory_write_trace=memory_write_trace,
                    tool_trace=tool_trace,
                    persona=PersonaTrace(
                        companion_id=ti.context.companion_id,
                        genome_id=self._genome_id,
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
                    stale_generation_dropped=int(ti.metadata.get("stale_generation_dropped") or 0),
                    tool_repeat_suppressed_count=tool_repeat_suppressed_count,
                    development_guards=development_guards,
                    usage={"tokens_in": usage_in, "tokens_out": usage_out},
                ).to_metadata()
                timings = {
                    "guard_ms": ts_guard_ms,
                    "triage_ms": ts_triage_ms,
                    "compile_ms": ts_compile_ms,
                    "first_delta_ms": first_delta_ms,
                    "first_model_activity_ms": first_model_activity_ms,
                    "model_activity_counts": dict(model_activity_counts),
                    "output_ms": ts_output_ms,
                    "tool_ms": tool_ms_total,
                    "context_ledger": ti.metadata.get("context_ledger"),
                    "memory_trace": ti.metadata.get("memory_trace"),
                    "commitment_context_trace": ti.metadata.get("commitment_context_trace"),
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
                    "stale_generation_dropped": ti.metadata.get("stale_generation_dropped") or 0,
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
                        finished_at=datetime.now(UTC),
                        first_delta_ms=first_delta_ms,
                        total_ms=total_ms,
                        usage_in=usage_in,
                        usage_out=usage_out,
                        error_code=error_code,
                        timings=timings,
                        user_text=(ti.text or "") if committed_turn.persistence_allowed else "",
                        assistant_text=(
                            assistant_text_for_persist if committed_turn.persistence_allowed else ""
                        ),
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
            if self._bus is not None and not speculative and committed_turn.persistence_allowed:
                self._background.create(
                    self._publish_turn_completed(
                        ti=ti,
                        status=status,
                        triage_kind=triage_kind,
                    ),
                    name=f"turn-{ti.turn_id}-completed-event",
                )

    # ---- helpers -------------------------------------------------------------

    async def _tool_schemas(
        self, ti: TurnInput, cfg: CompanionRuntimeConfig
    ) -> tuple[list, dict[str, ToolPort]]:
        """Per-turn, context-aware tool assembly (the F1/F2 junction).

        Filters the global static tools by this companion's allow/deny policy,
        then appends one dynamic tool per compatible online capability contract.
        Companion names are tool targets; physical provider lookup remains in
        the body-control service. Returns the LLM-visible schemas plus the
        per-turn dynamic tool overlay resolved by name at dispatch.
        """
        schemas = ToolVisibilityPolicy.filter(
            self._tools.list_schemas(),
            allow=cfg.tool_allow,
            deny=cfg.tool_deny,
        )
        extra_tools: dict[str, ToolPort] = {}
        if cfg.allow_body_control and self._body_capability_provider is not None:
            cap_schemas, cap_ports = await self._body_capability_provider.assemble(ti.context)
            # deny applies to synthetic tools too: drop from BOTH schemas and overlay
            # so a denied capability can neither be seen nor actuated.
            if cfg.tool_deny:
                cap_schemas = [s for s in cap_schemas if s.name not in cfg.tool_deny]
                cap_ports = {n: p for n, p in cap_ports.items() if n not in cfg.tool_deny}
            schemas = schemas + cap_schemas
            extra_tools.update(cap_ports)
        visible = self._harness.visible_tool_schemas(schemas)
        visible_names = {schema.name for schema in visible}
        extra_tools = {name: port for name, port in extra_tools.items() if name in visible_names}
        return visible, extra_tools

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
                    trace_id=ti.context.trace_id,
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
        """Persist recent history after terminal paths that did not do so inline.

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
        del started_at

    async def _publish_memory_observation(
        self,
        ti: TurnInput,
        started_at: datetime,
    ) -> None:
        """Publish canonical user evidence without joining the reply path."""

        policy = TurnRuntimePolicy.from_metadata(ti.metadata)
        try:
            status = await self._fanout.publish_turn(
                owner_id=ti.context.owner_id,
                companion_id=ti.context.companion_id,
                memory_realm_id=ti.context.memory_realm_id,
                device_id=ti.context.device_id,
                session_id=ti.session_id,
                turn_id=ti.turn_id,
                user_text=ti.text or "",
                assistant_text="",
                timestamp_iso=started_at.isoformat(),
                trace_id=ti.context.trace_id,
                metadata={
                    "memory_ingest_policy": "semantic_steward",
                    "source_component": "turn_observer",
                    "conversation_id": ti.conversation_id,
                    "genome_id": self._genome_id,
                    "privacy_mode": policy.privacy.mode,
                },
            )
            ti.metadata["memory_observation_state"] = getattr(status, "state", None)
            if getattr(status, "error", None):
                ti.metadata["memory_observation_error"] = status.error
        except Exception:
            ti.metadata["memory_observation_state"] = "enqueue_failed"
            _log.exception("memory observation publish failed")

    async def _persist_messages(
        self,
        ti: TurnInput,
        user_text: str,
        assistant_text: str,
        *,
        is_private: bool = False,
    ) -> None:
        now = datetime.now(UTC)
        if user_text:
            await self._history.append(
                conversation_id=ti.conversation_id,
                message=ChatMessage(
                    id=_turn_message_id(ti.turn_id, 0),
                    role=MessageRole.USER,
                    content=user_text,
                    created_at=now,
                    metadata={
                        "turn_id": ti.turn_id,
                        "seq_in_turn": 0,
                        **({"is_private": True} if is_private else {}),
                    },
                ),
            )
        if assistant_text:
            await self._history.append(
                conversation_id=ti.conversation_id,
                message=ChatMessage(
                    id=_turn_message_id(ti.turn_id, 1),
                    role=MessageRole.ASSISTANT,
                    content=assistant_text,
                    created_at=now,
                    metadata={
                        "turn_id": ti.turn_id,
                        "seq_in_turn": 1,
                        **({"is_private": True} if is_private else {}),
                    },
                ),
            )


def _turn_message_id(turn_id: str, seq_in_turn: int) -> str:
    return f"msg_{turn_id}_{seq_in_turn}"


class _SeqGen:
    __slots__ = ("_n",)

    def __init__(self) -> None:
        self._n = -1

    def next(self) -> int:
        self._n += 1
        return self._n


def _tool_answer_announcement(call: ToolCall, ti: TurnInput) -> str:
    """Substantive answer text injected for tool calls that complete async."""
    if call.name == DELEGATE_TO_COWORKER_TOOL:
        return _persona_phrase(
            ti,
            "coworker_delegated",
            "收到，我已交给后台 coworker 处理，会继续跟进。",
        )
    return ""


def _persona_phrase(ti: TurnInput, key: str, default: str) -> str:
    """Genome-overridable hot-path canned line (template lookup, no LLM).

    Reads ``persona_spoken_phrases`` stashed on the TurnInput by the context
    compiler. Pre-compile paths (refuse / forget ack) have no persona loaded
    yet, so they fall back to ``default``.
    """
    phrases = ti.metadata.get("persona_spoken_phrases")
    if isinstance(phrases, dict):
        value = phrases.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return default


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
    first_activity_at: int | None,
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
        "llm_activity=%s llm_ttft=%s output=%s tokens_in=%d tokens_out=%d",
        turn_id,
        total_ms,
        guard_ms,
        triage_ms,
        compile_ms,
        _delta(first_activity_at, compile_at),
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
    policy: TurnRuntimePolicy,
    mode: str = "enabled",
) -> dict:
    mode = _normalize_guard_mode(mode)
    skipped_reason: str | None = None
    if mode == "disabled":
        skipped_reason = "policy_disabled"
    elif (
        ti.input_modality == "voice"
        and ti.metadata.get("committed_turn_persistence_allowed") is not True
    ):
        skipped_reason = "uncommitted_voice_turn"
    elif not ti.text:
        skipped_reason = "empty_user_text"
    elif not policy.post_turn_side_effects_allowed:
        skipped_reason = "privacy_policy"
    elif mode == "shadow":
        skipped_reason = "shadow_only"
    return {
        "trace_kind": "memory_turn_observation",
        "durable_result": "async_memory_worker",
        "source_turn_id": ti.turn_id,
        "conversation_id": ti.conversation_id,
        "privacy_mode": policy.privacy.mode,
        "mode": mode,
        "shadow_only": mode == "shadow",
        "ingest_policy": "semantic_steward",
        "fanout_allowed": skipped_reason is None,
        "skipped_reason": skipped_reason,
        "enqueue_state": ti.metadata.get("memory_observation_state"),
        "enqueue_ms": ti.metadata.get("memory_observation_enqueue_ms"),
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
            "ingest_policy": memory_write_trace.get("ingest_policy"),
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
