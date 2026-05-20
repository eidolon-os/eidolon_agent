"""Mind state — the inner state of the companion at this moment.

Updated post-turn by reflectors; decayed on idle ticks. Values are exposed to
the LLM only as *natural language* hints (via :meth:`MindState.to_prompt_hint`)
so the LLM never echoes the numbers back to the user.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum


class AttentionTarget(str, Enum):
    USER = "user"
    TASK = "task"
    DRIFT = "drift"
    IDLE = "idle"


@dataclass(frozen=True, slots=True)
class MoodVector:
    """Discrete emotions with overall intensity. Half-life governs decay."""

    joy: float = 0.0
    sad: float = 0.0
    anger: float = 0.0
    fear: float = 0.0
    surprise: float = 0.0
    intensity: float = 0.0  # 0..1 scalar magnitude
    decay_half_life_s: int = 1800
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def decayed(self, now: datetime) -> MoodVector:
        dt = max(0.0, (now - self.updated_at).total_seconds())
        if dt == 0 or self.intensity <= 0:
            return self
        factor = math.pow(0.5, dt / max(1, self.decay_half_life_s))
        return MoodVector(
            joy=self.joy * factor,
            sad=self.sad * factor,
            anger=self.anger * factor,
            fear=self.fear * factor,
            surprise=self.surprise * factor,
            intensity=self.intensity * factor,
            decay_half_life_s=self.decay_half_life_s,
            updated_at=now,
        )

    def dominant(self) -> tuple[str, float]:
        """Return (label, value) for the strongest discrete emotion."""
        candidates = (
            ("joy", self.joy),
            ("sad", self.sad),
            ("anger", self.anger),
            ("fear", self.fear),
            ("surprise", self.surprise),
        )
        return max(candidates, key=lambda kv: kv[1])


@dataclass(frozen=True, slots=True)
class Energy:
    level: float = 0.7  # 0..1
    circadian_phase: float = 0.5  # 0..1 — 0=midnight, 0.5=noon
    refilled_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass(frozen=True, slots=True)
class Attention:
    target: AttentionTarget = AttentionTarget.IDLE
    focus_score: float = 0.0  # 0..1
    last_user_signal_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class Bond:
    level: int = 0  # 0..10
    xp: int = 0  # within current level
    milestones: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class MindState:
    mood: MoodVector = field(default_factory=MoodVector)
    energy: Energy = field(default_factory=Energy)
    attention: Attention = field(default_factory=Attention)
    bond: Bond = field(default_factory=Bond)

    def to_prompt_hint(self) -> str:
        """Render as a single natural-language hint line.

        Concise (≤80 chars) by design — the LLM should *sense* the state, not
        recite it. Returns empty string when state is fully neutral.
        """
        parts: list[str] = []
        label, val = self.mood.dominant()
        if val > 0.4:
            parts.append({
                "joy": "心情不错",
                "sad": "略低落",
                "anger": "有点烦躁",
                "fear": "有些不安",
                "surprise": "微微惊讶",
            }[label])
        if self.energy.level < 0.3:
            parts.append("精力有点低")
        elif self.energy.level > 0.85:
            parts.append("状态饱满")
        if self.attention.target == AttentionTarget.DRIFT:
            parts.append("思绪有点跳")
        if self.bond.level >= 5:
            parts.append("跟用户关系亲近")
        return "；".join(parts)
