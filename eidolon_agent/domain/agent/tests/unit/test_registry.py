from __future__ import annotations

import pytest

from eidolon_agent.domain.agent.registry import AgentRegistry, AgentTemplate

pytestmark = pytest.mark.unit


async def test_registry_resolves_dynamic_data_genome() -> None:
    created = []

    async def _factory(instance):
        created.append(instance)
        return object()

    registry = AgentRegistry(
        instance_factory=_factory,
        default_genome_id="g_default",
    )
    registry.register_template(AgentTemplate(genome_id="g_default", name="Default"))

    first = await registry.resolve_for_caller(
        owner_id="benchmark",
        companion_id="test",
        genome_id="g_benchmark_default_v1",
    )
    second = await registry.resolve_for_caller(
        owner_id="benchmark",
        companion_id="test",
        genome_id="g_benchmark_default_v1",
    )

    assert first is second
    assert first.companion_id == "test"
    assert first.genome_id == "g_benchmark_default_v1"
    assert len(created) == 1
