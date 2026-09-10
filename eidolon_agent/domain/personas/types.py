"""Runtime persona types derived from the canonical SDK genome contract."""

from __future__ import annotations

import math
from datetime import datetime, timezone
from enum import Enum
from typing import Literal

from eidolon_sdk.biz.persona import ConversationPreferences, PersonaEvidenceRef, PersonaGenome
from pydantic import BaseModel, ConfigDict, Field


class AttentionTarget(str, Enum):
    USER = "user"
    TASK = "task"
    DRIFT = "drift"
    IDLE = "idle"


class MoodVector(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    joy: float = Field(0.0, ge=0.0)
    sad: float = Field(0.0, ge=0.0)
    anger: float = Field(0.0, ge=0.0)
    fear: float = Field(0.0, ge=0.0)
    surprise: float = Field(0.0, ge=0.0)
    intensity: float = Field(0.0, ge=0.0, le=1.0)
    decay_half_life_s: int = Field(1800, ge=1)
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    def decayed(self, now: datetime) -> "MoodVector":
        elapsed = max(0.0, (now - self.updated_at).total_seconds())
        if elapsed == 0 or self.intensity <= 0:
            return self
        factor = math.pow(0.5, elapsed / self.decay_half_life_s)
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
        return max(
            (
                ("joy", self.joy),
                ("sad", self.sad),
                ("anger", self.anger),
                ("fear", self.fear),
                ("surprise", self.surprise),
            ),
            key=lambda item: item[1],
        )


class Energy(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    level: float = Field(0.7, ge=0.0, le=1.0)
    circadian_phase: float = Field(0.5, ge=0.0, le=1.0)
    refilled_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class Attention(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    target: AttentionTarget = AttentionTarget.IDLE
    focus_score: float = Field(0.0, ge=0.0, le=1.0)
    last_user_signal_at: datetime | None = None


class PersonaRuntimeState(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    mood: MoodVector = Field(default_factory=MoodVector)
    energy: Energy = Field(default_factory=Energy)
    attention: Attention = Field(default_factory=Attention)
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    def decayed(self, now: datetime | None = None) -> "PersonaRuntimeState":
        now = now or datetime.now(timezone.utc)
        return self.model_copy(update={"mood": self.mood.decayed(now), "updated_at": now})

    def to_prompt_hint(self) -> str:
        parts: list[str] = []
        label, value = self.mood.dominant()
        if value > 0.4:
            parts.append(
                {
                    "joy": "心情不错",
                    "sad": "略低落",
                    "anger": "有点烦躁",
                    "fear": "有些不安",
                    "surprise": "微微惊讶",
                }[label]
            )
        if self.energy.level < 0.3:
            parts.append("精力有点低")
        elif self.energy.level > 0.85:
            parts.append("状态饱满")
        if self.attention.target == AttentionTarget.USER:
            parts.append("注意力在 owner 身上")
        elif self.attention.target == AttentionTarget.DRIFT:
            parts.append("思绪有点跳")
        return "；".join(parts)


class StoredPersonaGenome(BaseModel):
    """One committed immutable snapshot with its persistence identity."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    owner_id: str
    companion_id: str
    genome_id: str
    genome_hash: str
    realizer_version: str
    version: int = Field(ge=1)
    status: str = "committed"
    genome: PersonaGenome
    conversation_preferences: ConversationPreferences = Field(default_factory=ConversationPreferences)
    preference_revision: int = 1


class PersonaSnapshot(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    stored: StoredPersonaGenome
    runtime_state: PersonaRuntimeState
    prompt_hint: str = ""


class AdaptedMemoryContext(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    content: str = ""
    instructions: tuple[str, ...] = ()
    evidence_refs: tuple[PersonaEvidenceRef, ...] = ()
    degraded: bool = False


class RealizedPersona(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    owner_id: str
    companion_id: str
    genome_id: str
    genome_hash: str
    version: int
    system_prompt: str
    identity_block: str
    expression_block: str
    memory_block: str
    runtime_state_block: str = ""
    stable_prompt: str = ""
    volatile_prompt: str = ""
    spoken_phrases: dict[str, str] = Field(default_factory=dict)
    evidence_refs: tuple[PersonaEvidenceRef, ...] = ()
    debug_trace: tuple[str, ...] = ()
    stored: StoredPersonaGenome | None = Field(default=None, exclude=True)


class PersonaSignalInput(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    owner_id: str
    companion_id: str
    dominant_emotion: str | None = None
    emotion_confidence: float = Field(0.0, ge=0.0, le=1.0)
    speech_rate: Literal["slow", "normal", "fast"] | None = None
    presence: Literal["present", "away", "distracted"] = "present"
    confidence_overall: float = Field(0.0, ge=0.0, le=1.0)
    notable_events: tuple[str, ...] = ()
    created_at: datetime | None = None


class PersonaProactiveDecision(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    companion_id: str
    owner_id: str
    intent: str
    text: str
    style_hint: str = ""
    cooldown_s: int = Field(300, ge=0)


__all__ = [
    "AdaptedMemoryContext",
    "Attention",
    "AttentionTarget",
    "Energy",
    "MoodVector",
    "PersonaProactiveDecision",
    "PersonaRuntimeState",
    "PersonaSignalInput",
    "PersonaSnapshot",
    "RealizedPersona",
    "StoredPersonaGenome",
]
