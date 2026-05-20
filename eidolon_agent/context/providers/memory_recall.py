"""Inject memory recall (via MemoryPort.recall_context)."""

from __future__ import annotations

from eidolon_agent.core.ports.context import ProviderContext
from eidolon_agent.core.types.context import ContextSegment, SegmentType, SegmentWeight
from eidolon_agent.core.types.memory import MemoryQueryPlan


class MemoryRecallProvider:
    name = "memory_recall"
    segment_type = SegmentType.MEMORY
    default_weight = SegmentWeight.HIGH
    soft_timeout_s = 0.25  # eidolon-memory has its own 300ms hard budget

    def __init__(self, memory_port=None, *, default_top_k: int = 5) -> None:
        self._mem = memory_port
        self._top_k = default_top_k

    async def provide(self, ctx: ProviderContext) -> list[ContextSegment]:
        if self._mem is None or not ctx.turn_input.text:
            return []
        plan = MemoryQueryPlan(
            episodic_query=ctx.turn_input.text,
            semantic_query=ctx.turn_input.text,
            episodic_k=3,
            semantic_k=self._top_k,
            voice=ctx.turn_input.caller.caller_kind.value == "livekit_voice",
        )
        formatted, _hits, degraded = await self._mem.recall_context(
            user_id=ctx.turn_input.caller.user_id,
            query=ctx.turn_input.text,
            plan=plan,
            timeout_s=self.soft_timeout_s,
        )
        if not formatted:
            if degraded:
                return [
                    ContextSegment(
                        type=self.segment_type,
                        weight=SegmentWeight.LOW,
                        content="（记忆暂不可用）",
                        tokens=4,
                        source=self.name,
                        metadata={"degraded": True},
                    )
                ]
            return []
        return [
            ContextSegment(
                type=self.segment_type,
                weight=self.default_weight,
                content=formatted,
                tokens=max(1, len(formatted) // 3),
                source=self.name,
                metadata={"degraded": degraded},
            )
        ]
