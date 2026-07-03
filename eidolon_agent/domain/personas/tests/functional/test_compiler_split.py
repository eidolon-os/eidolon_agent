"""PersonaCompiler KV-cache split: stable identity prefix vs volatile state."""

from __future__ import annotations

import pytest

from eidolon_agent.domain.personas.compiler import PersonaCompiler
from eidolon_agent.domain.personas.runtime_state import PersonaRuntimeStateStore
from eidolon_agent.domain.personas.instance_store import YamlCompanionPersonaStore

pytestmark = pytest.mark.functional


async def _instance(canonical_template_registry, tmp_path):
    store = YamlCompanionPersonaStore(tmp_path / "instances")
    template = canonical_template_registry.get("caretaker_jiezhi")
    return await store.create_from_template(
        template=template, owner_id="u", companion_id="i"
    )


async def _mood(emotion: str, energy: float):
    store = PersonaRuntimeStateStore()
    return await store.update(
        companion_id="i", emotion=emotion, emotion_delta=0.8, energy_level=energy
    )


async def test_stable_prefix_is_byte_identical_across_mood_changes(
    canonical_template_registry, tmp_path
):
    instance = await _instance(canonical_template_registry, tmp_path)
    compiler = PersonaCompiler()

    happy = compiler.compile(instance=instance, runtime_state=await _mood("joy", 0.9))
    tired = compiler.compile(instance=instance, runtime_state=await _mood("sad", 0.1))

    # The cached prefix must not move when only mood/energy change.
    assert happy.stable_prompt == tired.stable_prompt
    assert happy.stable_prompt  # non-empty
    # Mood lives in the volatile tail and DOES differ.
    assert happy.volatile_prompt != tired.volatile_prompt
    # Identity is in the stable part; mood is not.
    assert instance.identity_core.base_pronouns in happy.stable_prompt
    assert "心情" in happy.volatile_prompt or "状态" in happy.volatile_prompt
    assert "心情" not in happy.stable_prompt


async def test_system_prompt_still_concatenates_both_parts(
    canonical_template_registry, tmp_path
):
    instance = await _instance(canonical_template_registry, tmp_path)
    compiled = PersonaCompiler().compile(
        instance=instance, runtime_state=await _mood("joy", 0.9)
    )
    assert compiled.stable_prompt in compiled.system_prompt
    assert compiled.volatile_prompt in compiled.system_prompt
