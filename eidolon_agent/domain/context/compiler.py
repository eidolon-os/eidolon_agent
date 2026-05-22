"""Concurrent context compiler.

Design notes:
- All providers run in ``asyncio.gather`` with per-provider soft timeouts.
- Late or failing providers are tagged degraded; never raise out of compile.
- Pruning order: drop low-weight segments first; never drop CRITICAL.
- The output ``messages`` list is the *final* sequence that goes to the LLM,
  including the system block synthesised from non-USER_INPUT segments.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from datetime import datetime, timezone

from eidolon_agent.core.ports.context import ContextProvider, ProviderContext
from eidolon_agent.core.types.context import (
    CompiledContext,
    ContextSegment,
    SegmentType,
    SegmentWeight,
)
from eidolon_agent.core.types.messages import ChatMessage, MessageRole
from eidolon_agent.core.types.turn import TurnInput

_log = logging.getLogger(__name__)


class ContextCompiler:
    def __init__(
        self,
        providers: list[ContextProvider],
        *,
        max_token_budget: int = 6000,
    ) -> None:
        self._providers = providers
        self._budget = max_token_budget

    async def compile(self, turn_input: TurnInput) -> CompiledContext:
        ctx = ProviderContext(
            turn_input=turn_input,
            now_ms=int(time.time() * 1000),
            token_budget_hint=self._budget,
        )

        # ---- Run providers concurrently with soft per-provider timeouts -----

        async def _run(p: ContextProvider) -> tuple[str, list[ContextSegment] | None, str | None]:
            try:
                segs = await asyncio.wait_for(p.provide(ctx), timeout=p.soft_timeout_s)
                return p.name, segs, None
            except asyncio.TimeoutError:
                _log.warning("provider %s soft-timeout (%.2fs)", p.name, p.soft_timeout_s)
                return p.name, None, "timeout"
            except Exception as exc:
                _log.exception("provider %s raised", p.name)
                return p.name, None, f"error:{type(exc).__name__}"

        results = await asyncio.gather(*[_run(p) for p in self._providers])

        segments: list[ContextSegment] = []
        degraded: list[str] = []
        for name, segs, err in results:
            if err is not None:
                degraded.append(name)
                continue
            if segs:
                segments.extend(segs)

        # ---- Always include the current user input ---------------------------
        user_text = turn_input.text or ""
        if user_text:
            segments.append(
                ContextSegment(
                    type=SegmentType.USER_INPUT,
                    weight=SegmentWeight.CRITICAL,
                    content=user_text,
                    tokens=_estimate_tokens(user_text),
                    source="compiler.user_input",
                )
            )

        # ---- Sort by weight desc, prune until budget fits --------------------
        segments.sort(key=lambda s: (-int(s.weight), s.source))
        total = sum(s.tokens for s in segments)
        pruned = 0
        while total > self._budget and segments:
            # find the lowest-weight non-critical segment
            for i in range(len(segments) - 1, -1, -1):
                if segments[i].weight is not SegmentWeight.CRITICAL:
                    total -= segments[i].tokens
                    del segments[i]
                    pruned += 1
                    break
            else:
                # all remaining are CRITICAL — accept overrun
                break

        messages = _assemble_messages(segments)
        return CompiledContext(
            segments=tuple(segments),
            messages=tuple(messages),
            total_tokens=total,
            budget=self._budget,
            pruned_count=pruned,
            degraded_providers=tuple(degraded),
        )


def _assemble_messages(segments: list[ContextSegment]) -> list[ChatMessage]:
    """Convert segments → flat ChatMessage list ready for the LLM.

    The mapping is intentionally simple: all non-USER_INPUT / non-HISTORY
    segments are concatenated into a single ``system`` block; ``HISTORY``
    segments are split into individual messages (their content is JSON-encoded
    ChatMessage tuples produced by the HistoryProvider); ``USER_INPUT`` becomes
    the trailing user message.
    """
    now = datetime.now(timezone.utc)
    system_parts: list[str] = []
    history: list[ChatMessage] = []
    user_input: ChatMessage | None = None

    for seg in segments:
        if seg.type is SegmentType.USER_INPUT:
            user_input = ChatMessage(
                id=uuid.uuid4().hex,
                role=MessageRole.USER,
                content=seg.content,
                created_at=now,
            )
        elif seg.type is SegmentType.HISTORY:
            # HistoryProvider must encode messages JSON-line-by-line; we tolerate
            # raw text by treating it as a single assistant turn.
            for line in seg.content.splitlines():
                if not line:
                    continue
                history.append(
                    ChatMessage(
                        id=uuid.uuid4().hex,
                        role=MessageRole.ASSISTANT,
                        content=line,
                        created_at=now,
                    )
                )
        else:
            label = seg.type.value.upper()
            system_parts.append(f"[{label}]\n{seg.content}")

    out: list[ChatMessage] = []
    if system_parts:
        out.append(
            ChatMessage(
                id=uuid.uuid4().hex,
                role=MessageRole.SYSTEM,
                content="\n\n".join(system_parts),
                created_at=now,
            )
        )
    out.extend(history)
    if user_input is not None:
        out.append(user_input)
    return out


def _estimate_tokens(text: str) -> int:
    """Cheap heuristic: 1 token ≈ 3 chars for mixed zh/en. Replaced by tokenizer later."""
    return max(1, len(text) // 3)
