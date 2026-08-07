"""Admin chat test runtime identity helpers."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from eidolon_agent.app.admin.routers.chat_test import (
    _refresh_memory_discovery_for_admin_chat,
)

pytestmark = pytest.mark.functional


async def test_admin_chat_test_refreshes_memory_discovery_before_turn() -> None:
    class Refresher:
        def __init__(self) -> None:
            self.calls = 0

        async def refresh_once(self) -> bool:
            self.calls += 1
            return True

    refresher = Refresher()
    request = SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(memory_discovery_refresher=refresher))
    )

    refreshed = await _refresh_memory_discovery_for_admin_chat(
        request,
        owner_id="owner-1",
        companion_id="companion-1",
    )

    assert refreshed is True
    assert refresher.calls == 1
