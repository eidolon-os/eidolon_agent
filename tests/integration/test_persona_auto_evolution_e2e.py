from __future__ import annotations

from pathlib import Path

import pytest
from eidolon_data import DataSettings, DataStore

from eidolon_agent.domain.personas import PersonasService, PersonaTemplateRegistry
from eidolon_agent.domain.personas.types import PersonaInteractionEvent, PersonaObservation
from eidolon_agent.infra.persistence.eidolon_data_persona import (
    EidolonDataCompanionPersonaStore,
    EidolonDataEvolutionHistoryStore,
    EidolonDataPersonaEvolutionProposalStore,
    EidolonDataPersonaObservationStore,
)

pytestmark = pytest.mark.integration


async def _build_personas_service(tmp_path: Path) -> tuple[PersonasService, DataStore]:
    data_store = DataStore.open(DataSettings(sqlite_path=str(tmp_path / "eidolon.sqlite3")))
    await data_store.init_schema()
    registry = PersonaTemplateRegistry(Path("eidolon_agent/domain/personas/templates"))
    await registry.load_all()
    evolution_history = EidolonDataEvolutionHistoryStore(data_store)
    service = PersonasService(
        registry=registry,
        instances=EidolonDataCompanionPersonaStore(data_store),
        audit_port=evolution_history,
        evolution_repo=evolution_history,
        observation_repo=EidolonDataPersonaObservationStore(data_store),
        proposal_repo=EidolonDataPersonaEvolutionProposalStore(data_store),
    )
    return service, data_store


@pytest.mark.asyncio
async def test_auto_evolution_applies_low_risk_feedback_end_to_end(tmp_path):
    service, data_store = await _build_personas_service(tmp_path)
    try:
        await service.start()
        await service.create_instance(
            owner_id="u",
            companion_id="i-auto-e2e",
            template_id="caretaker_jiezhi",
        )
        before = await service.get_instance(
            owner_id="u",
            companion_id="i-auto-e2e",
        )

        await service.submit_interaction(
            PersonaInteractionEvent(
                owner_id="u",
                companion_id="i-auto-e2e",
                genome_id="caretaker_jiezhi",
                kind="positive_feedback_received",
                user_text="刚才那样的回复我很喜欢。",
                payload={"confidence": 0.92, "strength": 0.85},
            )
        )
        await service.drain_evolution_queue()

        after = await service.get_instance(
            owner_id="u",
            companion_id="i-auto-e2e",
        )
        assert after.version == before.version + 1
        assert after.behavioral_knobs["intimacy"].current == pytest.approx(
            before.behavioral_knobs["intimacy"].current + 0.03
        )

        proposal_rows = await service.list_evolution_proposals("i-auto-e2e")
        assert len(proposal_rows) == 1
        assert proposal_rows[0].status == "applied"
        assert proposal_rows[0].decided_by == "auto-evolution"

        observations = await service.list_observations("i-auto-e2e")
        assert observations[0].status == "converted"

        history = await service.list_evolution_history("i-auto-e2e")
        assert history[0].applied is True
    finally:
        await service.stop()
        await data_store.close()


@pytest.mark.asyncio
async def test_auto_evolution_keeps_higher_risk_stress_proposal_pending_e2e(tmp_path):
    service, data_store = await _build_personas_service(tmp_path)
    try:
        await service.create_instance(
            owner_id="u",
            companion_id="i-review-e2e",
            template_id="caretaker_jiezhi",
        )
        before = await service.get_instance(
            owner_id="u",
            companion_id="i-review-e2e",
        )
        await service.record_observation(
            PersonaObservation(
                id="obs-review-e2e",
                owner_id="u",
                companion_id="i-review-e2e",
                kind="stressor_memory_recalled",
                source="e2e",
                strength=0.9,
                confidence=0.9,
                summary="stress evidence should not silently raise intimacy",
            )
        )

        proposal_rows = await service.run_reflection(
            owner_id="u",
            companion_id="i-review-e2e",
            limit=50,
        )
        assert len(proposal_rows) == 1
        assert proposal_rows[0].status == "pending"
        assert "requires review" in proposal_rows[0].decision_reason

        after = await service.get_instance(
            owner_id="u",
            companion_id="i-review-e2e",
        )
        assert after.version == before.version
        assert (
            after.behavioral_knobs["intimacy"].current
            == before.behavioral_knobs["intimacy"].current
        )

        pending = await service.list_evolution_proposals(
            "i-review-e2e",
            status="pending",
        )
        assert pending[0].id == proposal_rows[0].id
    finally:
        await service.stop()
        await data_store.close()
