"""Canonical personas domain types.

Templates define the base personality genome. Companion personas are full
per-owner copies that evolve independently after an owner binds a template.
"""

from __future__ import annotations

import math
from datetime import datetime, timezone
from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# ---------------------------------------------------------------------------
# Legacy-key shim (persistence compatibility).
#
# Rows persisted before the eidolon_data vocabulary rename may still carry the
# old identity keys ``user_id`` / ``instance_id`` / ``overlay_version`` and the
# removed ``tenant_id``. This single, centralized function maps old keys onto
# the new field names and drops ``tenant_id`` so old DB rows / YAML files stay
# readable. It is wired as a ``model_validator(mode="before")`` on every model
# that is persisted with identity keys.
# ---------------------------------------------------------------------------

_LEGACY_IDENTITY_KEYS = {
    "user_id": "owner_id",
    "instance_id": "companion_id",
    "overlay_version": "version",
}


def migrate_legacy_identity_keys(data: object) -> object:
    if not isinstance(data, dict):
        return data
    if not (data.keys() & (_LEGACY_IDENTITY_KEYS.keys() | {"tenant_id"})):
        return data
    out = dict(data)
    out.pop("tenant_id", None)
    for old_key, new_key in _LEGACY_IDENTITY_KEYS.items():
        if old_key in out:
            value = out.pop(old_key)
            out.setdefault(new_key, value)
    return out


