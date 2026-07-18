from __future__ import annotations

from datetime import datetime, timedelta, timezone

import httpx
import pytest
from eidolon_sdk.biz.body import (
    CapabilityManifest,
    OwnerDeviceBlackboardSnapshot,
    RuntimeDeviceEntry,
    capability_manifest_revision,
    owner_device_blackboard_key,
)

from eidolon_agent.domain.body_control.adapters import (
    HubBodyCommandClient,
    NatsRuntimeBodyDeviceStore,
)
from eidolon_agent.domain.body_control.errors import BodyDeviceOffline


@pytest.mark.asyncio
async def test_runtime_store_uses_only_caller_scoped_online_blackboard() -> None:
    kv = _FakeKV({owner_device_blackboard_key("owner-1"): _snapshot_bytes()})
    store = NatsRuntimeBodyDeviceStore(kv)

    devices = await store.list_devices(
        owner_id="owner-1",
        companion_id="companion-1",
        source_device_id=None,
    )

    assert kv.requested == owner_device_blackboard_key("owner-1")
    assert [item.device_id for item in devices] == ["atk-guard", "box-3"]
    by_id = {item.device_id: item for item in devices}
    assert by_id["box-3"].status == "online_control"
    assert "小王" in by_id["box-3"].aliases
    assert by_id["box-3"].last_seen == datetime(2026, 6, 29, 9, 14, 4, tzinfo=timezone.utc)
    assert by_id["atk-guard"].capabilities[0].name == "device.roll_call"
    assert by_id["atk-guard"].capabilities[0].version == 1
    assert by_id["atk-guard"].provider_companion_id == "companion-2"
    assert by_id["atk-guard"].provider_companion_name == "Companion 2"


@pytest.mark.asyncio
async def test_runtime_store_fails_closed_for_missing_malformed_or_other_owner_snapshot() -> None:
    key = owner_device_blackboard_key("owner-1")
    missing = NatsRuntimeBodyDeviceStore(_FakeKV({}))
    malformed = NatsRuntimeBodyDeviceStore(_FakeKV({key: b"not-json"}))
    other_owner = OwnerDeviceBlackboardSnapshot.from_bytes(
        _snapshot_bytes(), expected_owner_id="owner-1"
    ).model_copy(update={"owner_id": "owner-2"})
    mismatched = NatsRuntimeBodyDeviceStore(_FakeKV({key: other_owner.to_bytes()}))

    for store in (missing, malformed, mismatched):
        assert (
            await store.list_devices(
                owner_id="owner-1",
                companion_id="companion-1",
            )
            == []
        )


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
                    "capability_version": 1,
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
                "capability_version": 1,
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
            capability_version=1,
            payload={},
            idempotency_key="idem-1",
            qos="result",
            ttl_ms=5000,
        )

    assert result.status == "done"
    assert result.result == {"played": True}
    assert status_reads == 2


@pytest.mark.asyncio
async def test_runtime_client_reports_blackboard_disconnect_race_as_offline() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(409, json={"detail": "device 'atk-guard' is not online"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = HubBodyCommandClient(http, base_url="http://hub", timeout_s=1.0)
        with pytest.raises(BodyDeviceOffline, match="not online"):
            await client.send_command(
                owner_id="owner-1",
                companion_id="companion-1",
                source_device_id="box-3",
                device_id="atk-guard",
                op="device.roll_call",
                capability_version=1,
                payload={},
                idempotency_key="idem-1",
                qos="result",
            )


class _FakeKV:
    def __init__(self, values: dict[str, bytes]):
        self.values = values
        self.requested = ""

    async def get(self, key: str):
        self.requested = key
        return self.values.get(key)


def _declared(name: str):
    manifest = CapabilityManifest.model_validate(
        {
            "capabilities": [
                {
                    "name": name,
                    "version": 1,
                    "description": f"Execute {name}",
                    "input_schema": {
                        "type": "object",
                        "properties": {},
                        "additionalProperties": False,
                    },
                    "result_schema": {"type": "object", "properties": {}},
                }
            ]
        }
    )
    return manifest, manifest.capabilities


def _entry(
    device_id: str,
    *,
    companion_id: str,
    capability_name: str,
    status: str = "online",
    aliases: tuple[str, ...] = (),
    last_seen: datetime | None = None,
    companion_name: str | None = None,
):
    now = datetime.now(timezone.utc)
    manifest, capabilities = _declared(capability_name)
    return RuntimeDeviceEntry(
        device_id=device_id,
        registration_id=f"reg-{device_id}",
        provider_companion_id=companion_id,
        provider_companion_name=companion_name or companion_id.replace("-", " ").title(),
        name=device_id,
        aliases=aliases,
        capabilities=capabilities,
        manifest_revision=capability_manifest_revision(manifest),
        status=status,
        registered_at=now,
        last_seen_at=last_seen or now,
        lease_expires_at=now + timedelta(seconds=30),
    )


def _snapshot_bytes() -> bytes:
    now = datetime.now(timezone.utc)
    return OwnerDeviceBlackboardSnapshot(
        owner_id="owner-1",
        epoch="epoch-1",
        revision=1,
        ready=True,
        hub_lease_expires_at=now + timedelta(seconds=30),
        updated_at=now,
        devices={
            "stale-device": _entry(
                "stale-device",
                companion_id="companion-1",
                capability_name="device.identify",
                status="registered_waiting_transport",
            ),
            "box-3": _entry(
                "box-3",
                companion_id="companion-1",
                capability_name="device.identify",
                aliases=("小王",),
                last_seen=datetime(2026, 6, 29, 9, 14, 4, tzinfo=timezone.utc),
            ),
            "atk-guard": _entry(
                "atk-guard",
                companion_id="companion-2",
                capability_name="device.roll_call",
            ),
        },
    ).to_bytes()
