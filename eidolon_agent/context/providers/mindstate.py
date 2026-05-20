"""Inject the assistant's current MindState as a natural-language hint."""

from __future__ import annotations

from eidolon_agent.core.ports.context import ProviderContext
from eidolon_agent.core.types.context import ContextSegment, SegmentType, SegmentWeight


class MindStateProvider:
    name = "mindstate"
    segment_type = SegmentType.MINDSTATE
    default_weight = SegmentWeight.NORMAL
    soft_timeout_s = 0.02

    def __init__(self, mind_service=None) -> None:
        self._mind = mind_service

    async def provide(self, ctx: ProviderContext) -> list[ContextSegment]:
        if self._mind is None:
            return []
        state = self._mind.snapshot(
            instance_id=ctx.turn_input.caller.agent_instance_id or "",
        )
        hint = state.to_prompt_hint()
        if not hint:
            return []
        return [
            ContextSegment(
                type=self.segment_type,
                weight=self.default_weight,
                content=f"（你此刻的状态：{hint}）",
                tokens=max(1, len(hint) // 3),
                source=self.name,
            )
        ]
