"""HistoryManager — the single point of read/write for conversation messages.

Short-term window is held in-process for hot reads; durable persistence flows
through the SQLite UoW. Reads merge in-memory recent messages with DB tail
when the window is asked for more than is cached.
"""

from __future__ import annotations

import asyncio
from collections import OrderedDict, deque
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

from eidolon_agent.core.types.messages import ChatMessage

HistoryHydrator = Callable[..., Awaitable[list[ChatMessage]]]


class HistoryManager:
    def __init__(
        self,
        *,
        hydrate_messages: HistoryHydrator | None = None,
        window_size: int = 50,
        hydrate_timeout_s: float = 0.05,
    ) -> None:
        # conv_id → bounded deque of ChatMessage (newest at the right end)
        self._windows: OrderedDict[str, deque[ChatMessage]] = OrderedDict()
        self._window_size = window_size
        self._lock = asyncio.Lock()
        self._hydrate_messages = hydrate_messages
        self._hydrate_timeout_s = hydrate_timeout_s
        self._hydration_tasks: set[asyncio.Task[list[ChatMessage] | None]] = set()

    async def append(self, *, conversation_id: str, message: ChatMessage) -> None:
        async with self._lock:
            w = self._windows.get(conversation_id)
            if w is None:
                w = deque(maxlen=self._window_size)
                self._windows[conversation_id] = w
            w.append(message)

    async def recent_window(self, *, conversation_id: str, window: int = 20) -> list[ChatMessage]:
        async with self._lock:
            w = self._windows.get(conversation_id)
            if w is None:
                cached: list[ChatMessage] = []
            else:
                cached = _public_messages(list(w))[-window:]
            if len(cached) >= window or self._hydrate_messages is None:
                return cached

        hydration = asyncio.create_task(
            self._bounded_hydrate(conversation_id=conversation_id, window=window),
            name=f"history-hydrate:{conversation_id}",
        )
        self._hydration_tasks.add(hydration)
        hydration.add_done_callback(self._hydration_done)
        try:
            # Context compilation can be cancelled independently by both the
            # preemptive turn and its parent stream. Keep that cancellation at
            # this boundary: the owned task applies exactly one DB timeout and
            # is allowed to return its connection before it leaves the set.
            hydrated = await asyncio.shield(hydration)
        except asyncio.CancelledError:
            raise
        if hydrated is None:
            return cached
        return _merge_tail(hydrated, cached, window=window)

    async def drop_session(self, conversation_id: str) -> None:
        async with self._lock:
            self._windows.pop(conversation_id, None)

    async def _hydrate_external(self, *, conversation_id: str, window: int) -> list[ChatMessage]:
        if self._hydrate_messages is None:
            return []
        messages = await self._hydrate_messages(
            conversation_id=conversation_id,
            window=window,
        )
        return _public_messages(messages)

    async def _bounded_hydrate(
        self,
        *,
        conversation_id: str,
        window: int,
    ) -> list[ChatMessage] | None:
        try:
            return await asyncio.wait_for(
                self._hydrate_external(conversation_id=conversation_id, window=window),
                timeout=self._hydrate_timeout_s,
            )
        except Exception:
            return None

    def _hydration_done(self, task: asyncio.Task[list[ChatMessage] | None]) -> None:
        self._hydration_tasks.discard(task)
        if task.cancelled():
            return
        # Retrieve any BaseException raised outside the normal bounded path so
        # asyncio never reports an unobserved task during process shutdown.
        task.exception()


def _public_messages(messages: list[ChatMessage]) -> list[ChatMessage]:
    return [m for m in messages if not bool(m.metadata.get("is_private", False))]


def _merge_tail(
    hydrated: list[ChatMessage],
    cached: list[ChatMessage],
    *,
    window: int,
) -> list[ChatMessage]:
    by_id: dict[str, ChatMessage] = {}
    for msg in [*hydrated, *cached]:
        by_id[_stable_message_key(msg)] = msg
    return sorted(by_id.values(), key=lambda m: _utc_sort_key(m.created_at))[-window:]


def _stable_message_key(message: ChatMessage) -> str:
    turn_id = str(message.metadata.get("turn_id") or "").strip()
    seq_in_turn = message.metadata.get("seq_in_turn")
    if turn_id and isinstance(seq_in_turn, int):
        return f"turn:{turn_id}:{seq_in_turn}"
    return f"message:{message.id}"


def _utc_sort_key(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
