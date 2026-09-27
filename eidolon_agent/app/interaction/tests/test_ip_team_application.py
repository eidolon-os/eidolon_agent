from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from test_prepare_coordination import selection

from eidolon_agent.app.interaction.coordination.application import IpTeamApplication
from eidolon_agent.core.errors import PermissionDeniedError

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("order", [(), ("a", "a"), ("foreign",)])
async def test_bad_policy_candidates_fail_before_authority_or_effects(order):
    authority = SimpleNamespace(resolve=AsyncMock())
    model = SimpleNamespace(stream=AsyncMock())
    effects = dict(present=AsyncMock(), stop=AsyncMock(), transcribe=AsyncMock())
    application = IpTeamApplication(llm=model, runtime_authority=authority)
    with pytest.raises(ValueError):
        await application.prepare_demo(
            selection(), authenticated_owner_id="alice", order=order, **effects,
        )
    authority.resolve.assert_not_called()
    model.stream.assert_not_called()
    for effect in effects.values():
        effect.assert_not_called()


async def test_application_cannot_bypass_companion_authorization():
    authority = SimpleNamespace(resolve=AsyncMock(side_effect=PermissionDeniedError("denied")))
    model = SimpleNamespace(stream=AsyncMock())
    effects = dict(present=AsyncMock(), stop=AsyncMock(), transcribe=AsyncMock())
    application = IpTeamApplication(llm=model, runtime_authority=authority)
    with pytest.raises(PermissionDeniedError):
        await application.prepare_demo(
            selection(), authenticated_owner_id="alice", order=("a", "b"), **effects,
        )
    model.stream.assert_not_called()
    for effect in effects.values():
        effect.assert_not_called()
