from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from eidolon_agent.personas.runtime_state import PersonaRuntimeStateStore
from eidolon_agent.personas.types import AttentionTarget, MoodVector, PersonaRuntimeState


@pytest.mark.asyncio
async def test_runtime_state_updates_and_prompts():
    store = PersonaRuntimeStateStore()
    state = await store.update(
        instance_id="i",
        emotion="joy",
        emotion_delta=0.7,
        energy_level=0.9,
        attention_target=AttentionTarget.USER,
        focus_score=0.8,
    )
    assert state.mood.joy == 0.7
    assert "心情不错" in state.to_prompt_hint()
    assert "状态饱满" in state.to_prompt_hint()
    assert "注意力在用户身上" in state.to_prompt_hint()


@pytest.mark.asyncio
async def test_runtime_state_decays():
    old = datetime.now(timezone.utc) - timedelta(seconds=3600)
    store = PersonaRuntimeStateStore()
    store._states["i"] = PersonaRuntimeState(
        mood=MoodVector(joy=1.0, intensity=1.0, updated_at=old),
        updated_at=old,
    )
    state = await store.snapshot(instance_id="i")
    assert state.mood.joy < 1.0
    assert state.mood.intensity < 1.0


@pytest.mark.asyncio
async def test_runtime_state_not_written_to_instance_yaml(personas_service, persona_instance_store):
    await personas_service.create_instance(
        tenant_id="t",
        user_id="u",
        instance_id="i-runtime",
        template_id="caretaker_jiezhi",
    )
    await personas_service.update_runtime_state(
        instance_id="i-runtime",
        emotion="sad",
        emotion_delta=0.8,
    )
    loaded = persona_instance_store.load("t", "u", "i-runtime")
    assert not hasattr(loaded, "runtime_state")

