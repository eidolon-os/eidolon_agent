"""Admin chat test runtime identity helpers."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from eidolon_data import DataSettings, DataStore

from eidolon_agent.app.admin.routers.chat_test import (
    _refresh_memory_discovery_for_admin_chat,
)

pytestmark = pytest.mark.functional


async def test_admin_chat_test_does_not_create_device_rows(tmp_path) -> None:
    store = DataStore.open(DataSettings(sqlite_path=str(tmp_path / "eidolon.sqlite3")))
    await store.init_schema()
    try:
        await store.owner_service.create_owner(owner_id="owner-1", display_name="Owner 1")
        await store.workspace_provisioning.provision_workspace(
            owner_id="owner-1",
            companion_id="companion-1",
            companion_display_name="Companion 1",
            genome_id="genome-1",
            realm_id="realm-1",
        )
        await store.devices.create_device(
            device_id="real-body-1",
            owner_id="owner-1",
            name="Companion 1",
            kind="esp32",
            status="active",
            bound_companion_id="companion-1",
        )

        rows = await store.devices.list_devices_for_owner("owner-1")
        assert [row.device_id for row in rows] == ["real-body-1"]
    finally:
        await store.close()


async def test_admin_chat_test_refreshes_memory_discovery_before_turn() -> None:
    class Refresher:
        def __init__(self) -> None:
            self.calls = 0

        async def refresh_once(self) -> bool:
            self.calls += 1
            return True

    refresher = Refresher()
    request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(memory_discovery_refresher=refresher)
        )
    )

    refreshed = await _refresh_memory_discovery_for_admin_chat(
        request,
        owner_id="owner-1",
        companion_id="companion-1",
    )

    assert refreshed is True
    assert refresher.calls == 1
