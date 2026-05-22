"""SignalFuser — weighted fusion, confidence gating."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from eidolon_agent.core.types.signal import RealtimeSignal, SignalModality
from eidolon_agent.domain.signals import SignalBus, SignalFuser

pytestmark = pytest.mark.functional


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def test_digest_empty_window_returns_default() -> None:
    fuser = SignalFuser(SignalBus())
    digest = await fuser.digest("s-none")
    assert digest.dominant_emotion is None
    assert digest.presence == "present"
    assert digest.confidence_overall == 0.0


async def test_low_confidence_signals_are_dropped() -> None:
    bus = SignalBus()
    # Below 0.6 confidence floor
    await bus.publish(
        "s1",
        RealtimeSignal(ts=_now(), modality=SignalModality.FACE, label="angry", confidence=0.3),
    )
    fuser = SignalFuser(bus)
    digest = await fuser.digest("s1")
    assert digest.dominant_emotion is None


async def test_weighted_vote_picks_face_over_ambient() -> None:
    bus = SignalBus()
    # ambient has tiny weight; face has 0.4. Even with more ambient votes,
    # a single high-confidence face signal should dominate.
    await bus.publish(
        "s1",
        RealtimeSignal(ts=_now(), modality=SignalModality.FACE, label="smile", confidence=0.95),
    )
    for _ in range(3):
        await bus.publish(
            "s1",
            RealtimeSignal(ts=_now(), modality=SignalModality.AMBIENT, label="neutral", confidence=0.9),
        )
    digest = await SignalFuser(bus).digest("s1")
    assert digest.dominant_emotion == "smile"


async def test_speech_rate_aggregates_from_prosody() -> None:
    bus = SignalBus()
    for _ in range(3):
        await bus.publish(
            "s1",
            RealtimeSignal(ts=_now(), modality=SignalModality.PROSODY, label="fast", confidence=0.9),
        )
    await bus.publish(
        "s1",
        RealtimeSignal(ts=_now(), modality=SignalModality.PROSODY, label="slow", confidence=0.9),
    )
    digest = await SignalFuser(bus).digest("s1")
    assert digest.speech_rate == "fast"


async def test_notable_events_collected_and_counted() -> None:
    bus = SignalBus()
    for _ in range(2):
        await bus.publish(
            "s1",
            RealtimeSignal(ts=_now(), modality=SignalModality.PROSODY, label="sigh", confidence=0.9),
        )
    digest = await SignalFuser(bus).digest("s1")
    assert any(n.startswith("sigh_x") for n in digest.notable_events)
