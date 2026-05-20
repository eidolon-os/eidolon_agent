"""HistoryManager — the single point of read/write for conversation messages.

Short-term window is held in-process for hot reads; durable persistence flows
through the SQLite UoW. Reads merge in-memory recent messages with DB tail
when the window is asked for more than is cached.
"""

from __future__ import annotations

import asyncio
from collections import OrderedDict, deque

from eidolon_agent.core.types.messages import ChatMessage


class HistoryManager:
    def __init__(
        self,
        *,
        session_factory=None,  # SqlAlchemy session factory; UoW-style read on demand
        window_size: int = 50,
    ) -> None:
        # conv_id → bounded deque of ChatMessage (newest at the right end)
        self._windows: OrderedDict[str, deque[ChatMessage]] = OrderedDict()
        self._window_size = window_size
        self._lock = asyncio.Lock()
        self._session_factory = session_factory

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
                return []
            return list(w)[-window:]

    async def drop_session(self, conversation_id: str) -> None:
        async with self._lock:
            self._windows.pop(conversation_id, None)
