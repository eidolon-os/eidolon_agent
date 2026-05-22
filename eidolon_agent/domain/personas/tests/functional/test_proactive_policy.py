from __future__ import annotations

import pytest

pytestmark = pytest.mark.functional

@pytest.mark.asyncio
async def test_proactive_policy_returns_decision_for_close_persona(personas_service):
    await personas_service.create_instance(
        tenant_id="t",
        user_id="u",
        instance_id="i-proactive",
        template_id="caretaker_jiezhi",
    )
    decision = await personas_service.propose_proactive(
        tenant_id="t",
        user_id="u",
        instance_id="i-proactive",
    )
    assert decision is not None
    assert decision.intent == "gentle_check_in"
