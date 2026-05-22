"""ProactiveEngine — runs sources, throttles, publishes triggered/suppressed."""

from __future__ import annotations

import asyncio

import pytest

from eidolon_agent.domain.proactive import ProactiveEngine, ProactiveThrottler
from eidolon_agent.domain.proactive.engine import ProactiveDecision

pytestmark = pytest.mark.functional


def _decision(intent: str = "check_in") -> ProactiveDecision:
    return ProactiveDecision(
        instance_id="inst-1",
        user_id="alice",
        intent=intent,
        text="还在吗？",
    )


async def test_source_decision_published_as_triggered(event_bus) -> None:
    triggered: list = []

    async def _on_trigger(ev) -> None:
        triggered.append(ev)

    await event_bus.subscribe("agent.proactive.triggered.inst-1", _on_trigger)

    engine = ProactiveEngine(event_bus=event_bus, interval_s=0.05)

    async def _src() -> ProactiveDecision:
        return _decision()

    engine.register_source(_src)
    await engine.start()
    await asyncio.sleep(0.2)
    await engine.stop()
    assert triggered, "expected at least one triggered event"
    assert triggered[0].payload["intent"] == "check_in"


async def test_throttled_decisions_publish_suppressed(event_bus) -> None:
    suppressed: list = []

    async def _on_suppressed(ev) -> None:
        suppressed.append(ev)

    await event_bus.subscribe("agent.proactive.suppressed.inst-1", _on_suppressed)

    # max 1 per hour and very short cooldown: first ok, second suppressed.
    engine = ProactiveEngine(
        event_bus=event_bus,
        throttler=ProactiveThrottler(max_per_hour=1, min_cooldown_s=0),
        interval_s=0.05,
    )

    calls = {"n": 0}

    async def _src() -> ProactiveDecision | None:
        calls["n"] += 1
        return _decision()

    engine.register_source(_src)
    await engine.start()
    await asyncio.sleep(0.3)
    await engine.stop()
    assert suppressed, "expected at least one suppressed event"


async def test_source_exception_does_not_stop_engine(event_bus) -> None:
    async def _bad_src() -> ProactiveDecision:
        raise RuntimeError("source broken")

    triggered: list = []

    async def _on(ev) -> None:
        triggered.append(ev)

    await event_bus.subscribe("agent.proactive.triggered.inst-1", _on)
    engine = ProactiveEngine(event_bus=event_bus, interval_s=0.05)
    engine.register_source(_bad_src)

    async def _good_src() -> ProactiveDecision:
        return _decision("ok")

    engine.register_source(_good_src)
    await engine.start()
    await asyncio.sleep(0.2)
    await engine.stop()
    # Good source still fired despite bad source raising.
    assert triggered
