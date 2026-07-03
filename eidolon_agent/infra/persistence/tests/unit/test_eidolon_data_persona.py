from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest
from eidolon_data import DataSettings, DataStore

from eidolon_agent.domain.personas.types import (
    PersonaEvolutionProposal,
    PersonaEvolutionResult,
    PersonaObservation,
)
from eidolon_agent.infra.persistence.eidolon_data_persona import (
    EidolonDataCompanionPersonaStore,
    EidolonDataCustomTemplateStore,
    EidolonDataEvolutionHistoryStore,
    EidolonDataPersonaEvolutionProposalStore,
    EidolonDataPersonaObservationStore,
)


@pytest.fixture
async def data_store(tmp_path):
    store = DataStore.open(DataSettings(sqlite_path=str(tmp_path / "eidolon.sqlite3")))
    await store.init_schema()
    try:
        yield store
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_persona_instance_store_uses_persona_genomes(
    data_store: DataStore,
    canonical_template_registry,
) -> None:
    store = EidolonDataCompanionPersonaStore(data_store)
    template = canonical_template_registry.get("caretaker_jiezhi")

    created = await store.create_from_template(
        template=template,
        owner_id="user-1",
        companion_id="companion-1",
    )
    loaded = await store.load("user-1", "companion-1")
    assert loaded == created
    assert loaded.version == 1

    bumped = loaded.model_copy(
        update={
            "version": 2,
            "updated_at": datetime.now(timezone.utc),
        }
    )
    await store.save(bumped, reason="test")
    fresh = await store.load("user-1", "companion-1")
    assert fresh.version == 2
    assert [item.companion_id for item in await store.list_all()] == ["companion-1"]

    companion = await data_store.companions.get("companion-1")
    assert companion is not None
    assert companion.current_genome_id == "genome-companion-1-2"

    await store.delete("user-1", "companion-1")
    assert await store.exists("user-1", "companion-1") is False


@pytest.mark.asyncio
async def test_persona_instance_store_loads_owner_workspace_genome(
    data_store: DataStore,
) -> None:
    await data_store.owner_service.create_owner(
        owner_id="benchmark",
        display_name="Benchmark",
    )
    await data_store.workspace_provisioning.provision_workspace(
        owner_id="benchmark",
        companion_id="test",
        companion_display_name="Test Companion",
        genome_id="g_benchmark_default_v1",
        genome_json={
            "identity": {"name": "Test", "archetype": "companion"},
            "style": {"tone": "warm", "initiative": "balanced"},
        },
        prompt_markdown="# Test\n\nReply warmly.",
        realm_id="r_benchmark_default",
    )

    store = EidolonDataCompanionPersonaStore(data_store)
    loaded = await store.load("benchmark", "test")

    assert loaded.companion_id == "test"
    assert loaded.owner_id == "benchmark"
    assert loaded.origin_template_id == "g_benchmark_default_v1"
    assert loaded.metadata.name == "Test"
    assert any("# Test" in item for item in loaded.style_compiler.base_instructions)


@pytest.mark.asyncio
async def test_persona_instance_create_is_idempotent_under_concurrent_first_writes(
    data_store: DataStore,
    canonical_template_registry,
) -> None:
    store = EidolonDataCompanionPersonaStore(data_store)
    template = canonical_template_registry.get("caretaker_jiezhi")

    async def _create() -> None:
        await store.create_from_template(
            template=template,
            owner_id="user-race",
            companion_id="companion-race",
        )

    await asyncio.gather(*(_create() for _ in range(12)))

    loaded = await store.load("user-race", "companion-race")
    assert loaded.version == 1
    assert [item.companion_id for item in await store.list_all()] == ["companion-race"]


@pytest.mark.asyncio
async def test_evolution_history_observations_and_proposals_use_events(
    data_store: DataStore,
    canonical_template_registry,
) -> None:
    instance_store = EidolonDataCompanionPersonaStore(data_store)
    template = canonical_template_registry.get("caretaker_jiezhi")
    await instance_store.create_from_template(
        template=template,
        owner_id="user-1",
        companion_id="companion-1",
    )

    history = EidolonDataEvolutionHistoryStore(data_store)
    result = PersonaEvolutionResult(
        companion_id="companion-1",
        applied=True,
        rationale="test evolution",
    )
    await history.record_evolution(result)
    rows = await history.list_for_instance("companion-1")
    assert rows[0].rationale == "test evolution"

    observations = EidolonDataPersonaObservationStore(data_store)
    observation = PersonaObservation(
        id="obs-1",
        owner_id="user-1",
        companion_id="companion-1",
        kind="positive_feedback_received",
        summary="user liked warmer tone",
    )
    await observations.add(observation)
    await observations.set_status("obs-1", "converted")
    converted = await observations.get("obs-1")
    assert converted is not None
    assert converted.status == "converted"
    assert await observations.list_for_instance("companion-1", status="active") == []

    proposals = EidolonDataPersonaEvolutionProposalStore(data_store)
    proposal = PersonaEvolutionProposal(
        id="proposal-1",
        owner_id="user-1",
        companion_id="companion-1",
        rationale="nudge warmer",
    )
    await proposals.add(proposal)
    await proposals.save(proposal.model_copy(update={"status": "rejected"}))
    rejected = await proposals.get("proposal-1")
    assert rejected is not None
    assert rejected.status == "rejected"
    assert await proposals.list_for_instance("companion-1", status="pending") == []


@pytest.mark.asyncio
async def test_custom_template_store_uses_events(
    data_store: DataStore,
    canonical_template_registry,
) -> None:
    custom_store = EidolonDataCustomTemplateStore(data_store)
    instance_store = EidolonDataCompanionPersonaStore(data_store)
    yaml_body = canonical_template_registry.raw_yaml("caretaker_jiezhi")

    created = await custom_store.create(
        template_id="custom-care",
        owner_id="user-1",
        display_name="Custom Care",
        archetype="caretaker",
        yaml_body=yaml_body,
    )
    assert created.revision == 1
    assert created.yaml_body == yaml_body

    updated = await custom_store.update("custom-care", display_name="Custom Care 2")
    assert updated.revision == 2
    assert updated.display_name == "Custom Care 2"

    template = canonical_template_registry.get("caretaker_jiezhi").model_copy(
        update={
            "metadata": canonical_template_registry.get("caretaker_jiezhi").metadata.model_copy(
                update={"template_id": "custom-care", "template_revision": 2}
            )
        }
    )
    await instance_store.create_from_template(
        template=template,
        owner_id="user-1",
        companion_id="companion-1",
    )
    assert await custom_store.count_referring_instances("custom-care") == 1
    assert [item.template_id for item in await custom_store.list_all()] == ["custom-care"]
