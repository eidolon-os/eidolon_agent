from __future__ import annotations

from datetime import datetime, timezone

import pytest

from eidolon_agent.core.errors import EvolutionGuardError, ValidationError
from eidolon_agent.core.types.memory import MemoryHit, MemoryKind
from eidolon_agent.domain.personas import (
    PersonaTemplateRegistry,
    YamlPersonaInstanceStore,
)
from eidolon_agent.domain.personas.evolution import PersonaEvolutionEngine
from eidolon_agent.domain.personas.memory_adapter import PersonaMemoryAdapter
from eidolon_agent.domain.personas.types import (
    PersonaEvolutionEvent,
    PersonaEvolutionProposal,
    PersonaInteractionEvent,
    PersonaObservation,
    PersonaProposalPatch,
)

pytestmark = pytest.mark.functional


@pytest.mark.asyncio
async def test_registry_loads_canonical_template(canonical_template_registry):
    summaries = canonical_template_registry.list_templates()
    assert [s.template_id for s in summaries] == [
        "accountability_partner_lixing",
        "caretaker_jiezhi",
        "quiet_listener_xibai",
        "reflective_mirror_qinglan",
        "secure_base_anyu",
        "spark_companion_yuguang",
        "story_weaver_lanxu",
        "strategic_mentor_xingqiao",
    ]
    template = canonical_template_registry.get("caretaker_jiezhi")
    assert template.metadata.name == "解之"
    assert "intimacy" in template.behavioral_knobs


