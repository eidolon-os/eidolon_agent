"""Hourly + cooldown throttling for proactive messages."""

from __future__ import annotations

import time
from collections import defaultdict, deque


class ProactiveThrottler:
    def __init__(self, *, max_per_hour: int = 6, min_cooldown_s: int = 90) -> None:
        self._max = max_per_hour
        self._cooldown = min_cooldown_s
        self._recent: defaultdict[str, deque[float]] = defaultdict(deque)

    def allow(self, instance_id: str) -> bool:
        now = time.monotonic()
        q = self._recent[instance_id]
        # Drop expired entries
        while q and now - q[0] > 3600:
            q.popleft()
        if q and now - q[-1] < self._cooldown:
            return False
        if len(q) >= self._max:
            return False
        q.append(now)
        return True
