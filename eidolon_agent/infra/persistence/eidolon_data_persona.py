"""Persona store mapped from the System Data Runtime Authority port."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from eidolon_sdk.biz.persona import (
    PersonaEvolutionProposalEvent,
    PersonaObservationEvent,
    normalize_persona_genome,
    persona_genome_hash,
)

from eidolon_agent.core.errors import ValidationError
from eidolon_agent.core.ports.runtime_authority import CompanionRuntimeAuthority
from eidolon_agent.core.types.companion_runtime import CompanionRuntimeFacts
from eidolon_agent.domain.personas.types import StoredPersonaGenome

ObservationSink = Callable[[PersonaObservationEvent], Awaitable[None]]


class RuntimeAuthorityPersonaGenomeStore:
    """Read runtime snapshots and optionally delegate Data-owned commands."""

    def __init__(
        self,
        authority: CompanionRuntimeAuthority,
        *,
        evolution_commands: object | None = None,
        observation_sink: ObservationSink | None = None,
    ) -> None:
        self._authority = authority
        self._commands = evolution_commands
        self._observation_sink = observation_sink

    async def load_current(self, owner_id: str, companion_id: str) -> StoredPersonaGenome:
        facts = await self._authority.resolve(
            owner_id=owner_id,
            companion_id=companion_id,
        )
        return _stored(facts)

    async def load_pinned(
        self,
        owner_id: str,
        companion_id: str,
        genome_id: str,
        genome_hash: str,
    ) -> StoredPersonaGenome:
        facts = await self._authority.resolve(
            owner_id=owner_id,
            companion_id=companion_id,
            genome_id=genome_id,
        )
        if facts.genome_hash != genome_hash:
            raise ValidationError(
                f"runtime persona pin does not resolve: {genome_id}/{genome_hash}"
            )
        return _stored(facts)

    async def record_observation(self, event: PersonaObservationEvent) -> None:
        if self._observation_sink is None:
            raise ValidationError("persona observation sink is not configured")
        await self._observation_sink(event)

    async def create_evolution_proposal(
        self,
        proposal: PersonaEvolutionProposalEvent,
    ) -> StoredPersonaGenome:
        commands = self._require_commands()
        row = await commands.create_evolution_proposal(proposal)
        return _stored_row(row, owner_id=proposal.owner_id)

    async def approve_evolution(
        self,
        *,
        owner_id: str,
        companion_id: str,
        proposed_genome_id: str,
        expected_base_genome_id: str,
    ) -> StoredPersonaGenome:
        commands = self._require_commands()
        row = await commands.approve_evolution(
            owner_id=owner_id,
            companion_id=companion_id,
            proposed_genome_id=proposed_genome_id,
            expected_base_genome_id=expected_base_genome_id,
        )
        return _stored_row(row, owner_id=owner_id)

    async def reject_evolution(
        self,
        *,
        owner_id: str,
        proposed_genome_id: str,
        reason: str,
    ) -> None:
        commands = self._require_commands()
        await commands.reject_evolution(
            owner_id=owner_id,
            genome_id=proposed_genome_id,
            reason=reason,
        )

    async def rollback(
        self,
        *,
        owner_id: str,
        companion_id: str,
        genome_id: str,
    ) -> StoredPersonaGenome:
        commands = self._require_commands()
        row = await commands.rollback_to_genome(
            owner_id=owner_id,
            companion_id=companion_id,
            genome_id=genome_id,
        )
        return _stored_row(row, owner_id=owner_id)

    def _require_commands(self) -> object:
        if self._commands is None:
            raise ValidationError(
                "persona evolution commands are not exposed by the runtime read authority"
            )
        return self._commands


def _stored(facts: CompanionRuntimeFacts) -> StoredPersonaGenome:
    return StoredPersonaGenome(
        conversation_preferences=facts.runtime_config.get("conversation_preferences", {}),
        preference_revision=facts.runtime_config.get("preference_revision", 1),
        owner_id=facts.owner_id,
        companion_id=facts.companion_id,
        genome_id=facts.genome_id,
        genome_hash=facts.genome_hash,
        realizer_version=facts.realizer_version,
        version=facts.genome_version,
        status="committed",
        genome=facts.genome,
    )


def _stored_row(row: object, *, owner_id: str) -> StoredPersonaGenome:
    genome = normalize_persona_genome(row.genome_json)
    if persona_genome_hash(genome) != row.genome_hash:
        raise ValidationError(f"persona genome hash mismatch: {row.genome_id}")
    return StoredPersonaGenome(
        owner_id=owner_id,
        companion_id=row.companion_id,
        genome_id=row.genome_id,
        genome_hash=row.genome_hash,
        realizer_version=row.realizer_version,
        version=row.version,
        status=row.status,
        genome=genome,
    )


__all__ = ["RuntimeAuthorityPersonaGenomeStore"]
