"""In-process MemoryPort that stands in for eidolon-memory when it is absent.

Used by the standalone runtime profile (and available as a degraded-mode
fallback): recall always reports degraded with no hits so the turn engine
keeps talking, and every write is a no-op. Satisfies the full MemoryPort
protocol so the brain can run end to end with no memory service.
"""

from __future__ import annotations

from eidolon_agent.core.types.memory import (
    ActiveCommitmentReadResult,
    MemoryHit,
    MemoryQueryPlan,
    MemoryRecallResult,
    MemoryScope,
)

_DEGRADED_REASON = "standalone_no_memory_service"


class NullMemoryPort:
    """No-op MemoryPort: empty recall, dropped writes, always healthy."""

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
        return []

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
    ) -> str:
        return ""

    async def health(self) -> bool:
        return True

    async def close(self) -> None:
        return None