class PersonaMetadata(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    template_id: str
    template_revision: int = Field(1, ge=1)
    archetype: str
    name: str
    description: str = ""


class IdentityCore(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    base_pronouns: str = "她"
    unbreakable_rules: tuple[str, ...] = ()
    values: tuple[str, ...] = ()
    taboos: tuple[str, ...] = ()


class BehavioralKnob(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    current: float = Field(..., ge=0.0, le=1.0)
    min: float = Field(0.0, ge=0.0, le=1.0)
    max: float = Field(1.0, ge=0.0, le=1.0)
    step_limit: float = Field(0.05, ge=0.0, le=1.0)
    cooldown_hours: int = Field(0, ge=0)
    last_changed_at: datetime | None = None

    @model_validator(mode="after")
    def _validate_range(self) -> BehavioralKnob:
        if self.min > self.max:
            raise ValueError("knob min must be <= max")
        if self.current < self.min or self.current > self.max:
            raise ValueError("knob current must be within min/max")
        return self


class StyleRange(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    range: tuple[float, float]
    instruction: str
    preferred_particles: tuple[str, ...] = ()

    @field_validator("range")
    @classmethod
    def _validate_range(cls, value: tuple[float, float]) -> tuple[float, float]:
        if len(value) != 2:
            raise ValueError("range must contain exactly two values")
        lo, hi = value
        if not (0.0 <= lo <= hi <= 1.0):
            raise ValueError("range bounds must satisfy 0 <= lo <= hi <= 1")
        return value


class StyleCompiler(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    knob_mappings: dict[str, tuple[StyleRange, ...]] = Field(default_factory=dict)
    base_instructions: tuple[str, ...] = ()
    # Hot-path canned lines a persona may override (keyed by, e.g.,
    # "tool_preamble", "slow_tool_hint", "refuse"). Read via PersonaVoice.phrase
    # on the latency-critical path — template lookups only, never LLM-rendered.
    spoken_phrases: dict[str, str] = Field(default_factory=dict)


class RetrievedFactHandling(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    recency_bias: float = Field(0.5, ge=0.0, le=1.0)
    emotion_resonance: float = Field(0.5, ge=0.0, le=1.0)


class GraphRelationPolicy(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    relation_type: str
    reaction_style: str
    transient_knob_adjustments: dict[str, float] = Field(default_factory=dict)
    persistent_events: tuple[str, ...] = ()


class MemoryAdapterSpec(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    retrieved_fact_handling: RetrievedFactHandling = Field(default_factory=RetrievedFactHandling)
    graph_relation_policies: tuple[GraphRelationPolicy, ...] = ()


class EvolutionRule(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    event: str
    target: str
    action: Literal["increase", "decrease", "set"]
    amount: float = Field(..., ge=0.0, le=1.0)
    cooldown_hours: int = Field(0, ge=0)


class EvolutionState(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    applied_rule_timestamps: dict[str, datetime] = Field(default_factory=dict)
    last_event_summary: str = ""


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

    def decayed(self, now: datetime) -> MoodVector:
        dt = max(0.0, (now - self.updated_at).total_seconds())
        if dt == 0 or self.intensity <= 0:
            return self
        factor = math.pow(0.5, dt / self.decay_half_life_s)
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
        candidates = (
            ("joy", self.joy),
            ("sad", self.sad),
            ("anger", self.anger),
            ("fear", self.fear),
            ("surprise", self.surprise),
        )
        return max(candidates, key=lambda item: item[1])


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

    def decayed(self, now: datetime | None = None) -> PersonaRuntimeState:
        now = now or datetime.now(timezone.utc)
        return self.model_copy(update={"mood": self.mood.decayed(now), "updated_at": now})

    def to_prompt_hint(self) -> str:
        parts: list[str] = []
        label, value = self.mood.dominant()
        if value > 0.4:
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
        if self.attention.target == AttentionTarget.USER:
            parts.append("注意力在用户身上")
        elif self.attention.target == AttentionTarget.DRIFT:
            parts.append("思绪有点跳")
        return "；".join(parts)


class PersonaAssets(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    default_voice_id: str | None = None
    default_avatar_id: str | None = None


class PersonaTemplate(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    metadata: PersonaMetadata
    identity_core: IdentityCore
    behavioral_knobs: dict[str, BehavioralKnob]
    style_compiler: StyleCompiler = Field(default_factory=StyleCompiler)
    memory_adapter: MemoryAdapterSpec = Field(default_factory=MemoryAdapterSpec)
    evolution_rules: tuple[EvolutionRule, ...] = ()
    assets: PersonaAssets = Field(default_factory=PersonaAssets)


class CompanionPersona(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    @model_validator(mode="before")
    @classmethod
    def _shim_legacy_keys(cls, data: object) -> object:
        return migrate_legacy_identity_keys(data)

    companion_id: str
    owner_id: str
    origin_template_id: str
    origin_template_revision: int
    # Monotonic per-companion version stamp. Incremented every time the
    # evolution worker (or admin edit / rollback) writes a new overlay. Used
    # for observability (Turn metadata) and admin rollback comparisons.
    version: int = Field(1, ge=1)
    created_at: datetime
    updated_at: datetime
    metadata: PersonaMetadata
    identity_core: IdentityCore
    behavioral_knobs: dict[str, BehavioralKnob]
    style_compiler: StyleCompiler = Field(default_factory=StyleCompiler)
    memory_adapter: MemoryAdapterSpec = Field(default_factory=MemoryAdapterSpec)
    evolution_rules: tuple[EvolutionRule, ...] = ()
    evolution_state: EvolutionState = Field(default_factory=EvolutionState)
    assets: PersonaAssets = Field(default_factory=PersonaAssets)


class PersonaTemplateSummary(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    template_id: str
    template_revision: int
    name: str
    archetype: str
    description: str = ""


class AdaptedMemoryContext(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    content: str = ""
    instructions: tuple[str, ...] = ()
    transient_knob_adjustments: dict[str, float] = Field(default_factory=dict)
    triggered_events: tuple[str, ...] = ()
    degraded: bool = False


class CompiledPersona(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    companion_id: str
    # Snapshot of the persona's version at compile time. Threaded
    # through the Turn pipeline so post-turn events / admin probes can answer
    # "which persona overlay produced this answer?" without an extra DB hit.
    version: int = 1
    system_prompt: str
    identity_block: str
    style_block: str
    memory_block: str
    runtime_state_block: str = ""
    # KV-cache split (P1-C): ``stable_prompt`` is invariant within a genome
    # version (identity + style) and belongs in the cached prompt prefix;
    # ``volatile_prompt`` (mood/energy + per-turn memory/realtime) changes each
    # turn and belongs at the tail near the current request. ``system_prompt``
    # is their concatenation, kept for callers that want the whole thing.
    stable_prompt: str = ""
    volatile_prompt: str = ""
    # Genome-defined hot-path canned lines (see StyleCompiler.spoken_phrases),
    # threaded through so the turn engine can persona-override outward utterances
    # (soften, slow-tool hint, coworker announcement) without an extra load.
    spoken_phrases: dict[str, str] = Field(default_factory=dict)
    transient_knobs: dict[str, float] = Field(default_factory=dict)
    debug_trace: tuple[str, ...] = ()


class PersonaSnapshot(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    instance: CompanionPersona
    runtime_state: PersonaRuntimeState
    prompt_hint: str = ""


class PersonaEvolutionEvent(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: str
    source: str = "system"
    confidence: float = Field(1.0, ge=0.0, le=1.0)
    payload: dict = Field(default_factory=dict)
    created_at: datetime | None = None


class PersonaObservation(BaseModel):
    """Evidence about how a companion persona should evolve.

    Observations are durable evidence, not direct persona edits. The reflection
    step may convert several observations into a bounded proposal.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    @model_validator(mode="before")
    @classmethod
    def _shim_legacy_keys(cls, data: object) -> object:
        return migrate_legacy_identity_keys(data)

    id: str
    owner_id: str
    companion_id: str
    kind: str
    source: str = "system"
    status: Literal["active", "dismissed", "converted"] = "active"
    strength: float = Field(0.5, ge=0.0, le=1.0)
    confidence: float = Field(0.5, ge=0.0, le=1.0)
    summary: str = ""
    evidence: dict = Field(default_factory=dict)
    memory_ids: tuple[str, ...] = ()
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class PersonaProposalPatch(BaseModel):
    """A narrow, auditable change proposed for a persona overlay."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    type: Literal["knob_delta", "memory_policy_hint", "style_preference_hint"]
    target: str
    delta: float | None = Field(default=None, ge=-1.0, le=1.0)
    value: str | None = None
    rationale: str = ""


class PersonaEvolutionProposal(BaseModel):
    """Admin-reviewable evolution proposal for a companion persona."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    @model_validator(mode="before")
    @classmethod
    def _shim_legacy_keys(cls, data: object) -> object:
        return migrate_legacy_identity_keys(data)

    id: str
    owner_id: str
    companion_id: str
    status: Literal["pending", "applied", "rejected"] = "pending"
    patches: tuple[PersonaProposalPatch, ...] = ()
    confidence: float = Field(0.0, ge=0.0, le=1.0)
    rationale: str = ""
    evidence_ids: tuple[str, ...] = ()
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    decided_by: str | None = None
    decided_at: datetime | None = None
    decision_reason: str | None = None


class PersonaInteractionEvent(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    owner_id: str
    companion_id: str
    genome_id: str | None = None
    kind: str
    user_text: str = ""
    assistant_text: str = ""
    payload: dict = Field(default_factory=dict)
    created_at: datetime | None = None


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


class PersonaEvolutionChange(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    path: str
    old: object
    new: object
    rule_id: str | None = None


class PersonaEvolutionResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    @model_validator(mode="before")
    @classmethod
    def _shim_legacy_keys(cls, data: object) -> object:
        return migrate_legacy_identity_keys(data)

    companion_id: str
    applied: bool
    changes: tuple[PersonaEvolutionChange, ...] = ()
    events: tuple[PersonaEvolutionEvent, ...] = ()
    rationale: str = ""


class PersonaMockResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    compiled: CompiledPersona
    evolution: PersonaEvolutionResult | None = None
