"""SignalBus — ring buffer, time-window, capacity."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from eidolon_agent.core.types.signal import RealtimeSignal, SignalModality
from eidolon_agent.domain.signals import SignalBus

pytestmark = pytest.mark.unit


def _sig(ts: datetime, label: str = "smile", conf: float = 0.9) -> RealtimeSignal:
    return RealtimeSignal(ts=ts, modality=SignalModality.FACE, label=label, confidence=conf)


async def test_publish_and_recent_within_window() -> None:
    bus = SignalBus()
    now = datetime.now(timezone.utc)
    await bus.publish("s1", _sig(now - timedelta(milliseconds=500), "smile"))
    await bus.publish("s1", _sig(now, "frown"))
    items = await bus.recent("s1", window_ms=1_000)
    assert [s.label for s in items] == ["smile", "frown"]


async def test_recent_drops_out_of_window_entries() -> None:
    bus = SignalBus()
    now = datetime.now(timezone.utc)
    await bus.publish("s1", _sig(now - timedelta(seconds=30), "old"))
    await bus.publish("s1", _sig(now, "new"))
    items = await bus.recent("s1", window_ms=1_000)
    assert [s.label for s in items] == ["new"]


async def test_recent_returns_empty_for_unknown_session() -> None:
    bus = SignalBus()
    assert await bus.recent("ghost", window_ms=10_000) == []


async def test_capacity_bound_drops_oldest() -> None:
    bus = SignalBus(capacity_per_session=3)
    now = datetime.now(timezone.utc)
    for i in range(5):
        await bus.publish("s1", _sig(now, label=f"l{i}"))
    items = await bus.recent("s1", window_ms=60_000)
    assert [s.label for s in items] == ["l2", "l3", "l4"]
