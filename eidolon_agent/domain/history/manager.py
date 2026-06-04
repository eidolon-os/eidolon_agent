"""HistoryManager — the single point of read/write for conversation messages.

Short-term window is held in-process for hot reads; durable persistence flows
through the SQLite UoW. Reads merge in-memory recent messages with DB tail
when the window is asked for more than is cached.
"""

from __future__ import annotations

import asyncio
from collections import OrderedDict, deque
from collections.abc import Awaitable, Callable

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

    async def append(self, *, conversation_id: str, message: ChatMessage) -> None:
        async with self._lock:
            w = self._windows.get(conversation_id)
            if w is None:
                w = deque(maxlen=self._window_size)
                self._windows[conversation_id] = w
            w.append(message)

    async def recent_window(
        self, *, conversation_id: str, window: int = 20
    ) -> list[ChatMessage]:
        async with self._lock:
            w = self._windows.get(conversation_id)
            if w is None:
                cached: list[ChatMessage] = []
            else:
                cached = _public_messages(list(w))[-window:]
            if len(cached) >= window or self._hydrate_messages is None:
                return cached

        try:
            hydrated = await asyncio.wait_for(
                self._hydrate_external(conversation_id=conversation_id, window=window),
                timeout=self._hydrate_timeout_s,
            )
        except Exception:
            return cached
        return _merge_tail(hydrated, cached, window=window)

    async def drop_session(self, conversation_id: str) -> None:
        async with self._lock:
            self._windows.pop(conversation_id, None)

    async def forget_matching(self, *, conversation_id: str, query: str) -> int:
        """Best-effort in-process privacy scrub for the current conversation."""

        terms = _forget_terms(query)
        if not terms:
            return 0
        async with self._lock:
            w = self._windows.get(conversation_id)
            if w is None:
                return 0
            kept = [m for m in w if not any(term in m.content for term in terms)]
            removed = len(w) - len(kept)
            w.clear()
            w.extend(kept)
            return removed

    async def _hydrate_external(
        self, *, conversation_id: str, window: int
    ) -> list[ChatMessage]:
        if self._hydrate_messages is None:
            return []
        messages = await self._hydrate_messages(
            conversation_id=conversation_id,
            window=window,
        )
        return _public_messages(messages)


def _public_messages(messages: list[ChatMessage]) -> list[ChatMessage]:
    return [m for m in messages if not bool(m.metadata.get("is_private", False))]


def _forget_terms(query: str) -> list[str]:
    """Extract conservative text fragments for local forget scrubbing."""

    cleaned = query.strip()
    if not cleaned:
        return []
    terms = {cleaned}
    markers = (
        "忘记",
        "别记",
        "不要再提",
        "不再提",
        "不要提",
        "忘掉",
    )
    suffix_stoppers = "，。,.!?！？"
    for marker in markers:
        if marker not in cleaned:
            continue
        suffix = cleaned.split(marker, 1)[1].strip()
        suffix = suffix.strip(suffix_stoppers).strip()
        if len(suffix) >= 3:
            terms.add(suffix)
    return sorted(terms, key=len, reverse=True)


def _merge_tail(
    hydrated: list[ChatMessage],
    cached: list[ChatMessage],
    *,
    window: int,
) -> list[ChatMessage]:
    by_id: dict[str, ChatMessage] = {}
    for msg in [*hydrated, *cached]:
        by_id[msg.id] = msg
    return sorted(by_id.values(), key=lambda m: m.created_at)[-window:]
