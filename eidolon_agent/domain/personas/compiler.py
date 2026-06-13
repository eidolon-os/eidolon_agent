"""Compile a persona instance into LLM-facing prompt text."""

from __future__ import annotations

from eidolon_agent.domain.personas.types import (
    AdaptedMemoryContext,
    BehavioralKnob,
    CompiledPersona,
    PersonaInstance,
    PersonaRuntimeState,
)


class PersonaCompiler:
    def compile(
        self,
        *,
        instance: PersonaInstance,
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
        tool_policy_block = _tool_policy_block()
        memory_block = _memory_block(adapted_memory)
        runtime_state_block = _runtime_state_block(runtime_state)
        realtime_block = _realtime_block(realtime)

        parts = [identity_block, style_block, tool_policy_block]
        if runtime_state_block:
            parts.append(runtime_state_block)
        if memory_block:
            parts.append(memory_block)
        if realtime_block:
            parts.append(realtime_block)

        return CompiledPersona(
            instance_id=instance.instance_id,
            overlay_version=instance.overlay_version,
            system_prompt="\n\n".join(parts),
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


def _identity_block(instance: PersonaInstance) -> str:
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
    instance: PersonaInstance,
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


def _tool_policy_block() -> str:
    return "\n".join(
        [
            "工具使用策略：",
            "- 能直接回答的问题，直接简洁回答，不要为了展示能力而调用工具。",
            "- 需要真实外部动作、查询、系统事件或异步处理时，必须调用合适工具；不要假装已经完成。",
            "- 需要多步骤、长耗时、外部智能体处理或稍后回流结果的任务，调用 submit_long_task。",
            "- 调用 submit_long_task 后，不要编造最终结果；只说明任务已开始，等待后续进度或结果。",
            "- 工具结果返回后，再基于真实结果总结给用户；工具失败时如实说明并给出可行下一步。",
        ]
    )


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
