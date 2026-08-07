"""One credential resolves to one immutable Owner/Companion/Session scope."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from eidolon_agent.core.errors import PermissionDeniedError, ValidationError
from eidolon_agent.domain.runtime_session import RuntimeSessionAuthorizer


class _Authority:
    def __init__(self, facts: SimpleNamespace) -> None:
        self.facts = facts
        self.calls: list[tuple[str, str]] = []

    async def resolve(self, *, owner_id: str, companion_id: str) -> SimpleNamespace:
        self.calls.append((owner_id, companion_id))
        return self.facts


def _facts(**overrides: object) -> SimpleNamespace:
    values = {
        "owner_id": "owner-1",
        "companion_id": "companion-1",
        "runtime_config": {"tools": {"deny": ["reboot"]}},
    }
    values.update(overrides)
    return SimpleNamespace(**values)


async def test_authorizer_builds_one_pinned_runtime_scope() -> None:
    authority = _Authority(_facts())

    scope = await RuntimeSessionAuthorizer(authority).authorize(
        owner_id="owner-1",
        companion_id="companion-1",
        device_id=" device-1 ",
        session_id=" room-1 ",
    )

    assert scope.owner_id == "owner-1"
    assert scope.companion_id == "companion-1"
    assert scope.device_id == "device-1"
    assert scope.session_id == "room-1"
    assert scope.config.tool_deny == frozenset({"reboot"})
    assert authority.calls == [("owner-1", "companion-1")]


async def test_authorizer_rejects_authority_facts_outside_token_owner_scope() -> None:
    authority = _Authority(_facts(owner_id="owner-2"))

    with pytest.raises(PermissionDeniedError, match="outside token scope"):
        await RuntimeSessionAuthorizer(authority).authorize(
            owner_id="owner-1",
            companion_id="companion-1",
            device_id=None,
            session_id="room-1",
        )


async def test_authorizer_requires_signed_session_binding() -> None:
    authority = _Authority(_facts())

    with pytest.raises(PermissionDeniedError, match="not bound to a session"):
        await RuntimeSessionAuthorizer(authority).authorize(
            owner_id="owner-1",
            companion_id="companion-1",
            device_id=None,
            session_id=None,
        )


async def test_authorizer_rejects_malformed_authority_policy() -> None:
    authority = _Authority(_facts(runtime_config={"tools": "all"}))

    with pytest.raises(ValidationError, match="invalid Companion runtime config"):
        await RuntimeSessionAuthorizer(authority).authorize(
            owner_id="owner-1",
            companion_id="companion-1",
            device_id=None,
            session_id="room-1",
        )
