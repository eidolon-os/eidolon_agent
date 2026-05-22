"""SqlEvolutionHistoryRepository — record, list_for_instance, get."""

from __future__ import annotations

import pytest

from eidolon_agent.domain.personas.types import PersonaEvolutionResult

pytestmark = pytest.mark.unit


def _result(instance_id: str, *, applied: bool = True, rationale: str = "") -> PersonaEvolutionResult:
    return PersonaEvolutionResult(
        instance_id=instance_id, applied=applied, rationale=rationale
    )


async def test_record_then_list_for_instance(uow_factory) -> None:
    async with uow_factory() as uow:
        await uow.evolution_history.record(_result("inst-1", rationale="first"))
        await uow.evolution_history.record(_result("inst-1", rationale="second"))
        await uow.evolution_history.record(_result("inst-2", rationale="other"))
        await uow.commit()
    async with uow_factory() as uow:
        items = await uow.evolution_history.list_for_instance("inst-1")
    rationales = sorted(i.rationale for i in items)
    assert rationales == ["first", "second"]


async def test_list_for_instance_respects_limit(uow_factory) -> None:
    async with uow_factory() as uow:
        for i in range(5):
            await uow.evolution_history.record(_result("inst-many", rationale=f"r{i}"))
        await uow.commit()
    async with uow_factory() as uow:
        items = await uow.evolution_history.list_for_instance("inst-many", limit=2)
    assert len(items) == 2


async def test_list_for_unknown_instance_returns_empty(uow_factory) -> None:
    async with uow_factory() as uow:
        assert await uow.evolution_history.list_for_instance("ghost") == []


async def test_not_applied_results_record_with_applied_at_null(uow_factory) -> None:
    from sqlalchemy import select

    from eidolon_agent.infra.persistence.models import EvolutionHistoryRow

    async with uow_factory() as uow:
        await uow.evolution_history.record(_result("inst-x", applied=False))
        await uow.commit()
    async with uow_factory() as uow:
        rows = (
            await uow._session.execute(  # type: ignore[attr-defined]
                select(EvolutionHistoryRow).where(EvolutionHistoryRow.instance_id == "inst-x")
            )
        ).scalars().all()
    assert len(rows) == 1
    assert rows[0].applied_at is None
