"""In-process MemoryPort that stands in for eidolon-memory when it is absent.

Used by the standalone runtime profile (and available as a degraded-mode
fallback): recall always reports degraded with no hits so the turn engine
keeps talking. Completed-turn delivery remains the responsibility of
``HistoryFanout`` rather than this read port.
"""

from __future__ import annotations

from eidolon_agent.core.types.memory import (
    ActiveCommitmentReadResult,
    MemoryQueryPlan,
    MemoryRecallResult,
)

_DEGRADED_REASON = "standalone_no_memory_service"


class NullMemoryPort:
    """No-op MemoryPort: empty recall, always healthy."""

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
        return MemoryRecallResult(degraded=True, degraded_reason=_DEGRADED_REASON)

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
        del owner_id, memory_realm_id, companion_id, device_id, session_id
        del limit, timeout_s
        return ActiveCommitmentReadResult(
            degraded=True,
            degraded_reason=_DEGRADED_REASON,
        )

    async def health(self) -> bool:
        return True

    async def close(self) -> None:
        return None
