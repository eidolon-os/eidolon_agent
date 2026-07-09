"""Companion-first genome authoring.

Owners author a companion's genome directly (companion-first; no template reuse).
This module turns the free-text-primary authored content into a validated
``CompanionPersona`` and renders a human-readable prompt preview. Pure
functions — no I/O — so they are trivially unit-testable and side-effect free.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime, timezone

from eidolon_agent.domain.personas.types import (
    BehavioralKnob,
    CompanionPersona,
    IdentityCore,
    PersonaMetadata,
    StyleCompiler,
)

AUTHORED_TEMPLATE_ID = "authored"


def assemble_genome(
    *,
    owner_id: str,
    companion_id: str,
    name: str,
    version: int = 1,
    archetype: str = "companion",
    description: str = "",
    pronouns: str = "她",
    values: Sequence[str] = (),
    taboos: Sequence[str] = (),
    unbreakable_rules: Sequence[str] = (),
    style: Sequence[str] = (),
    example_dialogs: Sequence[str] = (),
    goals: Sequence[str] = (),
    pinned_facts: Sequence[str] = (),
    relationship_stage: str = "",
    knobs: Mapping[str, float] | None = None,
    created_at: datetime | None = None,
) -> CompanionPersona:
    """Assemble authored content into a validated CompanionPersona (no template).

    Raises pydantic ValidationError on invalid input (e.g. out-of-range knobs).
    """
    now = created_at or datetime.now(timezone.utc)
    return CompanionPersona(
        companion_id=companion_id,
        owner_id=owner_id,
        origin_template_id=AUTHORED_TEMPLATE_ID,
        origin_template_revision=1,
        version=version,
        created_at=now,
        updated_at=now,
        metadata=PersonaMetadata(
            template_id=AUTHORED_TEMPLATE_ID,
            template_revision=1,
            archetype=archetype or "companion",
            name=name,
            description=description,
        ),
        identity_core=IdentityCore(
            base_pronouns=pronouns or "她",
            unbreakable_rules=tuple(unbreakable_rules),
            values=tuple(values),
            taboos=tuple(taboos),
        ),
        behavioral_knobs={k: BehavioralKnob(current=float(v)) for k, v in (knobs or {}).items()},
        style_compiler=StyleCompiler(base_instructions=tuple(style)),
        example_dialogs=tuple(example_dialogs),
        goals=tuple(goals),
        pinned_facts=tuple(pinned_facts),
        relationship_stage=relationship_stage or "",
    )


def render_authored_markdown(persona: CompanionPersona) -> str:
    """Render the authored layers as a human-readable markdown genome sheet."""
    meta = persona.metadata
    ic = persona.identity_core
    lines: list[str] = [f"# {meta.name}", ""]
    if meta.archetype:
        lines.append(f"**原型**：{meta.archetype}")
    if meta.description:
        lines.append("")
        lines.append(meta.description)

    def _section(title: str, items: Sequence[str]) -> None:
        if not items:
            return
        lines.append("")
        lines.append(f"## {title}")
        lines.extend(f"- {item}" for item in items)

    _section("价值观", ic.values)
    _section("禁忌", ic.taboos)
    _section("铁律", ic.unbreakable_rules)
    _section("说话风格", persona.style_compiler.base_instructions)
    _section("目标", persona.goals)
    _section("关于主人（钉住）", persona.pinned_facts)
    _section("示例对话", persona.example_dialogs)
    if persona.relationship_stage:
        lines.append("")
        lines.append(f"**关系阶段**：{persona.relationship_stage}")
    return "\n".join(lines).strip() + "\n"
