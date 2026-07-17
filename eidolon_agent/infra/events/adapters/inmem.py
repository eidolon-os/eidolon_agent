"""In-process EventBus + KVStore — for tests and degraded-mode operation.

Behaviourally compatible with the NATS implementations for the small subset of
features tests exercise: pub/sub with wildcard subjects, request/reply,
get/put/cas/watch.
"""

from __future__ import annotations

import asyncio
import fnmatch
import logging
import time
from collections import defaultdict
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass

from eidolon_agent.core.errors import ConflictError
from eidolon_agent.core.types.event import Event

_log = logging.getLogger(__name__)


@dataclass
class _Subscription:
    pattern: str
    handler: Callable[[Event], Awaitable[None]]
    queue_group: str | None


def _matches(pattern: str, subject: str) -> bool:
    """NATS-like wildcard match: ``*`` for single token, ``>`` for tail."""
    # Translate to fnmatch syntax: '*' → '*', '>' → '**' (any tail)
    if pattern.endswith(">"):
        head = pattern[:-1]
        return subject.startswith(head)
    return fnmatch.fnmatchcase(subject, pattern)


class InMemoryEventBus:
    """Thread-safe single-process EventBus."""

    def __init__(self) -> None:
        self._subs: list[_Subscription] = []
        self._lock = asyncio.Lock()
        # request/reply handlers are keyed by exact subject
        self._rr: dict[str, Callable[[dict], Awaitable[dict]]] = {}
        # Strong refs to in-flight delivery tasks. asyncio holds only weakrefs
        # to "free" tasks, so without this set a task created by ``publish``
        # could be GC'd before the handler runs. We add on create + drop on
        # completion via ``Task.add_done_callback``.
        self._delivery_tasks: set[asyncio.Task] = set()

    async def publish(self, event: Event, *, persistent: bool = False) -> None:
        # Persistence is a no-op in-process; we just deliver to subscribers.
        # Handlers run concurrently via ``asyncio.create_task`` so one slow
        # handler can't stall the others. This matches NATS semantics: publish
        # returns when the event has been *scheduled*, not consumed. Tests that
        # need delivery to complete should ``await asyncio.sleep(0)``.
        targets: list[_Subscription] = []
        async with self._lock:
            seen_groups: set[str] = set()
            for sub in self._subs:
                if not _matches(sub.pattern, event.subject):
                    continue
                if sub.queue_group is not None:
                    if sub.queue_group in seen_groups:
                        continue
                    seen_groups.add(sub.queue_group)
                targets.append(sub)
        for sub in targets:
            # Fire-and-forget by design; tests use `await asyncio.sleep(0)` to
            # let handlers run before asserting. We retain a strong reference
            # so the task can't be garbage-collected before the handler runs.
            task = asyncio.create_task(_safe_invoke(sub.handler, event))
            self._delivery_tasks.add(task)
            task.add_done_callback(self._delivery_tasks.discard)

    async def subscribe(
        self,
        subject: str,
        handler: Callable[[Event], Awaitable[None]],
        *,
        durable: str | None = None,
        queue_group: str | None = None,
    ) -> Callable[[], Awaitable[None]]:
        sub = _Subscription(pattern=subject, handler=handler, queue_group=queue_group)
        async with self._lock:
            self._subs.append(sub)

        async def _unsub() -> None:
            async with self._lock:
                if sub in self._subs:
                    self._subs.remove(sub)

        return _unsub

    async def request(self, subject: str, payload: dict, *, timeout_s: float = 5.0) -> dict:
        if subject not in self._rr:
            raise asyncio.TimeoutError(f"no responder for {subject}")
        try:
            return await asyncio.wait_for(self._rr[subject](payload), timeout=timeout_s)
        except asyncio.TimeoutError:
            raise

    def register_responder(
        self,
        subject: str,
        handler: Callable[[dict], Awaitable[dict]],
    ) -> None:
        """Helper used by tests to wire a synchronous responder for a subject."""
        self._rr[subject] = handler

    async def health(self) -> bool:
        return True


