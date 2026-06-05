"""MemoryPort — external eidolon-memory adapter.

Reads go through MCP (synchronous, 150–300ms budget). Writes go through NATS
JetStream (asynchronous, fire-and-forget with at-least-once delivery).

The Port hides this dual-channel from callers. ``write_turn`` returns when the
publish ack lands (not when the memory service finishes ingestion).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Protocol, runtime_checkable

from eidolon_agent.core.types.memory import (
    MemoryHit,
    MemoryQueryPlan,
    MemoryRecallResult,
    MemoryScope,
)


@runtime_checkable
class MemoryPort(Protocol):
    """Combined read (MCP) + write (NATS) interface to eidolon-memory."""

    async def search(
        self,
        user_id: str,
        query: str,
        *,
        top_k: int = 5,
        scope: MemoryScope = MemoryScope.ALL,
        voice: bool = True,
        timeout_s: float = 0.2,
    ) -> list[MemoryHit]:
        """Vector + KG fused retrieval. Returns [] on soft-timeout (never raises)."""
        ...

    async def recall_context(
        self,
        user_id: str,
        query: str,
        *,
        plan: MemoryQueryPlan,
        timeout_s: float = 0.2,
    ) -> MemoryRecallResult:
        """Returns prompt-ready recall context plus operator diagnostics.

        ``degraded=True`` indicates the result is best-effort (timeout / error).
        Callers must inject a sentinel into the prompt rather than raise.
        """
        ...

    async def write_turn(
        self,
        user_id: str,
        session_id: str,
        turn_id: str,
        user_text: str,
        assistant_text: str,
        *,
        metadata: dict | None = None,
    ) -> None:
        """Publish a ConversationTurnPayload to NATS for steward ingestion."""
        ...

    async def assert_fact(
        self,
        user_id: str,
        subject: str,
        predicate: str,
        object_: str,
        *,
        confidence: float = 0.9,
    ) -> None:
        """Publish an explicit KG triple write command."""
        ...

    async def forget(self, user_id: str, query: str) -> int:
        """Delete memories matching ``query``. Returns count removed."""
        ...

    async def health(self) -> bool:
        """Lightweight reachability check — used by /readyz."""
        ...


MemoryEventHandler = Callable[[dict], Awaitable[None]]
"""Signature for subscribers to ``agent.memory.event.*`` (promise_due, etc)."""
