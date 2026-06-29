from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from eidolon_agent.domain.body_control.adapters import EidolonDataBodyDeviceStore


@pytest.mark.asyncio
async def test_data_store_lists_companion_bound_body_devices() -> None:
    store = EidolonDataBodyDeviceStore(
        _FakeDataStore(
            [
                _row(
                    "unbound-device-1",
                    "Unbound Device",
                    kind="esp32",
                    bound_companion_id=None,
                ),
                _row("box-3", "box-3", kind="esp32"),
            ]
        ),
        runtime_client=_FakeRuntime(),
    )

    devices = await store.list_devices(
        owner_id="owner-1",
        companion_id="companion-1",
        source_device_id=None,
    )

    assert [item.device_id for item in devices] == ["box-3"]
    assert devices[0].status == "online_control"
    assert "小王" in devices[0].aliases
    assert devices[0].last_seen == datetime(2026, 6, 29, 9, 14, 4, tzinfo=timezone.utc)


def _row(
    device_id: str,
    name: str,
    *,
    kind: str,
    bound_companion_id: str | None = "companion-1",
    metadata_json: dict | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        device_id=device_id,
        name=name,
        kind=kind,
        status="active",
        revoked_at=None,
        bound_companion_id=bound_companion_id,
        capabilities_json={},
        metadata_json=metadata_json or {},
        last_seen_at=datetime(2026, 6, 29, 9, 0, 0),
    )


class _FakeDataStore:
    def __init__(self, rows: list[SimpleNamespace]) -> None:
        self.devices = _FakeDeviceRepo(rows)
        self.companions = _FakeCompanionRepo()


class _FakeDeviceRepo:
    def __init__(self, rows: list[SimpleNamespace]) -> None:
        self._rows = rows

    async def list_devices_for_owner(self, owner_id: str):
        return list(self._rows)


class _FakeCompanionRepo:
    async def get(self, companion_id: str):
        return SimpleNamespace(companion_id=companion_id, display_name="小王")


class _FakeRuntime:
    async def list_runtime_devices(self):
        return [
            {
                "device_id": "unbound-device-1",
                "status": "offline",
            },
            {
                "device_id": "box-3",
                "status": "online",
                "room_name": "box-3-control",
                "last_seen": "2026-06-29T09:14:04+00:00",
            },
        ]
