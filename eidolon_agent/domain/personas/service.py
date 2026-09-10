"""Canonical persona genome runtime and evolution service."""

from __future__ import annotations

from eidolon_sdk.biz.persona import (
    PersonaEvolutionProposalEvent,
    PersonaObservationEvent,
    validate_persona_evolution,
)

from eidolon_agent.core.errors import ValidationError
from eidolon_agent.core.types.memory import ActiveCommitment, MemoryHit
from eidolon_agent.domain.personas.memory_adapter import PersonaMemoryAdapter
from eidolon_agent.domain.personas.ports import PersonaGenomeStore
from eidolon_agent.domain.personas.realizer import PersonaRealizer
from eidolon_agent.domain.personas.runtime_state import PersonaRuntimeStateStore
from eidolon_agent.domain.personas.types import (
    AttentionTarget,
    PersonaSignalInput,
    PersonaSnapshot,
    RealizedPersona,
    StoredPersonaGenome,
)


class PersonasService:
    def __init__(
        self,
        *,
        store: PersonaGenomeStore,
        realizer: PersonaRealizer | None = None,
        memory_adapter: PersonaMemoryAdapter | None = None,
        runtime_state: PersonaRuntimeStateStore | None = None,
    ) -> None:
        self._store = store
        self._realizer = realizer or PersonaRealizer()
        self._memory_adapter = memory_adapter or PersonaMemoryAdapter()
        self._runtime = runtime_state or PersonaRuntimeStateStore()

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None

    async def get_snapshot(
        self,
        *,
        owner_id: str,
        companion_id: str,
        genome_id: str | None = None,
        genome_hash: str | None = None,
    ) -> PersonaSnapshot:
        if bool(genome_id) != bool(genome_hash):
            raise ValidationError("genome_id and genome_hash must be supplied together")
        stored = (
            await self._store.load_pinned(
                owner_id,
                companion_id,
                genome_id or "",
                genome_hash or "",
            )
            if genome_id and genome_hash
            else await self._store.load_current(owner_id, companion_id)
        )
        runtime_state = await self._runtime.snapshot(companion_id=companion_id)
        return PersonaSnapshot(
            stored=stored,
            runtime_state=runtime_state,
            prompt_hint=runtime_state.to_prompt_hint(),
        )

    async def realize_context(
        self,
        *,
        owner_id: str,
        companion_id: str,
        genome_id: str | None = None,
        genome_hash: str | None = None,
        user_text: str,
        realtime: dict | None = None,
        dry_run_memory: list[MemoryHit] | None = None,
        memory_degraded: bool = False,
        modality: str = "text",
    ) -> RealizedPersona:
        del user_text
        snapshot = await self.get_snapshot(
            owner_id=owner_id,
            companion_id=companion_id,
            genome_id=genome_id,
            genome_hash=genome_hash,
        )
        hits = dry_run_memory or []
        adapted = self._memory_adapter.adapt(
            genome=snapshot.stored.genome,
            formatted_context="\n".join(hit.content for hit in hits),
            hits=hits,
            degraded=memory_degraded,
        )
        return self._realizer.realize(
            stored=snapshot.stored,
            adapted_memory=adapted,
            runtime_state=snapshot.runtime_state,
            realtime=realtime,
            modality=modality,
        )

    async def apply_memory_evidence(
        self,
        *,
        persona: RealizedPersona,
        hits: list[MemoryHit],
        realtime: dict | None = None,
        modality: str = "text",
    ) -> RealizedPersona:
        """Apply genome-owned memory consumption policy to recalled evidence.

        The recalled facts remain a separate context segment. This method only
        adds relationship-specific guidance and auditable evidence references;
        it never mutates the stored genome.
        """
        stored = persona.stored or await self._store.load_pinned(
            persona.owner_id,
            persona.companion_id,
            persona.genome_id,
            persona.genome_hash,
        )
        adapted = self._memory_adapter.adapt(
            genome=stored.genome,
            formatted_context="",
            hits=hits,
        )
        runtime_state = await self._runtime.snapshot(companion_id=persona.companion_id)
        return self._realizer.realize(
            stored=stored,
            adapted_memory=adapted,
            runtime_state=runtime_state,
            realtime=realtime,
            modality=modality,
        )

    def realize_commitment_context(
        self,
        commitments: list[ActiveCommitment],
    ) -> str:
        """Render product-approved active commitments without mutating persona."""
        return self._realizer.realize_commitment_context(commitments)

    async def record_observation(self, event: PersonaObservationEvent) -> None:
        await self._store.record_observation(event)

    async def create_evolution_proposal(
        self, proposal: PersonaEvolutionProposalEvent
    ) -> StoredPersonaGenome:
        base = await self._store.load_pinned(
            proposal.owner_id,
            proposal.companion_id,
            proposal.base_genome_id,
            proposal.base_genome_hash,
        )
        _validate_evolution(base, proposal)
        return await self._store.create_evolution_proposal(proposal)

    async def approve_evolution(
        self,
        *,
        owner_id: str,
        companion_id: str,
        proposed_genome_id: str,
        expected_base_genome_id: str,
    ) -> StoredPersonaGenome:
        return await self._store.approve_evolution(
            owner_id=owner_id,
            companion_id=companion_id,
            proposed_genome_id=proposed_genome_id,
            expected_base_genome_id=expected_base_genome_id,
        )

    async def reject_evolution(
        self, *, owner_id: str, proposed_genome_id: str, reason: str
    ) -> None:
        await self._store.reject_evolution(
            owner_id=owner_id,
            proposed_genome_id=proposed_genome_id,
            reason=reason,
        )

    async def rollback(
        self, *, owner_id: str, companion_id: str, genome_id: str
    ) -> StoredPersonaGenome:
        return await self._store.rollback(
            owner_id=owner_id,
            companion_id=companion_id,
            genome_id=genome_id,
        )

    async def update_runtime_state(
        self,
        *,
        companion_id: str,
        emotion: str | None = None,
        emotion_delta: float = 0.0,
        energy_level: float | None = None,
        attention_target: AttentionTarget | None = None,
        focus_score: float | None = None,
    ):
        return await self._runtime.update(
            companion_id=companion_id,
            emotion=emotion,
            emotion_delta=emotion_delta,
            energy_level=energy_level,
            attention_target=attention_target,
            focus_score=focus_score,
        )

    async def submit_signal(self, signal: PersonaSignalInput) -> None:
        emotion = signal.dominant_emotion
        emotion_delta = signal.emotion_confidence * 0.25 if emotion else 0.0
        attention = (
            AttentionTarget.USER
            if signal.presence == "present"
            else AttentionTarget.DRIFT
            if signal.presence == "distracted"
            else AttentionTarget.IDLE
        )
        await self.update_runtime_state(
            companion_id=signal.companion_id,
            emotion=emotion,
            emotion_delta=emotion_delta,
            attention_target=attention,
            focus_score=signal.confidence_overall,
        )


def _validate_evolution(
    base: StoredPersonaGenome,
    proposal: PersonaEvolutionProposalEvent,
) -> None:
    try:
        validate_persona_evolution(base.genome, proposal)
    except ValueError as exc:
        raise ValidationError(str(exc)) from exc


__all__ = ["PersonasService"]
