"""Realize one canonical persona genome for a runtime modality."""

from __future__ import annotations

from eidolon_sdk.biz.persona import PersonaGenome

from eidolon_agent.core.types.memory import ActiveCommitment
from eidolon_agent.domain.personas.types import (
    AdaptedMemoryContext,
    PersonaRuntimeState,
    RealizedPersona,
    StoredPersonaGenome,
)


class PersonaRealizer:
    """Organize semantic meaning without trait-to-prompt field mappings."""

    def realize(
        self,
        *,
        stored: StoredPersonaGenome,
        adapted_memory: AdaptedMemoryContext | None = None,
        runtime_state: PersonaRuntimeState | None = None,
        realtime: dict | None = None,
        modality: str = "text",
    ) -> RealizedPersona:
        memory = adapted_memory or AdaptedMemoryContext()
        identity_block = _identity_block(stored.genome)
        relationship_block = _relationship_block(stored.genome)
        expression_block = _expression_block(stored.genome, modality=modality)
        memory_block = _memory_block(memory)
        runtime_state_block = _runtime_state_block(runtime_state)
        realtime_block = _realtime_block(realtime)
        stable_prompt = "\n\n".join(
            block for block in (identity_block, relationship_block, expression_block) if block
        )
        volatile_prompt = "\n\n".join(
            block for block in (runtime_state_block, memory_block, realtime_block) if block
        )
        return RealizedPersona(
            owner_id=stored.owner_id,
            companion_id=stored.companion_id,
            genome_id=stored.genome_id,
            genome_hash=stored.genome_hash,
            version=stored.version,
            system_prompt="\n\n".join(
                part for part in (stable_prompt, volatile_prompt) if part
            ),
            stable_prompt=stable_prompt,
            volatile_prompt=volatile_prompt,
            spoken_phrases=dict(stored.genome.expression.signature_phrases),
            identity_block=identity_block,
            expression_block="\n\n".join(
                block for block in (relationship_block, expression_block) if block
            ),
            memory_block=memory_block,
            runtime_state_block=runtime_state_block,
            evidence_refs=memory.evidence_refs,
            debug_trace=(f"realized modality={modality}",),
        )

    def realize_commitment_context(
        self,
        commitments: list[ActiveCommitment],
    ) -> str:
        """Render bounded active Commitment records as relationship context."""
        lines: list[str] = []
        for record in commitments:
            if record.status not in {"proposed", "confirmed"}:
                continue
            parts = [
                f"id={record.commitment_id}",
                f"status={record.status}",
                f"承诺方={record.promisor}",
                f"内容={record.action}",
            ]
            if record.beneficiaries:
                parts.append("受益人=" + "、".join(record.beneficiaries))
            if record.participants:
                parts.append("参与者=" + "、".join(record.participants))
            if record.condition:
                parts.append("条件=" + record.condition)
            if record.due_at:
                parts.append("到期时间=" + record.due_at)
            lines.append("- " + "；".join(parts))
        return "\n".join(lines)


def _identity_block(genome: PersonaGenome) -> str:
    constitution = genome.constitution
    character = genome.character
    lines = [f"你是「{constitution.name}」（{constitution.archetype}）。"]
    if constitution.self_concept:
        lines.append("你如何理解自己：" + constitution.self_concept)
    if character.portrait:
        lines.append("你的人格画像：" + character.portrait)
    if constitution.values:
        lines.append("你重视：" + "；".join(constitution.values))
    if constitution.boundaries:
        lines.append("不可突破的边界：" + "；".join(constitution.boundaries))
    if character.tensions:
        lines.append("你内在持续面对的张力：" + "；".join(character.tensions))
    if character.growth_edges:
        lines.append("你正在学习和成长的方向：" + "；".join(character.growth_edges))
    return "\n".join(lines)


def _relationship_block(genome: PersonaGenome) -> str:
    relationship = genome.relationship
    lines = [f"你与 owner 的关系阶段：{relationship.stage}"]
    if relationship.narrative:
        lines.append("你们的关系：" + relationship.narrative)
    if relationship.commitments:
        lines.append("你对这段关系的承诺：")
        lines.extend(f"- {item}" for item in relationship.commitments)
    if relationship.pinned_facts:
        lines.append("owner 已确认且需要保持连续性的事实：")
        lines.extend(f"- {item}" for item in relationship.pinned_facts)
    if relationship.owner_preferences:
        lines.append("owner 明确表达的偏好：")
        lines.extend(
            f"- {key}: {value}" for key, value in relationship.owner_preferences.items()
        )
    if relationship.safety_boundaries:
        lines.append("这段关系的安全边界：")
        lines.extend(f"- {item}" for item in relationship.safety_boundaries)
    return "\n".join(lines)


def _expression_block(genome: PersonaGenome, *, modality: str) -> str:
    expression = genome.expression
    lines = ["表达方式："]
    if expression.voice_portrait:
        lines.append(expression.voice_portrait)
    lines.extend(f"- {item}" for item in expression.behavior_guidance)
    if note := expression.modality_notes.get(modality):
        lines.append(f"当前媒介（{modality}）：{note}")
    if expression.dialogue_examples:
        lines.append("典型表达示例（学习气质，不照抄内容）：")
        lines.extend(f"- {item}" for item in expression.dialogue_examples)
    return "\n".join(lines) if len(lines) > 1 else ""


def _memory_block(memory: AdaptedMemoryContext) -> str:
    lines: list[str] = []
    if memory.degraded:
        lines.append("记忆状态：当前不可用或部分降级，不要假装记得未提供的信息。")
    if memory.content:
        lines.extend(("召回记忆（只把有证据的内容当作事实）：", memory.content))
    if memory.instructions:
        lines.append("记忆消费边界：")
        lines.extend(f"- {item}" for item in memory.instructions)
    return "\n".join(lines)


def _runtime_state_block(state: PersonaRuntimeState | None) -> str:
    if state is None:
        return ""
    return f"当前状态：{hint}" if (hint := state.to_prompt_hint()) else ""


def _realtime_block(realtime: dict | None) -> str:
    if not realtime:
        return ""
    parts: list[str] = []
    if realtime.get("interrupted"):
        parts.append("owner 刚刚打断上一轮，优先回应当前意图并保持简洁")
    if realtime.get("temporary"):
        parts.append("当前是临时会话，不把本轮内容视为长期关系事实")
    return "当前交互状态：" + "；".join(parts) if parts else ""


__all__ = ["PersonaRealizer"]
