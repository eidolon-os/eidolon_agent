from __future__ import annotations

import pytest

from eidolon_agent.domain.agent.registry import AgentRegistry, AgentTemplate

pytestmark = pytest.mark.unit


async def test_registry_recovers_active_instance_before_generating_fallback() -> None:
    created = []

    async def _factory(instance):
        created.append(instance)
        return object()

    async def _resolver(**kwargs):
        assert kwargs == {
            "tenant_id": "tenant-1",
            "user_id": "alice",
            "requested_template_id": None,
        }
        return "agent-active", "tpl-active"

    registry = AgentRegistry(
        instance_factory=_factory,
        default_template_id="tpl-default",
        active_instance_resolver=_resolver,
    )
    registry.register_template(AgentTemplate(template_id="tpl-default", name="Default"))
    registry.register_template(AgentTemplate(template_id="tpl-active", name="Active"))

    first = await registry.resolve_for_caller(tenant_id="tenant-1", user_id="alice")
    second = await registry.resolve_for_caller(tenant_id="tenant-1", user_id="alice")

    assert first is second
    assert first.instance_id == "agent-active"
    assert first.template_id == "tpl-active"
    assert len(created) == 1
