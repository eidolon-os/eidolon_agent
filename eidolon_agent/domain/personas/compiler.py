"""Compile a companion persona into LLM-facing prompt text."""

from __future__ import annotations

from eidolon_agent.domain.personas.types import (
    AdaptedMemoryContext,
    BehavioralKnob,
    CompanionPersona,
    CompiledPersona,
    PersonaRuntimeState,
)


class PersonaCompiler:
    def compile(
        self,
        *,
        instance: CompanionPersona,
        adapted_memory: AdaptedMemoryContext | None = None,
        runtime_state: PersonaRuntimeState | None = None,
        realtime: dict | None = None,
    ) -> CompiledPersona:
        adapted_memory = adapted_memory or AdaptedMemoryContext()
        effective_knobs = _apply_transient_adjustments(
            instance.behavioral_knobs,
            adapted_memory.transient_knob_adjustments,
        )
        identity_block = _identity_block(instance)
        style_block, trace = _style_block(instance, effective_knobs)
        memory_block = _memory_block(adapted_memory)
        runtime_state_block = _runtime_state_block(runtime_state)
        realtime_block = _realtime_block(realtime)

        # KV-cache split: identity + style are invariant within a genome
        # version → stable prefix; mood/energy + per-turn memory/realtime
        # change every turn → volatile tail (placed near the current request
        # by the context compiler). Concatenation preserves the old prompt.
        stable_parts = [identity_block, style_block]
        volatile_parts = [
            block
            for block in (runtime_state_block, memory_block, realtime_block)
            if block
        ]
        stable_prompt = "\n\n".join(stable_parts)
        volatile_prompt = "\n\n".join(volatile_parts)
        system_prompt = "\n\n".join(
            part for part in (stable_prompt, volatile_prompt) if part
        )

        return CompiledPersona(
            companion_id=instance.companion_id,
            version=instance.version,
            system_prompt=system_prompt,
            stable_prompt=stable_prompt,
            volatile_prompt=volatile_prompt,
            identity_block=identity_block,
            style_block=style_block,
            memory_block=memory_block,
            runtime_state_block=runtime_state_block,
            transient_knobs={
                key: knob.current for key, knob in effective_knobs.items()
                if key in adapted_memory.transient_knob_adjustments
            },
            debug_trace=tuple(trace),
        )


def _identity_block(instance: CompanionPersona) -> str:
    core = instance.identity_core
    lines = [
        f"你是「{instance.metadata.name}」（{instance.metadata.archetype}）。",
        f"基础称谓/代词：{core.base_pronouns}",
    ]
    if core.values:
        lines.append("价值观：" + "；".join(core.values))
    if core.unbreakable_rules:
        lines.append("不可打破的规则：" + "；".join(core.unbreakable_rules))
    if core.taboos:
        lines.append("绝不：" + "；".join(core.taboos))
    return "\n".join(lines)


def _style_block(
    instance: CompanionPersona,
    effective_knobs: dict[str, BehavioralKnob],
) -> tuple[str, list[str]]:
    lines = ["人格表现指令："]
    lines.extend(f"- {item}" for item in instance.style_compiler.base_instructions)
    trace: list[str] = []
    for knob_name in sorted(instance.style_compiler.knob_mappings):
        knob = effective_knobs.get(knob_name)
        if knob is None:
            continue
        for mapping in instance.style_compiler.knob_mappings[knob_name]:
            lo, hi = mapping.range
            if lo <= knob.current <= hi:
                lines.append(f"- {mapping.instruction}")
                if mapping.preferred_particles:
                    lines.append("- 可自然使用语气词：" + "、".join(mapping.preferred_particles))
                trace.append(f"{knob_name}={knob.current:.3f} -> [{lo:.2f}, {hi:.2f}]")
                break
    return "\n".join(lines), trace


def _memory_block(adapted_memory: AdaptedMemoryContext) -> str:
    lines: list[str] = []
    if adapted_memory.degraded:
        lines.append("记忆状态：记忆暂不可用或部分降级，避免假装记得未提供的信息。")
    if adapted_memory.content:
        lines.append("召回记忆：")
        lines.append(adapted_memory.content)
    if adapted_memory.instructions:
        lines.append("记忆消费方式：")
        lines.extend(f"- {item}" for item in adapted_memory.instructions)
    return "\n".join(lines)


def _runtime_state_block(runtime_state: PersonaRuntimeState | None) -> str:
    if runtime_state is None:
        return ""
    hint = runtime_state.to_prompt_hint()
    if not hint:
        return ""
    return "当前人格状态：" + hint


def _realtime_block(realtime: dict | None) -> str:
    if not realtime:
        return ""
    bits = [f"{key}={value}" for key, value in sorted(realtime.items()) if value is not None]
    if not bits:
        return ""
    return "实时上下文：" + "；".join(bits)


def _apply_transient_adjustments(
    knobs: dict[str, BehavioralKnob],
    adjustments: dict[str, float],
) -> dict[str, BehavioralKnob]:
    if not adjustments:
        return knobs
    out = dict(knobs)
    for name, delta in adjustments.items():
        knob = knobs.get(name)
        if knob is None:
            continue
        current = min(knob.max, max(knob.min, knob.current + delta))
        out[name] = knob.model_copy(update={"current": current})
    return out
