from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from test_prepare_coordination import selection

from eidolon_agent.app.interaction.coordination.application import IpTeamApplication
from eidolon_agent.core.errors import PermissionDeniedError

pytestmark = pytest.mark.unit


async def test_unconfigured_decision_fails_before_authority_or_effects():
    authority = SimpleNamespace(resolve=AsyncMock())
    model = SimpleNamespace(stream=AsyncMock())
    effects = dict(present=AsyncMock(), stop=AsyncMock(), transcribe=AsyncMock())
    application = IpTeamApplication(llm=model, runtime_authority=authority, decide=None)
    with pytest.raises(ValueError):
        await application.prepare(
            selection(), authenticated_owner_id="alice", **effects,
        )
    authority.resolve.assert_not_called()
    model.stream.assert_not_called()
    for effect in effects.values():
        effect.assert_not_called()


async def test_application_cannot_bypass_companion_authorization():
    authority = SimpleNamespace(resolve=AsyncMock(side_effect=PermissionDeniedError("denied")))
    model = SimpleNamespace(stream=AsyncMock())
    effects = dict(present=AsyncMock(), stop=AsyncMock(), transcribe=AsyncMock())
    application = IpTeamApplication(llm=model, runtime_authority=authority, decide=AsyncMock())
    with pytest.raises(PermissionDeniedError):
        await application.prepare(
            selection(), authenticated_owner_id="alice", **effects,
        )
    model.stream.assert_not_called()
    for effect in effects.values():
        effect.assert_not_called()
