"""Canonical personas domain types.

Templates define the base personality genome. Instances are full per-user
copies that evolve independently after a user binds a template.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


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


class PersonaInstance(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    instance_id: str
    tenant_id: str
    user_id: str
    origin_template_id: str
    origin_template_revision: int
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

    instance_id: str
    system_prompt: str
    identity_block: str
    style_block: str
    memory_block: str
    transient_knobs: dict[str, float] = Field(default_factory=dict)
    debug_trace: tuple[str, ...] = ()


class PersonaEvolutionEvent(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: str
    source: str = "system"
    confidence: float = Field(1.0, ge=0.0, le=1.0)
    payload: dict = Field(default_factory=dict)
    created_at: datetime | None = None


class PersonaEvolutionChange(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    path: str
    old: object
    new: object
    rule_id: str | None = None


class PersonaEvolutionResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    instance_id: str
    applied: bool
    changes: tuple[PersonaEvolutionChange, ...] = ()
    events: tuple[PersonaEvolutionEvent, ...] = ()
    rationale: str = ""


class PersonaMockResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    compiled: CompiledPersona
    evolution: PersonaEvolutionResult | None = None
