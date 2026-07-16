"""MemoryPort: external eidolon-memory adapter."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Protocol, runtime_checkable

from eidolon_agent.core.types.memory import (
    MemoryForgetOutcome,
    MemoryForgetPreview,
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

    async def assert_fact(
        self,
        owner_id: str | None,
        companion_id: str | None,
        memory_realm_id: str,
        subject: str,
        predicate: str,
        object_: str,
        *,
        confidence: float = 0.9,
    ) -> None:
        """Publish an explicit KG triple write command."""
        ...

    async def write_confirmed_fact(
        self,
        owner_id: str | None,
        companion_id: str | None,
        memory_realm_id: str,
        device_id: str | None,
        session_id: str | None,
        text: str,
        *,
        confidence: float = 0.99,
        tags: list[str] | None = None,
    ) -> None:
        """Publish a verbatim user-confirmed fact when KG shape is unsuitable."""
        ...

    async def preview_forget(
        self,
        owner_id: str | None,
        companion_id: str | None,
        memory_realm_id: str,
        device_id: str | None,
        query: str,
        *,
        action: str = "archive",
        session_id: str | None = None,
    ) -> MemoryForgetPreview:
        """Resolve a topic to exact memory IDs without mutating memory."""
        ...

    async def confirm_forget(
        self,
        owner_id: str | None,
        companion_id: str | None,
        memory_realm_id: str,
        device_id: str | None,
        confirmation_token: str,
        *,
        session_id: str | None = None,
        wait_applied_seconds: float = 2.0,
    ) -> MemoryForgetOutcome:
        """Submit one preview-bound exact-ID privacy command."""
        ...

    async def health(self) -> bool:
        """Lightweight reachability check used by /readyz."""
        ...


MemoryEventHandler = Callable[[dict], Awaitable[None]]
