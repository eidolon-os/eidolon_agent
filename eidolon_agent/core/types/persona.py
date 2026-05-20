"""Persona = Template (genome, immutable Git YAML) + Overlay (evolution layer).

The resolver merges them into :class:`CompanionProfile` at runtime. The split
guarantees the agent's identity (templates) never drifts while still allowing
per-instance growth (overlays).
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class SpeechStyle(BaseModel):
    """How the persona talks — short, lyrical, formal, etc."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    sentence_length: Literal["short", "medium", "long"] = "medium"
    formality: Literal["casual", "neutral", "formal"] = "casual"
    emoji_density: Literal["none", "light", "rich"] = "light"
    first_person: str = "我"
    catchphrases: tuple[str, ...] = ()
    avoid_phrases: tuple[str, ...] = ()


class Big5(BaseModel):
    """OCEAN personality vector; each in [0, 1]."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    openness: float = Field(0.6, ge=0.0, le=1.0)
    conscientiousness: float = Field(0.6, ge=0.0, le=1.0)
    extraversion: float = Field(0.5, ge=0.0, le=1.0)
    agreeableness: float = Field(0.7, ge=0.0, le=1.0)
    neuroticism: float = Field(0.3, ge=0.0, le=1.0)


class EvolutionPolicy(BaseModel):
    """What evolution is allowed to touch on this template."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    big5_step_max: float = Field(0.1, ge=0.0, le=1.0)
    evolvable_fields: tuple[str, ...] = (
        "speech_style.catchphrases",
        "speech_style.emoji_density",
        "skills",
        "milestones",
    )
    require_human_approval_milestones: bool = True
    proposal_cooldown_hours: int = 24


class SkillRef(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    enabled: bool = True
    unlocked_at_bond: int = 0


# ---------------------------------------------------------------------------
# Template (immutable genome) and Overlay (evolution layer)
# ---------------------------------------------------------------------------


class PersonaTemplate(BaseModel):
    """Genome. Lives in personas/templates/*.yaml — Git-managed, read-only at runtime."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: int = 1
    template_id: str
    version: int = 1  # bumped on template content change
    name: str
    archetype: Literal["caretaker", "mentor", "playmate", "muse", "custom"]
    pronouns: str = "她/她"
    big5_base: Big5 = Field(default_factory=Big5)
    speech_style_base: SpeechStyle = Field(default_factory=SpeechStyle)
    values_base: tuple[str, ...] = ()
    taboos: tuple[str, ...] = ()
    skills: tuple[SkillRef, ...] = ()
    locked_fields: tuple[str, ...] = ("template_id", "archetype", "taboos")
    evolution_policy: EvolutionPolicy = Field(default_factory=EvolutionPolicy)
    default_voice_id: str | None = None
    default_avatar_id: str | None = None
    description: str = ""


class PersonaOverlay(BaseModel):
    """Per-instance evolution layer. Lives in personas/overlays/<tenant>/<user>/<instance>.yaml."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: int = 1
    instance_id: str
    template_id: str
    template_version: int
    created_at: datetime
    updated_at: datetime
    overrides: dict = Field(default_factory=dict)  # only evolvable_fields end up here
    unlocked_skills: tuple[str, ...] = ()
    milestones: tuple[str, ...] = ()
    bond_history_summary: str = ""
    nickname_alias: str | None = None
    voice_id: str | None = None
    avatar_id: str | None = None
    last_git_commit: str | None = None


class CompanionProfile(BaseModel):
    """The merged runtime profile = Template ⊕ Overlay."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    instance_id: str
    template_id: str
    template_version: int
    name: str
    archetype: str
    pronouns: str
    big5: Big5
    speech_style: SpeechStyle
    values: tuple[str, ...]
    taboos: tuple[str, ...]
    skills: tuple[SkillRef, ...]
    voice_id: str | None
    avatar_id: str | None
    milestones: tuple[str, ...] = ()
    bond_history_summary: str = ""
    nickname_alias: str | None = None
    locked_fields: tuple[str, ...] = ()


# ---------------------------------------------------------------------------
# Evolution delta
# ---------------------------------------------------------------------------


class EvolutionDelta(BaseModel):
    """A proposed (and possibly applied) change to a PersonaOverlay."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    instance_id: str
    from_overlay_version: int
    to_overlay_version: int
    changed_fields: dict[str, tuple]  # field_path -> (old, new)
    rationale: str
    proposed_by: Literal["self", "emotion_service", "user_feedback", "admin"]
    requires_human_approval: bool
    approved_by: str | None = None
    applied_at: datetime | None = None
    git_commit: str | None = None
    rolled_back_at: datetime | None = None
