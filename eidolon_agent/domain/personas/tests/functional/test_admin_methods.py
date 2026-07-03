"""PersonasService admin helpers: list_instances / delete_instance / reload /
rollback_evolution / get_template_raw / list_evolution_history.

These methods aren't on the hot path — they back the admin HTTP router. Tests
verify behaviour through the high-level service so the in-memory YAML store +
canonical template registry both contribute.
"""

from __future__ import annotations

import pytest

from eidolon_agent.core.errors import NotFoundError
from eidolon_agent.domain.personas.types import (
    PersonaEvolutionChange,
    PersonaEvolutionEvent,
    PersonaEvolutionResult,
)

pytestmark = pytest.mark.functional


async def test_list_instances_returns_every_known_overlay(personas_service):
    await personas_service.create_instance(
        owner_id="u1", companion_id="i1", template_id="caretaker_jiezhi"
    )
    await personas_service.create_instance(
        owner_id="u2", companion_id="i2", template_id="caretaker_jiezhi"
    )
    rows = await personas_service.list_instances()
    assert {r.companion_id for r in rows} == {"i1", "i2"}


async def test_delete_instance_removes_overlay(personas_service):
    await personas_service.create_instance(
        owner_id="u", companion_id="i-del", template_id="caretaker_jiezhi"
    )
    await personas_service.delete_instance(
        owner_id="u", companion_id="i-del"
    )
    with pytest.raises(NotFoundError):
        await personas_service.get_instance(
            owner_id="u", companion_id="i-del"
        )


async def test_reload_templates_returns_count(personas_service):
    count = await personas_service.reload_templates()
    assert count >= 1  # canonical template registry has at least caretaker_jiezhi


async def test_get_template_raw_returns_yaml_text(personas_service):
    raw = await personas_service.get_template_raw("caretaker_jiezhi")
    assert "metadata:" in raw
    assert "caretaker_jiezhi" in raw


async def test_get_template_raw_unknown_raises(personas_service):
    with pytest.raises(NotFoundError):
        await personas_service.get_template_raw("does-not-exist")


# ---- evolution history / rollback ----------------------------------------


class _InMemoryEvolutionRepo:
    """Spy that satisfies PersonaEvolutionRepository for unit tests."""

    def __init__(self) -> None:
        self.records: dict[str, PersonaEvolutionResult] = {}

    async def record(self, result: PersonaEvolutionResult) -> None:
        self.records[f"delta-{len(self.records)}"] = result

    async def list_for_instance(
        self, companion_id: str, *, limit: int = 50
    ) -> list[PersonaEvolutionResult]:
        return [r for r in self.records.values() if r.companion_id == companion_id][
            -limit:
        ]

    async def get(self, delta_id: str) -> PersonaEvolutionResult | None:
        return self.records.get(delta_id)


async def test_list_evolution_history_returns_empty_without_repo(personas_service):
    assert await personas_service.list_evolution_history("any-id") == []


async def test_rollback_reverses_recorded_change(
    canonical_template_registry, persona_instance_store
):
    from eidolon_agent.domain.personas.service import PersonasService

    repo = _InMemoryEvolutionRepo()

    class _AuditRecorder:
        async def record_evolution(self, result):
            await repo.record(result)

    service = PersonasService(
        registry=canonical_template_registry,
        instances=persona_instance_store,
        audit_port=_AuditRecorder(),
        evolution_repo=repo,
    )
    await service.create_instance(
        owner_id="u", companion_id="i", template_id="caretaker_jiezhi",
    )
    before = await service.get_instance(owner_id="u", companion_id="i")

    # Trigger an evolution → version bumps and audit row recorded.
    result = await service.evolve(
        owner_id="u",
        companion_id="i",
        events=[PersonaEvolutionEvent(kind="positive_feedback_received", source="test")],
    )
    assert result.applied
    after_evolve = await service.get_instance(owner_id="u", companion_id="i")
    assert after_evolve.version == before.version + 1

    # Find the persisted delta id and roll it back.
    delta_id = next(iter(repo.records))
    # Stub the result with concrete changes for the rollback to act on.
    repo.records[delta_id] = PersonaEvolutionResult(
        companion_id="i",
        applied=True,
        changes=(
            PersonaEvolutionChange(
                path="behavioral_knobs.intimacy",
                old=before.behavioral_knobs["intimacy"].current,
                new=after_evolve.behavioral_knobs["intimacy"].current,
                rule_id="r",
            ),
        ),
    )
    rolled = await service.rollback_evolution(
        owner_id="u", companion_id="i", delta_id=delta_id,
    )
    assert rolled.applied is True
    rolled_inst = await service.get_instance(owner_id="u", companion_id="i")
    # Knob restored to pre-evolution value
    assert (
        rolled_inst.behavioral_knobs["intimacy"].current
        == pytest.approx(before.behavioral_knobs["intimacy"].current)
    )
    # Version bumped again, NOT decremented
    assert rolled_inst.version == after_evolve.version + 1


async def test_rollback_unknown_delta_raises(
    canonical_template_registry, persona_instance_store
):
    from eidolon_agent.domain.personas.service import PersonasService

    repo = _InMemoryEvolutionRepo()
    service = PersonasService(
        registry=canonical_template_registry,
        instances=persona_instance_store,
        evolution_repo=repo,
    )
    await service.create_instance(
        owner_id="u", companion_id="i", template_id="caretaker_jiezhi"
    )
    with pytest.raises(NotFoundError):
        await service.rollback_evolution(
            owner_id="u", companion_id="i", delta_id="ghost"
        )
