"""High-level memory access strategy — plans queries and orchestrates writes.

Consumers should prefer this class over the raw port: it knows the three-layer
working/episodic/semantic model and applies it consistently.
"""

from __future__ import annotations

from eidolon_agent.core.types.memory import MemoryQueryPlan
from eidolon_agent.domain.memory_policy import (
    MemoryWriteDisposition,
    MemoryWriteDispositionKind,
    classify_memory_write,
)


class MemoryStrategy:
    def __init__(self, *, port) -> None:
        self._port = port

    def plan_for_turn(self, *, user_text: str, voice: bool) -> MemoryQueryPlan:
        return MemoryQueryPlan(
            working_k=20,
            episodic_query=user_text,
            episodic_k=3,
            semantic_query=user_text,
            semantic_k=5,
            promise_force=True,
            voice=voice,
        )

    async def retrieve(self, *, user_id: str, user_text: str, voice: bool):
        plan = self.plan_for_turn(user_text=user_text, voice=voice)
        timeout_s = 0.15 if voice else 0.25
        return await self._port.recall_context(
            user_id=user_id,
            query=user_text,
            plan=plan,
            timeout_s=timeout_s,
        )

    def classify_write(
        self,
        *,
        user_text: str,
        assistant_text: str = "",
    ) -> MemoryWriteDisposition:
        return classify_memory_write(
            user_text=user_text,
            assistant_text=assistant_text,
        )

    async def write_turn(self, *, user_id: str, session_id: str, turn_id: str, user_text: str, assistant_text: str) -> None:
        await self._port.write_turn(user_id, session_id, turn_id, user_text, assistant_text)

    async def forget(self, *, user_id: str, query: str) -> int:
        return await self._port.forget(user_id, query)


__all__ = [
    "MemoryStrategy",
    "MemoryWriteDisposition",
    "MemoryWriteDispositionKind",
]
