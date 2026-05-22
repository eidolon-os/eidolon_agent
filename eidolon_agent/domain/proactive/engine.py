"""ProactiveEngine — periodic evaluator that emits proactive decisions.

Sources register themselves and return optional ``ProactiveDecision``s on
each tick. The engine publishes successful decisions on the EventBus; the
gRPC ``SubscribeProactive`` stream forwards them to subscribed callers.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from eidolon_agent.core.types.event import Event
from eidolon_agent.core.types.topics import Topics
from eidolon_agent.domain.proactive.throttler import ProactiveThrottler

_log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ProactiveDecision:
    instance_id: str
    user_id: str
    intent: str  # short label, e.g. "check_in" / "remind_promise"
    text: str
    style_hint: str = ""
    cooldown_s: int = 300


ProactiveSource = Callable[[], Awaitable[ProactiveDecision | None]]


class ProactiveEngine:
    def __init__(
        self,
        *,
        event_bus=None,
        throttler: ProactiveThrottler | None = None,
        interval_s: float = 5.0,
    ) -> None:
        self._bus = event_bus
        self._throttler = throttler or ProactiveThrottler()
        self._sources: list[ProactiveSource] = []
        self._interval_s = interval_s
        self._task: asyncio.Task | None = None
        self._stopping = asyncio.Event()

    def register_source(self, source: ProactiveSource) -> None:
        self._sources.append(source)

    async def start(self) -> None:
        if self._task is not None:
            return
        self._task = asyncio.create_task(self._loop(), name="proactive-engine")

    async def stop(self) -> None:
        self._stopping.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _loop(self) -> None:
        while not self._stopping.is_set():
            for source in self._sources:
                try:
                    decision = await source()
                except Exception:
                    _log.exception("proactive source raised")
                    continue
                if decision is None:
                    continue
                if not self._throttler.allow(decision.instance_id):
                    if self._bus is not None:
                        await self._bus.publish(
                            Event(
                                subject=Topics.proactive_suppressed(decision.instance_id),
                                payload={"reason": "throttled", "intent": decision.intent},
                                source="proactive.engine",
                            )
                        )
                    continue
                if self._bus is not None:
                    await self._bus.publish(
                        Event(
                            subject=Topics.proactive_triggered(decision.instance_id),
                            payload={
                                "instance_id": decision.instance_id,
                                "user_id": decision.user_id,
                                "intent": decision.intent,
                                "text": decision.text,
                                "style_hint": decision.style_hint,
                            },
                            source="proactive.engine",
                        )
                    )
            try:
                await asyncio.wait_for(self._stopping.wait(), timeout=self._interval_s)
            except asyncio.TimeoutError:
                continue
