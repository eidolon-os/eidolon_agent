"""Bounded, ephemeral home sessions; no Companion memory or shared device state."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field

from eidolon_sdk.biz.smarthome import HomeSessionScope

from eidolon_agent.domain.smarthome import HomeContext


class HomeSessionUnavailable(Exception):
    pass


@dataclass
class HomeSession:
    scope: HomeSessionScope
    context: HomeContext = field(default_factory=HomeContext)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    touched: float = field(default_factory=time.monotonic)


class HomeSessions:
    def __init__(self, *, ttl_s: float = 120, capacity: int = 256, context_turns: int = 3):
        self._context_turns = context_turns
        self._ttl = ttl_s
        self._capacity = capacity
        self._items: dict[tuple[str, str, str], HomeSession] = {}

    def get(self, scope: HomeSessionScope) -> HomeSession:
        now = time.monotonic()
        for key, value in list(self._items.items()):
            if not value.lock.locked() and now - value.touched >= self._ttl:
                value.context.active = False
                del self._items[key]
        key = (scope.owner_id, scope.device_ref, scope.session_id)
        if key not in self._items:
            if len(self._items) >= self._capacity:
                raise HomeSessionUnavailable("home session capacity reached")
            self._items[key] = HomeSession(scope=scope, context=HomeContext(history_limit=self._context_turns))
        item = self._items[key]
        if item.scope != scope:
            raise HomeSessionUnavailable("changing a Companion requires a new home session")
        item.touched = now
        if not item.context.active:
            raise HomeSessionUnavailable("home session is closed")
        return item

    def close(self, scope: HomeSessionScope) -> None:
        item = self._items.get((scope.owner_id, scope.device_ref, scope.session_id))
        if item is not None:
            if item.scope != scope:
                raise HomeSessionUnavailable("home session Companion does not match")
            item.context.active = False
            item.context.clear()
            item.touched = time.monotonic()

    def clear(self) -> None:
        for item in self._items.values():
            item.context.active = False
            item.context.clear()
        self._items.clear()
