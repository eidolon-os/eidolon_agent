"""MemoryPort: external eidolon-memory adapter."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Protocol, runtime_checkable

from eidolon_agent.core.types.memory import (
    ActiveCommitmentReadResult,
    MemoryQueryPlan,
    MemoryRecallResult,
)


@runtime_checkable
class MemoryPort(Protocol):
    """Read-only hot-path interface to eidolon-memory.

    Completed turns are delivered once through ``HistoryFanout`` and its
    durable outbox. Keeping writes off this port prevents a second producer
    from competing with that lifecycle.
    """

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

    async def health(self) -> bool:
        """Lightweight reachability check used by /readyz."""
        ...


MemoryEventHandler = Callable[[dict], Awaitable[None]]
