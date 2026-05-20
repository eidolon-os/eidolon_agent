"""Realtime multimodal signals pushed by the caller (LiveKit pipeline).

The fuser collects raw signals over a sliding window and emits a single
:class:`SignalDigest` per turn. Only digests above a confidence threshold are
injected into the prompt — noisy low-confidence signals must not bias the LLM.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Literal


class SignalModality(str, Enum):
    ASR = "asr"  # speech recognition derivatives (filler words, length)
    PROSODY = "prosody"  # pitch, rate, jitter
    FACE = "face"  # facial expression
    GAZE = "gaze"  # gaze direction / blink rate
    AMBIENT = "ambient"  # room noise, presence
    TEXT_SENTIMENT = "text_sentiment"  # text-derived sentiment


@dataclass(frozen=True, slots=True)
class RealtimeSignal:
    """A single signal sample. Producers push these via gRPC PushSignal."""

    ts: datetime
    modality: SignalModality
    label: str  # e.g. "smile", "sigh", "angry_tone", "silence_5s"
    confidence: float  # 0..1
    raw: dict = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class SignalDigest:
    """Fused, low-noise summary over a sliding window. Safe to inject into prompts."""

    window_ms: int
    dominant_emotion: str | None = None
    emotion_confidence: float = 0.0
    speech_rate: Literal["slow", "normal", "fast"] | None = None
    presence: Literal["present", "away", "distracted"] = "present"
    confidence_overall: float = 0.0
    notable_events: tuple[str, ...] = ()  # e.g. ("sigh_x1",)

    def to_prompt_line(self, threshold: float = 0.6) -> str:
        if self.confidence_overall < threshold:
            return ""
        bits: list[str] = []
        if self.dominant_emotion and self.emotion_confidence >= threshold:
            bits.append(f"用户当前情绪偏 {self.dominant_emotion}")
        if self.speech_rate and self.speech_rate != "normal":
            bits.append(f"语速{'偏慢' if self.speech_rate == 'slow' else '偏快'}")
        if self.presence != "present":
            bits.append("用户注意力不在场" if self.presence == "away" else "用户分神")
        if self.notable_events:
            bits.append("信号: " + ",".join(self.notable_events))
        return "；".join(bits)
