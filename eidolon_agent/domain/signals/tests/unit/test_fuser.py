"""SignalFuser — realtime signal fusion."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from eidolon_agent.core.types.signal import RealtimeSignal, SignalModality
from eidolon_agent.domain.signals import SignalFuser

pytestmark = pytest.mark.unit


def _sig(label: str, confidence: float, *, modality=SignalModality.PROSODY, age_ms=0):
    now = datetime.now(timezone.utc)
    return RealtimeSignal(
        ts=now - timedelta(milliseconds=age_ms),
        modality=modality,
        label=label,
        confidence=confidence,
        raw={},
    )


def test_low_confidence_signals_are_ignored() -> None:
    assert SignalFuser(confidence_threshold=0.7).fuse([_sig("sad", 0.4)]) is None


def test_expired_signals_are_ignored() -> None:
    assert SignalFuser(window_ms=1000).fuse([_sig("sad", 0.9, age_ms=2000)]) is None


def test_multiple_signals_pick_weighted_dominant_emotion() -> None:
    digest = SignalFuser().fuse([
        _sig("sad", 0.7),
        _sig("calm", 0.8),
        _sig("sad", 0.7),
        _sig("fast", 0.9, modality=SignalModality.ASR),
        _sig("distracted", 0.9, modality=SignalModality.GAZE),
    ])

    assert digest is not None
    assert digest.dominant_emotion == "sad"
    assert digest.speech_rate == "fast"
    assert digest.presence == "distracted"
