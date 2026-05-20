"""In-memory ring buffer for incoming RealtimeSignal samples."""

from __future__ import annotations

import asyncio
from collections import deque

from eidolon_agent.core.types.signal import RealtimeSignal


class SignalBus:
    def __init__(self, *, capacity_per_session: int = 256) -> None:
        self._buf: dict[str, deque[RealtimeSignal]] = {}
        self._capacity = capacity_per_session
        self._lock = asyncio.Lock()

    async def publish(self, session_id: str, sig: RealtimeSignal) -> None:
        async with self._lock:
            q = self._buf.get(session_id)
            if q is None:
                q = deque(maxlen=self._capacity)
                self._buf[session_id] = q
            q.append(sig)

    async def recent(self, session_id: str, *, window_ms: int) -> list[RealtimeSignal]:
        async with self._lock:
            q = self._buf.get(session_id)
            if not q:
                return []
            from datetime import datetime, timedelta, timezone

            cutoff = datetime.now(timezone.utc) - timedelta(milliseconds=window_ms)
            return [s for s in q if s.ts >= cutoff]
