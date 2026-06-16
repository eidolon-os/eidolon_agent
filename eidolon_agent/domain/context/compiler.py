"""Direct prompt compilation — no pluggable providers.

The hot path is fixed: persona prompt + realtime harness policy + memory
recall + realtime digest as a single system message, recent history as
user/assistant messages, current user input as the trailing user message.
If a future segment is needed it goes here, not behind an abstraction.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from datetime import datetime, timezone

from eidolon_agent.core.types.memory import MemoryQueryPlan
from eidolon_agent.core.types.messages import ChatMessage, MessageRole
from eidolon_agent.core.types.turn import TurnInput
from eidolon_agent.domain.context.types import (
    ContextBudget,
    ContextLedger,
    ContextSegment,
    ContextSegmentKind,
)
from eidolon_agent.domain.harness import (
    HARNESS_POLICY_SOURCE,
    RealtimeAgentHarness,
)
from eidolon_agent.domain.runtime_policy import TurnRuntimePolicy

_log = logging.getLogger(__name__)


class ContextCompiler:
    """Assemble the LLM message list for a Turn.

    Locator returns ``(instance_id, template_id)`` for a tenant/user/conv
    triple. Memory port is optional — when omitted, memory recall is skipped.
    """

    def __init__(
        self,
        *,
        personas_service,
        instance_locator,
        history_manager,
        memory_port=None,
        history_window: int = 20,
        memory_timeout_s: float = 0.2,
        memory_top_k: int = 5,
        context_budget_tokens: int | None = None,
        context_budget_mode: str = "enabled",
        harness: RealtimeAgentHarness | None = None,
    ) -> None:
        self._personas = personas_service
        self._locator = instance_locator
        self._history = history_manager
        self._memory = memory_port
        self._history_window = history_window
        self._memory_timeout_s = memory_timeout_s
        self._memory_top_k = memory_top_k
        self._context_budget_tokens = context_budget_tokens
        self._context_budget_mode = _normalize_budget_mode(context_budget_mode)
        self._harness = harness or RealtimeAgentHarness()

    async def compile(self, ti: TurnInput) -> list[ChatMessage]:
        instance_id, template_id = self._locator(
            ti.caller.tenant_id, ti.caller.user_id, ti.conversation_id
        )
        policy = TurnRuntimePolicy.from_metadata(ti.metadata)

        # ---- Run the three independent fetches concurrently ----------------
        # Persona compile, memory recall, and history window have no data
        # dependency on each other; awaiting them sequentially costs ~250ms
        # in production. ``return_exceptions=True`` keeps a single fetch
        # failure from poisoning the others — each branch handles its own
        # degraded path below.
        compile_t0 = time.monotonic()
        persona_task = _timed(
            "persona",
            self._personas.compile_prompt(
                tenant_id=ti.caller.tenant_id,
                user_id=ti.caller.user_id,
                instance_id=instance_id,
                template_id=template_id,
                user_text=ti.text or "",
                realtime=_realtime_dict(ti.realtime),
                dry_run_memory=[],
            ),
        )
        memory_task = _timed("memory", self._memory_recall(ti))
        history_coro = (
            _empty_history()
            if not policy.history_context_allowed
            else self._history.recent_window(
                conversation_id=ti.conversation_id,
                window=self._history_window,
            )
        )
        history_task = _timed("history", history_coro)

        results = await asyncio.gather(
            persona_task, memory_task, history_task, return_exceptions=True
        )
        # Each _timed task yields (value, elapsed_ms) on success; on exception
        # asyncio.gather replaces the tuple with the exception itself.
        persona_res, memory_res, history_res = results

        def _unpack(res):  # type: ignore[no-untyped-def]
            if isinstance(res, BaseException):
                return res, None
            return res

        persona, persona_ms = _unpack(persona_res)
        memory_payload, memory_ms = _unpack(memory_res)
        history, history_ms = _unpack(history_res)

        gather_ms = int((time.monotonic() - compile_t0) * 1000)
        _log.info(
            "compile_timings conv=%s gather_ms=%d persona=%s memory=%s history=%s",
            ti.conversation_id,
            gather_ms,
            persona_ms,
            memory_ms,
            history_ms,
        )

        # Persona is the only segment we cannot proceed without — re-raise
        # to surface configuration errors instead of silently degrading.
        if isinstance(persona, BaseException):
            raise persona

        segments: list[ContextSegment] = []
        system_parts_by_segment: dict[int, str] = {}
        history_by_segment: dict[int, ChatMessage] = {}
        degraded_sources: list[str] = []

        persona_segment = ContextSegment(
            kind=ContextSegmentKind.PERSONA,
            content="",
            source="personas_service",
            token_estimate=_estimate_tokens(persona.system_prompt),
            droppable=False,
        )
        segments.append(persona_segment)
        system_parts_by_segment[id(persona_segment)] = persona.system_prompt

        harness_policy = self._harness.policy_prompt()
        harness_policy_segment = ContextSegment(
            kind=ContextSegmentKind.HARNESS_POLICY,
            content="",
            source=HARNESS_POLICY_SOURCE,
            token_estimate=_estimate_tokens(harness_policy),
            droppable=False,
        )
        segments.append(harness_policy_segment)
        system_parts_by_segment[id(harness_policy_segment)] = harness_policy

        memory_text: str | None = None
        memory_degraded = False
        memory_degraded_reason: str | None = None
        memory_hit_ids: list[str] = []
        memory_kg_triple_ids: list[str] = []
        if isinstance(memory_payload, BaseException):
            _log.warning("memory recall raised: %s", memory_payload)
            memory_degraded = True
            memory_degraded_reason = _exception_degraded_reason(memory_payload)
        elif memory_payload:
            (
                memory_text,
                memory_degraded,
                memory_hit_ids,
                memory_degraded_reason,
                memory_kg_triple_ids,
            ) = memory_payload

        if memory_degraded:
            degraded_sources.append("memory")
        if memory_degraded and not memory_text:
            memory_text = self._MEMORY_DEGRADED_NOTICE
        memory_segment: ContextSegment | None = None
        if memory_text:
            memory_segment = ContextSegment(
                kind=ContextSegmentKind.MEMORY,
                content="",
                source="memory",
                token_estimate=_estimate_tokens(memory_text),
                priority=80,
                droppable=not memory_degraded,
                metadata={"degraded": memory_degraded},
            )
            segments.append(memory_segment)
            system_parts_by_segment[id(memory_segment)] = f"[MEMORY]\n{memory_text}"

        # ---- Realtime digest (signals from voice pipeline) ------------------
        if ti.realtime is not None:
            line = _realtime_line(ti.realtime)
            if line:
                realtime_segment = ContextSegment(
                    kind=ContextSegmentKind.REALTIME,
                    content="",
                    source="turn_input.realtime",
                    token_estimate=_estimate_tokens(line),
                    priority=90,
                )
                segments.append(realtime_segment)
                system_parts_by_segment[id(realtime_segment)] = f"（实时信号：{line}）"

        if isinstance(history, BaseException):
            _log.warning("history window raised: %s", history)
            history = []
        elif history:
            for idx, msg in enumerate(history):
                history_segment = ContextSegment(
                    kind=ContextSegmentKind.HISTORY,
                    content="",
                    source="history_manager",
                    token_estimate=_estimate_tokens(msg.content),
                    # Keep newer history first when budget is tight.
                    priority=60 + idx,
                    metadata={"message_id": msg.id, "role": msg.role.value},
                )
                segments.append(history_segment)
                history_by_segment[id(history_segment)] = msg

        current_user_segment: ContextSegment | None = None
        if ti.text:
            current_user_segment = ContextSegment(
                kind=ContextSegmentKind.CURRENT_USER,
                content="",
                source="turn_input.text",
                token_estimate=_estimate_tokens(ti.text),
                droppable=False,
            )
            segments.append(current_user_segment)

        kept_segments, ledger, budget_guard = self._apply_budget(segments)
        for source in degraded_sources:
            ledger.mark_degraded(source)
        kept_ids = {id(seg) for seg in kept_segments}
        memory_kept = memory_segment is not None and id(memory_segment) in kept_ids
        ti.metadata["memory_trace"] = {
            "attempted": (
                self._memory is not None
                and bool(ti.text)
                and policy.memory_recall_allowed
            ),
            "skipped_reason": (
                "privacy_policy"
                if self._memory is not None and bool(ti.text) and not policy.memory_recall_allowed
                else None
            ),
            "degraded": memory_degraded,
            "degraded_reason": memory_degraded_reason,
            "elapsed_ms": memory_ms,
            "timeout_ms": int(self._memory_timeout_s * 1000),
            "hit_ids": memory_hit_ids,
            "hit_count": len(memory_hit_ids),
            "kg_triple_ids": memory_kg_triple_ids,
            "kg_triple_count": len(memory_kg_triple_ids),
            "context_injected": memory_kept,
        }

        # ---- Assemble messages ---------------------------------------------
        now = datetime.now(timezone.utc)
        system_parts = [
            system_parts_by_segment[id(seg)]
            for seg in kept_segments
            if id(seg) in system_parts_by_segment
        ]
        kept_history = [
            history_by_segment[id(seg)]
            for seg in kept_segments
            if id(seg) in history_by_segment
        ]
        out: list[ChatMessage] = [
            ChatMessage(
                id=uuid.uuid4().hex,
                role=MessageRole.SYSTEM,
                content="\n\n".join(system_parts),
                created_at=now,
            ),
            *kept_history,
        ]
        if current_user_segment is not None and id(current_user_segment) in kept_ids:
            out.append(
                ChatMessage(
                    id=uuid.uuid4().hex,
                    role=MessageRole.USER,
                    content=ti.text,
                    created_at=now,
                )
            )
        ti.metadata["context_ledger"] = ledger.to_metadata()
        ti.metadata.setdefault("development_guards", {})["context_budget"] = budget_guard
        ti.metadata["harness_snapshot"] = self._harness.snapshot(
            segment_kinds=[seg.kind.value for seg in kept_segments],
            budget=budget_guard,
            memory={
                "attempted": ti.metadata["memory_trace"]["attempted"],
                "degraded": memory_degraded,
                "degraded_reason": memory_degraded_reason,
                "context_injected": memory_kept,
                "hit_count": len(memory_hit_ids),
                "kg_triple_count": len(memory_kg_triple_ids),
            },
            history={
                "allowed": policy.history_context_allowed,
                "message_count": len(kept_history),
            },
        ).to_metadata()
        return out

    def _apply_budget(
        self, segments: list[ContextSegment]
    ) -> tuple[list[ContextSegment], ContextLedger, dict]:
        unbudgeted = ContextLedger(kept_segments=list(segments))
        if self._context_budget_tokens is None:
            return segments, unbudgeted, {
                "mode": "disabled",
                "configured": False,
                "applied": False,
                "max_tokens": None,
                "kept_token_estimate": unbudgeted.total_token_estimate,
                "dropped_count": 0,
                "shadow_dropped_count": 0,
                "shadow_dropped_kinds": [],
            }

        budget = ContextBudget(max_tokens=self._context_budget_tokens)
        pruned_segments, pruned_ledger = budget.prune(segments)
        guard = {
            "mode": self._context_budget_mode,
            "configured": True,
            "applied": self._context_budget_mode == "enabled",
            "max_tokens": self._context_budget_tokens,
            "kept_token_estimate": (
                pruned_ledger.total_token_estimate
                if self._context_budget_mode == "enabled"
                else unbudgeted.total_token_estimate
            ),
            "dropped_count": (
                len(pruned_ledger.dropped_segments)
                if self._context_budget_mode == "enabled"
                else 0
            ),
            "shadow_dropped_count": len(pruned_ledger.dropped_segments),
            "shadow_dropped_kinds": [
                seg.kind.value for seg in pruned_ledger.dropped_segments
            ],
        }
        if self._context_budget_mode == "enabled":
            return pruned_segments, pruned_ledger, guard
        return segments, unbudgeted, guard

    # Injected into the system prompt when memory recall raises. Tells the
    # LLM not to confabulate prior context — degraded honestly beats
    # silently-amnesiac-pretending-to-remember.
    _MEMORY_DEGRADED_NOTICE = (
        "（系统提示：本轮 memory backend 暂不可达,你没有任何过往记忆访问权。"
        "请如实承认这点,不要假装记得用户之前说过的事;"
        "也不要主动声称会记住用户接下来说的——因为本轮记忆链路是断的。）"
    )

    async def _memory_recall(
        self, ti: TurnInput
    ) -> tuple[str | None, bool, list[str], str | None, list[str]] | None:
        """Memory recall branch for the parallel ``gather`` above.

        Three return shapes:
          - ``None``: memory was not attempted (port absent, no text). No
            block goes into the system prompt.
          - non-empty hit string: normal recall produced context. Gets
            injected as ``[MEMORY]\\n<block>``.
          - ``_MEMORY_DEGRADED_NOTICE``: recall raised. The LLM is told
            in-prompt that memory is down for this turn, so it won't
            silently confabulate "as you mentioned earlier...". This
            replaces the previous silent ``None`` fallback whose net
            effect was an amnesiac-but-confident assistant — exactly
            the failure mode that motivated this change.

        The compile() outer loop treats all three the same way (truthy →
        append to system_parts) so no caller code changes.
        """
        if self._memory is None or not ti.text:
            return None
        if not TurnRuntimePolicy.from_metadata(ti.metadata).memory_recall_allowed:
            return None
        try:
            plan = MemoryQueryPlan(
                episodic_query=ti.text,
                semantic_query=ti.text,
                episodic_k=3,
                semantic_k=self._memory_top_k,
                voice=ti.caller.caller_kind.value == "livekit_voice",
            )
            recall_query, query_source = await self._memory_recall_query(ti)
            ti.metadata["memory_recall_query"] = {
                "source": query_source,
                "preview": recall_query[:160],
            }
            recall = await self._memory.recall_context(
                user_id=ti.caller.user_id,
                query=recall_query,
                plan=plan,
                timeout_s=self._memory_timeout_s,
            )
            formatted, hits, _degraded = recall
            kg_triples = getattr(recall, "kg_triples", []) or []
            return (
                formatted or None,
                bool(_degraded),
                [h.id for h in hits],
                getattr(recall, "degraded_reason", None),
                _kg_triple_ids(kg_triples),
            )
        except Exception as exc:
            _log.exception(
                "memory recall failed for user=%s; injecting degraded notice "
                "into system prompt",
                ti.caller.user_id,
            )
            return (
                self._MEMORY_DEGRADED_NOTICE,
                True,
                [],
                _exception_degraded_reason(exc),
                [],
            )

    async def _memory_recall_query(self, ti: TurnInput) -> tuple[str, str]:
        """Build a recall query with a tiny history peek for anaphora.

        Users often continue with "它/他/再找找/你忘了吗" after naming the
        entity in the previous turn. KG recall only sees the query string, so
        current text alone can miss the entity. Keep the peek short and
        timeout-bounded so voice TTFT still belongs to the memory backend, not
        to SQLite/history hydration.
        """

        user_text = (ti.text or "").strip()
        if not TurnRuntimePolicy.from_metadata(ti.metadata).history_context_allowed:
            return user_text, "current_only"
        try:
            recent = await asyncio.wait_for(
                self._history.recent_window(
                    conversation_id=ti.conversation_id,
                    window=min(4, self._history_window),
                ),
                timeout=0.025,
            )
        except Exception:
            return user_text, "current_only_history_peek_failed"
        if not recent:
            return user_text, "current_only"
        snippets: list[str] = []
        for msg in recent[-4:]:
            content = (msg.content or "").strip()
            if not content:
                continue
            role = "用户" if msg.role is MessageRole.USER else "你"
            snippets.append(f"{role}: {_truncate_for_query(content)}")
        if not snippets:
            return user_text, "current_only"
        return "\n".join([*snippets, f"当前用户: {user_text}"]), "current_plus_recent_history"


async def _timed(_name: str, coro):  # type: ignore[no-untyped-def]
    """Wrap a coroutine to also return its elapsed ms.

    Returns ``(value, elapsed_ms)``. Exceptions propagate untouched; the
    asyncio.gather caller (with ``return_exceptions=True``) records them as
    the gather result and the timing is lost — which is fine, we only care
    about the success path for diagnostics.
    """
    t0 = time.monotonic()
    value = await coro
    return value, int((time.monotonic() - t0) * 1000)


async def _empty_history() -> list[ChatMessage]:
    return []


def _exception_degraded_reason(exc: BaseException) -> str:
    details = getattr(exc, "details", None)
    if isinstance(details, dict):
        reason = details.get("reason")
        if isinstance(reason, str) and reason:
            return reason
    return "error"


def _realtime_dict(digest) -> dict | None:  # type: ignore[no-untyped-def]
    if digest is None:
        return None
    return {
        "dominant_emotion": digest.dominant_emotion,
        "emotion_confidence": digest.emotion_confidence,
        "speech_rate": digest.speech_rate,
        "presence": digest.presence,
        "confidence_overall": digest.confidence_overall,
        "notable_events": list(digest.notable_events),
    }


def _realtime_line(digest) -> str:  # type: ignore[no-untyped-def]
    """Compact one-line summary of a SignalDigest."""
    parts: list[str] = []
    if digest.dominant_emotion:
        parts.append(f"{digest.dominant_emotion}({digest.emotion_confidence:.2f})")
    if digest.speech_rate:
        parts.append(f"语速{digest.speech_rate}")
    if digest.presence and digest.presence != "present":
        parts.append(digest.presence)
    if digest.notable_events:
        parts.extend(digest.notable_events)
    return " | ".join(parts)


def _normalize_budget_mode(mode: str) -> str:
    if mode in {"enabled", "shadow", "disabled"}:
        return mode
    _log.warning("unknown context_budget_mode=%s; falling back to enabled", mode)
    return "enabled"


def _estimate_tokens(text: str) -> int:
    return max(1, len(text) // 3) if text else 0


def _truncate_for_query(text: str, limit: int = 120) -> str:
    text = text.strip()
    return text if len(text) <= limit else text[:limit] + "…"


def _kg_triple_ids(triples: object) -> list[str]:
    if not isinstance(triples, list):
        return []
    ids: list[str] = []
    for triple in triples:
        if isinstance(triple, dict):
            triple_id = triple.get("id")
        else:
            triple_id = getattr(triple, "id", None)
        if triple_id:
            ids.append(str(triple_id))
    return ids
