from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import httpx
import pytest

from eidolon_agent.domain.body_control.adapters import (
    EidolonDataBodyDeviceStore,
    HubBodyCommandClient,
)


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
                _row(
                    "atk-guard",
                    "ATK Guard",
                    kind="atk-guard",
                    bound_companion_id="companion-2",
                    capabilities_json={"ops": ["device.roll_call"]},
                ),
                _row(
                    "private-guard",
                    "Private Guard",
                    kind="atk-guard",
                    bound_companion_id="companion-2",
                    capabilities_json={"ops": ["device.roll_call"]},
                    access_policy_json={"capability_visibility": "bound_companion"},
                ),
            ]
        ),
        runtime_client=_FakeRuntime(),
    )

    devices = await store.list_devices(
        owner_id="owner-1",
        companion_id="companion-1",
        source_device_id=None,
    )

    assert [item.device_id for item in devices] == ["box-3", "atk-guard"]
    assert devices[0].status == "online_control"
    assert "小王" in devices[0].aliases
    assert devices[0].last_seen == datetime(2026, 6, 29, 9, 14, 4, tzinfo=timezone.utc)
    assert devices[1].capabilities[0].name == "device.roll_call"
    assert "Guard Companion" in devices[1].aliases


@pytest.mark.asyncio
async def test_runtime_client_waits_for_guard_terminal_result() -> None:
    status_reads = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal status_reads
        if request.method == "POST":
            assert request.url.path == "/api/runtime/devices/atk-guard/commands"
            assert b'"requester_companion_id":"companion-1"' in request.content
            return httpx.Response(
                200,
                json={
                    "command_id": "cmd-1",
                    "device_id": "atk-guard",
                    "op": "device.roll_call",
                    "status": "sent",
                },
            )
        status_reads += 1
        return httpx.Response(
            200,
            json={
                "command_id": "cmd-1",
                "device_id": "atk-guard",
                "op": "device.roll_call",
                "status": "completed" if status_reads > 1 else "accepted",
                "result": {"played": True} if status_reads > 1 else None,
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = HubBodyCommandClient(http, base_url="http://hub", timeout_s=1.0)
        result = await client.send_command(
            owner_id="owner-1",
            companion_id="companion-1",
            source_device_id="box-3",
            device_id="atk-guard",
            op="device.roll_call",
            payload={},
            qos="result",
            ttl_ms=5000,
        )

    assert result.status == "done"
    assert result.result == {"played": True}
    assert status_reads == 2


def _row(
    device_id: str,
    name: str,
    *,
    kind: str,
    bound_companion_id: str | None = "companion-1",
    metadata_json: dict | None = None,
    capabilities_json: dict | None = None,
    access_policy_json: dict | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        device_id=device_id,
        name=name,
        kind=kind,
        status="active",
        revoked_at=None,
        bound_companion_id=bound_companion_id,
        capabilities_json=capabilities_json or {},
        access_policy_json=access_policy_json or {},
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
        return SimpleNamespace(
            companion_id=companion_id,
            display_name="Guard Companion" if companion_id == "companion-2" else "小王",
        )


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
            {
                "device_id": "atk-guard",
                "status": "online",
                "room_name": "atk-guard-control",
            },
        ]
