from __future__ import annotations

import pytest

from eidolon_agent.domain.personas.signal_adapter import PersonaSignalAdapter
from eidolon_agent.domain.personas.types import AttentionTarget, PersonaSignalInput


def test_signal_adapter_ignores_low_confidence():
    update = PersonaSignalAdapter().to_runtime_update(
        PersonaSignalInput(
            tenant_id="t",
            user_id="u",
            instance_id="i",
            dominant_emotion="happy",
            emotion_confidence=0.9,
            confidence_overall=0.4,
        )
    )
    assert update == {}


def test_signal_adapter_maps_digest_to_runtime_update():
    update = PersonaSignalAdapter().to_runtime_update(
        PersonaSignalInput(
            tenant_id="t",
            user_id="u",
            instance_id="i",
            dominant_emotion="happy",
            emotion_confidence=0.9,
            presence="distracted",
            confidence_overall=0.9,
        )
    )
    assert update["emotion"] == "joy"
    assert update["attention_target"] == AttentionTarget.DRIFT


@pytest.mark.asyncio
async def test_submit_signal_updates_runtime_state(personas_service):
    await personas_service.submit_signal(
        PersonaSignalInput(
            tenant_id="t",
            user_id="u",
            instance_id="i-signal",
            dominant_emotion="happy",
            emotion_confidence=0.9,
            confidence_overall=0.9,
        )
    )
    snapshot = await personas_service.get_snapshot(
        tenant_id="t",
        user_id="u",
        instance_id="i-signal",
        template_id="caretaker_jiezhi",
    )
    assert snapshot.runtime_state.mood.joy > 0

