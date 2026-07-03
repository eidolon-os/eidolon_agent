"""Persona-specific memory consumption rules."""

from __future__ import annotations

from collections import defaultdict

from eidolon_agent.core.types.memory import MemoryHit
from eidolon_agent.domain.personas.types import AdaptedMemoryContext, CompanionPersona


class PersonaMemoryAdapter:
    def adapt(
        self,
        *,
        instance: CompanionPersona,
        formatted_context: str,
        hits: list[MemoryHit],
        degraded: bool = False,
    ) -> AdaptedMemoryContext:
        policies = {
            policy.relation_type: policy for policy in instance.memory_adapter.graph_relation_policies
        }
        instructions: list[str] = []
        triggered_events: list[str] = []
        adjustments: defaultdict[str, float] = defaultdict(float)

        for hit in hits:
            relation_type = _relation_type(hit)
            if relation_type is None:
                continue
            policy = policies.get(relation_type)
            if policy is None:
                continue
            instructions.append(policy.reaction_style)
            triggered_events.extend(policy.persistent_events)
            for knob, delta in policy.transient_knob_adjustments.items():
                adjustments[knob] += delta

        if formatted_context and instance.memory_adapter.retrieved_fact_handling.emotion_resonance > 0:
            emotion_instructions = _emotion_instructions(
                hits,
                resonance=instance.memory_adapter.retrieved_fact_handling.emotion_resonance,
            )
            instructions.extend(emotion_instructions)

        return AdaptedMemoryContext(
            content=formatted_context,
            instructions=tuple(dict.fromkeys(instructions)),
            transient_knob_adjustments=dict(adjustments),
            triggered_events=tuple(dict.fromkeys(triggered_events)),
            degraded=degraded,
        )


def _relation_type(hit: MemoryHit) -> str | None:
    meta = hit.metadata or {}
    relation_type = meta.get("relation_type")
    if relation_type:
        return str(relation_type)
    relation = meta.get("relation")
    if isinstance(relation, dict) and relation.get("type"):
        return str(relation["type"])
    predicate = meta.get("predicate")
    if predicate:
        return str(predicate)
    return None


def _emotion_instructions(hits: list[MemoryHit], *, resonance: float) -> list[str]:
    out: list[str] = []
    for hit in hits:
        emotion = (hit.metadata or {}).get("emotion")
        if not emotion:
            continue
        out.append(
            f"召回的记忆带有 {emotion} 情绪标签；以 {resonance:.2f} 的共鸣强度回应，先照顾情绪再处理事实。"
        )
    return out

