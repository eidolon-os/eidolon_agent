"""MemoryPort: external eidolon-memory adapter."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Protocol, runtime_checkable

from eidolon_agent.core.types.memory import (
    ActiveCommitmentReadResult,
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
        owner_id: str | None,
        query: str,
        *,
        memory_realm_id: str,
        companion_id: str | None = None,
        device_id: str | None = None,
        top_k: int = 5,
        scope: MemoryScope = MemoryScope.ALL,
        voice: bool = True,
        timeout_s: float = 0.2,
        session_id: str | None = None,
    ) -> list[MemoryHit]:
        """Vector + KG fused retrieval. Returns [] on soft-timeout."""
        ...

    async def recall_context(
        self,
        owner_id: str | None,
        query: str,
        *,
        memory_realm_id: str,
        plan: MemoryQueryPlan,
        companion_id: str | None = None,
        device_id: str | None = None,
        timeout_s: float = 0.2,
        session_id: str | None = None,
    ) -> MemoryRecallResult:
        """Returns prompt-ready recall context plus diagnostics."""
        ...

    async def read_active_commitments(
        self,
        owner_id: str | None,
        *,
        memory_realm_id: str,
        companion_id: str | None = None,
        device_id: str | None = None,
        session_id: str | None = None,
        limit: int = 5,
        timeout_s: float = 0.2,
    ) -> ActiveCommitmentReadResult:
        """Read a bounded current Commitment set from the caller's Realm."""
        ...

    async def write_turn(
        self,
        owner_id: str | None,
        companion_id: str | None,
        memory_realm_id: str,
        device_id: str | None,
        session_id: str | None,
        turn_id: str,
        owner_text: str,
        assistant_text: str,
        *,
        metadata: dict | None = None,
    ) -> None:
        """Publish a ConversationTurnPayload to NATS for steward ingestion."""
        ...

    async def health(self) -> bool:
        """Lightweight reachability check used by /readyz."""
        ...


MemoryEventHandler = Callable[[dict], Awaitable[None]]
