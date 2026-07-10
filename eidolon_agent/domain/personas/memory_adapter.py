"""Adapt recalled evidence according to a canonical persona genome."""

from __future__ import annotations

from eidolon_sdk.biz.persona import PersonaEvidenceRef, PersonaGenome

from eidolon_agent.core.types.memory import MemoryHit
from eidolon_agent.domain.personas.types import AdaptedMemoryContext


class PersonaMemoryAdapter:
    def adapt(
        self,
        *,
        genome: PersonaGenome,
        formatted_context: str,
        hits: list[MemoryHit],
        degraded: bool = False,
    ) -> AdaptedMemoryContext:
        recall_policy = genome.memory_policy.recall_policy
        if recall_policy.get("use_memory_as_evidence") is False:
            return AdaptedMemoryContext(degraded=degraded)

        instructions: list[str] = []
        for hit in hits:
            relation_type = _relation_type(hit)
            policy = genome.memory_policy.relation_policies.get(relation_type or "")
            if isinstance(policy, dict):
                reaction = policy.get("reaction_style") or policy.get("guidance")
                if isinstance(reaction, str) and reaction.strip():
                    instructions.append(reaction.strip())

        evidence_refs = tuple(
            PersonaEvidenceRef(
                kind="memory_fragment",
                ref_id=hit.id,
                summary=hit.content[:300],
                confidence=max(0.0, min(1.0, hit.similarity)),
            )
            for hit in hits
        )
        return AdaptedMemoryContext(
            content=formatted_context,
            instructions=tuple(dict.fromkeys(instructions)),
            evidence_refs=evidence_refs,
            degraded=degraded,
        )


def _relation_type(hit: MemoryHit) -> str | None:
    metadata = hit.metadata or {}
    if value := metadata.get("relation_type"):
        return str(value)
    relation = metadata.get("relation")
    if isinstance(relation, dict) and relation.get("type"):
        return str(relation["type"])
    if value := metadata.get("predicate"):
        return str(value)
    return None


__all__ = ["PersonaMemoryAdapter"]
