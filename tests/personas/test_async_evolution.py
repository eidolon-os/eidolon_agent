from __future__ import annotations

import pytest

from eidolon_agent.domain.personas.types import PersonaInteractionEvent


@pytest.mark.asyncio
async def test_submit_interaction_is_nonblocking_and_worker_evolves(personas_service):
    await personas_service.create_instance(
        tenant_id="t",
        user_id="u",
        instance_id="i-async",
        template_id="caretaker_jiezhi",
    )
    before = await personas_service.get_instance(
        tenant_id="t",
        user_id="u",
        instance_id="i-async",
    )
    await personas_service.submit_interaction(
        PersonaInteractionEvent(
            tenant_id="t",
            user_id="u",
            instance_id="i-async",
            template_id="caretaker_jiezhi",
            kind="positive_feedback_received",
        )
    )
    immediate = await personas_service.get_instance(
        tenant_id="t",
        user_id="u",
        instance_id="i-async",
    )
    assert immediate.behavioral_knobs["intimacy"].current == before.behavioral_knobs["intimacy"].current

    await personas_service._worker.drain_once()
    after = await personas_service.get_instance(
        tenant_id="t",
        user_id="u",
        instance_id="i-async",
    )
    assert after.behavioral_knobs["intimacy"].current > before.behavioral_knobs["intimacy"].current


@pytest.mark.asyncio
async def test_worker_updates_runtime_state(personas_service):
    await personas_service.submit_interaction(
        PersonaInteractionEvent(
            tenant_id="t",
            user_id="u",
            instance_id="i-state",
            kind="turn_completed",
            payload={"emotion": "joy", "emotion_delta": 0.5},
        )
    )
    await personas_service._worker.drain_once()
    snapshot = await personas_service._runtime.snapshot(instance_id="i-state")
    assert snapshot.mood.joy == pytest.approx(0.5)
