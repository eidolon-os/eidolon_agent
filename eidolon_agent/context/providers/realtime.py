"""Inject realtime multimodal signals from the caller pipeline."""

from __future__ import annotations

from eidolon_agent.core.ports.context import ProviderContext
from eidolon_agent.core.types.context import ContextSegment, SegmentType, SegmentWeight


class RealtimeSignalProvider:
    name = "realtime"
    segment_type = SegmentType.REALTIME
    default_weight = SegmentWeight.NORMAL
    soft_timeout_s = 0.01

    def __init__(self, signal_fuser=None) -> None:
        self._fuser = signal_fuser

    async def provide(self, ctx: ProviderContext) -> list[ContextSegment]:
        # Prefer a fused digest from the SignalFuser; fall back to the digest
        # that was attached to TurnInput directly by the caller.
        digest = ctx.turn_input.realtime
        if digest is None and self._fuser is not None:
            digest = self._fuser.digest()
        if digest is None:
            return []
        line = digest.to_prompt_line()
        if not line:
            return []
        return [
            ContextSegment(
                type=self.segment_type,
                weight=self.default_weight,
                content=f"（实时信号：{line}）",
                tokens=max(1, len(line) // 3),
                source=self.name,
            )
        ]
