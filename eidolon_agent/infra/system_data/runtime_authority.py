"""Adapters for the versioned System Data Companion Runtime contract."""

from __future__ import annotations

from eidolon_sdk.biz.persona import (
    PERSONA_GENOME_SCHEMA,
    PERSONA_REALIZER,
    normalize_persona_genome,
    persona_genome_hash,
)
from eidolon_sdk.biz.system_data import (
    CompanionRuntimeSnapshot,
    SystemDataError,
    SystemDataNotFound,
    SystemDataPrecondition,
    SystemDataRuntimeClient,
)

from eidolon_agent.core.errors import DependencyError, NotFoundError, ValidationError
from eidolon_agent.core.types.companion_runtime import CompanionRuntimeFacts


class SystemDataCompanionRuntimeAuthority:
    """HTTP consumer used by production Agent composition."""

    def __init__(self, client: SystemDataRuntimeClient) -> None:
        self._client = client

    async def resolve(
        self,
        *,
        owner_id: str,
        companion_id: str,
        genome_id: str | None = None,
    ) -> CompanionRuntimeFacts:
        try:
            snapshot = await self._client.get_companion_runtime(
                companion_id,
                genome_id=genome_id,
            )
        except (SystemDataNotFound, SystemDataPrecondition) as exc:
            raise NotFoundError(str(exc)) from exc
        except SystemDataError as exc:
            raise DependencyError(f"System Data runtime authority unavailable: {exc}") from exc
        facts = _facts(snapshot)
        if facts.owner_id != owner_id:
            raise NotFoundError(f"companion not found for owner: {companion_id}")
        return facts


class LocalCompanionRuntimeAuthority:
    """Current Data V2 adapter used only by the self-contained dev profile."""

    def __init__(self, store: object) -> None:
        self._store = store

    async def resolve(
        self,
        *,
        owner_id: str,
        companion_id: str,
        genome_id: str | None = None,
    ) -> CompanionRuntimeFacts:
        companion = await self._store.companions.get(companion_id)
        if companion is None or companion.owner_id != owner_id or companion.status != "active":
            raise NotFoundError(f"active companion not found for owner: {companion_id}")
        realm = await self._store.memory_realms.get(companion.default_memory_realm_id or "")
        # The realm belongs to the Owner, not to this Companion: one memory,
        # read by every Companion the Owner has. So what has to hold is that
        # the Companion points at its own Owner's realm — asserting it pointed
        # at a realm of its own would now reject every valid case.
        if realm is None or realm.owner_id != owner_id or realm.status != "active":
            raise NotFoundError(f"active memory realm not found for owner: {owner_id}")
        selected_genome_id = genome_id or companion.current_genome_id
        genome = await self._store.persona_genomes.get(selected_genome_id or "")
        if genome is None or genome.companion_id != companion_id or genome.status != "committed":
            raise NotFoundError(f"committed persona genome not found: {companion_id}")
        return _validated_facts(
            owner_id=owner_id,
            companion_id=companion_id,
            memory_realm_id=realm.realm_id,
            genome_id=genome.genome_id,
            genome_version=genome.version,
            schema_version=genome.schema_version,
            genome_hash=genome.genome_hash,
            realizer_version=genome.realizer_version,
            genome_json=dict(genome.genome_json or {}),
            runtime_config=dict(companion.runtime_config_json or {}),
        )


def _facts(snapshot: CompanionRuntimeSnapshot) -> CompanionRuntimeFacts:
    genome = snapshot.persona_genome
    return _validated_facts(
        owner_id=snapshot.owner_id,
        companion_id=snapshot.companion_id,
        memory_realm_id=snapshot.memory_realm.realm_id,
        genome_id=genome.genome_id,
        genome_version=genome.version,
        schema_version=genome.schema_version,
        genome_hash=genome.genome_hash,
        realizer_version=genome.realizer_version,
        genome_json=genome.genome,
        runtime_config=snapshot.runtime_config,
    )


def _validated_facts(
    *,
    owner_id: str,
    companion_id: str,
    memory_realm_id: str,
    genome_id: str,
    genome_version: int,
    schema_version: str,
    genome_hash: str,
    realizer_version: str,
    genome_json: dict,
    runtime_config: dict,
) -> CompanionRuntimeFacts:
    if schema_version != PERSONA_GENOME_SCHEMA:
        raise ValidationError(f"unsupported persona genome schema: {schema_version}")
    if realizer_version != PERSONA_REALIZER:
        raise ValidationError(f"unsupported persona realizer: {realizer_version}")
    genome = normalize_persona_genome(genome_json)
    if persona_genome_hash(genome) != genome_hash:
        raise ValidationError(f"persona genome hash mismatch: {genome_id}")
    return CompanionRuntimeFacts(
        owner_id=owner_id,
        companion_id=companion_id,
        memory_realm_id=memory_realm_id,
        genome_id=genome_id,
        genome_version=genome_version,
        schema_version=schema_version,
        genome_hash=genome_hash,
        realizer_version=realizer_version,
        genome=genome,
        runtime_config=dict(runtime_config),
    )


__all__ = [
    "LocalCompanionRuntimeAuthority",
    "SystemDataCompanionRuntimeAuthority",
]
