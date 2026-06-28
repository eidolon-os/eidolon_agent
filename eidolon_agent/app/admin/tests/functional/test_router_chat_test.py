"""Admin chat test runtime identity helpers."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from eidolon_data import DataSettings, DataStore

from eidolon_agent.app.admin.routers.chat_test import (
    _admin_console_device_id,
    _ensure_admin_console_device,
    _refresh_memory_discovery_for_admin_chat,
)

pytestmark = pytest.mark.functional


async def test_admin_chat_test_provisions_bound_console_device(tmp_path) -> None:
    store = DataStore.open(DataSettings(sqlite_path=str(tmp_path / "eidolon.sqlite3")))
    await store.init_schema()
    try:
        await store.owner_service.create_owner(owner_id="owner-1", display_name="Owner 1")
        await store.companion_workspace.initialize_workspace(
            owner_id="owner-1",
            companion_id="companion-1",
            companion_display_name="Companion 1",
            genome_id="genome-1",
            realm_id="realm-1",
        )

        device_id = await _ensure_admin_console_device(
            store,
            owner_id="owner-1",
            companion_id="companion-1",
        )

        assert device_id == _admin_console_device_id(
            owner_id="owner-1",
            companion_id="companion-1",
        )
        row = await store.devices.get_device(device_id)
        assert row is not None
        assert row.owner_id == "owner-1"
        assert row.kind == "admin_console"
        assert row.status == "active"
        assert row.bound_companion_id == "companion-1"
        assert row.interaction_mode == "admin_test"
    finally:
        await store.close()


async def test_admin_chat_test_device_id_is_stable_per_owner_companion(tmp_path) -> None:
    store = DataStore.open(DataSettings(sqlite_path=str(tmp_path / "eidolon.sqlite3")))
    await store.init_schema()
    try:
        await store.owner_service.create_owner(owner_id="owner-1", display_name="Owner 1")
        await store.companion_workspace.initialize_workspace(
            owner_id="owner-1",
            companion_id="companion-1",
            companion_display_name="Companion 1",
            genome_id="genome-1",
            realm_id="realm-1",
        )

        first = await _ensure_admin_console_device(
            store,
            owner_id="owner-1",
            companion_id="companion-1",
        )
        second = await _ensure_admin_console_device(
            store,
            owner_id="owner-1",
            companion_id="companion-1",
        )

        assert second == first
        rows = await store.devices.list_devices_for_owner("owner-1")
        assert [row.device_id for row in rows] == [first]
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
