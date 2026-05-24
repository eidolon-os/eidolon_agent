"""Render a :class:`PersonaTemplate` into a self-contained markdown document.

Why this exists separately from :mod:`compiler`:
    ``PersonaCompiler.compile`` works on a fully-instantiated
    :class:`PersonaInstance` plus runtime context (memory adapter, realtime
    state, transient overlays). The output is meant to be fed straight to the
    LLM mid-turn — it bakes in evolution overlays and is not stable across
    calls. The Phase 25 use case is different: admin operator picks a
    template, we want a stable initial markdown snapshot at *bind* time, with
    no runtime/memory/transient context. Bending compiler to also serve this
    case would force it to handle "template-only mode" branches everywhere,
    which fights its current design.

    The two paths can converge later if/when we decide device-bound agents
    should also go through the evolution loop — but that's an explicit
    follow-up, not something this phase decides.

This renderer is a **pure function**: takes a :class:`PersonaTemplate`, returns
a string. No I/O, no side effects, no awareness of NATS, devices, instances,
or anything outside the template object. That's what lets the admin
orchestrator wrap it in a "render → write to NATS" step without coupling
agent to admin's concerns.
"""
from __future__ import annotations

from .types import (
    BehavioralKnob,
    PersonaTemplate,
    StyleCompiler,
    StyleRange,
)


def render_template_markdown(template: PersonaTemplate) -> str:
    """Render the template's initial state as markdown.

    "Initial state" = each knob at its declared ``current`` value, no
    evolution applied. The output is meant as the *starting* soul.md for a
    device-bound agent; once written to NATS it can be edited freely by the
    operator without re-touching this function.
    """
    sections: list[str] = []
    sections.append(_render_header(template))
    sections.append(_render_identity(template))
    sections.append(_render_knobs(template.behavioral_knobs))
    sections.append(_render_style(template.style_compiler, template.behavioral_knobs))
    sections.append(_render_memory_policy(template))
    sections.append(_render_evolution_rules(template))
    sections.append(_render_assets(template))
    # Single trailing newline; collapsing multiple blank lines that the
    # individual section renderers may leave behind.
    return "\n\n".join(s.rstrip() for s in sections if s.strip()) + "\n"


# ---- section renderers (each returns a markdown block) ---------------------

def _render_header(template: PersonaTemplate) -> str:
    m = template.metadata
    lines = [f"# {m.name}", ""]
    lines.append(f"- **template_id**: `{m.template_id}` (revision {m.template_revision})")
    lines.append(f"- **archetype**: {m.archetype}")
    if m.description:
        lines.append("")
        lines.append(m.description)
    return "\n".join(lines)


def _render_identity(template: PersonaTemplate) -> str:
    core = template.identity_core
    lines = ["## 身份核心 / Identity Core", ""]
    lines.append(f"- 称呼自己: **{core.base_pronouns}**")
    if core.unbreakable_rules:
        lines.append("")
        lines.append("### 不可违反的规则")
        for rule in core.unbreakable_rules:
            lines.append(f"- {rule}")
    if core.values:
        lines.append("")
        lines.append("### 价值观")
        for v in core.values:
            lines.append(f"- {v}")
    if core.taboos:
        lines.append("")
        lines.append("### 禁忌")
        for t in core.taboos:
            lines.append(f"- {t}")
    return "\n".join(lines)


def _render_knobs(knobs: dict[str, BehavioralKnob]) -> str:
    if not knobs:
        return ""
    lines = ["## 行为旋钮 / Behavioral Knobs", ""]
    lines.append("| 旋钮 | 当前 | 范围 | 单步上限 | 冷却 (h) |")
    lines.append("|---|---|---|---|---|")
    for name, knob in knobs.items():
        lines.append(
            f"| `{name}` | {knob.current:.2f} | [{knob.min:.2f}, {knob.max:.2f}] "
            f"| {knob.step_limit:.2f} | {knob.cooldown_hours} |"
        )
    return "\n".join(lines)


def _render_style(
    style: StyleCompiler, knobs: dict[str, BehavioralKnob]
) -> str:
    """Pick the style instructions that are active for the *current* knob values.

    A template's style_compiler maps each knob to a tuple of (range, instruction)
    rules. With knobs at their declared "current" value we evaluate which
    range each knob lands in and emit those instructions as the initial style
    guidance. This is exactly the active-range selection logic that
    PersonaCompiler does at turn time, but specialized to the template's
    starting point (no transient adjustments, no evolution overlays).
    """
    base = list(style.base_instructions)
    active: list[tuple[str, str]] = []
    for knob_name, ranges in style.knob_mappings.items():
        knob = knobs.get(knob_name)
        if knob is None:
            continue
        match = _pick_active_range(ranges, knob.current)
        if match is not None:
            active.append((knob_name, match.instruction))

    if not base and not active:
        return ""
    lines = ["## 表达风格 / Style", ""]
    for instr in base:
        lines.append(f"- {instr}")
    for knob_name, instr in active:
        lines.append(f"- ({knob_name}) {instr}")
    return "\n".join(lines)


def _pick_active_range(
    ranges: tuple[StyleRange, ...], value: float
) -> StyleRange | None:
    for r in ranges:
        lo, hi = r.range
        if lo <= value <= hi:
            return r
    return None


def _render_memory_policy(template: PersonaTemplate) -> str:
    spec = template.memory_adapter
    handling = spec.retrieved_fact_handling
    lines = ["## 记忆处理 / Memory Adapter", ""]
    lines.append(f"- 近期偏好 (recency_bias): {handling.recency_bias:.2f}")
    lines.append(f"- 情绪共鸣 (emotion_resonance): {handling.emotion_resonance:.2f}")
    if spec.graph_relation_policies:
        lines.append("")
        lines.append("### 图关系策略")
        for p in spec.graph_relation_policies:
            lines.append(f"- **{p.relation_type}** → {p.reaction_style}")
    return "\n".join(lines)


def _render_evolution_rules(template: PersonaTemplate) -> str:
    if not template.evolution_rules:
        return ""
    lines = ["## 演化规则 / Evolution Rules", ""]
    lines.append("| ID | 触发事件 | 目标 | 动作 | 幅度 | 冷却 (h) |")
    lines.append("|---|---|---|---|---|---|")
    for rule in template.evolution_rules:
        lines.append(
            f"| `{rule.id}` | {rule.event} | `{rule.target}` | "
            f"{rule.action} | {rule.amount:.2f} | {rule.cooldown_hours} |"
        )
    return "\n".join(lines)


def _render_assets(template: PersonaTemplate) -> str:
    a = template.assets
    if not (a.default_voice_id or a.default_avatar_id):
        return ""
    lines = ["## 资源 / Assets", ""]
    if a.default_voice_id:
        lines.append(f"- 默认音色: `{a.default_voice_id}`")
    if a.default_avatar_id:
        lines.append(f"- 默认头像: `{a.default_avatar_id}`")
    return "\n".join(lines)
