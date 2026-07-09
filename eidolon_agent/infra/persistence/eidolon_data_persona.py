"""Persona persistence backed by ``eidolon_data``.

The persona domain speaks in ``CompanionPersona`` and proposal/observation
objects. This adapter maps those types onto Eidolon's sovereign schema:

* ``companions`` are the long-lived persona subjects.
* ``persona_genomes`` hold versioned companion persona snapshots.
* ``events`` hold custom template state, evolution history, observations, and
  proposal state.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any

from eidolon_sdk.biz.persona import (
    PERSONA_COMPILER_VERSION,
    PERSONA_GENOME_SCHEMA_VERSION,
    PersonaGenomeV1,
    PersonaIdentityCore,
    PersonaMemoryAdapterV1,
    PersonaRelationship,
    PersonaStyleCompilerV1,
    PersonaTraitState,
    normalize_persona_genome,
    persona_genome_hash,
    persona_genome_to_json,
)
from eidolon_data.schema.models import (
    CompanionRow,
    EventRow,
    OwnerRow,
    PersonaGenomeRow,
)
from eidolon_data.services.datastore import DataStore
from sqlalchemy import delete, desc, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from eidolon_agent.core.errors import NotFoundError
from eidolon_agent.core.ports.events import KVStore
from eidolon_agent.domain.personas.types import (
    BehavioralKnob,
    CompanionPersona,
    EvolutionState,
    IdentityCore,
    PersonaAssets,
    PersonaEvolutionProposal,
    PersonaEvolutionResult,
    PersonaMetadata,
    PersonaObservation,
    PersonaTemplate,
    StyleCompiler,
)
from eidolon_agent.infra.persistence.custom_template_types import (
    CustomTemplateAlreadyExists,
    CustomTemplateNotFound,
    CustomTemplateView,
)

_EVOLUTION_KEY = "persona_evolution_result"
_OBSERVATION_KEY = "persona_observation"
_PROPOSAL_KEY = "persona_evolution_proposal"
_CUSTOM_TEMPLATE_KEY = "custom_template"
_CUSTOM_TEMPLATE_KIND = "agent_custom_template"

_log = logging.getLogger(__name__)


class EidolonDataCompanionPersonaStore:
    """``CompanionPersonaStore`` implemented on ``persona_genomes``."""

    def __init__(self, data_store: DataStore, *, cache_kv: KVStore | None = None) -> None:
        self._data_store = data_store
        self._cache_kv = cache_kv
        self._persona_cache: dict[str, tuple[str, CompanionPersona]] = {}

    async def exists(self, owner_id: str, companion_id: str) -> bool:
        return await self._load_or_none(owner_id, companion_id) is not None

    async def load(self, owner_id: str, companion_id: str) -> CompanionPersona:
        persona = await self._load_or_none(owner_id, companion_id)
        if persona is None:
            raise NotFoundError(f"companion persona not found: {owner_id}/{companion_id}")
        return persona

    async def save(self, persona: CompanionPersona, *, reason: str = "") -> None:
        async with self._data_store.session_factory() as session:
            await _ensure_owner_and_companion(
                session,
                owner_id=persona.owner_id,
                companion_id=persona.companion_id,
            )
            row = await _get_genome_by_version(
                session,
                companion_id=persona.companion_id,
                version=persona.version,
            )
            now = datetime.now(timezone.utc)
            genome_json = _persona_to_genome_json(persona, reason=reason)
            # Every version links back to the authored origin (v1, self-referential)
            # so a reset-to-origin can walk the chain back. Backward-compatible:
            # rows written before this stamp simply have a null base.
            base_genome_id = f"genome-{persona.companion_id}-1"
            if row is None:
                genome_id = f"genome-{persona.companion_id}-{persona.version}"
                insert_values = {
                    "genome_id": genome_id,
                    "companion_id": persona.companion_id,
                    "version": persona.version,
                    "status": "committed",
                    "base_genome_id": base_genome_id,
                    "schema_version": PERSONA_GENOME_SCHEMA_VERSION,
                    "genome_hash": persona_genome_hash(genome_json),
                    "compiler_version": PERSONA_COMPILER_VERSION,
                    "stable_prompt_hash": None,
                    "applied_event_id": None,
                    "source_json": _persona_source_json(persona),
                    "genome_json": genome_json,
                    "created_at": persona.created_at,
                    "updated_at": now,
                }
                await _insert_ignore(
                    session,
                    PersonaGenomeRow,
                    insert_values,
                    index_elements=["genome_id"],
                )
                row = await session.get(PersonaGenomeRow, genome_id)
                if row is None:
                    raise RuntimeError(f"persona genome insert failed: {genome_id}")
            else:
                row.source_json = _persona_source_json(persona)
                row.genome_json = genome_json
                row.schema_version = PERSONA_GENOME_SCHEMA_VERSION
                row.genome_hash = persona_genome_hash(genome_json)
                row.compiler_version = PERSONA_COMPILER_VERSION
                row.updated_at = now

            companion = await session.get(CompanionRow, persona.companion_id)
            if companion is not None:
                companion.current_genome_id = row.genome_id
                companion.updated_at = now
            await session.commit()
            self._persona_cache[row.genome_id] = (row.genome_hash, persona)
            await self._write_persona_cache(
                owner_id=persona.owner_id,
                row=row,
                persona=persona,
            )

    async def create_from_template(
        self,
        *,
        template: PersonaTemplate,
        owner_id: str,
        companion_id: str,
    ) -> CompanionPersona:
        now = datetime.now(timezone.utc)
        persona = CompanionPersona(
            companion_id=companion_id,
            owner_id=owner_id,
            origin_template_id=template.metadata.template_id,
            origin_template_revision=template.metadata.template_revision,
            version=1,
            created_at=now,
            updated_at=now,
            metadata=template.metadata,
            identity_core=template.identity_core,
            behavioral_knobs=template.behavioral_knobs,
            style_compiler=template.style_compiler,
            memory_adapter=template.memory_adapter,
            evolution_rules=template.evolution_rules,
            assets=template.assets,
            # Seed the blueprint-level components; owner-specific components
            # (pinned_facts, relationship_stage) start empty and are authored
            # per companion.
            example_dialogs=template.example_dialogs,
            goals=template.goals,
        )
        await self.save(persona, reason="create_from_template")
        return persona

    async def list_all(self) -> list[CompanionPersona]:
        async with self._data_store.session_factory() as session:
            rows = (
                (
                    await session.execute(
                        select(PersonaGenomeRow)
                        .join(
                            CompanionRow,
                            CompanionRow.current_genome_id == PersonaGenomeRow.genome_id,
                        )
                        .order_by(desc(PersonaGenomeRow.updated_at))
                    )
                )
                .scalars()
                .all()
            )
            return [_genome_row_to_persona(row) for row in rows]

    async def delete(self, owner_id: str, companion_id: str) -> None:
        async with self._data_store.session_factory() as session:
            companion = await session.get(CompanionRow, companion_id)
            if companion is not None and companion.owner_id == owner_id:
                companion.current_genome_id = None
                companion.status = "deleted"
                companion.updated_at = datetime.now(timezone.utc)
            await session.execute(
                delete(PersonaGenomeRow).where(PersonaGenomeRow.companion_id == companion_id)
            )
            await session.commit()
            self._persona_cache = {
                key: value
                for key, value in self._persona_cache.items()
                if value[1].companion_id != companion_id
            }
            await self._delete_current_cache(owner_id=owner_id, companion_id=companion_id)

    async def save_with_history(
        self,
        persona: CompanionPersona,
        result: PersonaEvolutionResult,
    ) -> None:
        async with self._data_store.session_factory() as session:
            await _ensure_owner_and_companion(
                session,
                owner_id=persona.owner_id,
                companion_id=persona.companion_id,
            )
            row = await _get_genome_by_version(
                session,
                companion_id=persona.companion_id,
                version=persona.version,
            )
            now = datetime.now(timezone.utc)
            if row is None:
                genome_id = f"genome-{persona.companion_id}-{persona.version}"
                genome_json = _persona_to_genome_json(persona, reason="save_with_history")
                await _insert_ignore(
                    session,
                    PersonaGenomeRow,
                    {
                        "genome_id": genome_id,
                        "companion_id": persona.companion_id,
                        "version": persona.version,
                        "status": "committed",
                        "schema_version": PERSONA_GENOME_SCHEMA_VERSION,
                        "genome_hash": persona_genome_hash(genome_json),
                        "compiler_version": PERSONA_COMPILER_VERSION,
                        "stable_prompt_hash": None,
                        "applied_event_id": None,
                        "source_json": _persona_source_json(persona),
                        "genome_json": genome_json,
                        "created_at": persona.created_at,
                        "updated_at": now,
                    },
                    index_elements=["genome_id"],
                )
                row = await session.get(PersonaGenomeRow, genome_id)
                if row is None:
                    raise RuntimeError(f"persona genome insert failed: {genome_id}")
            else:
                row.genome_json = _persona_to_genome_json(persona, reason="save_with_history")
                row.schema_version = PERSONA_GENOME_SCHEMA_VERSION
                row.genome_hash = persona_genome_hash(row.genome_json)
                row.compiler_version = PERSONA_COMPILER_VERSION
                row.updated_at = now
            companion = await session.get(CompanionRow, persona.companion_id)
            if companion is not None:
                companion.current_genome_id = row.genome_id
                companion.updated_at = now
            session.add(
                _event_row(
                    owner_id=persona.owner_id,
                    companion_id=persona.companion_id,
                    subject_type="persona_genome",
                    subject_id=row.genome_id,
                    event_type="persona.genome.committed",
                    payload_json={
                        _EVOLUTION_KEY: result.model_dump(mode="json"),
                        "companion_id": persona.companion_id,
                        "genome_id": row.genome_id,
                        "genome_hash": row.genome_hash,
                        "schema_version": row.schema_version,
                        "compiler_version": row.compiler_version,
                    },
                )
            )
            await session.commit()
            self._persona_cache[row.genome_id] = (row.genome_hash, persona)
            await self._write_persona_cache(
                owner_id=persona.owner_id,
                row=row,
                persona=persona,
            )

    async def _load_or_none(
        self,
        owner_id: str,
        companion_id: str,
    ) -> CompanionPersona | None:
        cached_persona = await self._read_persona_cache(
            owner_id=owner_id,
            companion_id=companion_id,
        )
        if cached_persona is not None:
            return cached_persona
        async with self._data_store.session_factory() as session:
            row = await _get_current_genome(session, companion_id=companion_id)
            if row is None:
                return None
            cached = self._persona_cache.get(row.genome_id)
            if cached is not None and cached[0] == row.genome_hash:
                persona = cached[1]
                return persona if persona.owner_id == owner_id else None
            persona = _genome_row_to_persona(row, owner_id=owner_id)
            if persona.owner_id != owner_id:
                return None
            self._persona_cache[row.genome_id] = (row.genome_hash, persona)
            await self._write_persona_cache(owner_id=owner_id, row=row, persona=persona)
            return persona

    async def _read_persona_cache(
        self,
        *,
        owner_id: str,
        companion_id: str,
    ) -> CompanionPersona | None:
        if self._cache_kv is None:
            return None
        try:
            raw_current = await self._cache_kv.get(_current_cache_key(owner_id, companion_id))
            if raw_current is None:
                return None
            current = json.loads(raw_current.decode("utf-8"))
            if current.get("owner_id") != owner_id or current.get("companion_id") != companion_id:
                return None
            genome_id = str(current.get("genome_id") or "")
            genome_hash = str(current.get("genome_hash") or "")
            if not genome_id or not genome_hash:
                return None
            in_process = self._persona_cache.get(genome_id)
            if in_process is not None and in_process[0] == genome_hash:
                return in_process[1]
            raw_genome = await self._cache_kv.get(_genome_cache_key(genome_id))
            if raw_genome is None:
                return None
            cached = json.loads(raw_genome.decode("utf-8"))
            if cached.get("genome_hash") != genome_hash:
                return None
            row = _cached_genome_row(cached)
            persona = _genome_row_to_persona(row, owner_id=owner_id)
            if persona.owner_id != owner_id:
                return None
            self._persona_cache[genome_id] = (genome_hash, persona)
            return persona
        except Exception as exc:  # pragma: no cover - derivative cache must not break DB path
            _log.warning(
                "persona cache read failed owner=%s companion=%s: %s",
                owner_id,
                companion_id,
                exc,
            )
            return None

    async def _write_persona_cache(
        self,
        *,
        owner_id: str,
        row: PersonaGenomeRow,
        persona: CompanionPersona,
    ) -> None:
        if self._cache_kv is None:
            return
        try:
            await self._cache_kv.put(
                _genome_cache_key(row.genome_id),
                json.dumps(_cache_payload_for_row(row), sort_keys=True).encode("utf-8"),
            )
            await self._cache_kv.put(
                _current_cache_key(owner_id, persona.companion_id),
                json.dumps(
                    {
                        "owner_id": owner_id,
                        "companion_id": persona.companion_id,
                        "genome_id": row.genome_id,
                        "genome_hash": row.genome_hash,
                        "schema_version": row.schema_version,
                        "compiler_version": row.compiler_version,
                    },
                    sort_keys=True,
                ).encode("utf-8"),
            )
        except Exception as exc:  # pragma: no cover - derivative cache must not break DB path
            _log.warning(
                "persona cache write failed owner=%s companion=%s genome=%s: %s",
                owner_id,
                persona.companion_id,
                row.genome_id,
                exc,
            )

    async def _delete_current_cache(self, *, owner_id: str, companion_id: str) -> None:
        if self._cache_kv is None:
            return
        try:
            await self._cache_kv.delete(_current_cache_key(owner_id, companion_id))
        except Exception as exc:  # pragma: no cover - derivative cache must not break DB path
            _log.warning(
                "persona cache delete failed owner=%s companion=%s: %s",
                owner_id,
                companion_id,
                exc,
            )


class EidolonDataEvolutionHistoryStore:
    """Evolution audit/repository implemented on ``events``."""

    def __init__(self, data_store: DataStore) -> None:
        self._data_store = data_store

    async def record_evolution(self, result: PersonaEvolutionResult) -> None:
        await self.record(result)

    async def record(self, result: PersonaEvolutionResult) -> None:
        owner_id = await self._owner_for_companion(result.companion_id)
        async with self._data_store.session_factory() as session:
            session.add(
                _event_row(
                    owner_id=owner_id,
                    subject_type="persona",
                    subject_id=result.companion_id,
                    event_type="persona.genome.committed",
                    companion_id=result.companion_id,
                    payload_json={_EVOLUTION_KEY: result.model_dump(mode="json")},
                )
            )
            await session.commit()

    async def list_for_instance(
        self, companion_id: str, *, limit: int = 50
    ) -> list[PersonaEvolutionResult]:
        async with self._data_store.session_factory() as session:
            rows = (
                (
                    await session.execute(
                        select(EventRow)
                        .where(EventRow.subject_type == "persona")
                        .where(EventRow.subject_id == companion_id)
                        .where(EventRow.event_type == "persona.genome.committed")
                        .order_by(desc(EventRow.created_at))
                        .limit(limit)
                    )
                )
                .scalars()
                .all()
            )
            return [_event_to_evolution(row) for row in rows]

    async def get(self, delta_id: str) -> PersonaEvolutionResult | None:
        async with self._data_store.session_factory() as session:
            row = await session.get(EventRow, delta_id)
            if row is None or row.event_type != "persona.genome.committed":
                return None
            return _event_to_evolution(row)

    async def _owner_for_companion(self, companion_id: str) -> str:
        async with self._data_store.session_factory() as session:
            companion = await session.get(CompanionRow, companion_id)
            if companion is None:
                # No silent "owner-default": mis-attributing a companion's
                # evolution history to a phantom owner corrupts the audit
                # trail and the owner-scoped event queries that read it back.
                # The companion must be provisioned before it can evolve.
                raise NotFoundError(
                    f"cannot record evolution: companion not provisioned: {companion_id}"
                )
            return companion.owner_id


class EidolonDataPersonaObservationStore:
    """Observation repository implemented as event-sourced state."""

    def __init__(self, data_store: DataStore) -> None:
        self._data_store = data_store

    async def add(self, observation: PersonaObservation) -> None:
        async with self._data_store.session_factory() as session:
            await _ensure_owner_and_companion(
                session,
                owner_id=observation.owner_id,
                companion_id=observation.companion_id,
            )
            session.add(
                _event_row(
                    owner_id=observation.owner_id,
                    companion_id=observation.companion_id,
                    subject_type="persona_observation",
                    subject_id=observation.id,
                    event_type="persona.observation.created",
                    payload_json={
                        _OBSERVATION_KEY: observation.model_dump(mode="json"),
                        "observation_id": observation.id,
                        "companion_id": observation.companion_id,
                        "kind": observation.kind,
                        "source": observation.source,
                    },
                )
            )
            await session.commit()

    async def list_for_instance(
        self,
        companion_id: str,
        *,
        status: str | None = None,
        limit: int = 50,
    ) -> list[PersonaObservation]:
        observations = await self._latest_observations()
        rows = [item for item in observations if item.companion_id == companion_id]
        if status is not None:
            rows = [item for item in rows if item.status == status]
        rows.sort(key=lambda item: item.created_at, reverse=True)
        return rows[:limit]

    async def get(self, observation_id: str) -> PersonaObservation | None:
        return (await self._latest_observations_by_id()).get(observation_id)

    async def set_status(self, observation_id: str, status: str) -> None:
        observation = await self.get(observation_id)
        if observation is None:
            return
        await self.add(observation.model_copy(update={"status": status}))

    async def _latest_observations(self) -> list[PersonaObservation]:
        return list((await self._latest_observations_by_id()).values())

    async def _latest_observations_by_id(self) -> dict[str, PersonaObservation]:
        async with self._data_store.session_factory() as session:
            rows = (
                (
                    await session.execute(
                        select(EventRow)
                        .where(EventRow.subject_type == "persona_observation")
                        .where(EventRow.event_type == "persona.observation.created")
                        .order_by(EventRow.created_at)
                    )
                )
                .scalars()
                .all()
            )
        latest: dict[str, PersonaObservation] = {}
        for row in rows:
            observation = PersonaObservation.model_validate(row.payload_json[_OBSERVATION_KEY])
            latest[observation.id] = observation
        return latest


class EidolonDataPersonaEvolutionProposalStore:
    """Evolution proposal repository implemented as event-sourced state."""

    def __init__(self, data_store: DataStore) -> None:
        self._data_store = data_store

    async def add(self, proposal: PersonaEvolutionProposal) -> None:
        await self.save(proposal)

    async def save(self, proposal: PersonaEvolutionProposal) -> None:
        async with self._data_store.session_factory() as session:
            await _ensure_owner_and_companion(
                session,
                owner_id=proposal.owner_id,
                companion_id=proposal.companion_id,
            )
            session.add(
                _event_row(
                    owner_id=proposal.owner_id,
                    companion_id=proposal.companion_id,
                    subject_type="persona_proposal",
                    subject_id=proposal.id,
                    event_type=_proposal_event_type(proposal),
                    payload_json={
                        _PROPOSAL_KEY: proposal.model_dump(mode="json"),
                        "proposal_id": proposal.id,
                        "companion_id": proposal.companion_id,
                        "status": proposal.status,
                    },
                )
            )
            await session.commit()

    async def list_for_instance(
        self,
        companion_id: str,
        *,
        status: str | None = None,
        limit: int = 50,
    ) -> list[PersonaEvolutionProposal]:
        proposals = [
            item
            for item in (await self._latest_proposals_by_id()).values()
            if item.companion_id == companion_id
        ]
        if status is not None:
            proposals = [item for item in proposals if item.status == status]
        proposals.sort(key=lambda item: item.created_at, reverse=True)
        return proposals[:limit]

    async def get(self, proposal_id: str) -> PersonaEvolutionProposal | None:
        return (await self._latest_proposals_by_id()).get(proposal_id)

    async def _latest_proposals_by_id(self) -> dict[str, PersonaEvolutionProposal]:
        async with self._data_store.session_factory() as session:
            rows = (
                (
                    await session.execute(
                        select(EventRow)
                        .where(EventRow.subject_type == "persona_proposal")
                        .where(
                            EventRow.event_type.in_(
                                [
                                    "persona.evolution.proposed",
                                    "persona.evolution.approved",
                                    "persona.evolution.rejected",
                                ]
                            )
                        )
                        .order_by(EventRow.created_at)
                    )
                )
                .scalars()
                .all()
            )
        latest: dict[str, PersonaEvolutionProposal] = {}
        for row in rows:
            proposal = PersonaEvolutionProposal.model_validate(row.payload_json[_PROPOSAL_KEY])
            latest[proposal.id] = proposal
        return latest


class EidolonDataCustomTemplateStore:
    """Custom template store backed by latest-state events.

    Custom templates are generation inputs, not persona sovereignty entities, so
    they no longer get a dedicated Eidolon Data table.
    """

    def __init__(self, data_store: DataStore) -> None:
        self._data_store = data_store

    async def get(self, template_id: str) -> CustomTemplateView | None:
        return (await self._latest_templates_by_id()).get(template_id)

    async def exists(self, template_id: str) -> bool:
        return await self.get(template_id) is not None

    async def list_all(self, *, owner_id: str | None = None) -> list[CustomTemplateView]:
        rows = list((await self._latest_templates_by_id()).values())
        if owner_id is not None:
            rows = [row for row in rows if row.owner_id == owner_id]
        rows.sort(key=lambda row: row.created_at)
        return rows

    async def create(
        self,
        *,
        template_id: str,
        owner_id: str,
        display_name: str,
        archetype: str,
        yaml_body: str,
    ) -> CustomTemplateView:
        now = datetime.now(timezone.utc)
        if await self.exists(template_id):
            raise CustomTemplateAlreadyExists(f"template {template_id!r} already exists")
        async with self._data_store.session_factory() as session:
            await _ensure_owner(session, owner_id=owner_id, kind="team")
            view = CustomTemplateView(
                template_id=template_id,
                owner_id=owner_id,
                display_name=display_name,
                archetype=archetype,
                yaml_body=yaml_body,
                revision=1,
                created_at=now,
                updated_at=now,
            )
            session.add(
                _custom_template_event(
                    template_id=template_id,
                    owner_id=owner_id,
                    view=view,
                    event_type="persona_template.custom.saved",
                )
            )
            await session.commit()
            return view

    async def update(
        self,
        template_id: str,
        *,
        display_name: str | None = None,
        yaml_body: str | None = None,
    ) -> CustomTemplateView:
        current = await self.get(template_id)
        if current is None:
            raise CustomTemplateNotFound(f"template {template_id!r} not found")
        now = datetime.now(timezone.utc)
        view = CustomTemplateView(
            template_id=template_id,
            owner_id=current.owner_id,
            display_name=display_name if display_name is not None else current.display_name,
            archetype=current.archetype,
            yaml_body=yaml_body if yaml_body is not None else current.yaml_body,
            revision=current.revision + 1,
            created_at=current.created_at,
            updated_at=now,
        )
        async with self._data_store.session_factory() as session:
            session.add(
                _custom_template_event(
                    template_id=template_id,
                    owner_id=current.owner_id,
                    view=view,
                    event_type="persona_template.custom.saved",
                )
            )
            await session.commit()
            return view

    async def delete(self, template_id: str) -> None:
        current = await self.get(template_id)
        if current is None:
            raise CustomTemplateNotFound(f"template {template_id!r} not found")
        async with self._data_store.session_factory() as session:
            session.add(
                _custom_template_event(
                    template_id=template_id,
                    owner_id=current.owner_id,
                    view=current,
                    event_type="persona_template.custom.deleted",
                    deleted=True,
                )
            )
            await session.commit()

    async def count_referring_instances(self, template_id: str) -> int:
        async with self._data_store.session_factory() as session:
            rows = (await session.execute(select(PersonaGenomeRow))).scalars().all()
        count = 0
        for row in rows:
            source = dict(row.source_json or {})
            genome = dict(row.genome_json or {})
            if source.get("template_id") == template_id or genome.get("origin_template_id") == template_id:
                count += 1
        return count

    async def _latest_templates_by_id(self) -> dict[str, CustomTemplateView]:
        async with self._data_store.session_factory() as session:
            rows = (
                (
                    await session.execute(
                        select(EventRow)
                        .where(EventRow.subject_type == "custom_template")
                        .where(
                            EventRow.event_type.in_(
                                [
                                    "persona_template.custom.saved",
                                    "persona_template.custom.deleted",
                                ]
                            )
                        )
                        .order_by(EventRow.created_at)
                    )
                )
                .scalars()
                .all()
            )
        latest: dict[str, CustomTemplateView] = {}
        for row in rows:
            payload = dict(row.payload_json or {})
            if payload.get("kind") != _CUSTOM_TEMPLATE_KIND:
                continue
            template_id = row.subject_id
            if payload.get("deleted"):
                latest.pop(template_id, None)
                continue
            latest[template_id] = _custom_template_payload_to_view(payload)
        return latest


async def _ensure_owner_and_companion(
    session,
    *,
    owner_id: str,
    companion_id: str,
) -> None:
    owner = await session.get(OwnerRow, owner_id)
    if owner is None or owner.status != "active":
        raise NotFoundError(f"owner not provisioned or inactive: {owner_id}")
    companion = await session.get(CompanionRow, companion_id)
    if companion is None:
        raise NotFoundError(f"companion not provisioned: {companion_id}")
    if companion.owner_id != owner_id:
        raise NotFoundError(f"companion {companion_id} does not belong to owner {owner_id}")
    if companion.status != "active":
        raise NotFoundError(f"companion inactive: {companion_id}")
    await session.flush()


async def _ensure_owner(session, *, owner_id: str, kind: str) -> None:
    await _insert_ignore(
        session,
        OwnerRow,
        {
            "owner_id": owner_id,
            "display_name": owner_id,
            "kind": kind,
            "profile_json": {},
        },
        index_elements=["owner_id"],
    )
    await session.flush()


async def _insert_ignore(session, model, values: dict[str, Any], *, index_elements: list[str]) -> None:
    dialect = session.get_bind().dialect.name
    if dialect == "sqlite":
        stmt = sqlite_insert(model).values(**values).on_conflict_do_nothing(
            index_elements=index_elements
        )
        await session.execute(stmt)
        return
    if dialect == "postgresql":
        stmt = pg_insert(model).values(**values).on_conflict_do_nothing(
            index_elements=index_elements
        )
        await session.execute(stmt)
        return
    if len(index_elements) == 1:
        pk_value = values.get(index_elements[0])
        if pk_value is not None and await session.get(model, pk_value) is None:
            session.add(model(**values))
        return
    session.add(model(**values))


async def _get_current_genome(session, *, companion_id: str) -> PersonaGenomeRow | None:
    companion = await session.get(CompanionRow, companion_id)
    if companion is not None and companion.current_genome_id:
        row = await session.get(PersonaGenomeRow, companion.current_genome_id)
        if row is not None:
            return row
    return (
        await session.execute(
            select(PersonaGenomeRow)
            .where(PersonaGenomeRow.companion_id == companion_id)
            .order_by(desc(PersonaGenomeRow.version))
            .limit(1)
        )
    ).scalar_one_or_none()


async def _get_genome_by_version(
    session,
    *,
    companion_id: str,
    version: int,
) -> PersonaGenomeRow | None:
    return (
        await session.execute(
            select(PersonaGenomeRow)
            .where(PersonaGenomeRow.companion_id == companion_id)
            .where(PersonaGenomeRow.version == version)
            .limit(1)
        )
    ).scalar_one_or_none()


def _persona_to_genome_json(persona: CompanionPersona, *, reason: str = "") -> dict[str, Any]:
    genome = PersonaGenomeV1(
        identity_core=PersonaIdentityCore(
            name=persona.metadata.name,
            archetype=persona.metadata.archetype,
            values=list(persona.identity_core.values),
            boundaries=list(persona.identity_core.unbreakable_rules),
            base_pronouns=persona.identity_core.base_pronouns,
            taboos=list(persona.identity_core.taboos),
            description=persona.metadata.description,
        ),
        relationship=PersonaRelationship(
            stage=persona.relationship_stage or "new",
            pinned_facts=list(persona.pinned_facts),
            owner_preferences={},
            safety_boundaries=list(persona.identity_core.taboos),
        ),
        traits={
            _trait_storage_key(key): PersonaTraitState(
                value=knob.current,
                confidence=1.0,
                last_changed_at=knob.last_changed_at,
                source="memory_reflection" if reason == "save_with_history" else "template",
                min=knob.min,
                max=knob.max,
                step_limit=knob.step_limit,
                cooldown_hours=knob.cooldown_hours,
            )
            for key, knob in persona.behavioral_knobs.items()
        },
        style_compiler=PersonaStyleCompilerV1(
            base_instructions=list(persona.style_compiler.base_instructions),
            trait_mappings={
                _trait_storage_key(key): [item.model_dump(mode="json") for item in mappings]
                for key, mappings in persona.style_compiler.knob_mappings.items()
            },
            spoken_phrases=_spoken_phrase_list(persona.style_compiler.spoken_phrases),
        ),
        memory_adapter=PersonaMemoryAdapterV1(
            recall_policy=persona.memory_adapter.retrieved_fact_handling.model_dump(mode="json"),
            relation_policies={
                item.relation_type: item.model_dump(mode="json")
                for item in persona.memory_adapter.graph_relation_policies
            },
        ),
        evolution_policy={
            "enabled": True,
            "auto_apply_low_risk": True,
            "max_delta_per_commit": 0.05,
            "review_required_traits": ["core.intimacy", "core.vulnerability"],
            "rules": [item.model_dump(mode="json") for item in persona.evolution_rules],
            "state": persona.evolution_state.model_dump(mode="json"),
        },
        provenance={
            "origin": "template",
            "base_genome_id": f"genome-{persona.companion_id}-1",
            "evidence_refs": [],
            "owner_id": persona.owner_id,
            "companion_id": persona.companion_id,
            "origin_template_id": persona.origin_template_id,
            "origin_template_revision": persona.origin_template_revision,
            "version": persona.version,
            "reason": reason,
            "assets": persona.assets.model_dump(mode="json"),
            "example_dialogs": list(persona.example_dialogs),
            "goals": list(persona.goals),
        },
    )
    return persona_genome_to_json(genome)


def _persona_source_json(persona: CompanionPersona) -> dict[str, Any]:
    return {
        "source_type": "template",
        "template_id": persona.origin_template_id,
        "template_revision": persona.origin_template_revision,
        "generated_at": persona.created_at.isoformat(),
    }


def _current_cache_key(owner_id: str, companion_id: str) -> str:
    return f"persona:current:{owner_id}:{companion_id}"


def _genome_cache_key(genome_id: str) -> str:
    return f"persona:genome:{genome_id}"


def _cache_payload_for_row(row: PersonaGenomeRow) -> dict[str, Any]:
    return {
        "genome_id": row.genome_id,
        "companion_id": row.companion_id,
        "version": row.version,
        "status": row.status,
        "base_genome_id": row.base_genome_id,
        "schema_version": row.schema_version,
        "genome_hash": row.genome_hash,
        "compiler_version": row.compiler_version,
        "stable_prompt_hash": row.stable_prompt_hash,
        "applied_event_id": row.applied_event_id,
        "source_json": row.source_json or {},
        "genome_json": row.genome_json or {},
        "change_summary": row.change_summary or "",
        "created_at": _datetime_to_json(row.created_at),
        "updated_at": _datetime_to_json(row.updated_at),
    }


def _cached_genome_row(payload: dict[str, Any]) -> SimpleNamespace:
    return SimpleNamespace(
        genome_id=str(payload.get("genome_id") or ""),
        companion_id=str(payload.get("companion_id") or ""),
        version=int(payload.get("version") or 1),
        status=str(payload.get("status") or "committed"),
        base_genome_id=payload.get("base_genome_id"),
        schema_version=str(payload.get("schema_version") or PERSONA_GENOME_SCHEMA_VERSION),
        genome_hash=str(payload.get("genome_hash") or ""),
        compiler_version=str(payload.get("compiler_version") or PERSONA_COMPILER_VERSION),
        stable_prompt_hash=payload.get("stable_prompt_hash"),
        applied_event_id=payload.get("applied_event_id"),
        source_json=dict(payload.get("source_json") or {}),
        genome_json=dict(payload.get("genome_json") or {}),
        change_summary=str(payload.get("change_summary") or ""),
        created_at=_datetime_from_json(payload.get("created_at")),
        updated_at=_datetime_from_json(payload.get("updated_at")),
    )


def _datetime_to_json(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _datetime_from_json(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return None


def _trait_storage_key(key: str) -> str:
    if "." in key:
        return key
    return f"core.{key}"


def _trait_runtime_key(key: str) -> str:
    if key.startswith("core."):
        return key.removeprefix("core.")
    return key


def _spoken_phrase_list(phrases: dict[str, str]) -> list[dict[str, str]]:
    return [
        {"key": str(key), "text": str(text)}
        for key, text in sorted((phrases or {}).items())
        if str(key) and str(text)
    ]


def _spoken_phrase_dict(raw: Any) -> dict[str, str]:
    if isinstance(raw, dict):
        return {str(key): str(value) for key, value in raw.items() if str(key) and str(value)}
    if not isinstance(raw, list):
        return {}
    phrases: dict[str, str] = {}
    for index, item in enumerate(raw):
        if isinstance(item, dict):
            key = str(item.get("key") or item.get("id") or "")
            text = str(item.get("text") or item.get("phrase") or item.get("value") or "")
        else:
            key = f"phrase_{index}"
            text = str(item or "")
        if key and text:
            phrases[key] = text
    return phrases


def _genome_row_to_persona(
    row: PersonaGenomeRow,
    *,
    owner_id: str | None = None,
) -> CompanionPersona:
    genome = normalize_persona_genome(
        row.genome_json or {},
        name=row.companion_id,
        base_genome_id=row.base_genome_id,
    )
    identity = genome.identity_core
    relationship = genome.relationship
    provenance = dict(genome.provenance.model_dump(mode="json"))
    evolution_policy = genome.evolution_policy.model_dump(mode="json")
    now = datetime.now(timezone.utc)
    return CompanionPersona(
        companion_id=row.companion_id,
        owner_id=owner_id or str(provenance.get("owner_id") or _owner_id_from_companion(row.companion_id)),
        origin_template_id=str(provenance.get("origin_template_id") or row.genome_id),
        origin_template_revision=int(provenance.get("origin_template_revision") or row.version),
        version=row.version,
        created_at=row.created_at or now,
        updated_at=row.updated_at or now,
        metadata=PersonaMetadata(
            template_id=str(provenance.get("origin_template_id") or row.genome_id),
            template_revision=int(provenance.get("origin_template_revision") or row.version),
            archetype=identity.archetype,
            name=identity.name,
            description=str(getattr(identity, "description", "") or ""),
        ),
        identity_core=IdentityCore(
            base_pronouns=str(getattr(identity, "base_pronouns", "") or "她"),
            values=tuple(identity.values),
            unbreakable_rules=tuple(identity.boundaries),
            taboos=tuple(getattr(identity, "taboos", ()) or ()),
        ),
        behavioral_knobs={
            _trait_runtime_key(key): BehavioralKnob(
                current=trait.value,
                min=float(getattr(trait, "min", 0.0) or 0.0),
                max=float(getattr(trait, "max", 1.0) or 1.0),
                step_limit=float(getattr(trait, "step_limit", 0.05) or 0.05),
                cooldown_hours=int(getattr(trait, "cooldown_hours", 0) or 0),
                last_changed_at=trait.last_changed_at,
            )
            for key, trait in genome.traits.items()
        },
        style_compiler=StyleCompiler.model_validate(
            {
                "base_instructions": tuple(genome.style_compiler.base_instructions),
                "knob_mappings": {
                    _trait_runtime_key(key): tuple(value)
                    for key, value in genome.style_compiler.trait_mappings.items()
                },
                "spoken_phrases": _spoken_phrase_dict(genome.style_compiler.spoken_phrases),
            }
        ),
        evolution_state=EvolutionState.model_validate(dict(evolution_policy.get("state") or {})),
        assets=PersonaAssets.model_validate(dict(provenance.get("assets") or {})),
        example_dialogs=tuple(provenance.get("example_dialogs") or ()),
        goals=tuple(provenance.get("goals") or ()),
        pinned_facts=tuple(relationship.pinned_facts),
        relationship_stage=relationship.stage,
    )


def _owner_id_from_companion(companion_id: str) -> str:
    if companion_id.startswith("c_") and "_" in companion_id.removeprefix("c_"):
        return companion_id.removeprefix("c_").rsplit("_", 1)[0]
    parts = companion_id.split(":")
    if len(parts) >= 3 and parts[0] == "c":
        return parts[1]
    return companion_id


def _event_row(
    *,
    owner_id: str,
    subject_type: str,
    subject_id: str,
    event_type: str,
    payload_json: dict[str, Any],
    companion_id: str | None = None,
) -> EventRow:
    return EventRow(
        event_id=f"evt_{uuid.uuid4().hex}",
        owner_id=owner_id,
        companion_id=companion_id,
        subject_type=subject_type,
        subject_id=subject_id,
        event_type=event_type,
        actor_type="system",
        payload_json=payload_json,
    )


def _proposal_event_type(proposal: PersonaEvolutionProposal) -> str:
    if proposal.status == "rejected":
        return "persona.evolution.rejected"
    if proposal.status == "applied":
        return "persona.evolution.approved"
    return "persona.evolution.proposed"


def _event_to_evolution(row: EventRow) -> PersonaEvolutionResult:
    return PersonaEvolutionResult.model_validate(row.payload_json[_EVOLUTION_KEY])


def _custom_template_event(
    *,
    template_id: str,
    owner_id: str,
    view: CustomTemplateView,
    event_type: str,
    deleted: bool = False,
) -> EventRow:
    return _event_row(
        owner_id=owner_id,
        subject_type="custom_template",
        subject_id=template_id,
        event_type=event_type,
        payload_json={
            _CUSTOM_TEMPLATE_KEY: view.to_dict(),
            "kind": _CUSTOM_TEMPLATE_KIND,
            "deleted": deleted,
        },
    )


def _custom_template_payload_to_view(payload: dict[str, Any]) -> CustomTemplateView:
    template = dict(payload[_CUSTOM_TEMPLATE_KEY])
    # Legacy-key shim: rows written before the vocabulary rename stored the
    # owning identity under "tenant_id".
    owner = template.get("owner_id", template.get("tenant_id"))
    return CustomTemplateView(
        template_id=str(template["template_id"]),
        owner_id=str(owner),
        display_name=str(template["display_name"]),
        archetype=str(template["archetype"]),
        yaml_body=str(template["yaml_body"]),
        revision=int(template["revision"]),
        created_at=datetime.fromisoformat(str(template["created_at"])),
        updated_at=datetime.fromisoformat(str(template["updated_at"])),
    )


__all__ = [
    "EidolonDataCompanionPersonaStore",
    "EidolonDataCustomTemplateStore",
    "EidolonDataEvolutionHistoryStore",
    "EidolonDataPersonaEvolutionProposalStore",
    "EidolonDataPersonaObservationStore",
]
