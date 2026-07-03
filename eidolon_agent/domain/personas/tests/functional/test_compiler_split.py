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


async def test_persona_components_render_in_stable_prefix(
    canonical_template_registry, tmp_path
):
    base = await _instance(canonical_template_registry, tmp_path)
    # Companion-first: author the components directly on the genome.
    instance = base.model_copy(
        update={
            "goals": ("陪用户坚持早睡",),
            "pinned_facts": ("用户叫小满", "用户在常州工作"),
            "relationship_stage": "刚认识不久",
            "example_dialogs": ("用户：累了。你：那先靠一会儿，我陪着你。",),
        }
    )
    compiled = PersonaCompiler().compile(
        instance=instance, runtime_state=await _mood("joy", 0.9)
    )
    # All componentized content is genome-stable → cached prefix, not the tail.
    for token in ("陪用户坚持早睡", "用户叫小满", "刚认识不久", "靠一会儿"):
        assert token in compiled.stable_prompt
        assert token not in compiled.volatile_prompt


async def test_components_absent_by_default(canonical_template_registry, tmp_path):
    instance = await _instance(canonical_template_registry, tmp_path)
    compiled = PersonaCompiler().compile(instance=instance)
    # No components authored → no component headers leak into the prompt.
    assert "自主动机" not in compiled.stable_prompt
    assert "关系阶段" not in compiled.stable_prompt


async def test_create_from_template_seeds_blueprint_components(tmp_path) -> None:
    from eidolon_agent.domain.personas.instance_store import YamlCompanionPersonaStore
    from eidolon_agent.domain.personas.types import (
        IdentityCore,
        PersonaMetadata,
        PersonaTemplate,
    )

    template = PersonaTemplate(
        metadata=PersonaMetadata(
            template_id="t-seed", template_revision=1, archetype="伙伴",
            name="小马", description="",
        ),
        identity_core=IdentityCore(base_pronouns="我/你"),
        behavioral_knobs={},
        goals=("帮用户完成今天的目标",),
        example_dialogs=("用户：早。你：早呀，今天想先做点什么？",),
    )
    store = YamlCompanionPersonaStore(tmp_path / "instances")
    persona = await store.create_from_template(
        template=template, owner_id="alice", companion_id="pony-1"
    )
    # Blueprint components seed; owner-specific ones start empty.
    assert persona.goals == ("帮用户完成今天的目标",)
    assert persona.example_dialogs
    assert persona.pinned_facts == ()
    assert persona.relationship_stage == ""
