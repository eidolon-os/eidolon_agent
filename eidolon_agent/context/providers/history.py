"""Inject recent conversation history."""

from __future__ import annotations

import json

from eidolon_agent.core.ports.context import ProviderContext
from eidolon_agent.core.types.context import ContextSegment, SegmentType, SegmentWeight


class HistoryProvider:
    name = "history"
    segment_type = SegmentType.HISTORY
    default_weight = SegmentWeight.HIGH
    soft_timeout_s = 0.05

    def __init__(self, history_manager=None, *, window: int = 20) -> None:
        self._mgr = history_manager
        self._window = window

    async def provide(self, ctx: ProviderContext) -> list[ContextSegment]:
        if self._mgr is None:
            return []
        ti = ctx.turn_input
        msgs = await self._mgr.recent_window(conversation_id=ti.conversation_id, window=self._window)
        if not msgs:
            return []
        # Encode messages one-per-line so the compiler can split them back.
        lines = [
            json.dumps(
                {"role": m.role.value, "content": m.content},
                ensure_ascii=False,
            )
            for m in msgs
        ]
        text = "\n".join(lines)
        return [
            ContextSegment(
                type=self.segment_type,
                weight=self.default_weight,
                content=text,
                tokens=max(1, len(text) // 3),
                source=self.name,
            )
        ]
