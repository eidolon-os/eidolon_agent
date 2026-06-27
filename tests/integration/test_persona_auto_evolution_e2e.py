from __future__ import annotations

from pathlib import Path

import pytest
from eidolon_data import DataSettings, DataStore
from fastapi.testclient import TestClient

from eidolon_agent.app.admin import build_admin_app
from eidolon_agent.config.settings import Settings
from eidolon_agent.domain.personas import PersonasService, PersonaTemplateRegistry
from eidolon_agent.domain.personas.types import PersonaInteractionEvent, PersonaObservation
from eidolon_agent.infra.persistence.eidolon_data_persona import (
    EidolonDataEvolutionHistoryStore,
    EidolonDataPersonaEvolutionProposalStore,
    EidolonDataPersonaInstanceStore,
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
        instances=EidolonDataPersonaInstanceStore(data_store),
        audit_port=evolution_history,
        evolution_repo=evolution_history,
        observation_repo=EidolonDataPersonaObservationStore(data_store),
        proposal_repo=EidolonDataPersonaEvolutionProposalStore(data_store),
    )
    return service, data_store


def _client(service: PersonasService) -> TestClient:
    app = build_admin_app(
        settings=Settings(),
        agent_registry=object(),
        pairing=object(),
        personas_service=service,
    )
    return TestClient(app)


@pytest.mark.asyncio
async def test_auto_evolution_applies_low_risk_feedback_end_to_end(tmp_path):
    service, data_store = await _build_personas_service(tmp_path)
    try:
        await service.start()
        await service.create_instance(
            tenant_id="t",
            user_id="u",
            instance_id="i-auto-e2e",
            template_id="caretaker_jiezhi",
        )
        before = await service.get_instance(
            tenant_id="t",
            user_id="u",
            instance_id="i-auto-e2e",
        )

        await service.submit_interaction(
            PersonaInteractionEvent(
                tenant_id="t",
                user_id="u",
                instance_id="i-auto-e2e",
                template_id="caretaker_jiezhi",
                kind="positive_feedback_received",
                user_text="刚才那样的回复我很喜欢。",
                payload={"confidence": 0.92, "strength": 0.85},
            )
        )
        await service.drain_evolution_queue()

        after = await service.get_instance(
            tenant_id="t",
            user_id="u",
            instance_id="i-auto-e2e",
        )
        assert after.overlay_version == before.overlay_version + 1
        assert after.behavioral_knobs["intimacy"].current == pytest.approx(
            before.behavioral_knobs["intimacy"].current + 0.03
        )

        client = _client(service)
        proposals = client.get("/api/admin/personas/instances/t/u/i-auto-e2e/proposals")
        assert proposals.status_code == 200
        proposal_rows = proposals.json()
        assert len(proposal_rows) == 1
        assert proposal_rows[0]["status"] == "applied"
        assert proposal_rows[0]["decided_by"] == "auto-evolution"

        observations = client.get("/api/admin/personas/instances/t/u/i-auto-e2e/observations")
        assert observations.status_code == 200
        assert observations.json()[0]["status"] == "converted"

        history = client.get("/api/admin/personas/instances/t/u/i-auto-e2e/evolution")
        assert history.status_code == 200
        assert history.json()[0]["applied"] is True
    finally:
        await service.stop()
        await data_store.close()


@pytest.mark.asyncio
async def test_auto_evolution_keeps_higher_risk_stress_proposal_pending_e2e(tmp_path):
    service, data_store = await _build_personas_service(tmp_path)
    try:
        await service.create_instance(
            tenant_id="t",
            user_id="u",
            instance_id="i-review-e2e",
            template_id="caretaker_jiezhi",
        )
        before = await service.get_instance(
            tenant_id="t",
            user_id="u",
            instance_id="i-review-e2e",
        )
        await service.record_observation(
            PersonaObservation(
                id="obs-review-e2e",
                tenant_id="t",
                user_id="u",
                instance_id="i-review-e2e",
                kind="stressor_memory_recalled",
                source="e2e",
                strength=0.9,
                confidence=0.9,
                summary="stress evidence should not silently raise intimacy",
            )
        )

        client = _client(service)
        reflected = client.post(
            "/api/admin/personas/instances/t/u/i-review-e2e/reflect",
            json={"limit": 50},
        )
        assert reflected.status_code == 200
        proposal_rows = reflected.json()
        assert len(proposal_rows) == 1
        assert proposal_rows[0]["status"] == "pending"
        assert "requires review" in proposal_rows[0]["decision_reason"]

        after = await service.get_instance(
            tenant_id="t",
            user_id="u",
            instance_id="i-review-e2e",
        )
        assert after.overlay_version == before.overlay_version
        assert (
            after.behavioral_knobs["intimacy"].current
            == before.behavioral_knobs["intimacy"].current
        )

        pending = client.get(
            "/api/admin/personas/instances/t/u/i-review-e2e/proposals",
            params={"status": "pending"},
        )
        assert pending.status_code == 200
        assert pending.json()[0]["id"] == proposal_rows[0]["id"]
    finally:
        await service.stop()
        await data_store.close()
