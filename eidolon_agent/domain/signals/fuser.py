"""Fuse realtime signal samples into a prompt-safe digest."""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta, timezone

from eidolon_agent.core.types.signal import RealtimeSignal, SignalDigest, SignalModality


class SignalFuser:
    def __init__(
        self,
        *,
        window_ms: int = 3000,
        confidence_threshold: float = 0.6,
    ) -> None:
        self.window_ms = window_ms
        self.confidence_threshold = confidence_threshold

    def fuse(
        self,
        signals: list[RealtimeSignal],
        *,
        now: datetime | None = None,
    ) -> SignalDigest | None:
        now = now or datetime.now(timezone.utc)
        cutoff = now - timedelta(milliseconds=self.window_ms)
        fresh = [
            s for s in signals
            if s.ts >= cutoff and s.confidence >= self.confidence_threshold
        ]
        if not fresh:
            return None

        emotion_votes: Counter[str] = Counter()
        notable: list[str] = []
        speech_rate: str | None = None
        presence = "present"
        confidences: list[float] = []

        for sig in fresh:
            confidences.append(sig.confidence)
            label = sig.label
            if sig.modality in {
                SignalModality.PROSODY,
                SignalModality.FACE,
                SignalModality.TEXT_SENTIMENT,
            }:
                emotion_votes[label] += sig.confidence
            elif sig.modality is SignalModality.ASR and label in {"slow", "normal", "fast"}:
                speech_rate = label
            elif sig.modality in {SignalModality.GAZE, SignalModality.AMBIENT}:
                if label in {"away", "distracted"}:
                    presence = label
                else:
                    notable.append(label)
            else:
                notable.append(label)

        dominant = None
        emotion_conf = 0.0
        if emotion_votes:
            dominant, total = emotion_votes.most_common(1)[0]
            emotion_conf = min(1.0, float(total))

        return SignalDigest(
            window_ms=self.window_ms,
            dominant_emotion=dominant,
            emotion_confidence=emotion_conf,
            speech_rate=speech_rate,  # type: ignore[arg-type]
            presence=presence,  # type: ignore[arg-type]
            confidence_overall=sum(confidences) / len(confidences),
            notable_events=tuple(notable),
        )


__all__ = ["SignalFuser"]
