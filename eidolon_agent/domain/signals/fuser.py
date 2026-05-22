"""Weighted-confidence fusion of multimodal signals into a single Digest.

Implements the design's confidence-gating rule: signals below the per-modality
threshold are dropped before fusion; the overall digest is itself gated.
"""

from __future__ import annotations

from collections import Counter
from typing import Literal

from eidolon_agent.core.types.signal import RealtimeSignal, SignalDigest, SignalModality
from eidolon_agent.domain.signals.bus import SignalBus

# Per-modality weights and per-signal confidence floor.
_WEIGHTS = {
    SignalModality.FACE: 0.4,
    SignalModality.PROSODY: 0.3,
    SignalModality.TEXT_SENTIMENT: 0.15,
    SignalModality.ASR: 0.1,
    SignalModality.GAZE: 0.03,
    SignalModality.AMBIENT: 0.02,
}
_CONFIDENCE_FLOOR = 0.6


class SignalFuser:
    def __init__(self, bus: SignalBus, *, window_ms: int = 10_000) -> None:
        self._bus = bus
        self._window_ms = window_ms

    async def digest(self, session_id: str) -> SignalDigest:
        samples = await self._bus.recent(session_id, window_ms=self._window_ms)
        if not samples:
            return SignalDigest(window_ms=self._window_ms)
        usable = [s for s in samples if s.confidence >= _CONFIDENCE_FLOOR]
        if not usable:
            return SignalDigest(window_ms=self._window_ms)
        # Weighted vote on dominant emotion label
        votes: Counter[str] = Counter()
        total_weight = 0.0
        for s in usable:
            w = _WEIGHTS.get(s.modality, 0.01) * s.confidence
            votes[s.label] += w  # type: ignore[index]
            total_weight += w
        emotion, w_top = votes.most_common(1)[0]
        emo_conf = w_top / total_weight if total_weight else 0.0

        speech_rate = _aggregate_speech_rate(usable)
        presence = _aggregate_presence(usable)
        notable = tuple(_collect_notable(usable))

        overall = min(1.0, total_weight / max(0.01, sum(_WEIGHTS.values())))
        return SignalDigest(
            window_ms=self._window_ms,
            dominant_emotion=emotion if emo_conf >= _CONFIDENCE_FLOOR else None,
            emotion_confidence=emo_conf,
            speech_rate=speech_rate,
            presence=presence,
            confidence_overall=overall,
            notable_events=notable,
        )


def _aggregate_speech_rate(
    samples: list[RealtimeSignal],
) -> Literal["slow", "normal", "fast"] | None:
    rates = [s.label for s in samples if s.modality is SignalModality.PROSODY and s.label in ("slow", "normal", "fast")]
    if not rates:
        return None
    return Counter(rates).most_common(1)[0][0]  # type: ignore[return-value]


def _aggregate_presence(
    samples: list[RealtimeSignal],
) -> Literal["present", "away", "distracted"]:
    pres = [s.label for s in samples if s.modality is SignalModality.AMBIENT and s.label in ("present", "away", "distracted")]
    if not pres:
        return "present"
    return Counter(pres).most_common(1)[0][0]  # type: ignore[return-value]


def _collect_notable(samples: list[RealtimeSignal]) -> list[str]:
    counts: Counter[str] = Counter()
    for s in samples:
        if s.label in ("sigh", "laugh", "yawn", "silence_5s"):
            counts[s.label] += 1
    return [f"{lbl}_x{n}" for lbl, n in counts.most_common(3)]
