"""EventBus & KVStore — both backed by NATS (JetStream + KV) in production.

The interfaces are deliberately narrow: pub/sub for events, get/put/watch for
KV. Anything more complex (queues, streams replay) is the responsibility of a
specific feature module talking to JetStream directly.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Protocol, runtime_checkable

from eidolon_agent.core.types.event import Event

EventHandler = Callable[[Event], Awaitable[None]]
"""Subscriber callback. Must be reentrant and handle its own errors."""

Unsubscribe = Callable[[], Awaitable[None]]


@runtime_checkable
class EventBus(Protocol):
    """Pub/sub + optional request/reply over NATS (core + JetStream)."""

    async def publish(self, event: Event, *, persistent: bool = False) -> None:
        """Publish an event. When ``persistent`` is True, route via JetStream."""
        ...

    async def subscribe(
        self,
        subject: str,
        handler: EventHandler,
        *,
        durable: str | None = None,
        queue_group: str | None = None,
    ) -> Unsubscribe:
        """Subscribe to a subject pattern. Returns an async unsubscribe handle."""
        ...

    async def request(self, subject: str, payload: dict, *, timeout_s: float = 5.0) -> dict:
        """NATS request/reply for synchronous RPCs (e.g. dispatch submit)."""
        ...

    async def health(self) -> bool: ...


@runtime_checkable
class KVStore(Protocol):
    """Thin K/V on top of NATS JetStream KV. Bucket-scoped (one Store per bucket)."""

    async def get(self, key: str) -> bytes | None: ...

    async def put(self, key: str, value: bytes, *, ttl_s: int | None = None) -> int:
        """Returns the new revision; raises on conflict if CAS not satisfied."""
        ...

    async def delete(self, key: str) -> None: ...

    async def cas(self, key: str, value: bytes, *, expected_revision: int) -> int:
        """Compare-and-swap. Raises :class:`ConflictError` on revision mismatch."""
        ...

    async def keys(self, prefix: str = "") -> list[str]: ...

    def watch(self, key_pattern: str) -> AsyncIterator[tuple[str, bytes | None, int]]:
        """Yield (key, value, revision); value is None on delete."""
        ...
