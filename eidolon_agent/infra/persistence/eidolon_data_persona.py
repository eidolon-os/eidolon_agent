"""Canonical persona genome persistence backed by ``eidolon_data``."""

from __future__ import annotations

from eidolon_data import DataStore
from eidolon_data.schema.models import CompanionRow, PersonaGenomeRow
from eidolon_sdk.biz.persona import (
    PERSONA_GENOME_SCHEMA,
    PERSONA_REALIZER,
    PersonaEvolutionProposalEvent,
    PersonaObservationEvent,
    normalize_persona_genome,
    persona_genome_hash,
)

from eidolon_agent.core.errors import NotFoundError, ValidationError
from eidolon_agent.domain.personas.types import StoredPersonaGenome


class EidolonDataPersonaGenomeStore:
    """Read exact snapshots and delegate atomic transitions to Data services."""

    def __init__(self, data_store: DataStore) -> None:
        self._data_store = data_store

    async def load_current(
        self, owner_id: str, companion_id: str
    ) -> StoredPersonaGenome:
        async with self._data_store.session_factory() as session:
            companion = await session.get(CompanionRow, companion_id)
            if (
                companion is None
                or companion.owner_id != owner_id
                or companion.status != "active"
                or not companion.current_genome_id
            ):
                raise NotFoundError(f"active companion genome not found: {companion_id}")
            row = await session.get(PersonaGenomeRow, companion.current_genome_id)
            return _stored(row, owner_id=owner_id, companion_id=companion_id)

    async def load_pinned(
        self,
        owner_id: str,
        companion_id: str,
        genome_id: str,
        genome_hash: str,
    ) -> StoredPersonaGenome:
        async with self._data_store.session_factory() as session:
            companion = await session.get(CompanionRow, companion_id)
            if companion is None or companion.owner_id != owner_id or companion.status != "active":
                raise NotFoundError(f"active companion not found: {companion_id}")
            row = await session.get(PersonaGenomeRow, genome_id)
            stored = _stored(row, owner_id=owner_id, companion_id=companion_id)
            if stored.status != "committed" or stored.genome_hash != genome_hash:
                raise NotFoundError(
                    f"runtime persona pin does not resolve: {genome_id}/{genome_hash}"
                )
            return stored

    async def record_observation(self, event: PersonaObservationEvent) -> None:
        await self._require_owned_companion(event.owner_id, event.companion_id)
        await self._data_store.events.record_event(
            event_id=event.observation_id,
            event_type="persona.observation.created",
            owner_id=event.owner_id,
            companion_id=event.companion_id,
            subject_type="persona_observation",
            subject_id=event.observation_id,
            source="agent",
            payload_json=event.model_dump(mode="json", exclude_none=True),
        )

    async def create_evolution_proposal(
        self, proposal: PersonaEvolutionProposalEvent
    ) -> StoredPersonaGenome:
        row = await self._data_store.persona.create_evolution_proposal(proposal)
        return _stored(
            row,
            owner_id=proposal.owner_id,
            companion_id=proposal.companion_id,
        )

    async def approve_evolution(
        self,
        *,
        owner_id: str,
        companion_id: str,
        proposed_genome_id: str,
        expected_base_genome_id: str,
    ) -> StoredPersonaGenome:
        row = await self._data_store.persona.approve_evolution(
            owner_id=owner_id,
            companion_id=companion_id,
            proposed_genome_id=proposed_genome_id,
            expected_base_genome_id=expected_base_genome_id,
        )
        return _stored(row, owner_id=owner_id, companion_id=companion_id)

    async def reject_evolution(
        self, *, owner_id: str, proposed_genome_id: str, reason: str
    ) -> None:
        await self._data_store.persona.reject_evolution(
            owner_id=owner_id,
            genome_id=proposed_genome_id,
            reason=reason,
        )

    async def rollback(
        self, *, owner_id: str, companion_id: str, genome_id: str
    ) -> StoredPersonaGenome:
        row = await self._data_store.persona.rollback_to_genome(
            owner_id=owner_id,
            companion_id=companion_id,
            genome_id=genome_id,
        )
        return _stored(row, owner_id=owner_id, companion_id=companion_id)

    async def _require_owned_companion(self, owner_id: str, companion_id: str) -> None:
        async with self._data_store.session_factory() as session:
            companion = await session.get(CompanionRow, companion_id)
            if companion is None or companion.owner_id != owner_id:
                raise NotFoundError(f"companion not found for owner: {companion_id}")


def _stored(
    row: PersonaGenomeRow | None,
    *,
    owner_id: str,
    companion_id: str,
) -> StoredPersonaGenome:
    if row is None or row.companion_id != companion_id:
        raise NotFoundError(f"persona genome not found for companion: {companion_id}")
    if row.schema_version != PERSONA_GENOME_SCHEMA:
        raise ValidationError(f"unsupported persona genome schema: {row.schema_version}")
    if row.realizer_version != PERSONA_REALIZER:
        raise ValidationError(f"unsupported persona realizer: {row.realizer_version}")
    genome = normalize_persona_genome(row.genome_json)
    expected_hash = persona_genome_hash(genome)
    if row.genome_hash != expected_hash:
        raise ValidationError(f"persona genome hash mismatch: {row.genome_id}")
    return StoredPersonaGenome(
        owner_id=owner_id,
        companion_id=companion_id,
        genome_id=row.genome_id,
        genome_hash=row.genome_hash,
        realizer_version=row.realizer_version,
        version=row.version,
        status=row.status,
        genome=genome,
    )


__all__ = ["EidolonDataPersonaGenomeStore"]
