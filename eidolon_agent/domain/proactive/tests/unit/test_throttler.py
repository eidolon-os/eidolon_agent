"""ProactiveThrottler — hourly cap + cooldown."""

from __future__ import annotations

import time

import pytest

from eidolon_agent.domain.proactive import ProactiveThrottler

pytestmark = pytest.mark.unit


def test_allows_first_request() -> None:
    assert ProactiveThrottler().allow("inst-1") is True


def test_cooldown_blocks_back_to_back(monkeypatch: pytest.MonkeyPatch) -> None:
    t = ProactiveThrottler(min_cooldown_s=60)
    t.allow("inst-1")  # first allowed
    assert t.allow("inst-1") is False  # within cooldown


def test_hourly_cap_blocks_after_max(monkeypatch: pytest.MonkeyPatch) -> None:
    # Stub time.monotonic to step forward past cooldown each call but stay
    # within the hour so the hour-cap kicks in instead.
    fake_time = [0.0]

    def now() -> float:
        return fake_time[0]

    monkeypatch.setattr(time, "monotonic", now)
    t = ProactiveThrottler(max_per_hour=3, min_cooldown_s=10)
    for _ in range(3):
        fake_time[0] += 100  # exceed cooldown but stay < 3600
        assert t.allow("inst-1") is True
    fake_time[0] += 100
    assert t.allow("inst-1") is False  # hourly cap hit


def test_old_entries_expire_after_hour(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_time = [0.0]

    def now() -> float:
        return fake_time[0]

    monkeypatch.setattr(time, "monotonic", now)
    t = ProactiveThrottler(max_per_hour=1, min_cooldown_s=1)
    assert t.allow("inst-1") is True
    fake_time[0] += 3700  # > 1 hour later
    assert t.allow("inst-1") is True  # old entry expired
