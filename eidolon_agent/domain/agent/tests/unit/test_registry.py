from __future__ import annotations

import pytest

from eidolon_agent.core.errors import NotFoundError
from eidolon_agent.domain.agent.registry import AgentRegistry

pytestmark = pytest.mark.unit


async def test_registry_resolves_dynamic_data_genome() -> None:
    created = []

    async def _factory(instance):
        created.append(instance)
        return object()

    registry = AgentRegistry(instance_factory=_factory)

    first = await registry.resolve_for_caller(
        owner_id="benchmark",
        companion_id="test",
        genome_id="genome-benchmark",
    )
    second = await registry.resolve_for_caller(
        owner_id="benchmark",
        companion_id="test",
        genome_id="genome-benchmark",
    )

    assert first is second
    assert first.companion_id == "test"
    assert first.genome_id == "genome-benchmark"
    assert len(created) == 1


async def test_registry_rejects_identity_without_pinned_genome() -> None:
    async def _factory(instance):
        return object()

    registry = AgentRegistry(instance_factory=_factory)
    with pytest.raises(NotFoundError, match="does not pin"):
        await registry.resolve_for_caller(
            owner_id="owner",
            companion_id="companion",
        )
