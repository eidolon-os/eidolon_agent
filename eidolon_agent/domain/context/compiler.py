"""Direct prompt compilation — no pluggable providers, no budget pruning.

The hot path is fixed: persona prompt + memory recall + realtime digest as a
single system message, recent history as user/assistant messages, current
user input as the trailing user message. If a future segment is needed it
goes here, not behind an abstraction.
"""

from __future__ import annotations

import logging
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

        # ---- Persona system prompt (always present) -------------------------
        persona = await self._personas.compile_prompt(
            tenant_id=ti.caller.tenant_id,
            user_id=ti.caller.user_id,
            instance_id=instance_id,
            template_id=template_id,
            user_text=ti.text or "",
            realtime=_realtime_dict(ti.realtime),
        )
        system_parts: list[str] = [persona.system_prompt]

        # ---- Memory recall (best-effort, with timeout) ----------------------
        if self._memory is not None and ti.text:
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
                if formatted:
                    system_parts.append(f"[MEMORY]\n{formatted}")
            except Exception:
                _log.exception("memory recall failed; continuing without")

        # ---- Realtime digest (signals from voice pipeline) ------------------
        if ti.realtime is not None:
            line = _realtime_line(ti.realtime)
            if line:
                system_parts.append(f"（实时信号：{line}）")

        # ---- Assemble messages ---------------------------------------------
        now = datetime.now(timezone.utc)
        history = await self._history.recent_window(
            conversation_id=ti.conversation_id, window=self._history_window
        )
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