async def _safe_invoke(
    handler: Callable[[Event], Awaitable[None]], event: Event
) -> None:
    """Errors in handlers must not propagate to publisher or other subscribers.

    Failures are logged at WARNING with the subject + handler qualname so they
    surface in tests and production logs instead of vanishing silently. We do
    NOT re-raise — pub/sub is fire-and-forget, the publisher has already
    moved on by the time we get here.
    """
    try:
        await handler(event)
    except asyncio.CancelledError:
        raise
    except Exception:
        handler_name = getattr(handler, "__qualname__", repr(handler))
        _log.warning(
            "event handler %s failed for subject %r", handler_name, event.subject,
            exc_info=True,
        )


# ---------------------------------------------------------------------------
# KV
# ---------------------------------------------------------------------------


@dataclass
class _KVEntry:
    value: bytes
    revision: int
    expires_at: float | None  # epoch seconds


class InMemoryKVStore:
    """Single-bucket KV with optional TTL and CAS."""

    def __init__(self, bucket: str = "default") -> None:
        self.bucket = bucket
        self._store: dict[str, _KVEntry] = {}
        self._lock = asyncio.Lock()
        self._watchers: dict[str, list[asyncio.Queue]] = defaultdict(list)

    def _expired(self, e: _KVEntry, *, now: float | None = None) -> bool:
        return e.expires_at is not None and (now or time.time()) >= e.expires_at

    async def get(self, key: str) -> bytes | None:
        async with self._lock:
            entry = self._store.get(key)
            if entry is None or self._expired(entry):
                self._store.pop(key, None)
                return None
            return entry.value

    async def clear(self) -> None:
        async with self._lock:
            keys = list(self._store)
            self._store.clear()
            for key in keys:
                await self._notify(key, None, -1)

    async def put(self, key: str, value: bytes, *, ttl_s: int | None = None) -> int:
        async with self._lock:
            existing = self._store.get(key)
            rev = (existing.revision + 1) if existing is not None else 1
            entry = _KVEntry(
                value=value,
                revision=rev,
                expires_at=(time.time() + ttl_s) if ttl_s else None,
            )
            self._store[key] = entry
            await self._notify(key, value, rev)
            return rev

    async def delete(self, key: str) -> None:
        async with self._lock:
            self._store.pop(key, None)
            await self._notify(key, None, -1)

    async def cas(self, key: str, value: bytes, *, expected_revision: int) -> int:
        async with self._lock:
            existing = self._store.get(key)
            current_rev = existing.revision if existing is not None else 0
            if current_rev != expected_revision:
                raise ConflictError(
                    f"cas mismatch on {key}: expected rev {expected_revision}, found {current_rev}"
                )
            rev = current_rev + 1
            self._store[key] = _KVEntry(value=value, revision=rev, expires_at=None)
            await self._notify(key, value, rev)
            return rev

    async def keys(self, prefix: str = "") -> list[str]:
        async with self._lock:
            now = time.time()
            return [k for k, e in self._store.items() if k.startswith(prefix) and not self._expired(e, now=now)]

    async def watch(self, key_pattern: str) -> AsyncIterator[tuple[str, bytes | None, int]]:
        q: asyncio.Queue[tuple[str, bytes | None, int]] = asyncio.Queue()
        async with self._lock:
            self._watchers[key_pattern].append(q)
        try:
            while True:
                yield await q.get()
        finally:
            async with self._lock:
                self._watchers[key_pattern].remove(q)
                if not self._watchers[key_pattern]:
                    del self._watchers[key_pattern]

    async def _notify(self, key: str, value: bytes | None, revision: int) -> None:
        for pattern, qs in self._watchers.items():
            if _matches(pattern, key):
                for q in qs:
                    q.put_nowait((key, value, revision))


__all__ = ["InMemoryEventBus", "InMemoryKVStore"]


def _typecheck() -> None:
    """Runtime check that adapters satisfy the Port protocols."""
    from eidolon_agent.core.ports.events import EventBus, KVStore

    assert isinstance(InMemoryEventBus(), EventBus), "InMemoryEventBus must satisfy EventBus"
    assert isinstance(InMemoryKVStore(), KVStore), "InMemoryKVStore must satisfy KVStore"


_typecheck()
