from __future__ import annotations

from datetime import datetime, timezone

import pytest

from eidolon_agent.core.errors import EvolutionGuardError, ValidationError
from eidolon_agent.core.types.memory import MemoryHit, MemoryKind
from eidolon_agent.domain.personas import (
    PersonaInstanceStore,
    PersonaTemplateRegistry,
)
from eidolon_agent.domain.personas.evolution import PersonaEvolutionEngine
from eidolon_agent.domain.personas.memory_adapter import PersonaMemoryAdapter
from eidolon_agent.domain.personas.types import PersonaEvolutionEvent


@pytest.mark.asyncio
async def test_registry_loads_canonical_template(canonical_template_registry):
    summaries = canonical_template_registry.list_templates()
    assert [s.template_id for s in summaries] == ["caretaker_jiezhi"]
    template = canonical_template_registry.get("caretaker_jiezhi")
    assert template.metadata.name == "解之"
    assert "intimacy" in template.behavioral_knobs


@pytest.mark.asyncio
async def test_registry_rejects_schema_version(tmp_path):
    template_dir = tmp_path / "templates"
    template_dir.mkdir()
    (template_dir / "bad.yaml").write_text("schema_version: canonical\n", encoding="utf-8")
    registry = PersonaTemplateRegistry(template_dir)
    with pytest.raises(ValidationError):
        await registry.load_all()


@pytest.mark.asyncio
async def test_instance_is_full_copy(canonical_template_registry, tmp_path):
    store = PersonaInstanceStore(tmp_path / "instances")
    template = canonical_template_registry.get("caretaker_jiezhi")
    instance = store.create_from_template(
        template=template,
        tenant_id="t",
        user_id="u",
        instance_id="i",
    )
    loaded = store.load("t", "u", "i")
    assert loaded == instance
    assert loaded.origin_template_id == "caretaker_jiezhi"
    assert loaded.behavioral_knobs["intimacy"].current == template.behavioral_knobs["intimacy"].current


@pytest.mark.asyncio
async def test_compile_prompt_uses_style_mapping(personas_service):
    await personas_service.create_instance(
        tenant_id="t",
        user_id="u",
        instance_id="i",
        template_id="caretaker_jiezhi",
    )
    compiled = await personas_service.compile_prompt(
        tenant_id="t",
        user_id="u",
        instance_id="i",
        user_text="你好",
    )
    assert "你是「解之」" in compiled.system_prompt
    assert "保持礼貌、温和但有分寸的距离" in compiled.system_prompt
    assert any(trace.startswith("intimacy=") for trace in compiled.debug_trace)


@pytest.mark.asyncio
async def test_get_snapshot_and_compile_prompt_include_runtime_state(personas_service):
    await personas_service.create_instance(
        tenant_id="t",
        user_id="u",
        instance_id="i-snapshot",
        template_id="caretaker_jiezhi",
    )
    await personas_service.update_runtime_state(
        instance_id="i-snapshot",
        emotion="joy",
        emotion_delta=0.7,
    )
    snapshot = await personas_service.get_snapshot(
        tenant_id="t",
        user_id="u",
        instance_id="i-snapshot",
    )
    assert "心情不错" in snapshot.prompt_hint
    compiled = await personas_service.compile_prompt(
        tenant_id="t",
        user_id="u",
        instance_id="i-snapshot",
        user_text="你好",
    )
    assert "当前人格状态：心情不错" in compiled.system_prompt


@pytest.mark.asyncio
async def test_memory_adapter_relation_policy(personas_service):
    await personas_service.create_instance(
        tenant_id="t",
        user_id="u",
        instance_id="i-memory",
        template_id="caretaker_jiezhi",
    )
    hit = _hit(
        "用户又提到了老板带来的压力",
        metadata={"relation_type": "user_stressors", "emotion": "frustrated"},
    )
    result = await personas_service.mock_memory_trigger(
        tenant_id="t",
        user_id="u",
        instance_id="i-memory",
        user_text="又被老板骂了",
        memory_hits=[hit],
        apply=False,
    )
    assert "扮演情绪避风港" in result.compiled.system_prompt
    assert result.compiled.transient_knobs["extraversion"] < 0.55
    assert result.evolution is not None
    assert result.evolution.applied is False


@pytest.mark.asyncio
async def test_evolve_applies_and_persists(personas_service):
    await personas_service.create_instance(
        tenant_id="t",
        user_id="u",
        instance_id="i-evolve",
        template_id="caretaker_jiezhi",
    )
    before = await personas_service.get_instance(
        tenant_id="t",
        user_id="u",
        instance_id="i-evolve",
    )
    result = await personas_service.evolve(
        tenant_id="t",
        user_id="u",
        instance_id="i-evolve",
        events=[PersonaEvolutionEvent(kind="positive_feedback_received", source="test")],
    )
    after = await personas_service.get_instance(
        tenant_id="t",
        user_id="u",
        instance_id="i-evolve",
    )
    assert result.applied is True
    assert after.behavioral_knobs["intimacy"].current > before.behavioral_knobs["intimacy"].current


@pytest.mark.asyncio
async def test_evolution_rejects_unknown_target(canonical_template_registry, tmp_path):
    template = canonical_template_registry.get("caretaker_jiezhi")
    store = PersonaInstanceStore(tmp_path / "instances")
    instance = store.create_from_template(
        template=template,
        tenant_id="t",
        user_id="u",
        instance_id="i",
    )
    bad = instance.model_copy(
        update={
            "evolution_rules": (
                instance.evolution_rules[0].model_copy(
                    update={"target": "identity_core.unbreakable_rules"}
                ),
            )
        }
    )
    with pytest.raises(EvolutionGuardError):
        PersonaEvolutionEngine().evolve(
            instance=bad,
            events=[PersonaEvolutionEvent(kind="positive_feedback_received")],
        )


@pytest.mark.asyncio
async def test_memory_adapter_degrades_without_metadata(canonical_template_registry, tmp_path):
    template = canonical_template_registry.get("caretaker_jiezhi")
    instance = PersonaInstanceStore(tmp_path / "instances").create_from_template(
        template=template,
        tenant_id="t",
        user_id="u",
        instance_id="i",
    )
    adapted = PersonaMemoryAdapter().adapt(
        instance=instance,
        formatted_context="普通记忆",
        hits=[_hit("普通记忆")],
        degraded=True,
    )
    assert adapted.content == "普通记忆"
    assert adapted.degraded is True
    assert adapted.instructions == ()


def _hit(content: str, *, metadata: dict | None = None) -> MemoryHit:
    return MemoryHit(
        id="h1",
        content=content,
        kind=MemoryKind.FACT,
        similarity=1.0,
        valid_from=datetime.now(timezone.utc),
        metadata=metadata or {},
    )
