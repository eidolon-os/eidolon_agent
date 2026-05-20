"""EvolutionPlanner guard behaviour."""

import pytest

from eidolon_agent.core.errors import EvolutionGuardError
from eidolon_agent.persona import EvolutionPlanner


@pytest.mark.asyncio
async def test_locked_field_rejected(template_registry, overlay_store, resolver):
    # Initialize an empty overlay (resolver does this on first call)
    await resolver.resolve(
        instance_id="i1", template_id="caretaker_jiezhi", tenant_id="t", user_id="u"
    )
    planner = EvolutionPlanner(template_registry, overlay_store, resolver)
    with pytest.raises(EvolutionGuardError):
        await planner.propose(
            instance_id="i1",
            tenant_id="t",
            user_id="u",
            proposed_overrides={"taboos": ["新加禁忌"]},  # taboos are locked
        )


@pytest.mark.asyncio
async def test_big5_step_cap(template_registry, overlay_store, resolver):
    await resolver.resolve(
        instance_id="i2", template_id="caretaker_jiezhi", tenant_id="t", user_id="u"
    )
    planner = EvolutionPlanner(template_registry, overlay_store, resolver)
    # step_max is 0.08 for this template; agreeableness baseline 0.85 → 0.65 = 0.20 step
    with pytest.raises(EvolutionGuardError):
        await planner.propose(
            instance_id="i2",
            tenant_id="t",
            user_id="u",
            proposed_overrides={"big5": {"agreeableness": 0.65}},
        )


@pytest.mark.asyncio
async def test_valid_proposal_produces_delta(template_registry, overlay_store, resolver):
    await resolver.resolve(
        instance_id="i3", template_id="caretaker_jiezhi", tenant_id="t", user_id="u"
    )
    planner = EvolutionPlanner(template_registry, overlay_store, resolver)
    delta = await planner.propose(
        instance_id="i3",
        tenant_id="t",
        user_id="u",
        proposed_overrides={"speech_style": {"emoji_density": "rich"}},
        rationale="user prefers richer emoji",
    )
    assert delta.rationale == "user prefers richer emoji"
    assert "overrides.speech_style" in delta.changed_fields
