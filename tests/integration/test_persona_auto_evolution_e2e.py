from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from eidolon_agent.app.admin import build_admin_app
from eidolon_agent.config.settings import Settings, SqliteSettings
from eidolon_agent.domain.personas import PersonasService, PersonaTemplateRegistry
from eidolon_agent.domain.personas.types import PersonaInteractionEvent, PersonaObservation
from eidolon_agent.infra.persistence import (
    SqlEvolutionHistoryStore,
    SqlPersonaEvolutionProposalStore,
    SqlPersonaInstanceStore,
    SqlPersonaObservationStore,
    create_engine,
    create_session_factory,
    ensure_schema,
)

pytestmark = pytest.mark.integration


async def _build_sql_personas_service(tmp_path: Path) -> tuple[PersonasService, object]:
    engine = create_engine(SqliteSettings(path=tmp_path / "agent.sqlite3"))
    await ensure_schema(engine)
    session_factory = create_session_factory(engine)
    registry = PersonaTemplateRegistry(Path("eidolon_agent/domain/personas/templates"))
    await registry.load_all()
    evolution_history = SqlEvolutionHistoryStore(session_factory)
    service = PersonasService(
        registry=registry,
        instances=SqlPersonaInstanceStore(session_factory),
        audit_port=evolution_history,
        evolution_repo=evolution_history,
        observation_repo=SqlPersonaObservationStore(session_factory),
        proposal_repo=SqlPersonaEvolutionProposalStore(session_factory),
    )
    return service, engine


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
    service, engine = await _build_sql_personas_service(tmp_path)
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
        await engine.dispose()


@pytest.mark.asyncio
async def test_auto_evolution_keeps_higher_risk_stress_proposal_pending_e2e(tmp_path):
    service, engine = await _build_sql_personas_service(tmp_path)
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
        await engine.dispose()