@pytest.mark.asyncio
async def test_all_builtin_templates_compile_with_medium_length_style(personas_service):
    summaries = await personas_service.list_templates()
    seen_instructions: set[str] = set()
    for summary in summaries:
        instance_id = f"i-{summary.template_id}"
        await personas_service.create_instance(
            tenant_id="t",
            user_id="u",
            instance_id=instance_id,
            template_id=summary.template_id,
        )
        compiled = await personas_service.compile_prompt(
            tenant_id="t",
            user_id="u",
            instance_id=instance_id,
            user_text="今天有点累",
        )
        assert "默认回复保持中等长度" in compiled.system_prompt
        assert "避免一句话敷衍" in compiled.system_prompt
        assert "不写长篇报告式回答" in compiled.system_prompt
        assert compiled.debug_trace
        seen_instructions.add(compiled.style_block.splitlines()[1])
    assert len(seen_instructions) >= 8


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
    store = YamlPersonaInstanceStore(tmp_path / "instances")
    template = canonical_template_registry.get("caretaker_jiezhi")
    instance = await store.create_from_template(
        template=template,
        tenant_id="t",
        user_id="u",
        instance_id="i",
    )
    loaded = await store.load("t", "u", "i")
    assert loaded == instance
    assert loaded.origin_template_id == "caretaker_jiezhi"
    assert (
        loaded.behavioral_knobs["intimacy"].current == template.behavioral_knobs["intimacy"].current
    )


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
    store = YamlPersonaInstanceStore(tmp_path / "instances")
    instance = await store.create_from_template(
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
    instance = await YamlPersonaInstanceStore(tmp_path / "instances").create_from_template(
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


class _ObservationRepo:
    def __init__(self) -> None:
        self.rows: dict[str, PersonaObservation] = {}

    async def add(self, observation: PersonaObservation) -> None:
        self.rows[observation.id] = observation

    async def list_for_instance(
        self,
        instance_id: str,
        *,
        status: str | None = None,
        limit: int = 50,
    ) -> list[PersonaObservation]:
        rows = [row for row in self.rows.values() if row.instance_id == instance_id]
        if status is not None:
            rows = [row for row in rows if row.status == status]
        return rows[:limit]

    async def get(self, observation_id: str) -> PersonaObservation | None:
        return self.rows.get(observation_id)

    async def set_status(self, observation_id: str, status: str) -> None:
        row = self.rows.get(observation_id)
        if row is not None:
            self.rows[observation_id] = row.model_copy(update={"status": status})


class _ProposalRepo:
    def __init__(self) -> None:
        self.rows: dict[str, PersonaEvolutionProposal] = {}

    async def add(self, proposal: PersonaEvolutionProposal) -> None:
        self.rows[proposal.id] = proposal

    async def save(self, proposal: PersonaEvolutionProposal) -> None:
        self.rows[proposal.id] = proposal

    async def list_for_instance(
        self,
        instance_id: str,
        *,
        status: str | None = None,
        limit: int = 50,
    ) -> list[PersonaEvolutionProposal]:
        rows = [row for row in self.rows.values() if row.instance_id == instance_id]
        if status is not None:
            rows = [row for row in rows if row.status == status]
        return rows[:limit]

    async def get(self, proposal_id: str) -> PersonaEvolutionProposal | None:
        return self.rows.get(proposal_id)


@pytest.mark.asyncio
async def test_reflection_proposal_approval_applies_clamped_knob_delta(
    canonical_template_registry,
    persona_instance_store,
):
    from eidolon_agent.domain.personas.service import PersonasService

    observations = _ObservationRepo()
    proposals = _ProposalRepo()
    service = PersonasService(
        registry=canonical_template_registry,
        instances=persona_instance_store,
        observation_repo=observations,
        proposal_repo=proposals,
    )
    await service.create_instance(
        tenant_id="t",
        user_id="u",
        instance_id="i-prop",
        template_id="strategic_mentor_xingqiao",
    )
    await service.record_observation(
        PersonaObservation(
            id="obs-1",
            tenant_id="t",
            user_id="u",
            instance_id="i-prop",
            kind="goal_progress_shared",
            confidence=0.9,
            strength=0.8,
        )
    )
    generated = await service.run_reflection(
        tenant_id="t",
        user_id="u",
        instance_id="i-prop",
        auto_apply=False,
    )
    assert generated
    assert generated[0].status == "pending"
    before = await service.get_instance(tenant_id="t", user_id="u", instance_id="i-prop")
    result = await service.approve_evolution_proposal(generated[0].id)
    after = await service.get_instance(tenant_id="t", user_id="u", instance_id="i-prop")
    assert result.applied is True
    assert after.overlay_version == before.overlay_version + 1
    assert (
        after.behavioral_knobs["structure"].current > before.behavioral_knobs["structure"].current
    )
    assert proposals.rows[generated[0].id].status == "applied"


@pytest.mark.asyncio
async def test_reflection_auto_applies_low_risk_proposal(
    canonical_template_registry,
    persona_instance_store,
):
    from eidolon_agent.domain.personas.service import PersonasService

    observations = _ObservationRepo()
    proposals = _ProposalRepo()
    service = PersonasService(
        registry=canonical_template_registry,
        instances=persona_instance_store,
        observation_repo=observations,
        proposal_repo=proposals,
    )
    await service.create_instance(
        tenant_id="t",
        user_id="u",
        instance_id="i-auto",
        template_id="caretaker_jiezhi",
    )
    await service.record_observation(
        PersonaObservation(
            id="obs-auto",
            tenant_id="t",
            user_id="u",
            instance_id="i-auto",
            kind="positive_feedback_received",
            confidence=0.9,
            strength=0.8,
        )
    )
    before = await service.get_instance(tenant_id="t", user_id="u", instance_id="i-auto")
    generated = await service.run_reflection(
        tenant_id="t",
        user_id="u",
        instance_id="i-auto",
    )
    after = await service.get_instance(tenant_id="t", user_id="u", instance_id="i-auto")
    assert generated[0].status == "applied"
    assert generated[0].decided_by == "auto-evolution"
    assert after.overlay_version == before.overlay_version + 1
    assert after.behavioral_knobs["intimacy"].current == pytest.approx(
        before.behavioral_knobs["intimacy"].current + 0.03
    )


@pytest.mark.asyncio
async def test_reflection_leaves_higher_risk_proposal_pending(
    canonical_template_registry,
    persona_instance_store,
):
    from eidolon_agent.domain.personas.service import PersonasService

    observations = _ObservationRepo()
    proposals = _ProposalRepo()
    service = PersonasService(
        registry=canonical_template_registry,
        instances=persona_instance_store,
        observation_repo=observations,
        proposal_repo=proposals,
    )
    await service.create_instance(
        tenant_id="t",
        user_id="u",
        instance_id="i-review",
        template_id="caretaker_jiezhi",
    )
    await service.record_observation(
        PersonaObservation(
            id="obs-review",
            tenant_id="t",
            user_id="u",
            instance_id="i-review",
            kind="stressor_memory_recalled",
            confidence=0.9,
            strength=0.8,
        )
    )
    before = await service.get_instance(tenant_id="t", user_id="u", instance_id="i-review")
    generated = await service.run_reflection(
        tenant_id="t",
        user_id="u",
        instance_id="i-review",
    )
    after = await service.get_instance(tenant_id="t", user_id="u", instance_id="i-review")
    assert generated[0].status == "pending"
    assert "requires review" in (generated[0].decision_reason or "")
    assert after.overlay_version == before.overlay_version


@pytest.mark.asyncio
async def test_submit_interaction_auto_evolves_without_legacy_double_apply(
    canonical_template_registry,
    persona_instance_store,
):
    from eidolon_agent.domain.personas.service import PersonasService

    observations = _ObservationRepo()
    proposals = _ProposalRepo()
    service = PersonasService(
        registry=canonical_template_registry,
        instances=persona_instance_store,
        observation_repo=observations,
        proposal_repo=proposals,
    )
    await service.create_instance(
        tenant_id="t",
        user_id="u",
        instance_id="i-auto-interaction",
        template_id="caretaker_jiezhi",
    )
    before = await service.get_instance(
        tenant_id="t",
        user_id="u",
        instance_id="i-auto-interaction",
    )
    await service.submit_interaction(
        PersonaInteractionEvent(
            tenant_id="t",
            user_id="u",
            instance_id="i-auto-interaction",
            template_id="caretaker_jiezhi",
            kind="positive_feedback_received",
            payload={"confidence": 0.9, "strength": 0.8},
        )
    )
    immediate = await service.get_instance(
        tenant_id="t",
        user_id="u",
        instance_id="i-auto-interaction",
    )
    assert (
        immediate.behavioral_knobs["intimacy"].current
        == before.behavioral_knobs["intimacy"].current
    )

    await service._worker.drain_once()
    await service.drain_evolution_queue()
    after = await service.get_instance(
        tenant_id="t",
        user_id="u",
        instance_id="i-auto-interaction",
    )
    assert after.behavioral_knobs["intimacy"].current == pytest.approx(
        before.behavioral_knobs["intimacy"].current + 0.03
    )


@pytest.mark.asyncio
async def test_proposal_rejects_non_knob_targets(
    canonical_template_registry,
    persona_instance_store,
):
    from eidolon_agent.core.errors import EvolutionGuardError
    from eidolon_agent.domain.personas.service import PersonasService

    proposals = _ProposalRepo()
    service = PersonasService(
        registry=canonical_template_registry,
        instances=persona_instance_store,
        proposal_repo=proposals,
    )
    await service.create_instance(
        tenant_id="t",
        user_id="u",
        instance_id="i-bad-prop",
        template_id="caretaker_jiezhi",
    )
    bad = PersonaEvolutionProposal(
        id="proposal-bad",
        tenant_id="t",
        user_id="u",
        instance_id="i-bad-prop",
        patches=(
            PersonaProposalPatch(
                type="knob_delta",
                target="identity_core.values",
                delta=0.5,
            ),
        ),
    )
    await proposals.add(bad)
    with pytest.raises(EvolutionGuardError):
        await service.approve_evolution_proposal("proposal-bad")


def _hit(content: str, *, metadata: dict | None = None) -> MemoryHit:
    return MemoryHit(
        id="h1",
        content=content,
        kind=MemoryKind.FACT,
        similarity=1.0,
        valid_from=datetime.now(timezone.utc),
        metadata=metadata or {},
    )
