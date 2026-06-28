"""Persona persistence backed by ``eidolon_data``.

The persona domain still speaks in ``PersonaInstance`` and proposal/observation
objects. This adapter maps those types onto Eidolon's sovereign schema:

* ``companions`` are the long-lived persona subjects.
* ``persona_genomes`` hold versioned persona instance snapshots.
* ``events`` hold custom template state, evolution history, observations, and
  proposal state.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

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
from eidolon_agent.domain.personas.types import (
    BehavioralKnob,
    EvolutionState,
    IdentityCore,
    PersonaAssets,
    PersonaEvolutionProposal,
    PersonaEvolutionResult,
    PersonaInstance,
    PersonaMetadata,
    StyleCompiler,
    PersonaObservation,
    PersonaTemplate,
)
from eidolon_agent.infra.persistence.custom_template_types import (
    CustomTemplateAlreadyExists,
    CustomTemplateNotFound,
    CustomTemplateView,
)

_INSTANCE_KEY = "persona_instance"
_EVOLUTION_KEY = "persona_evolution_result"
_OBSERVATION_KEY = "persona_observation"
_PROPOSAL_KEY = "persona_evolution_proposal"
_CUSTOM_TEMPLATE_KEY = "custom_template"
_CUSTOM_TEMPLATE_KIND = "agent_custom_template"


class EidolonDataPersonaInstanceStore:
    """``PersonaInstanceStore`` implemented on ``persona_genomes``."""

    def __init__(self, data_store: DataStore) -> None:
        self._data_store = data_store

    async def exists(self, tenant_id: str, user_id: str, instance_id: str) -> bool:
        return await self._load_or_none(tenant_id, user_id, instance_id) is not None

    async def load(self, tenant_id: str, user_id: str, instance_id: str) -> PersonaInstance:
        instance = await self._load_or_none(tenant_id, user_id, instance_id)
        if instance is None:
            raise NotFoundError(f"persona instance not found: {tenant_id}/{user_id}/{instance_id}")
        return instance

    async def save(self, instance: PersonaInstance, *, reason: str = "") -> None:
        async with self._data_store.session_factory() as session:
            await _ensure_owner_and_companion(
                session,
                owner_id=instance.user_id,
                tenant_id=instance.tenant_id,
                companion_id=instance.instance_id,
            )
            row = await _get_genome_by_version(
                session,
                companion_id=instance.instance_id,
                version=instance.overlay_version,
            )
            now = datetime.now(timezone.utc)
            genome_json = _instance_to_genome_json(instance, reason=reason)
            if row is None:
                genome_id = f"genome-{instance.instance_id}-{instance.overlay_version}"
                await _insert_ignore(
                    session,
                    PersonaGenomeRow,
                    {
                        "genome_id": genome_id,
                        "companion_id": instance.instance_id,
                        "version": instance.overlay_version,
                        "source_json": _instance_source_json(instance),
                        "genome_json": genome_json,
                        "evolution_state_json": instance.evolution_state.model_dump(mode="json"),
                        "created_at": instance.created_at,
                        "updated_at": now,
                    },
                    index_elements=["genome_id"],
                )
                row = await session.get(PersonaGenomeRow, genome_id)
                if row is None:
                    raise RuntimeError(f"persona genome insert failed: {genome_id}")
            else:
                row.source_json = _instance_source_json(instance)
                row.genome_json = genome_json
                row.evolution_state_json = instance.evolution_state.model_dump(mode="json")
                row.updated_at = now

            companion = await session.get(CompanionRow, instance.instance_id)
            if companion is not None:
                companion.current_genome_id = row.genome_id
                companion.updated_at = now
            await session.commit()

    async def create_from_template(
        self,
        *,
        template: PersonaTemplate,
        tenant_id: str,
        user_id: str,
        instance_id: str,
    ) -> PersonaInstance:
        now = datetime.now(timezone.utc)
        instance = PersonaInstance(
            instance_id=instance_id,
            tenant_id=tenant_id,
            user_id=user_id,
            origin_template_id=template.metadata.template_id,
            origin_template_revision=template.metadata.template_revision,
            overlay_version=1,
            created_at=now,
            updated_at=now,
            metadata=template.metadata,
            identity_core=template.identity_core,
            behavioral_knobs=template.behavioral_knobs,
            style_compiler=template.style_compiler,
            memory_adapter=template.memory_adapter,
            evolution_rules=template.evolution_rules,
            assets=template.assets,
        )
        await self.save(instance, reason="create_from_template")
        return instance

    async def list_all(self) -> list[PersonaInstance]:
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
            return [_genome_row_to_instance(row) for row in rows]

    async def delete(self, tenant_id: str, user_id: str, instance_id: str) -> None:
        async with self._data_store.session_factory() as session:
            companion = await session.get(CompanionRow, instance_id)
            if companion is not None and companion.owner_id == user_id:
                companion.current_genome_id = None
                companion.status = "deleted"
                companion.updated_at = datetime.now(timezone.utc)
            await session.execute(
                delete(PersonaGenomeRow).where(PersonaGenomeRow.companion_id == instance_id)
            )
            await session.commit()

    async def save_with_history(
        self,
        instance: PersonaInstance,
        result: PersonaEvolutionResult,
    ) -> None:
        async with self._data_store.session_factory() as session:
            await _ensure_owner_and_companion(
                session,
                owner_id=instance.user_id,
                tenant_id=instance.tenant_id,
                companion_id=instance.instance_id,
            )
            row = await _get_genome_by_version(
                session,
                companion_id=instance.instance_id,
                version=instance.overlay_version,
            )
            now = datetime.now(timezone.utc)
            if row is None:
                genome_id = f"genome-{instance.instance_id}-{instance.overlay_version}"
                await _insert_ignore(
                    session,
                    PersonaGenomeRow,
                    {
                        "genome_id": genome_id,
                        "companion_id": instance.instance_id,
                        "version": instance.overlay_version,
                        "source_json": _instance_source_json(instance),
                        "genome_json": _instance_to_genome_json(instance, reason="save_with_history"),
                        "evolution_state_json": instance.evolution_state.model_dump(mode="json"),
                        "created_at": instance.created_at,
                        "updated_at": now,
                    },
                    index_elements=["genome_id"],
                )
                row = await session.get(PersonaGenomeRow, genome_id)
                if row is None:
                    raise RuntimeError(f"persona genome insert failed: {genome_id}")
            else:
                row.genome_json = _instance_to_genome_json(instance, reason="save_with_history")
                row.evolution_state_json = instance.evolution_state.model_dump(mode="json")
                row.updated_at = now
            companion = await session.get(CompanionRow, instance.instance_id)
            if companion is not None:
                companion.current_genome_id = row.genome_id
                companion.updated_at = now
            session.add(
                _event_row(
                    owner_id=instance.user_id,
                    subject_type="persona",
                    subject_id=instance.instance_id,
                    event_type="persona.evolution.applied",
                    payload_json={_EVOLUTION_KEY: result.model_dump(mode="json")},
                )
            )
            await session.commit()

    async def _load_or_none(
        self,
        tenant_id: str,
        user_id: str,
        instance_id: str,
    ) -> PersonaInstance | None:
        async with self._data_store.session_factory() as session:
            row = await _get_current_genome(session, companion_id=instance_id)
            if row is None:
                return None
            instance = _genome_row_to_instance(row, owner_id=user_id)
            if instance.tenant_id != tenant_id or instance.user_id != user_id:
                return None
            return instance


class EidolonDataEvolutionHistoryStore:
    """Evolution audit/repository implemented on ``events``."""

    def __init__(self, data_store: DataStore) -> None:
        self._data_store = data_store

    async def record_evolution(self, result: PersonaEvolutionResult) -> None:
        await self.record(result)

    async def record(self, result: PersonaEvolutionResult) -> None:
        owner_id = await self._owner_for_instance(result.instance_id)
        async with self._data_store.session_factory() as session:
            session.add(
                _event_row(
                    owner_id=owner_id,
                    subject_type="persona",
                    subject_id=result.instance_id,
                    event_type="persona.evolution.applied",
                    payload_json={_EVOLUTION_KEY: result.model_dump(mode="json")},
                )
            )
            await session.commit()

    async def list_for_instance(
        self, instance_id: str, *, limit: int = 50
    ) -> list[PersonaEvolutionResult]:
        async with self._data_store.session_factory() as session:
            rows = (
                (
                    await session.execute(
                        select(EventRow)
                        .where(EventRow.subject_type == "persona")
                        .where(EventRow.subject_id == instance_id)
                        .where(EventRow.event_type == "persona.evolution.applied")
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
            if row is None or row.event_type != "persona.evolution.applied":
                return None
            return _event_to_evolution(row)

    async def _owner_for_instance(self, instance_id: str) -> str:
        async with self._data_store.session_factory() as session:
            companion = await session.get(CompanionRow, instance_id)
            return companion.owner_id if companion is not None else "owner-default"


class EidolonDataPersonaObservationStore:
    """Observation repository implemented as event-sourced state."""

    def __init__(self, data_store: DataStore) -> None:
        self._data_store = data_store

    async def add(self, observation: PersonaObservation) -> None:
        async with self._data_store.session_factory() as session:
            await _ensure_owner_and_companion(
                session,
                owner_id=observation.user_id,
                tenant_id=observation.tenant_id,
                companion_id=observation.instance_id,
            )
            session.add(
                _event_row(
                    owner_id=observation.user_id,
                    subject_type="persona_observation",
                    subject_id=observation.id,
                    event_type="persona.observation.saved",
                    payload_json={_OBSERVATION_KEY: observation.model_dump(mode="json")},
                )
            )
            await session.commit()

    async def list_for_instance(
        self,
        instance_id: str,
        *,
        status: str | None = None,
        limit: int = 50,
    ) -> list[PersonaObservation]:
        observations = await self._latest_observations()
        rows = [item for item in observations if item.instance_id == instance_id]
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
                        .where(EventRow.event_type == "persona.observation.saved")
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
                owner_id=proposal.user_id,
                tenant_id=proposal.tenant_id,
                companion_id=proposal.instance_id,
            )
            session.add(
                _event_row(
                    owner_id=proposal.user_id,
                    subject_type="persona_proposal",
                    subject_id=proposal.id,
                    event_type="persona.proposal.saved",
                    payload_json={_PROPOSAL_KEY: proposal.model_dump(mode="json")},
                )
            )
            await session.commit()

    async def list_for_instance(
        self,
        instance_id: str,
        *,
        status: str | None = None,
        limit: int = 50,
    ) -> list[PersonaEvolutionProposal]:
        proposals = [
            item
            for item in (await self._latest_proposals_by_id()).values()
            if item.instance_id == instance_id
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
                        .where(EventRow.event_type == "persona.proposal.saved")
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

    async def list_all(self, *, tenant_id: str | None = None) -> list[CustomTemplateView]:
        rows = list((await self._latest_templates_by_id()).values())
        if tenant_id is not None:
            rows = [row for row in rows if row.tenant_id == tenant_id]
        rows.sort(key=lambda row: row.created_at)
        return rows

    async def create(
        self,
        *,
        template_id: str,
        tenant_id: str,
        display_name: str,
        archetype: str,
        yaml_body: str,
    ) -> CustomTemplateView:
        now = datetime.now(timezone.utc)
        if await self.exists(template_id):
            raise CustomTemplateAlreadyExists(f"template {template_id!r} already exists")
        async with self._data_store.session_factory() as session:
            await _ensure_owner(session, owner_id=tenant_id, tenant_id=tenant_id, kind="team")
            view = CustomTemplateView(
                template_id=template_id,
                tenant_id=tenant_id,
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
                    owner_id=tenant_id,
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
            tenant_id=current.tenant_id,
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
                    owner_id=current.tenant_id,
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
                    owner_id=current.tenant_id,
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
    tenant_id: str,
    companion_id: str,
) -> None:
    await _ensure_owner(session, owner_id=owner_id, tenant_id=tenant_id, kind="person")
    await _insert_ignore(
        session,
        CompanionRow,
        {
            "companion_id": companion_id,
            "owner_id": owner_id,
            "display_name": companion_id,
            "kind": "companion",
            "metadata_json": {"tenant_id": tenant_id, "source": "eidolon_agent.persona"},
        },
        index_elements=["companion_id"],
    )
    companion = await session.get(CompanionRow, companion_id)
    if companion is None:
        raise RuntimeError(f"companion insert failed: {companion_id}")
    else:
        companion.owner_id = owner_id
        metadata = dict(companion.metadata_json or {})
        metadata.setdefault("tenant_id", tenant_id)
        metadata["source"] = "eidolon_agent.persona"
        companion.metadata_json = metadata
    await session.flush()


async def _ensure_owner(session, *, owner_id: str, tenant_id: str, kind: str) -> None:
    await _insert_ignore(
        session,
        OwnerRow,
        {
            "owner_id": owner_id,
            "display_name": owner_id,
            "kind": kind,
            "profile_json": {"tenant_id": tenant_id},
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


def _instance_to_genome_json(instance: PersonaInstance, *, reason: str = "") -> dict[str, Any]:
    return {
        _INSTANCE_KEY: instance.model_dump(mode="json"),
        "tenant_id": instance.tenant_id,
        "user_id": instance.user_id,
        "origin_template_id": instance.origin_template_id,
        "origin_template_revision": instance.origin_template_revision,
        "overlay_version": instance.overlay_version,
        "reason": reason,
    }


def _instance_source_json(instance: PersonaInstance) -> dict[str, Any]:
    return {
        "source_type": "template",
        "template_id": instance.origin_template_id,
        "template_revision": instance.origin_template_revision,
        "generated_at": instance.created_at.isoformat(),
    }


def _genome_row_to_instance(
    row: PersonaGenomeRow,
    *,
    owner_id: str | None = None,
) -> PersonaInstance:
    genome_json = row.genome_json or {}
    if _INSTANCE_KEY in genome_json:
        return PersonaInstance.model_validate(genome_json[_INSTANCE_KEY])

    identity = genome_json.get("identity") if isinstance(genome_json.get("identity"), dict) else {}
    style = genome_json.get("style") if isinstance(genome_json.get("style"), dict) else {}
    boundaries = genome_json.get("boundaries") if isinstance(genome_json.get("boundaries"), dict) else {}

    name = str(identity.get("name") or row.companion_id)
    archetype = str(identity.get("archetype") or "companion")
    prompt = (row.prompt_markdown or "").strip()
    style_lines: list[str] = []
    if prompt:
        style_lines.append(prompt)
    tone = style.get("tone")
    if tone:
        style_lines.append(f"Tone: {tone}")
    initiative = style.get("initiative")
    if initiative:
        style_lines.append(f"Initiative: {initiative}")

    values = _string_tuple(identity.get("values"))
    rules = _string_tuple(boundaries.get("rules") or boundaries.get("unbreakable_rules"))
    taboos = _string_tuple(boundaries.get("taboos"))
    now = datetime.now(timezone.utc)
    return PersonaInstance(
        instance_id=row.companion_id,
        tenant_id=owner_id or _owner_id_from_companion(row.companion_id),
        user_id=owner_id or _owner_id_from_companion(row.companion_id),
        origin_template_id=row.genome_id,
        origin_template_revision=row.version,
        overlay_version=row.version,
        created_at=row.created_at or now,
        updated_at=row.updated_at or now,
        metadata=PersonaMetadata(
            template_id=row.genome_id,
            template_revision=row.version,
            archetype=archetype,
            name=name,
            description=str(identity.get("description") or ""),
        ),
        identity_core=IdentityCore(
            base_pronouns=str(identity.get("pronouns") or "她"),
            values=values,
            unbreakable_rules=rules,
            taboos=taboos,
        ),
        behavioral_knobs={
            "warmth": BehavioralKnob(
                current=_knob_value(style.get("warmth"), default=0.65)
            ),
            "initiative": BehavioralKnob(
                current=_knob_value(style.get("initiative_level"), default=0.5)
            ),
        },
        style_compiler=StyleCompiler(base_instructions=tuple(style_lines)),
        evolution_state=EvolutionState(),
        assets=PersonaAssets(),
    )


def _string_tuple(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,) if value else ()
    if isinstance(value, list | tuple):
        return tuple(str(item) for item in value if str(item))
    return (str(value),)


def _knob_value(value: Any, *, default: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return min(1.0, max(0.0, parsed))


def _owner_id_from_companion(companion_id: str) -> str:
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
) -> EventRow:
    return EventRow(
        event_id=f"evt-{uuid.uuid4().hex}",
        owner_id=owner_id,
        subject_type=subject_type,
        subject_id=subject_id,
        event_type=event_type,
        actor_type="system",
        payload_json=payload_json,
    )


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
    return CustomTemplateView(
        template_id=str(template["template_id"]),
        tenant_id=str(template["tenant_id"]),
        display_name=str(template["display_name"]),
        archetype=str(template["archetype"]),
        yaml_body=str(template["yaml_body"]),
        revision=int(template["revision"]),
        created_at=datetime.fromisoformat(str(template["created_at"])),
        updated_at=datetime.fromisoformat(str(template["updated_at"])),
    )


__all__ = [
    "EidolonDataCustomTemplateStore",
    "EidolonDataEvolutionHistoryStore",
    "EidolonDataPersonaEvolutionProposalStore",
    "EidolonDataPersonaInstanceStore",
    "EidolonDataPersonaObservationStore",
]
