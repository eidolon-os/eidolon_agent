"""Direct prompt compilation — no pluggable providers, no budget pruning.

The hot path is fixed: persona prompt + memory recall + realtime digest as a
single system message, recent history as user/assistant messages, current
user input as the trailing user message. If a future segment is needed it
goes here, not behind an abstraction.
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
    ) -> None:
        self._personas = personas_service
        self._locator = instance_locator
        self._history = history_manager
        self._memory = memory_port
        self._history_window = history_window
        self._memory_timeout_s = memory_timeout_s
        self._memory_top_k = memory_top_k

    async def compile(self, ti: TurnInput) -> list[ChatMessage]:
        instance_id, template_id = self._locator(
            ti.caller.tenant_id, ti.caller.user_id, ti.conversation_id
        )

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
        history_task = _timed(
            "history",
            self._history.recent_window(
                conversation_id=ti.conversation_id, window=self._history_window
            ),
        )

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
        memory_text, memory_ms = _unpack(memory_res)
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

        system_parts: list[str] = [persona.system_prompt]

        if isinstance(memory_text, BaseException):
            _log.warning("memory recall raised: %s", memory_text)
        elif memory_text:
            system_parts.append(f"[MEMORY]\n{memory_text}")

        # ---- Realtime digest (signals from voice pipeline) ------------------
        if ti.realtime is not None:
            line = _realtime_line(ti.realtime)
            if line:
                system_parts.append(f"（实时信号：{line}）")

        if isinstance(history, BaseException):
            _log.warning("history window raised: %s", history)
            history = []

        # ---- Assemble messages ---------------------------------------------
        now = datetime.now(timezone.utc)
        out: list[ChatMessage] = [
            ChatMessage(
                id=uuid.uuid4().hex,
                role=MessageRole.SYSTEM,
                content="\n\n".join(system_parts),
                created_at=now,
            ),
            *history,
        ]
        if ti.text:
            out.append(
                ChatMessage(
                    id=uuid.uuid4().hex,
                    role=MessageRole.USER,
                    content=ti.text,
                    created_at=now,
                )
            )
        return out

    # Injected into the system prompt when memory recall raises. Tells the
    # LLM not to confabulate prior context — degraded honestly beats
    # silently-amnesiac-pretending-to-remember.
    _MEMORY_DEGRADED_NOTICE = (
        "（系统提示：本轮 memory backend 暂不可达,你没有任何过往记忆访问权。"
        "请如实承认这点,不要假装记得用户之前说过的事;"
        "也不要主动声称会记住用户接下来说的——因为本轮记忆链路是断的。）"
    )

    async def _memory_recall(self, ti: TurnInput) -> str | None:
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
        try:
            plan = MemoryQueryPlan(
                episodic_query=ti.text,
                semantic_query=ti.text,
                episodic_k=3,
                semantic_k=self._memory_top_k,
                voice=ti.caller.caller_kind.value == "livekit_voice",
            )
            formatted, _hits, _degraded = await self._memory.recall_context(
                user_id=ti.caller.user_id,
                query=ti.text,
                plan=plan,
                timeout_s=self._memory_timeout_s,
            )
            return formatted or None
        except Exception:
            _log.exception(
                "memory recall failed for user=%s; injecting degraded notice "
                "into system prompt",
                ti.caller.user_id,
            )
            return self._MEMORY_DEGRADED_NOTICE


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
