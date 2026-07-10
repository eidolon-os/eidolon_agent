"""Ports for canonical persona genome persistence."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from eidolon_sdk.biz.persona import (
    PersonaEvolutionProposalEvent,
    PersonaObservationEvent,
)

from eidolon_agent.domain.personas.types import StoredPersonaGenome


@runtime_checkable
class PersonaGenomeStore(Protocol):
    async def load_current(
        self, owner_id: str, companion_id: str
    ) -> StoredPersonaGenome: ...

    async def load_pinned(
        self,
        owner_id: str,
        companion_id: str,
        genome_id: str,
        genome_hash: str,
    ) -> StoredPersonaGenome: ...

    async def record_observation(self, event: PersonaObservationEvent) -> None: ...

    async def create_evolution_proposal(
        self, proposal: PersonaEvolutionProposalEvent
    ) -> StoredPersonaGenome: ...

    async def approve_evolution(
        self,
        *,
        owner_id: str,
        companion_id: str,
        proposed_genome_id: str,
        expected_base_genome_id: str,
    ) -> StoredPersonaGenome: ...

    async def reject_evolution(
        self, *, owner_id: str, proposed_genome_id: str, reason: str
    ) -> None: ...

    async def rollback(
        self, *, owner_id: str, companion_id: str, genome_id: str
    ) -> StoredPersonaGenome: ...


__all__ = ["PersonaGenomeStore"]
